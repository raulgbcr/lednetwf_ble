import logging
import asyncio
import voluptuous as vol
from .lednetwf import LEDNETWFInstance
from typing import Any
from bluetooth_data_tools import human_readable_name
from homeassistant import config_entries
from homeassistant.const import CONF_MAC
from homeassistant.helpers.device_registry import format_mac
from homeassistant.data_entry_flow import FlowResult
from homeassistant.core import callback
from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_discovered_service_info,
)
import homeassistant.helpers.config_validation as cv
from bluetooth_sensor_state_data import BluetoothData
from home_assistant_bluetooth import BluetoothServiceInfo

from .const import (
    DOMAIN,
    CONF_NAME,
    CONF_DELAY,
    CONF_LEDCOUNT,
    CONF_LEDTYPE,
    CONF_COLORORDER,
    CONF_MODEL,
    RING_LIGHT_MODEL,
    STRIP_LIGHT_MODEL,
    LedTypes_StripLight,
    LedTypes_RingLight,
    ColorOrdering
)

LOGGER = logging.getLogger(__name__)

class DeviceData(BluetoothData):
    def __init__(self, discovery_info) -> None:
        self._discovery = discovery_info
    def supported(self):
        return self._discovery.name.lower().startswith("lednetwf")
    def address(self):
        return self._discovery.address
    def get_device_name(self):
        return self._discovery.name
    def name(self):
        return self._discovery.name
    def human_readable_name(self):
        return human_readable_name(None, self._discovery.name, self._discovery.address)
    def rssi(self):
        return self._discovery.rssi
    def model(self):
        return self._discovery.model
    def _start_update(self, service_info: BluetoothServiceInfo) -> None:
        """Update from BLE advertisement data."""
        LOGGER.debug("Parsing BLE advertisement data: %s", service_info)
        
