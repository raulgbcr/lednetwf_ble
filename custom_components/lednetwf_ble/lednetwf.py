import asyncio
from datetime import datetime
from homeassistant.components import bluetooth
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.components.light import (ColorMode)
from homeassistant.const import CONF_MAC

from bleak.backends.device import BLEDevice
from bleak.backends.service import BleakGATTCharacteristic, BleakGATTServiceCollection
from bleak.exc import BleakDBusError
from bleak_retry_connector import BLEAK_RETRY_EXCEPTIONS as BLEAK_EXCEPTIONS
from bleak_retry_connector import (
    BleakClientWithServiceCache,
    BleakError,
    BleakNotFoundError,
    ble_device_has_changed,
    establish_connection,
)
from typing import Any, TypeVar, cast, Tuple
from collections.abc import Callable
import traceback
import logging
import colorsys

from .const import (
    EFFECT_OFF_HA, # todo: why?
    EFFECT_MAP_0x53,
    EFFECT_LIST_0x53,
    EFFECT_ID_TO_NAME_0x53,
    EFFECT_MAP_0x56,
    EFFECT_LIST_0x56,
    EFFECT_ID_TO_NAME_0x56,
    RING_LIGHT_MODEL,
    STRIP_LIGHT_MODEL,
    CONF_LEDCOUNT,
    CONF_LEDTYPE,
    CONF_COLORORDER,
    CONF_LEDCOUNT,
    CONF_DELAY,
    DOMAIN,
    CONF_NAME,
    CONF_MODEL,
    LedTypes_StripLight,
    LedTypes_RingLight,
    ColorOrdering
)

LOGGER = logging.getLogger(__name__)

NAME_ARRAY                    = ["LEDnetWF"]
SUPPORTED_MODELS              = [0x53, 0x56] # [Ring light with CW/WW, Strip light with RGB only]
WRITE_CHARACTERISTIC_UUIDS    = ["0000ff01-0000-1000-8000-00805f9b34fb"]
NOTIFY_CHARACTERISTIC_UUIDS   = ["0000ff02-0000-1000-8000-00805f9b34fb"]
INITIAL_PACKET                = bytearray.fromhex("00 01 80 00 00 04 05 0a 81 8a 8b 96")
GET_LED_SETTINGS_PACKET       = bytearray.fromhex("00 02 80 00 00 05 06 0a 63 12 21 f0 86")
DEFAULT_ATTEMPTS              = 3
BLEAK_BACKOFF_TIME            = 0.25
RETRY_BACKOFF_EXCEPTIONS      = (BleakDBusError)

WrapFuncType = TypeVar("WrapFuncType", bound=Callable[..., Any])

def retry_bluetooth_connection_error(func: WrapFuncType) -> WrapFuncType:
    async def _async_wrap_retry_bluetooth_connection_error(
        self: "LEDNETWFInstance", *args: Any, **kwargs: Any
    ) -> Any:
        attempts = DEFAULT_ATTEMPTS
        max_attempts = attempts - 1

        for attempt in range(attempts):
            try:
                return await func(self, *args, **kwargs)
            except BleakNotFoundError:
                # The lock cannot be found so there is no
                # point in retrying.
                raise
            except RETRY_BACKOFF_EXCEPTIONS as err:
                if attempt >= max_attempts:
                    LOGGER.debug(
                        "%s: %s error calling %s, reach max attempts (%s/%s)",
                        self.name,
                        type(err),
                        func,
                        attempt,
                        max_attempts,
                        exc_info=True,
                    )
                    raise
                LOGGER.debug(
                    "%s: %s error calling %s, backing off %ss, retrying (%s/%s)...",
                    self.name,
                    type(err),
                    func,
                    BLEAK_BACKOFF_TIME,
                    attempt,
                    max_attempts,
                    exc_info=True,
                )
                await asyncio.sleep(BLEAK_BACKOFF_TIME)
            except BLEAK_EXCEPTIONS as err:
                if attempt >= max_attempts:
                    LOGGER.debug(
                        "%s: %s error calling %s, reach max attempts (%s/%s): %s",
                        self.name,
                        type(err),
                        func,
                        attempt,
                        max_attempts,
                        err,
                        exc_info=True,
                    )
                    raise
                LOGGER.debug(
                    "%s: %s error calling %s, retrying  (%s/%s)...: %s",
                    self.name,
                    type(err),
                    func,
                    attempt,
                    max_attempts,
                    err,
                    exc_info=True,
                )

    return cast(WrapFuncType, _async_wrap_retry_bluetooth_connection_error)


