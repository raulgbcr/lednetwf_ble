import asyncio
from datetime import datetime
from homeassistant.components import bluetooth
from homeassistant.exceptions import ConfigEntryNotReady
# from homeassistant.helpers.update_coordinator import (
#     CoordinatorEntity,
#     DataUpdateCoordinator,
# )

from homeassistant.components.light import (ColorMode)

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


# Add effects information in a separate file because there is a LOT of boilerplate.
from .effects import (
    EFFECT_MAP,
    EFFECT_LIST,
    EFFECT_ID_TO_NAME
)

LOGGER = logging.getLogger(__name__)

NAME_ARRAY = ["LEDnetWF"]
WRITE_CHARACTERISTIC_UUIDS    = ["0000ff01-0000-1000-8000-00805f9b34fb"]
NOTIFY_CHARACTERISTIC_UUIDS   = ["0000ff02-0000-1000-8000-00805f9b34fb"]
TURN_ON_CMD    = [bytearray.fromhex("00 04 80 00 00 0d 0e 0b 3b 23 00 00 00 00 00 00 00 32 00 00 90")]
TURN_OFF_CMD   = [bytearray.fromhex("00 5b 80 00 00 0d 0e 0b 3b 24 00 00 00 00 00 00 00 32 00 00 91")]
INITIAL_PACKET = bytearray.fromhex("00 01 80 00 00 04 05 0a 81 8a 8b 96")
MIN_COLOR_TEMPS_K = [2700]
MAX_COLOR_TEMPS_K = [6500]
DEFAULT_ATTEMPTS = 3
BLEAK_BACKOFF_TIME = 0.25
RETRY_BACKOFF_EXCEPTIONS = (BleakDBusError,)

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
    def __init__(self, address, reset: bool, delay: int, hass) -> None:
        self.loop = asyncio.get_running_loop()
        self._mac = address
        self._reset = reset
        self._delay = delay
        self._hass = hass
        self._device: BLEDevice | None = None
        self._device = bluetooth.async_ble_device_from_address(self._hass, address)
        if not self._device:
            raise ConfigEntryNotReady(
                f"You need to add bluetooth integration (https://www.home-assistant.io/integrations/bluetooth) or couldn't find a nearby device with address: {address}"
            )
        self._connect_lock: asyncio.Lock = asyncio.Lock()
        self._client: BleakClientWithServiceCache | None = None
        self._disconnect_timer: asyncio.TimerHandle | None = None
        self._cached_services: BleakGATTServiceCollection | None = None
        self._expected_disconnect = False
        self._packet_counter = 0
        self._is_on = None
        self._hs_color = None
        self._brightness = 255
        self._effect = None
        self._effect_speed = 0x64
        self._color_temp_kelvin = None
        self._color_mode = ColorMode.HS
        self._write_uuid = None
        self._read_uuid = None
        self._turn_on_cmd = None
        self._turn_off_cmd = None
        self._max_color_temp_kelvin = None
        self._min_color_temp_kelvin = None
        self._model = self._detect_model()
        self._on_update_callbacks = []
        
        LOGGER.debug(
            "Model information for device %s : ModelNo %s. MAC: %s",
            self._device.name,
            self._model,
            self._mac,
        )

    def _detect_model(self):
        x = 0
        for name in NAME_ARRAY:
            if self._device.name.lower().startswith(name.lower()):
                self._turn_on_cmd = TURN_ON_CMD[x]
                self._turn_off_cmd = TURN_OFF_CMD[x]
                self._max_color_temp_kelvin = MAX_COLOR_TEMPS_K[x]
                self._min_color_temp_kelvin = MIN_COLOR_TEMPS_K[x]
                return x
            x = x + 1

    async def _write(self, data: bytearray):
        """Send command to device and read response."""
        await self._ensure_connected()
        if self._packet_counter > 65535:
            self._packet_counter = 0
        data[0] = 0xFF00 & self._packet_counter
        data[1] = 0x00FF & self._packet_counter
        self._packet_counter = self._packet_counter + 1
        await self._write_while_connected(data)

    async def _write_while_connected(self, data: bytearray):
        LOGGER.debug(f"Writing data to {self.name}: {data}")
        await self._client.write_gatt_char(self._write_uuid, data, False)
    
    def _notification_handler(self, _sender: BleakGATTCharacteristic, data: bytearray) -> None:
        # Response data is decoded here:  https://github.com/8none1/zengge_lednetwf#response-data
        #TODO: If nothing has changed, bail out early
        """Handle BLE notifications from the device.  Update internal state to reflect the device state."""
        LOGGER.debug("N: %s: Notification received", self.name)
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
        LOGGER.debug("N: Payload: %s", payload)
        payload = bytearray.fromhex(payload)
        power  = payload[2]
        mode   = payload[3]
        selected_effect = payload[4]
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
                self._brightness = int(hsv[2] * 255 / 100)
                self._color_temp_kelvin = None
                self._effect = None
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
                self._effect = None
                self._color_temp_kelvin = color_temp_kelvin
                self._brightness = int(payload[5] * 255 / 100)
                LOGGER.debug(f"N: \t Color Temp kelvin: {self._color_temp_kelvin}")
                LOGGER.debug(f"N: \t Brightness: {self._brightness}")

        if mode == 0x25:
            LOGGER.debug("N: Effects mode")
            try:
                effect_name = EFFECT_ID_TO_NAME[selected_effect]
                LOGGER.debug(f"N: \t Effect name: {effect_name}")
            except KeyError:
                LOGGER.debug("N: \t Effect name not found")
                effect_name = "Unknown"
            self._effect = effect_name
            self._brightness = int(payload[6] * 255 / 100)
            self._effect_speed = int(payload[7] * 255 / 100)
            LOGGER.debug(f"N: \t Brightness (0-255): {self._brightness}")
            LOGGER.debug(f"N: \t Effect speed: {self._effect_speed}")

        self.local_callback()


    @property
    def mac(self):
        return self._device.address

    @property
    def reset(self):
        return self._reset

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
    def effect_list(self) -> list[str]:
        return EFFECT_LIST

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
        self._effect = None

    @retry_bluetooth_connection_error
    async def set_hs_color(self, hs: Tuple[int, int], new_brightness: int):
        # The device expects basic static colour information in HSV format.
        # The value for the Hue element is divided by two to fit in to a single byte.
        # Saturation and Value are percentages from 0 to 100 (0x64).
        # Value = Brightness
        LOGGER.debug("Setting HS Color")
        if hs is None:
            LOGGER.debug("HS is None")
            return
        else:
            LOGGER.debug(f"HS is {hs}")

        self._color_mode = ColorMode.HS
        self._hs_color = hs
        self._effect = None
        self._color_temp_kelvin = None
        hue = int(hs[0] / 2)
        saturation = int(hs[1])
        brightness_percent = self.normalize_brightness(new_brightness)
        color_hs_packet = bytearray.fromhex("00 00 80 00 00 0d 0e 0b 3b a1 00 64 64 00 00 00 00 00 00 00 00")
        color_hs_packet[10] = hue
        color_hs_packet[11] = saturation
        color_hs_packet[12] = brightness_percent
        await self._write(color_hs_packet)


    @retry_bluetooth_connection_error
    async def set_effect(self, effect: str, new_brightness: int):
        if effect not in EFFECT_LIST:
            LOGGER.error("Effect %s not supported", effect)
            return
        self._effect = effect
        #self._color_mode = None # I think changing this to None is messing up the brightness in the front end
        effect_packet = bytearray.fromhex("00 06 80 00 00 04 05 0b 38 01 32 64")
        effect_id = EFFECT_MAP.get(effect)
        effect_packet[9] = effect_id
        effect_packet[10] = self._effect_speed # TODO: Support variable speeds. FLASH should allow us to switch between "fast" and "slow", but I can't work it out
        effect_packet[11] = self.normalize_brightness(new_brightness)
        LOGGER.debug(f"Brightness passed in to set_effect is {new_brightness}")
        LOGGER.debug(f"After calling Normalized brightness is: {self._brightness}")
        await self._write(effect_packet)

    @retry_bluetooth_connection_error
    async def turn_on(self):
        await self._write(self._turn_on_cmd)
        self._is_on = True

    @retry_bluetooth_connection_error
    async def turn_off(self):
        await self._write(self._turn_off_cmd)
        self._is_on = False

    @retry_bluetooth_connection_error
    async def update(self):
        LOGGER.debug("%s: Update in lwdnetwf called", self.name)
        try:
            await self._ensure_connected()
            self._is_on = False
            await asyncio.sleep(3) # TODO: Find a better way!
            # What I'm trying to achieve here is to wait for the device to send a notification
            # so that the status is updated correctly.  If nothing gets returned within a few
            # seconds, assume the device is unavailable.  This might not be a safe assumption.
            # It does mean however, that if the device is available and working, then everything
            # in the frontend is correct.  I don't know if this is worth it though.
        except Exception as error:
            self._is_on = None # failed to connect, this should mark it as unavailable
            LOGGER.error("Error getting status: %s", error)
            track = traceback.format_exc()
            LOGGER.debug(track)

    async def _ensure_connected(self) -> None:
        """Ensure connection to device is established."""
        if self._connect_lock.locked():
            LOGGER.debug(
                "%s: Connection already in progress, waiting for it to complete",
                self.name,
            )
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
            
            # Send initial packets to device to see if it sends notifications
            LOGGER.debug("%s: Send initial packets", self.name)
            await self._write_while_connected(INITIAL_PACKET)

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
        if self._disconnect_timer:
            self._disconnect_timer.cancel()
        self._expected_disconnect = False
        if self._delay is not None and self._delay != 0:
            LOGGER.debug(
                "%s: Configured disconnect from device in %s seconds",
                self.name,
                self._delay,
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
            self._delay,
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
                await client.stop_notify(read_char) #  TODO:  I don't think this is needed.  Bleak docs say it isnt.
                await client.disconnect()
    
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
        if new_brightness < 2:
            new_brightness = 2
        if new_brightness > 255:
            new_brightness = 255
        LOGGER.debug("New brightness (0-255) is %s", new_brightness)
        self._brightness = new_brightness
        new_percentage = int(new_brightness * 100 / 255)
        LOGGER.debug("Normalized brightness percent is %s", new_percentage)
        return new_percentage
    