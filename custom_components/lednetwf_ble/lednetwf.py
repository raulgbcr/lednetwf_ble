import asyncio
from datetime import datetime
from homeassistant.components import bluetooth
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.components.light import (ColorMode)
from homeassistant.const import CONF_MAC
from homeassistant.components.light import EFFECT_OFF

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
        # LOGGER.debug(f"In instantiation of LEDNET instance.  Delay: {self._delay}")
        # LOGGER.debug(f"Data: {self._data}")
        # LOGGER.debug(f"Options: {self._options}")
        self.loop     = asyncio.get_running_loop()
        self._device:   BLEDevice | None = None
        self._device  = bluetooth.async_ble_device_from_address(self._hass, self._mac)
        # LOGGER.debug(f"INIT Device: {self._device}")
        if not self._device:
            raise ConfigEntryNotReady(
                f"You need to add bluetooth integration (https://www.home-assistant.io/integrations/bluetooth) or couldn't find a nearby device with address: {self._mac}"
            )
        service_info  = bluetooth.async_last_service_info(self._hass, self._mac).as_dict()
        LOGGER.debug(f"Service info: {service_info}")
        LOGGER.debug(f"Service info keys: {service_info.keys()}")

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
        self._effect                = EFFECT_OFF # 2024.2 this indicates HA that we support effects and they are currently off
        self._effect_speed          = 0x64 # 0-100% speed
        self._min_color_temp_kelvin = 2700
        self._max_color_temp_kelvin = 6500
        self._model                 = self._detect_model(service_info['manufacturer_data'])
        self._color_mode            = ColorMode.HS if self._model == RING_LIGHT_MODEL else ColorMode.RGB #COlorMode.RGB IS GETTING DEPRECATED, CHANGE !!!!!!!!!!
        self._write_uuid            = None
        self._read_uuid             = None
        self._led_count             = options.get(CONF_LEDCOUNT, None)
        self._color_order           = options.get(CONF_COLORORDER, None)
        self._chip_type             = options.get(CONF_LEDTYPE, None)
        self._color_temp_kelvin     = None
        self._on_update_callbacks = []

        LOGGER.debug(
            "Model information for device %s : ModelNo %s. MAC: %s",
            self._device.name,
            self._model,
            self._mac,
        )

    def log(self, text):
        LOGGER.debug(f"  *** {self._mac} : \t {text}")

    def _detect_model(self, manu_data):
        # This will pre-set a number of options to those which the device is currently advertising.
        # e.g. if the device is already on and red, this will pre-set those values.
        # Looking at the SDK on Github: https://github.com/ZNEGGE-SDK/Android_BLE_SDKDemo/blob/0f03ef45881f711fd58905407129948c3e324385/app/src/main/java/com/lednet/LEDBluetooth/COMM/LedDeviceInfo.java#L8
        # suggests that there might be a way of detecting RGBWW devices other than the firmware version.  This will need more device reports to confirm.
        manu_data_id = next(iter(manu_data))
        manu_data_data = bytearray(manu_data[manu_data_id])
        formatted = [f'0x{byte:02X}' for byte in manu_data_data]
        formatted_str = ' '.join(formatted)
        self.log(f"DM:\t\t Manu data:         {formatted_str}")

        self._color_mode = ColorMode.BRIGHTNESS  ## ColorMode.Brigtness is getting deprecated, change !!!!!!!!!!!

        self._fw_major   = manu_data_data[0]
        self._fw_minor   = f'{manu_data_data[8]:02X}{manu_data_data[9]:02X}.{manu_data_data[10]:02X}'
        self._led_count  = manu_data_data[24]
        self._is_on      = True if manu_data_data[14] == 0x23 else False
        
        if manu_data_data[15] == 0x61:
            # Colour mode (RGB & Whites) and "Static" effects
            if manu_data_data[16] == 0xf0:
                # RGB Mode 

                ### ColorMode.RGB is getting deprecated, change !!!!!!!!!!!!

                r,g,b = manu_data_data[18], manu_data_data[19], manu_data_data[20]
                if self._fw_major == RING_LIGHT_MODEL:
                    self._rgb_color = (r,g,b)
                    hsv              = rgb_to_hsv(r,g,b)
                    self._hs_color   = (hsv[0],hsv[1])
                    self._brightness = int(hsv[2] * 255 // 100)
                    self._color_mode = ColorMode.HS if self._fw_major == RING_LIGHT_MODEL else ColorMode.RGB
                if self._fw_major == STRIP_LIGHT_MODEL:
                    self._color_mode   = ColorMode.RGB
                    self._rgb_color = (manu_data_data[18], manu_data_data[19], manu_data_data[20])
            elif manu_data_data[16] == 0x0f:
                # White mode
                color_temp = manu_data_data[21] # 00=warm, 64=cold
                white_bri = manu_data_data[17]
                self._color_temp_kelvin = self._min_color_temp_kelvin + color_temp * (self._max_color_temp_kelvin - self._min_color_temp_kelvin) / 100 # CoPilot did this, is it right?
                self._brightness = int(white_bri * 255 // 100)
                self._color_mode = ColorMode.COLOR_TEMP
            else:
                self._rgb_color = (manu_data_data[18], manu_data_data[19], manu_data_data[20])
                hsv              = rgb_to_hsv(*self._rgb_color)
                self._hs_color   = (hsv[0],hsv[1])
                self._brightness = int(hsv[2] * 255 // 100)
                self._color_mode = ColorMode.RGB
                self._effect_speed = manu_data_data[17]
                if self._fw_major == STRIP_LIGHT_MODEL:
                    if 0x02 <= manu_data_data[16] <= 0x0a:
                        self._effect = EFFECT_ID_TO_NAME_0x56[manu_data_data[16] << 8]
                    else:
                        self._effect = EFFECT_OFF
                    # TODO: Detect music mode
        
        if manu_data_data[15] == 0x62:
            # Music reactive mode
            self.log("Music reactive mode maybe")
            self._color_mode = ColorMode.BRIGHTNESS
            effect = manu_data_data[16]
            self.log(f"Effect: {effect}")
            scaled_effect = (effect + 0x32) << 8
            self._effect = EFFECT_ID_TO_NAME_0x56[scaled_effect]

        if manu_data_data[15] == 0x25:
                # Effects mode
                effect             = manu_data_data[16]
                # TODO: How does this work with static and music effects?
                self._effect       = EFFECT_ID_TO_NAME_0x53[effect] if self._fw_major == RING_LIGHT_MODEL else EFFECT_ID_TO_NAME_0x56[effect]
                self._effect_speed = manu_data_data[19]             if self._fw_major == RING_LIGHT_MODEL else manu_data_data[17]
                self._brightness   = int(manu_data_data[18] * 255 // 100)
                self._color_mode   = ColorMode.BRIGHTNESS

        self.log(f"DM:\t\t LED count:    {self._led_count}")
        self.log(f"DM:\t\t Is on:        {self._is_on}")
        self.log(f"DM:\t\t HS Color:     {self._hs_color}")
        self.log(f"DM:\t\t RGB Color:    {self._rgb_color}")
        self.log(f"DM:\t\t Brightness:   {self._brightness}")
        self.log(f"DM:\t\t FW Major:     {self._fw_major}")
        self.log(f"DM:\t\t FW Minor:     {self._fw_minor}")
        self.log(f"DM:\t\t Color Mode:   {self._color_mode}")
        self.log(f"DM:\t\t Effect Speed: {self._effect_speed}")
        return self._fw_major # Is this the best way to differentiate between models?

    async def _write(self, data: bytearray):
        """Send command to device and read response."""
        await self._ensure_connected()
        if self._packet_counter > 65535:
            self._packet_counter = 0
        data[0] = 0xFF00 & self._packet_counter
        data[1] = 0x00FF & self._packet_counter
        self._packet_counter += 1
        await self._write_while_connected(data)

    async def _write_while_connected(self, data: bytearray):
        self.log(f"Writing data to {self.name}: {' '.join([f'{byte:02X}' for byte in data])}")
        await self._client.write_gatt_char(self._write_uuid, data, False)
    
    def _notification_handler(self, _sender: BleakGATTCharacteristic, data: bytearray) -> None:
        # Response data is decoded here:  https://github.com/8none1/zengge_lednetwf#response-data
        #TODO: If nothing has changed, bail out early
        """Handle BLE notifications from the device.  Update internal state to reflect the device state."""
        self.log(f"N: {self.name}: Notification received")
        self.log(f"N: Device info: {self._model, self.name, self._mac}")
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
        self.log(f"N: Response Payload: {' '.join([f'{byte:02X}' for byte in payload])}")
        if payload[0] == 0x81:
            # Status update response. TODO: Look up 0x81 (129d) in jadx
            self.log("N: Status response received")
            power           = payload[2]
            mode            = payload[3]
            selected_effect = payload[4]
            led_count       = payload[12]
            # checksum = payload[13] # TODO: Implement checksum checking?
            self._is_on = True if power == 0x23 else False

            if mode == 0x61:
                if selected_effect == 0xf0:
                    # Light  is in Colour mode 
                    hsv = rgb_to_hsv(payload[6],payload[7],payload[8])
                    self._color_mode = ColorMode.HS
                    self._hs_color = (hsv[0],hsv[1])
                    self._brightness = int(hsv[2] * 255 // 100)
                    self._color_temp_kelvin = None
                    self._effect = EFFECT_OFF
                if selected_effect == 0x0f:
                    # White mode
                    col_temp = payload[9]
                    color_temp_kelvin = self._min_color_temp_kelvin + col_temp * (self._max_color_temp_kelvin - self._min_color_temp_kelvin) / 100
                    self._color_mode = ColorMode.COLOR_TEMP
                    self._hs_color = None
                    self._effect = EFFECT_OFF
                    self._color_temp_kelvin = color_temp_kelvin
                    self._brightness = int(payload[5] * 255 // 100)
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
                    self._color_mode        = ColorMode.RGB
                    self._hs_color          = None
                    self._color_temp_kelvin = None
                    self._effect            = EFFECT_OFF
                    rgb_in = tuple(payload[6:9])
                    brightness_percent = max(self.normalize_brightness(self._brightness),1)
                    self._rgb_color = tuple(max(0, min(255, int(component * 100 / brightness_percent))) for component in rgb_in)
                if 0x02 <= selected_effect <= 0x0a:
                    # "Static" effects from strip lights
                    self._color_mode = ColorMode.RGB
                    effect = selected_effect << 8 # Shift back to the numbers defined in the effect map in const
                    self._effect = EFFECT_ID_TO_NAME_0x56[effect]
                    self._effect_speed = payload[5]  
            
            if mode == 0x62:
                # Music effects mode from strip lights
                self._color_mode = ColorMode.BRIGHTNESS
                effect = payload[4]
                scaled_effect = (effect + 0x32) << 8
                try:
                    self._effect = EFFECT_ID_TO_NAME_0x56[scaled_effect]
                except KeyError:
                    self._effect = "Unknown"
                

            if mode == 0x25:
                self.log("N: Effects mode")
                EFFECT_ID_TO_NAME = EFFECT_ID_TO_NAME_0x53 if self._model == RING_LIGHT_MODEL else EFFECT_ID_TO_NAME_0x56
                try:
                    effect_name = EFFECT_ID_TO_NAME[selected_effect]
                except KeyError:
                    self.log("N: \t Effect name not found")
                    effect_name = "Unknown"
                self._effect = effect_name
                speed = payload[7] if self._model == RING_LIGHT_MODEL else payload[5]
                self._color_mode = ColorMode.BRIGHTNESS # 2024.2 Allows setting color mode for changing effects brightness
                self._brightness = int(payload[6] * 255 // 100)
                self._effect_speed = speed # Speed 0-100
                self.log(f"N: \t Brightness (0-255): {self._brightness}")
                self.log(f"N: \t Effect speed (0-100): {self._effect_speed}")

        if self._model == RING_LIGHT_MODEL:
            if payload[0] == 0x63:
                self.log("N: LED settings packet: Ring device")
                led_count         = payload[2]
                chip_type         = payload[3]
                colour_order      = payload[4]
                self._led_count   = led_count
                self._chip_type   = LedTypes_RingLight.from_value(chip_type)
                self._color_order = ColorOrdering.from_value(colour_order)

        if self._model == STRIP_LIGHT_MODEL:
            if payload[1] == 0x63:
                led_count = bytes([payload[2], payload[3]])
                led_count = int.from_bytes(led_count, byteorder='big') * payload[5]
                chip_type = payload[6]
                colour_order = payload[7]
                self._led_count = led_count
                self._chip_type = LedTypes_StripLight.from_value(chip_type)
                self._color_order = ColorOrdering.from_value(colour_order)
        
        self.log(f"N: \t Is on: {self._is_on}")
        self.log(f"N: \t HS Color: {self._hs_color}")
        self.log(f"N: \t RGB Color: {self._rgb_color}")
        self.log(f"N: \t Brightness: {self._brightness}")
        self.log(f"N: \t Effect: {self._effect}")
        self.log(f"N: \t Effect speed: {self._effect_speed}")
        self.log(f"N: \t Color mode: {self._color_mode}")
        self.log(f"N: \t Color temp kelvin: {self._color_temp_kelvin}")
        self.log(f"N: \t LED count: {self._led_count}")

        self.local_callback()

    async def send_initial_packets(self):
        # Send initial packets to device to see if it sends notifications
        self.log("Send initial packets")
        await self._write(INITIAL_PACKET)
        if not self._chip_type:
            # We should only need to get this once, since config is immutable.
            # All future changes of this data will come via the config flow.
            self.log(f"Sending GET_LED_SETTINGS_PACKET to {self.name}")
            await self._write(GET_LED_SETTINGS_PACKET)
    
    @property
    def mac(self):
        return self._device.address

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
        value = max(self._min_color_temp_kelvin, min(value, self._max_color_temp_kelvin))
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
        self._effect = EFFECT_OFF

    @retry_bluetooth_connection_error
    async def set_hs_color(self, hs: Tuple[int, int], new_brightness: int):
        # The device expects basic static colour information in HSV format.
        # The value for the Hue element is divided by two to fit in to a single byte.
        # Saturation and Value are percentages from 0 to 100 (0x64).
        # Value = Brightness
        if hs is None:
            self.log("HS is None")
            return
        else:
            self.log(f"HS is {hs}")

        self._color_mode = ColorMode.HS
        self._hs_color = hs
        self._rgb_color = None
        self._color_temp_kelvin = None
        self._effect = EFFECT_OFF
        hue = int(hs[0] / 2)
        saturation = int(hs[1])
        brightness_percent = self.normalize_brightness(new_brightness)
        color_hs_packet = bytearray.fromhex("00 00 80 00 00 0d 0e 0b 3b a1 00 64 64 00 00 00 00 00 00 00 00")
        color_hs_packet[10] = hue
        color_hs_packet[11] = saturation
        color_hs_packet[12] = brightness_percent
        await self._write(color_hs_packet)

    @retry_bluetooth_connection_error
    async def set_rgb_color(self, rgb: Tuple[int, int, int], new_brightness: int):
        # The strip light devices on firmware 0x56 support RGB colours via a different command
        # RGB colour handling is difficult on these devices because they don't implement a separate brightness control.  Instead, the RGB values are scaled by the brightness percentage.
        # This means we have to try and recover brightness from the RGB values sent back by the notification.  If the values drop below a certain threshold all colour information is
        # lost and we can't get it back (e.g. a colour of 1,1,1).  
        self.log("Set RGB: Setting RGB Color")
        if rgb is None and self._rgb_color is None:
            rgb = (255,0,0)
        elif rgb is None and self._rgb_color is not None:
            rgb = self._rgb_color 
        self._color_mode = ColorMode.RGB
        self._hs_color = None
        self._brightness = new_brightness
        brightness_percent = self.normalize_brightness(new_brightness)
        rgb = tuple(max(0, min(255, int(component * brightness_percent / 100))) for component in rgb)
        background_col = [0,0,0] # Consider adding support for this in the future?  For now, set black
        rgb_packet = bytearray.fromhex("00 00 80 00 00 0d 0e 0b 41 02 ff 00 00 00 00 00 32 00 00 f0 64")
        rgb_packet[9]  = 0 # Mode "0" leaves the static current mode unchanged.  If we want this to switch the device back to an actual static RGB mode change this to 1.
        # Leaving it as zero allows people to use the colour picker to change the colour of the static mode in realtime.  I'm not sure what I prefer.  If people want actual
        # static colours they can change to "Static Mode 1" in the effects.  But perhaps that's not what they would expect to have to do?  It's quite hidden.
        # But they pay off is that they can change the colour of the other static modes as they drag the colour picker around, which is pretty neat. ?
        rgb_packet[10:13] = rgb
        rgb_packet[13:16] = background_col
        rgb_packet[16]    = self._effect_speed
        rgb_packet[20]    = sum(rgb_packet[8:19]) & 0xFF # Checksum
        self.log(f"Set RGB. RGB {self._rgb_color} Brightness {self._brightness}")
        await self._write(rgb_packet)
        
    @retry_bluetooth_connection_error
    async def set_effect(self, effect: str, new_brightness: int):
        EFFECT_LIST = EFFECT_LIST_0x53 if self._model == RING_LIGHT_MODEL else EFFECT_LIST_0x56
        EFFECT_MAP  = EFFECT_MAP_0x53  if self._model == RING_LIGHT_MODEL else EFFECT_MAP_0x56
        if effect not in EFFECT_LIST or effect is EFFECT_OFF:
            LOGGER.error(f"Effect {effect} not supported or effect off called")
            return
        
        self._effect       = effect
        self.log(f"Setting effect: {effect}")
        brightness_percent = self.normalize_brightness(new_brightness)
        effect_id          = EFFECT_MAP.get(effect)
        self.log(f"Effect ID: {effect_id}")
        if self._rgb_color is None:
            # We haven't set a colour yet, so set it to red
            self._rgb_color = (255,0,0)

        if 0x0100 <= effect_id <= 0x1100: # See const for the meaning of these values.
            # We are dealing with "static" special effect numbers
            self.log(f"'Static' effect: {effect_id}")
            effect_id = effect_id >> 8 # Shift back to the actual effect id
            self.log(f"Special effect after shifting: {effect_id}")
            effect_packet = bytearray.fromhex("00 00 80 00 00 0d 0e 0b 41 02 ff 00 00 00 00 00 32 00 00 f0 64")
            rgb = tuple(max(0, min(255, int(component * brightness_percent / 100))) for component in self._rgb_color)
            effect_packet[9] = effect_id
            effect_packet[10:13] = rgb
            effect_packet[16] = self._effect_speed
            effect_packet[20] = sum(effect_packet[8:19]) & 0xFF # checksum
            self.log(f"static effect packet : {' '.join([f'{byte:02X}' for byte in effect_packet])}")
            await self._write(effect_packet)
            return
        
        if 0x2100 <= effect_id <= 0x4100: # Music mode.
            # We are dealing with a music mode effect
            effect_packet = bytearray.fromhex("00 22 80 00 00 0d 0e 0b 73 00 26 01 ff 00 00 ff 00 00 20 1a d2")
            self.log(f"Music effect: {effect_id}")
            effect_id = (effect_id >> 8) - 0x32 # Shift back to the actual effect id
            self.log(f"Music effect after shifting: {effect_id}")
            effect_packet[9]     = 1 # On
            effect_packet[11]    = effect_id
            effect_packet[12:15] = self._rgb_color
            effect_packet[15:18] = self._rgb_color # maybe background colour?
            effect_packet[18]    = self._effect_speed # Actually sensitivity, but would like to avoid another slider if possible
            effect_packet[19]    = brightness_percent
            effect_packet[20]    = sum(effect_packet[8:19]) & 0xFF
            self.log(f"music effect packet : {' '.join([f'{byte:02X}' for byte in effect_packet])}")
            await self._write(effect_packet)
            return
        
        effect_packet     = bytearray.fromhex("00 00 80 00 00 04 05 0b 38 01 32 64") if self._model == RING_LIGHT_MODEL else bytearray.fromhex("00 00 80 00 00 05 06 0b 42 01 32 64 d9")
        self._color_mode  = ColorMode.BRIGHTNESS # 2024.2 Allows setting color mode for changing effects brightness.  Effects above here support RGB, so only set here.
        effect_packet[9]  = effect_id
        effect_packet[10] = self._effect_speed # TODO: Support variable speeds.
        effect_packet[11] = brightness_percent
        if self._model == STRIP_LIGHT_MODEL:
            effect_packet[12] = sum(effect_packet[8:11]) & 0xFF
        await self._write(effect_packet)

    @retry_bluetooth_connection_error
    async def set_effect_speed(self, speed):
        speed = max(0, min(100, speed)) # Should be zero for stationary effects?
        self._effect_speed = speed
        if self._effect == EFFECT_OFF:
            return
        await self.set_effect(self._effect, self._brightness)

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
            self.log("Not updating LED settings, nothing to change")
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
            led_settings_packet[15] = led_count & 0xFF
            led_settings_packet[16] = 1 # 1 music mode segment, can support more in the app.
            led_settings_packet[17] = sum(led_settings_packet[9:18]) & 0xFF
        if self._model == RING_LIGHT_MODEL:
            led_settings_packet     = bytearray.fromhex("00 00 80 00 00 06 07 0a 62 00 0e 01 00 71")
            led_settings_packet[10] = led_count & 0xFF
            led_settings_packet[11] = chip_type
            led_settings_packet[12] = color_order
            led_settings_packet[13] = sum(led_settings_packet[8:12]) & 0xFF

        self.log(f"LED settings packet: {' '.join([f'{byte:02X}' for byte in led_settings_packet])}")
        await self._write(led_settings_packet)
        await self._write(GET_LED_SETTINGS_PACKET)
        await self.stop()
    
    @retry_bluetooth_connection_error
    async def update(self):
        # Called when HA starts up and wants the devices to initialise themselves
        self.log(f"{self.name}: Update in lwdnetwf called")
        try:
            await self._ensure_connected()
        except Exception as error:
            self.log(f"Error getting status: {error}")
            track = traceback.format_exc()
            self.log(track)

    async def _ensure_connected(self) -> None:
        """Ensure connection to device is established."""
        self.log(f"{self.name}: Ensure connected")
        if self._connect_lock.locked():
            self.log(f"ES {self.name}: Connection already in progress, waiting for it to complete")
        
        if self._client and self._client.is_connected:
            self._reset_disconnect_timer()
            return

        async with self._connect_lock:
            # Check again while holding the lock
            if self._client and self._client.is_connected:
                self._reset_disconnect_timer()
                return
            self.log(f"{self.name}: Connecting")
            client = await establish_connection(
                BleakClientWithServiceCache,
                self._device,
                self.name,
                self._disconnected,
                cached_services=self._cached_services,
                ble_device_callback=lambda: self._device,
            )
            self.log(f"{self.name}: Connected")
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
            self.log(f"{self.name}: Subscribed to notifications")

    def _resolve_characteristics(self, services: BleakGATTServiceCollection) -> bool:
        """Resolve characteristics."""
        for characteristic in NOTIFY_CHARACTERISTIC_UUIDS:
            if char := services.get_characteristic(characteristic):
                self._read_uuid = char
                break
        for characteristic in WRITE_CHARACTERISTIC_UUIDS:
            if char := services.get_characteristic(characteristic):
                self._write_uuid = char
                break
        return bool(self._read_uuid and self._write_uuid)

    def _reset_disconnect_timer(self) -> None:
        """Reset disconnect timer."""
        if self._disconnect_timer:
            self._disconnect_timer.cancel()
        self._expected_disconnect = False
        if self._delay is not None and self._delay != 0:
            self._disconnect_timer = self.loop.call_later(self._delay, self._disconnect)

    def _disconnected(self, client: BleakClientWithServiceCache) -> None:
        """Disconnected callback."""
        if self._expected_disconnect:
            LOGGER.debug("Disconnected from device")
            return
        LOGGER.warning("Device unexpectedly disconnected")

    def _disconnect(self) -> None:
        """Disconnect from device."""
        self._disconnect_timer = None
        asyncio.create_task(self._execute_timed_disconnect())

    async def stop(self) -> None:
        """Stop the LEDNET WF device."""
        LOGGER.debug("%s: Stop", self.name)
        await self._execute_disconnect()

    async def _execute_timed_disconnect(self) -> None:
        """Execute timed disconnection."""
        self.log(f"Disconnecting after timeout of {self._delay}")
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
            self.log("Disconnected")
    
    def local_callback(self):
        # Placeholder to be replaced by a call from light.py
        # I can't work out how to plumb a callback from here to light.py
        return

    def normalize_brightness(self, new_brightness):
        "Make sure brightness is between 2 and 255 and then convert to percentage"
        #self.log("Doing Normalizing brightness function")
        #self.log("New brightness passed IN is %s", new_brightness)
        if new_brightness is None and self._brightness is None:
            new_brightness = 255
        elif new_brightness is None and self._brightness > 1:
            new_brightness = self._brightness
        new_brightness = max(new_brightness, 2)
        new_brightness = min(new_brightness, 255)
        #self.log("New brightness (0-255) is %s", new_brightness)
        self._brightness = new_brightness
        new_percentage = int(new_brightness * 100 / 255)
        self.log(f"Normalized brightness percent is {new_percentage}")
        return new_percentage
  