def rgb_to_hsv(r,g,b):
    h, s, v = colorsys.rgb_to_hsv(r/255.0,g/255.0,b/255.0)
    h, s, v = int(h*360), int(s*100), int(v*100)
    return [h,s,v]

class LEDNETWFInstance:
    def __init__(self, mac, hass, data={}, options={}) -> None:
        self._data    = data
        self._options = options
        self._hass    = hass
        self._mac     = mac
        self._delay   = self._options.get(CONF_DELAY, self._data.get(CONF_DELAY, 120)) # Try and read from options first, data second so that if this is changed via config then new values are picked up
        LOGGER.debug(f"In instantiation of LEDNET instance.  Delay: {self._delay}")
        LOGGER.debug(f"Data: {self._data}")
        LOGGER.debug(f"Options: {self._options}")
        self.loop     = asyncio.get_running_loop()
        self._device:   BLEDevice | None = None
        self._device  = bluetooth.async_ble_device_from_address(self._hass, self._mac)
        if not self._device:
            raise ConfigEntryNotReady(
                f"You need to add bluetooth integration (https://www.home-assistant.io/integrations/bluetooth) or couldn't find a nearby device with address: {self._mac}"
            )
        
        service_info  = bluetooth.async_last_service_info(self._hass, self._mac).as_dict()
        manu_data     = service_info['manufacturer_data']
        self._model                 = self._detect_model(manu_data)
        self._connect_lock: asyncio.Lock = asyncio.Lock()
        self._client: BleakClientWithServiceCache | None = None
        self._disconnect_timer: asyncio.TimerHandle | None = None
        self._cached_services: BleakGATTServiceCollection | None = None
        self._expected_disconnect   = False
        self._packet_counter        = 0
        self._is_on                 = None
        self._hs_color              = None
        self._rgb_color             = None
        self._brightness            = 255
        self._effect                = EFFECT_OFF_HA # 2024.2 this indicates HA that we support effects and they are currently off
        self._effect_speed          = 0x64
        self._color_mode            = ColorMode.HS if self._model == RING_LIGHT_MODEL else ColorMode.RGB
        self._write_uuid            = None
        self._read_uuid             = None
        self._led_count             = options.get(CONF_LEDCOUNT, None)
        self._color_order           = options.get(CONF_COLORORDER, None)
        self._chip_type             = options.get(CONF_LEDTYPE, None)
        self._color_temp_kelvin     = None
        self._max_color_temp_kelvin = 6500
        self._min_color_temp_kelvin = 2700
        self._on_update_callbacks = []
        LOGGER.debug(
            "Model information for device %s : ModelNo %s. MAC: %s",
            self._device.name,
            self._model,
            self._mac,
        )

    def _detect_model(self, manu_data):
        # This will pre-set a number of options to those which the device is currently advertising.  e.g. if the device is already on and red, this will pre-set those values.
        manu_data_id = next(iter(manu_data))
        manu_data_data = bytearray(manu_data[manu_data_id])
        formatted = [f'0x{byte:02X}' for byte in manu_data_data]
        formatted_str = ' '.join(formatted)
        LOGGER.debug(f"DM: \t\t Detecting model... {self.name}")
        LOGGER.debug(f"DM:\t\t Manufacturer id: {manu_data_id}")
        LOGGER.debug(f"DM:\t\t Manu data: {formatted_str}")
        # Example manu data:
        # 0    1    2    3    4    5    6    7    8    9    10   11   12   13   14   15   16   17   18   19   20   21   22   23   24   25   26
        # 0x53 0x05 0x08 0x65 0xF0 0x0C 0xDA 0x81 0x00 0x1D 0x0F 0x02 0x01 0x01 0x24 0x61 0xF0 0x00 0xFC 0x00 0x00 0x00 0x02 0x00 0x1C 0x00 0x00
        self._led_count  = manu_data_data[24]
        self._is_on      = manu_data_data[14] == 0x23
        r,g,b            = manu_data_data[18], manu_data_data[19], manu_data_data[20]
        hsv              = rgb_to_hsv(r,g,b)
        self._hs_color   = (hsv[0],hsv[1])
        self._brightness = int(hsv[2] * 255 / 100)
        brightness_percent = self.normalize_brightness(self._brightness)
        self._rgb_color  = tuple((component / brightness_percent)*100 for component in (r,g,b))
        self._fw_major   = manu_data_data[0]
        self._fw_minor   = f'{manu_data_data[8]:02X}{manu_data_data[9]:02X}.{manu_data_data[10]:02X}'
        self._color_mode = ColorMode.HS if self._fw_major == RING_LIGHT_MODEL else ColorMode.RGB
        LOGGER.debug(f"DM:\t\t LED count:  {self._led_count}")
        LOGGER.debug(f"DM:\t\t Is on:      {self._is_on}")
        LOGGER.debug(f"DM:\t\t HS Color:   {self._hs_color}")
        LOGGER.debug(f"DM:\t\t RGB Color:  {r},{g},{b}")
        LOGGER.debug(f"DM:\t\t Brightness: {self._brightness}")
        LOGGER.debug(f"DM:\t\t FW Major:   {self._fw_major}")
        LOGGER.debug(f"DM:\t\t FW Minor:   {self._fw_minor}")
        LOGGER.debug(f"DM:\t\t Color Mode: {self._color_mode}")
        return self._fw_major # Is this the best way to differentiate between models?

    async def _write(self, data: bytearray):
        """Send command to device and read response."""
        if not self._client:
            await self._ensure_connected(setup=True)
        else:
            await self._ensure_connected(setup=False)
        if self._packet_counter > 65535:
            self._packet_counter = 0
        data[0] = 0xFF00 & self._packet_counter
        data[1] = 0x00FF & self._packet_counter
        self._packet_counter += 1
        await self._write_while_connected(data)

    async def _write_while_connected(self, data: bytearray):
        LOGGER.debug(f"Writing data to {self.name}: {' '.join([f'{byte:02X}' for byte in data])}")
        await self._client.write_gatt_char(self._write_uuid, data, False)
    
    def _notification_handler(self, _sender: BleakGATTCharacteristic, data: bytearray) -> None:
        # Response data is decoded here:  https://github.com/8none1/zengge_lednetwf#response-data
        #TODO: If nothing has changed, bail out early
        """Handle BLE notifications from the device.  Update internal state to reflect the device state."""
        LOGGER.debug("N: %s: Notification received", self.name)
        LOGGER.debug(f"N: Device info: {self._model, self.name, self._mac}")
        response_str = data.decode("utf-8", errors="ignore")
        last_quote = response_str.rfind('"')
        if last_quote > 0:
            first_quote = response_str.rfind('"', 0, last_quote)
            if first_quote > 0:
                payload = response_str[first_quote+1:last_quote]
            else:
                return None
        else:
            return None
        payload = bytearray.fromhex(payload)
        LOGGER.debug(f"N: Response Payload: {' '.join([f'{byte:02X}' for byte in payload])}")
        if payload[0] == 0x81:
            # Status update response. TODO: Look up 0x81 (129d) in jadx
            LOGGER.debug("N: Status response received")   
            sending_model   = payload[1]
            power           = payload[2]
            mode            = payload[3]
            selected_effect = payload[4]
            led_count       = payload[12]
            # checksum = payload[13] # TODO: Implement checksum checking?

            if power == 0x23:
                self._is_on = True
            elif power == 0x24:
                self._is_on = False

            if mode == 0x61:
                if selected_effect == 0xf0:
                    # Light  is in Colour mode 
                    hsv = rgb_to_hsv(payload[6],payload[7],payload[8])
                    self._color_mode = ColorMode.HS
                    self._hs_color = (hsv[0],hsv[1])
                    self._brightness = int(hsv[2] * 255 / 100) # TODO: Maybe this is buggy?  Should brightnesses bs 8bit values not percentages?
                    self._color_temp_kelvin = None
                    self._effect = EFFECT_OFF_HA
                    LOGGER.debug(f"N: HS Color mode:")
                    LOGGER.debug(f"N: \t System colour: {self._hs_color}")
                    LOGGER.debug(f"N: \t Brightness: {self._brightness}")
                if selected_effect == 0x0f:
                    # White mode
                    LOGGER.debug("N: White mode")
                    col_temp = payload[9]
                    color_temp_kelvin = self._min_color_temp_kelvin + col_temp * (self._max_color_temp_kelvin - self._min_color_temp_kelvin) / 100
                    self._color_mode = ColorMode.COLOR_TEMP
                    self._hs_color = None
                    self._effect = EFFECT_OFF_HA
                    self._color_temp_kelvin = color_temp_kelvin
                    self._brightness = int(payload[5] * 255 / 100)
                    LOGGER.debug(f"N: \t Color Temp kelvin: {self._color_temp_kelvin}")
                    LOGGER.debug(f"N: \t Brightness: {self._brightness}")
                if selected_effect == 0x01:
                    # RGB mode
                    # RGB mode and brightness are a bit of a complex problem.  HA send us the colour and brightness separately.  i.e. the RGB colour coming in from HA
                    # is correct and not scaled by brightness.  However, these lights adjust brightness by scaling the RGB values, not by accepting a brightness value.
                    # This means the RGB tuple sent to the device is being scaled at the point of transmission, and so the actual RGB data sent to the device
                    # is different from the colour selected by the user.  To recover the original RGB values we can, I think, just scale back the other way and multiply
                    # the incoming RGB by the brightness percentage.  This will give us the original RGB values.  We can then send these to the UI.
                    # There is sometimes a lag between the outgoing packet being sent and the notification being received.  This means that the colours can jump around
                    # a bit when you are dragging the colour picker around.  I'm not sure this is a real problem though, it's easy to ignore.  How could we fix it?
                    # Maybe a rate limit on the incoming notifications?  For now, just live with it - it's no worse than it has been before.

                    LOGGER.debug("N: RGB mode")
                    self._color_mode        = ColorMode.RGB
                    self._hs_color          = None
                    self._color_temp_kelvin = None
                    self._effect            = EFFECT_OFF_HA
                    rgb_in = (payload[6],payload[7],payload[8])
                    LOGGER.debug(f"N: \t RGB Colour IN : {rgb_in}")
                    brightness_percent = self.normalize_brightness(self._brightness)
                    LOGGER.debug(f"N: \t Brightness: {brightness_percent}")
                    rgb_out = tuple(int((component * 100) / brightness_percent) for component in rgb_in)
                    rgb_out = tuple(max(0, min(255, component)) for component in rgb_out)
                    self._rgb_color = rgb_out
                    LOGGER.debug(f"N: \t RGB Colour OUT: {rgb_out}")
            if mode == 0x25:
                LOGGER.debug("N: Effects mode")
                EFFECT_ID_TO_NAME = EFFECT_ID_TO_NAME_0x53 if sending_model == RING_LIGHT_MODEL else EFFECT_ID_TO_NAME_0x56
                try:
                    effect_name = EFFECT_ID_TO_NAME[selected_effect]
                    LOGGER.debug(f"N: \t Effect name: {effect_name}")
                except KeyError:
                    LOGGER.debug("N: \t Effect name not found")
                    effect_name = "Unknown"
                self._effect = effect_name
                speed = payload[7] if sending_model == RING_LIGHT_MODEL else payload[5]
                self._color_mode = ColorMode.BRIGHTNESS # 2024.2 Allows setting color mode for changing effects brightness
                self._brightness = int(payload[6] * 255 / 100)
                self._effect_speed = int(speed * 255 / 100)
                if not 0 <= self._effect_speed <= 255:
                    self._effect_speed = 128
                LOGGER.debug(f"N: \t Brightness (0-255): {self._brightness}")
                LOGGER.debug(f"N: \t Effect speed: {self._effect_speed}")

        if self._model == RING_LIGHT_MODEL:
            if payload[0] == 0x63:
                LOGGER.debug("N: LED settings packet: Ring device")
                led_count         = payload[2]
                chip_type         = payload[3]
                colour_order      = payload[4]
                self._led_count   = led_count
                self._chip_type   = LedTypes_RingLight.from_value(chip_type)
                self._color_order = ColorOrdering.from_value(colour_order)
                LOGGER.debug(f"N: \t LED count: {led_count}")
                LOGGER.debug(f"N: \t Chip type: {chip_type} - {self._chip_type}")
                LOGGER.debug(f"N: \t Colour order: {colour_order} - {self._color_order}")

        if self._model == STRIP_LIGHT_MODEL:
            if payload[1] == 0x63:
                led_count = bytes([payload[2], payload[3]])
                led_count = int.from_bytes(led_count, byteorder='big') * payload[5]
                chip_type = payload[6]
                colour_order = payload[7]
                self._led_count = led_count
                self._chip_type = LedTypes_StripLight.from_value(chip_type)
                self._color_order = ColorOrdering.from_value(colour_order)
                LOGGER.debug("N: LED settings packet: Strip device")
                LOGGER.debug(f"N: \t Number of segments: {payload[5]}")
                LOGGER.debug(f"N: \t LED count: {led_count}")
                LOGGER.debug(f"N: \t Chip type: {chip_type} : {self._chip_type}")
                LOGGER.debug(f"N: \t Colour order: {colour_order} : {self._color_order}")
        
        self.local_callback()

    async def send_initial_packets(self):
        # Send initial packets to device to see if it sends notifications
        LOGGER.debug("%s: Send initial packets", self.name)
        await self._write(INITIAL_PACKET)
        if not self._chip_type:
            # We should only need to get this once, since config is immutable.
            # All future changes of this data will come via the config flow.
            LOGGER.debug(f"Sending GET_LED_SETTINGS_PACKET to {self.name}")
            await self._write(GET_LED_SETTINGS_PACKET)
    
    @property
    def mac(self):
        return self._device.address

    # @property
    # def reset(self):
    #     return self._reset

    @property
    def name(self):
        return self._device.name

    @property
    def rssi(self):
        return self._device.rssi

    @property
    def is_on(self):
        return self._is_on

    @property
    def brightness(self):
        return self._brightness 

    @property
    def min_color_temp_kelvin(self):
        return self._min_color_temp_kelvin

    @property
    def max_color_temp_kelvin(self):
        return self._max_color_temp_kelvin

    @property
    def color_temp_kelvin(self):
        return self._color_temp_kelvin

    @property
    def hs_color(self):
        return self._hs_color

    @property
    def rgb_color(self):
        return self._rgb_color
    
    @property
    def effect_list(self) -> list[str]:
        if self._model == RING_LIGHT_MODEL:
            return EFFECT_LIST_0x53
        else:
            return EFFECT_LIST_0x56

    @property
    def effect(self):
        return self._effect
    
    @property
    def color_mode(self):
        return self._color_mode

    @retry_bluetooth_connection_error
    async def set_color_temp_kelvin(self, value: int, new_brightness: int):
        # White colours are represented by colour temperature percentage from 0x0 to 0x64 from warm to cool
        # Warm (0x0) is only the warm white LED, cool (0x64) is only the cool white LED and then a mixture between the two
        if value is None or new_brightness is None:
            return
        if value < self._min_color_temp_kelvin:
            value = self._min_color_temp_kelvin
        if value > self._max_color_temp_kelvin:
            value = self._max_color_temp_kelvin
        self._color_temp_kelvin = value
        brightness_percent = self.normalize_brightness(new_brightness)

        color_temp_percent = int(
            ((value - self._min_color_temp_kelvin) * 100)
            / (self._max_color_temp_kelvin - self._min_color_temp_kelvin)
        )
        
        # Color temp packet + brightness
        color_temp_kelvin_packet = bytearray.fromhex("00 10 80 00 00 0d 0e 0b 3b b1 00 00 00 00 00 00 00 00 00 00 3d")
        color_temp_kelvin_packet[13] = color_temp_percent
        color_temp_kelvin_packet[14] = brightness_percent
        await self._write(color_temp_kelvin_packet)
        self._color_mode = ColorMode.COLOR_TEMP
        self._effect = EFFECT_OFF_HA

    @retry_bluetooth_connection_error
    async def set_hs_color(self, hs: Tuple[int, int], new_brightness: int):
        # The device expects basic static colour information in HSV format.
        # The value for the Hue element is divided by two to fit in to a single byte.
        # Saturation and Value are percentages from 0 to 100 (0x64).
        # Value = Brightness
        LOGGER.debug("Setting HS Color") # todo remove this bit
        if hs is None:
            LOGGER.debug("HS is None")
            return
        else:
            LOGGER.debug(f"HS is {hs}")

        self._color_mode = ColorMode.HS
        self._hs_color = hs
        self._rgb_color = None
        self._color_temp_kelvin = None
        self._effect = EFFECT_OFF_HA
        hue = int(hs[0] / 2)
        saturation = int(hs[1])
        brightness_percent = self.normalize_brightness(new_brightness)
        color_hs_packet = bytearray.fromhex("00 00 80 00 00 0d 0e 0b 3b a1 00 64 64 00 00 00 00 00 00 00 00")
        color_hs_packet[10] = hue
        color_hs_packet[11] = saturation
        color_hs_packet[12] = brightness_percent
        await self._write(color_hs_packet)

    @retry_bluetooth_connection_error
    async def set_rgb_color(self, rgb: Tuple[int, int, int], new_brightness: int, fixed_effect=EFFECT_OFF_HA):
        # The strip light devices on firmware 0x56 support RGB colours via a different command
        # RGB colour handling is difficult on these devices because they don't implement a separate brightness control.  Instead, the RGB values are scaled by the brightness percentage.
        # This means we have to try and recover brightness from the RGB values sent back by the notification.  If the values drop below a certain threshold all colour information is
        # lost and we can't get it back (e.g. a colour of 1,1,1).  To try and work around this we limit the minimum colour value to 25 (10%)  This didn't work.  25 is too high and meant
        # that the colours were off.  I think I might have fixed the problem though, things were getting scaled twice, on the way out and on the way back in via the notification.

        LOGGER.debug("Set RGB: Setting RGB Color")
        self._color_mode = ColorMode.RGB
        self._hs_color = None
        #self._effect = effect 
        self._brightness = new_brightness
        brightness_percent = self.normalize_brightness(new_brightness)
        LOGGER.debug(f"Set RGB: Raw RGB Color: {rgb}")
        if rgb is not None:
            self._rgb_color = rgb
            r = rgb[0] * brightness_percent // 100
            g = rgb[1] * brightness_percent // 100
            b = rgb[2] * brightness_percent // 100
            LOGGER.debug(f"Set RGB: Scaled RGB: RGB Color: {r},{g},{b}")
        else:
            rgb = self._rgb_color
        
        r = max(r, 0)
        g = max(g, 0)
        b = max(b, 0)

        background_col = [0,0,0] # Consider adding support for this in the future?  For now, set black
        rgb_packet = bytearray.fromhex("00 00 80 00 00 0d 0e 0b 41 02 ff 00 00 00 00 00 32 00 00 f0 64")
        rgb_packet[9]  = fixed_effect # 1 is simple RGB mode
        rgb_packet[10] = r
        rgb_packet[11] = g
        rgb_packet[12] = b
        rgb_packet[13] = background_col[0]
        rgb_packet[14] = background_col[1]
        rgb_packet[15] = background_col[2]
        rgb_packet[20] = sum(rgb_packet[8:19]) & 0xFF # Checksum
        await self._write(rgb_packet)
        
    @retry_bluetooth_connection_error
    async def set_effect(self, effect: str, new_brightness: int):
        EFFECT_LIST = EFFECT_LIST_0x53 if self._model == RING_LIGHT_MODEL else EFFECT_LIST_0x56
        EFFECT_MAP =  EFFECT_MAP_0x53  if self._model == RING_LIGHT_MODEL else EFFECT_MAP_0x56
        if effect not in EFFECT_LIST or effect is EFFECT_OFF_HA:
            LOGGER.error("Effect %s not supported or effect off called", effect)
            return
        self._effect = effect
        self._color_mode  = ColorMode.BRIGHTNESS # 2024.2 Allows setting color mode for changing effects brightness
        effect_packet     = bytearray.fromhex("00 00 80 00 00 04 05 0b 38 01 32 64") if self._model == RING_LIGHT_MODEL else bytearray.fromhex("00 00 80 00 00 05 06 0b 42 01 32 64 d9")
        effect_id         = EFFECT_MAP.get(effect)
        effect_packet[9]  = effect_id
        effect_packet[10] = self._effect_speed # TODO: Support variable speeds.
        effect_packet[11] = self.normalize_brightness(new_brightness)
        if self._model == STRIP_LIGHT_MODEL:
            effect_packet[12] = sum(effect_packet[8:11]) & 0xFF
        LOGGER.debug(f"Brightness passed in to set_effect is {new_brightness}")
        LOGGER.debug(f"After calling Normalized brightness is: {self._brightness}")
        await self._write(effect_packet)

    @retry_bluetooth_connection_error
    async def turn_on(self):
        await self._write(bytearray.fromhex("00 01 80 00 00 0d 0e 0b 3b 23 00 00 00 00 00 00 00 32 00 00 90"))
        self._is_on = True
    
    @retry_bluetooth_connection_error
    async def turn_off(self):
        await self._write(bytearray.fromhex("00 01 80 00 00 0d 0e 0b 3b 24 00 00 00 00 00 00 00 32 00 00 91"))
        self._is_on = False

    @retry_bluetooth_connection_error
    async def set_led_settings(self, options: dict):
        led_count   = options.get(CONF_LEDCOUNT)
        chip_type   = options.get(CONF_LEDTYPE)
        color_order = options.get(CONF_COLORORDER)
        self._delay = options.get(CONF_DELAY, 120)
        
        if led_count is None or chip_type is None or color_order is None:
            LOGGER.warn("LED count, chip type or colour order is None and shouldn't be.  Not setting LED settings.")
            return
        
        if led_count == self._led_count and chip_type == self._chip_type and color_order == self._color_order:
            # If the settings are the same as the current settings, don't bother sending the packet
            LOGGER.debug("Not updating LED settings, nothing to change")
            return
        else:
            self._chip_type         = chip_type
            self._color_order       = color_order
            self._led_count         = led_count

        if self._model == RING_LIGHT_MODEL:
            chip_type           = getattr(LedTypes_RingLight, chip_type).value
        elif self._model == STRIP_LIGHT_MODEL:
            chip_type           = getattr(LedTypes_StripLight, chip_type).value
        
        color_order             = getattr(ColorOrdering, color_order).value

        if self._model == STRIP_LIGHT_MODEL:
            led_settings_packet     = bytearray.fromhex("00 00 80 00 00 0b 0c 0b 62 00 64 00 03 01 00 64 03 f0 21")
            led_count_bytes         = bytearray(led_count.to_bytes(2, byteorder='big'))
            led_settings_packet[9], led_settings_packet[10]  = led_count_bytes
            led_settings_packet[11], led_settings_packet[12] = [0,1] # We're only supporting a single segment
            led_settings_packet[13] = chip_type
            led_settings_packet[14] = color_order
            led_settings_packet[15] = led_count & 0xFF # I think this is "music mode" which can have a different number of leds to "lightbar" mode. Not going to think about that yet.
            led_settings_packet[16] = 1 # 1 music mode segment
            led_settings_packet[17] = sum(led_settings_packet[9:18]) & 0xFF
        if self._model == RING_LIGHT_MODEL:
            led_settings_packet     = bytearray.fromhex("00 00 80 00 00 06 07 0a 62 00 0e 01 00 71")
            led_settings_packet[10] = led_count & 0xFF
            led_settings_packet[11] = chip_type
            led_settings_packet[12] = color_order
            led_settings_packet[13] = sum(led_settings_packet[8:12]) & 0xFF

        LOGGER.debug(f"LED settings packet: {' '.join([f'{byte:02X}' for byte in led_settings_packet])}")
        await self._write(led_settings_packet)
        await self._write(GET_LED_SETTINGS_PACKET)
        await self.stop()
    
    @retry_bluetooth_connection_error
    async def update(self, setup=False):
        # Called when HA starts up and wants the devices to initialise themselves
        LOGGER.debug("%s: Update in lwdnetwf called", self.name)
        if not self._client: setup=True
        try:
            await self._ensure_connected(setup=setup)
        except Exception as error:
            #self._is_on = None # failed to connect, this should mark it as unavailable.  TODO There might be a race here when setting RGB settings.
            LOGGER.error("Error getting status: %s", error)
            track = traceback.format_exc()
            LOGGER.debug(track)

    async def _ensure_connected(self, setup=False) -> None:
        """Ensure connection to device is established."""
        LOGGER.debug("%s: Ensure connected", self.name)
        LOGGER.debug(f"Setup is {setup}")
        if self._connect_lock.locked():
            LOGGER.debug(f"ES {self.name}: Connection already in progress, waiting for it to complete")
        
        if self._client and self._client.is_connected:
            self._reset_disconnect_timer()
            return

        async with self._connect_lock:
            # Check again while holding the lock
            if self._client and self._client.is_connected:
                self._reset_disconnect_timer()
                return
            LOGGER.debug("%s: Connecting", self.name)
            client = await establish_connection(
                BleakClientWithServiceCache,
                self._device,
                self.name,
                self._disconnected,
                cached_services=self._cached_services,
                ble_device_callback=lambda: self._device,
            )
            LOGGER.debug("%s: Connected", self.name)
            resolved = self._resolve_characteristics(client.services)
            if not resolved:
                # Try to handle services failing to load
                resolved = self._resolve_characteristics(await client.get_services())
            self._cached_services = client.services if resolved else None

            self._client = client
            self._reset_disconnect_timer()

            # Subscribe to notification is needed for LEDnetWF devices to accept commands
            self._notification_callback = self._notification_handler
            await client.start_notify(self._read_uuid, self._notification_callback)
            LOGGER.debug("%s: Subscribed to notifications", self.name)
            if setup:
                await self.send_initial_packets()

    def _resolve_characteristics(self, services: BleakGATTServiceCollection) -> bool:
        """Resolve characteristics."""
        for characteristic in NOTIFY_CHARACTERISTIC_UUIDS:
            if char := services.get_characteristic(characteristic):
                self._read_uuid = char
                LOGGER.debug("%s: Read UUID: %s", self.name, self._read_uuid)
                break
        for characteristic in WRITE_CHARACTERISTIC_UUIDS:
            if char := services.get_characteristic(characteristic):
                self._write_uuid = char
                break
        return bool(self._read_uuid and self._write_uuid)

    def _reset_disconnect_timer(self) -> None:
        """Reset disconnect timer."""
        LOGGER.debug("Pat the dog")
        if self._disconnect_timer:
            self._disconnect_timer.cancel()
        self._expected_disconnect = False
        if self._delay is not None and self._delay != 0:
            LOGGER.debug(
                "%s: Configured disconnect from device in %s seconds",
                self.name,
                self._delay
            )
            self._disconnect_timer = self.loop.call_later(self._delay, self._disconnect)

    def _disconnected(self, client: BleakClientWithServiceCache) -> None:
        """Disconnected callback."""
        if self._expected_disconnect:
            LOGGER.debug("%s: Disconnected from device", self.name)
            return
        LOGGER.warning("%s: Device unexpectedly disconnected", self.name)

    def _disconnect(self) -> None:
        """Disconnect from device."""
        self._disconnect_timer = None
        asyncio.create_task(self._execute_timed_disconnect())

    async def stop(self) -> None:
        """Stop the LEDBLE."""
        LOGGER.debug("%s: Stop", self.name)
        await self._execute_disconnect()

    async def _execute_timed_disconnect(self) -> None:
        """Execute timed disconnection."""
        LOGGER.debug(
            "%s: Disconnecting after timeout of %s",
            self.name,
            self._delay
        )
        await self._execute_disconnect()

    async def _execute_disconnect(self) -> None:
        """Execute disconnection."""
        async with self._connect_lock:
            read_char = self._read_uuid
            client = self._client
            self._expected_disconnect = True
            self._client = None
            self._write_uuid = None
            self._read_uuid = None
            if client and client.is_connected:
                await client.stop_notify(read_char)
                await client.disconnect()
            LOGGER.debug("%s: Disconnected", self.name)
    
    def local_callback(self):
        # Placeholder to be replaced by a call from light.py
        # I can't work out how to plumb a callback from here to light.py
        return

    def normalize_brightness(self, new_brightness):
        "Make sure brightness is between 2 and 255 and then convert to percentage"
        LOGGER.debug("Doing Normalizing brightness function")
        LOGGER.debug("New brightness passed IN is %s", new_brightness)
        if new_brightness is None and self._brightness is None:
            new_brightness = 255
        elif new_brightness is None and self._brightness > 1:
            new_brightness = self._brightness
        new_brightness = max(new_brightness, 2)
        new_brightness = min(new_brightness, 255)
        LOGGER.debug("New brightness (0-255) is %s", new_brightness)
        self._brightness = new_brightness
        new_percentage = int(new_brightness * 100 / 255)
        LOGGER.debug("Normalized brightness percent is %s", new_percentage)
        return new_percentage
  