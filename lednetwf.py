import asyncio
from datetime import datetime
from homeassistant.components import bluetooth
from homeassistant.exceptions import ConfigEntryNotReady

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
import asyncio
import logging

# Add effects information in a separate file because there is a LOT of boilerplate.
from .effects import (
    EFFECT_MAP,
    EFFECT_LIST,
    EFFECT_CMD,
)

LOGGER = logging.getLogger(__name__)

NAME_ARRAY = ["LEDnetWF"]
WRITE_CHARACTERISTIC_UUIDS = ["0000ff01-0000-1000-8000-00805f9b34fb"]
NOTIFY_CHARACTERISTIC_UUIDS  = ["0000ff02-0000-1000-8000-00805f9b34fb"]
SERVICE_CHARACTERISTICS_UUIDS = ["0000ffff-0000-1000-8000-00805f9b34fb"]
TURN_ON_CMD = [bytearray.fromhex("00 04 80 00 00 0d 0e 0b 3b 23 00 00 00 00 00 00 00 32 00 00 90")]
TURN_OFF_CMD = [bytearray.fromhex("00 5b 80 00 00 0d 0e 0b 3b 24 00 00 00 00 00 00 00 32 00 00 91")]
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
                    LOGGER.debug("%s: %s error calling %s, reach max attempts (%s/%s)",self.name,type(err),func,attempt,max_attempts,exc_info=True,)
                    raise
                LOGGER.debug("%s: %s error calling %s, backing off %ss, retrying (%s/%s)...",self.name,type(err),func,BLEAK_BACKOFF_TIME,attempt,max_attempts,exc_info=True,)
                await asyncio.sleep(BLEAK_BACKOFF_TIME)
            except BLEAK_EXCEPTIONS as err:
                if attempt >= max_attempts:
                    LOGGER.debug("%s: %s error calling %s, reach max attempts (%s/%s): %s",self.name,type(err),func,attempt,max_attempts,err,exc_info=True,)
                    raise
                LOGGER.debug("%s: %s error calling %s, retrying  (%s/%s)...: %s",self.name,type(err),func,attempt,max_attempts,err,exc_info=True,)

    return cast(WrapFuncType, _async_wrap_retry_bluetooth_connection_error)

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
            raise ConfigEntryNotReady(f"You need to add bluetooth integration (https://www.home-assistant.io/integrations/bluetooth) or couldn't find a nearby device with address: {address}")
        self._connect_lock: asyncio.Lock = asyncio.Lock()
        self._client: BleakClientWithServiceCache | None = None
        self._disconnect_timer: asyncio.TimerHandle | None = None
        self._cached_services: BleakGATTServiceCollection | None = None
        self._expected_disconnect = False
        self._is_on = None
        self._hs_color = None
        self._brightness = None
        self._effect = None
        self._effect_speed = None
        self._color_temp_kelvin = None
        self._write_uuid = None
        self._read_uuid = None
        self._turn_on_cmd = None
        self._turn_off_cmd = None
        self._max_color_temp_kelvin = None
        self._min_color_temp_kelvin = None
        self._model = self._detect_model()
        LOGGER.debug('Model information for device %s : ModelNo %s, Turn on cmd %s, Turn off cmd %s', self._device.name, self._model, self._turn_on_cmd, self._turn_off_cmd)

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
        await self._write_while_connected(data)

    async def _write_while_connected(self, data: bytearray):
        LOGGER.debug(''.join(format(x, ' 03x') for x in data))
        await self._client.write_gatt_char(self._write_uuid, data, False)

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
    
    @retry_bluetooth_connection_error
    async def set_color_temp_kelvin(self, value: int, brightness: int):
        # White colours are represented by colour temperature percentage from 0x0 to 0x64 from warm to cool
        # Warm (0x0) is only the warm white LED, cool (0x64) is only the white LED and then a mixture between the two
        self._color_temp_kelvin = value
        if value < self._min_color_temp_kelvin:
            value = self._min_color_temp_kelvin
        if value > self._max_color_temp_kelvin:
            value = self._max_color_temp_kelvin
        color_temp_percent = int(((value - self._min_color_temp_kelvin) * 100) / (self._max_color_temp_kelvin - self._min_color_temp_kelvin))
        if brightness is None:
            if self._brightness is None:
                self._brightness = 255
            brightness = self._brightness
        brightness_percent = int(brightness * 100 / 255) 
        # Color temp packet + brightness
        await self._write(bytearray.fromhex("00 10 80 00 00 0d 0e 0b 3b b1 00 00 00 " + str(hex(color_temp_percent)[2:]).rjust(2, '0') + " " + str(hex(brightness_percent)[2:]).rjust(2, '0') + " 00 00 00 00 00 3d"))
        
    @retry_bluetooth_connection_error
    async def set_hs_color(self, hs: Tuple[int, int], brightness: int):
        # The device expects basic static colour information in HSV format. 
        # The value for the Hue element is divided by two to fit in to a single byte.
        # Saturation and Value are percentages from 0 to 100 (0x64).
        # Value = Brightness
        self._hs_color = hs
        hue = int(hs[0] / 2)
        saturation = int(hs[1])
        if brightness is None:
            if self._brightness is None:
                self._brightness = 255
            brightness = self._brightness
        brightness_percent = int(brightness * 100 / 255)
        # HSV packet
        self._effect = None
        await self._write(bytearray.fromhex("00 05 80 00 00 0d 0e 0b 3b a1 " + str(hex(hue)[2:]).rjust(2, '0') + " " + str(hex(saturation)[2:]).rjust(2, '0') + " " + str(hex(brightness_percent)[2:]).rjust(2, '0') + " 00 00 00 00 00 00 00 3d"))
        
    async def set_brightness_local(self, value: int):
        if value is None:
            return
        if value < 0:
            value = 0
        if value > 255:
            value = 255
        self._brightness = value

    @retry_bluetooth_connection_error
    async def turn_on(self):
        await self._write(self._turn_on_cmd)
        self._is_on = True
                
    @retry_bluetooth_connection_error
    async def turn_off(self):
        await self._write(self._turn_off_cmd)
        self._is_on = False

    @retry_bluetooth_connection_error
    async def set_effect(self, effect: str, brightness: int):
        if effect not in EFFECT_LIST:
            LOGGER.error("Effect %s not supported", effect)
            return
        self._effect = effect
        effect_id = EFFECT_MAP.get(effect)
        effect_packet = EFFECT_CMD
        effect_packet[9] = effect_id
        #effect_packet[10] = self._effect_speed # TODO: Support variable speeds.  For now, hard coded to 50% in the packet declaration
        if self._brightness is None:
            # If no brightness has been set, set it to 100%
            self._brightness = 255
        effect_packet[11] = int(self._brightness * 100 / 255)
        await self._write(effect_packet)

    @retry_bluetooth_connection_error
    async def update(self):
        try:
            await self._ensure_connected()
            if(self._is_on is None):
                self._is_on = False
                self._hs_color = (0, 0)
                self._color_temp_kelvin = 5000
                self._brightness = 255

        except (Exception) as error:
            self._is_on = False
            LOGGER.error("Error getting status: %s", error)
            track = traceback.format_exc()
            LOGGER.debug(track)

    async def _ensure_connected(self) -> None:
        """Ensure connection to device is established."""
        if self._connect_lock.locked():
            LOGGER.debug(
                "%s: Connection already in progress, waiting for it to complete",
                self.name
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

            #Subscribe to notification is needed for LEDnetWF devices to accept commands
            LOGGER.debug("%s: Subscribe to notifications", self.name)
            await client.start_notify(self._read_uuid, self._notification_handler)

    def _notification_handler(self, _sender: int, data: bytearray) -> None:
        """Handle notification responses."""
        LOGGER.debug("%s: Notification received", self.name, data.hex())
        return

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
            LOGGER.debug("%s: Configured disconnect from device in %s seconds", self.name, self._delay)
            self._disconnect_timer = self.loop.call_later(
                self._delay, self._disconnect
            )

    def _disconnected(self, client: BleakClientWithServiceCache) -> None:
        """Disconnected callback."""
        if self._expected_disconnect:
            LOGGER.debug("%s: Disconnected from device", self.name)
            return
        LOGGER.warning("%s: Device unexpectedly disconnected",self.name)

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
                await client.stop_notify(read_char)
                await client.disconnect()