@config_entries.HANDLERS.register(DOMAIN)        
class LEDNETWFFlowHandler(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1
    CONNECTION_CLASS = config_entries.CONN_CLASS_LOCAL_POLL

    def __init__(self) -> None:
        self.mac = None
        self._instance = None
        self.name = None
        self._discovered_device: DeviceData | None = None
        self._discovered_devices = []

    async def async_step_bluetooth(self, discovery_info: BluetoothServiceInfoBleak) -> FlowResult:
        """Handle the bluetooth discovery step."""
        await self.async_set_unique_id(discovery_info.address)
        self._abort_if_unique_id_configured()
        self.device_data = DeviceData(discovery_info)
        self.mac = self.device_data.address()
        self.name = human_readable_name(None, self.device_data.name(), self.mac)
        self.context["title_placeholders"] = {"name": self.name}
        if self.device_data.supported():
            self._discovered_devices.append(self.device_data)
            return await self.async_step_bluetooth_confirm()
        else:
            return self.async_abort(reason="not_supported")

    async def async_step_bluetooth_confirm(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Confirm discovery."""
        #LOGGER.debug("Discovered bluetooth devices, step bluetooth confirm, : %s", user_input)
        self._set_confirm_only()
        return await self.async_step_user()
    
    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Handle the user step to pick discovered device.  All we care about here is getting the MAC of the device we want to connect to."""
        if user_input is not None:
            self.mac = user_input[CONF_MAC]
            if self.name is None:
                self.name = human_readable_name(None, self.mac_dict[self.mac], self.mac)
            await self.async_set_unique_id(self.mac, raise_on_progress=False)
            self._abort_if_unique_id_configured()
            return await self.async_step_validate()
        current_addresses = self._async_current_ids()

        for discovery_info in async_discovered_service_info(self.hass):
            mac = discovery_info.address
            if mac in current_addresses:
                # Device is already configured in HA, skip it.
                continue
            if (device for device in self._discovered_devices if device.address == mac) == ([]):
                # Device is already in the list of discovered devices, skip it.
                continue
            device = DeviceData(discovery_info)
            if device.supported():
                self._discovered_devices.append(device)
        self.mac_dict = { dev.address(): dev.name() for dev in self._discovered_devices }
        if len(self.mac_dict) == 0:
            return self.async_abort(reason="no_devices_found")
        
        return self.async_show_form(
            step_id="user", data_schema=vol.Schema(
                {
                    vol.Required(CONF_MAC): vol.In(self.mac_dict),
                }
            ),
            errors={})

    async def async_step_validate(self, user_input: "dict[str, Any] | None" = None):
        if user_input is not None:
            LOGGER.debug(f"async step validate with User input: {user_input}")
            led_count   = getattr(self._instance, '_led_count', 64) #May be Unsafe, leave blank ?
            led_type    = getattr(self._instance._chip_type, 'name', "Unknown")
            color_order = getattr(self._instance._color_order, 'name ', "RGB")
            model_num   = getattr(self._instance, '_model', 0x53) #May be unsafe, leave blank ?
            data        = {CONF_MAC: self.mac, CONF_NAME: self.name, CONF_DELAY: 120}
            options     = {CONF_LEDCOUNT: led_count, CONF_LEDTYPE: led_type, CONF_COLORORDER: color_order, CONF_MODEL: model_num}

            # TODO: deal with "none" better from old devices which haven't got config data yet. Also update the function in const to not error on none.

            if "flicker" in user_input:
                if user_input["flicker"]:
                    return self.async_create_entry(title=self.name, data=data, options=options)
                return self.async_abort(reason="cannot_validate")
            if "retry" in user_input and not user_input["retry"]:
                return self.async_abort(reason="cannot_connect")
        else:
            # We haven't yet been provided user input, so we need to connect to the device and check it's working.
            error = await self.toggle_light()
            if error:
                return self.async_show_form(
                    step_id="validate", data_schema=vol.Schema(
                        {
                            vol.Required("retry"): bool
                        }
                    ), errors={"base": "connect"})
            else:
                return self.async_show_form(
                    step_id="validate", data_schema=vol.Schema(
                        {
                            vol.Required("flicker"): bool
                        }
                    ), errors={})

    async def toggle_light(self):
        if not self._instance:
            self._instance = LEDNETWFInstance(self.mac, self.hass)
        try:
            await self._instance.update()
            await self._instance.send_initial_packets()
            for n in range(3):
                await self._instance.turn_on()
                await asyncio.sleep(1)
                await self._instance.turn_off()
                await asyncio.sleep(1)
        except (Exception) as error:
            return error
        finally:
            await self._instance.stop()

    @staticmethod
    @callback
    def async_get_options_flow(entry: config_entries.ConfigEntry):
        return OptionsFlowHandler(entry)

class OptionsFlowHandler(config_entries.OptionsFlow):
    VERSION = 1

    def __init__(self, config_entry):
        """Initialize options flow."""
        self._data = config_entry.data
        self._config_entry = config_entry
        self._options = dict(config_entry.options)
    
    async def async_step_init(self, _user_input=None):
        """Manage the options."""
        LOGGER.debug(f"Options flow handler step init")
        return await self.async_step_user()
    
    async def async_step_user(self, user_input=None):
        """Handle a flow initialized by the user."""
        errors = {}
        model   = self._options.get("model")

        if user_input is not None:
            new_led_type    = user_input.get(CONF_LEDTYPE)
            new_led_type    = LedTypes_StripLight[new_led_type].value if model == 0x56 else LedTypes_RingLight[new_led_type].value
            new_color_order = user_input.get(CONF_COLORORDER)
            new_color_order = ColorOrdering[new_color_order].value
            self._options.update(user_input)
            return self.async_create_entry(title=self._config_entry.title, data=self._options)
        
        default_conf_delay = self._options.get(CONF_DELAY, self._data.get(CONF_DELAY, 120))
        ledchiplist        = LedTypes_StripLight if model == 0x56 else LedTypes_RingLight
        ledchips_options   = [option.name for option in ledchiplist]
        colororder_options = [option.name for option in ColorOrdering]

        data_schema = vol.Schema(
            {
                vol.Optional(CONF_DELAY,      default=default_conf_delay): int,
                vol.Optional(CONF_LEDCOUNT,   default=self._options.get(CONF_LEDCOUNT)):   cv.positive_int,
                vol.Optional(CONF_LEDTYPE,    default=self._options.get(CONF_LEDTYPE)):    vol.In(ledchips_options),
                vol.Optional(CONF_COLORORDER, default=self._options.get(CONF_COLORORDER)): vol.In(colororder_options),
            }
        )
        return self.async_show_form(
            step_id="user",
            data_schema=data_schema,
            errors=errors
        )

    async def _update_options(self):
        """Update config entry options."""
        return self.async_create_entry(self._config_entry)
    