import logging

import asyncio
from .lednetwf import LEDNETWFInstance
from typing import Any
from bluetooth_data_tools import human_readable_name
from homeassistant import config_entries
from homeassistant.const import CONF_MAC
import voluptuous as vol
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
        #LOGGER.debug("Discovered bluetooth devices, DeviceData, : %s , %s", self._discovery.address, self._discovery.name)
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
        #LOGGER.debug("Discovered bluetooth devices, step bluetooth, : %s , %s", discovery_info.address, discovery_info.name)
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
        #LOGGER.debug(f"In async_step_user.  User input: {user_input}")
        #LOGGER.debug(f"Discovered devices: {self._discovered_devices}")

        if user_input is not None:
            self.mac = user_input[CONF_MAC]
            if self.name is None:
                self.name = human_readable_name(None, self.mac_dict[self.mac], self.mac)
            # if "title_placeholders" in self.context:
            #     self.name = self.context["title_placeholders"]["name"]
            # if 'source' in self.context.keys() and self.context['source'] == "user":
            #     LOGGER.debug(f"User context.  discovered devices: {self._discovered_devices}")
            #     for each in self._discovered_devices:
            #       LOGGER.debug(f"Address: {each.address()}")
            #       if each.address() == self.mac:
            #         self.name = each.get_device_name()
            #if self.name is None: self.name = self.mac
            await self.async_set_unique_id(self.mac, raise_on_progress=False)
            self._abort_if_unique_id_configured()
            return await self.async_step_validate()

        # Find all the current bluetooth devices known and add them to a list if they are
        # supported.  If we already know about them, don't add them to the list.
        current_addresses = self._async_current_ids()
        #LOGGER.debug(f"Current addresses: {current_addresses}")
        #LOGGER.debug(f"async current addresses: {self._async_current_ids()}")

        for discovery_info in async_discovered_service_info(self.hass):
            mac = discovery_info.address
            if mac in current_addresses:
                # Device is already configured in HA, skip it.
                #LOGGER.debug("Device %s in current_addresses", (mac))
                continue
            if (device for device in self._discovered_devices if device.address == mac) == ([]):
                # Device is already in the list of discovered devices, skip it.
                #LOGGER.debug("Device %s in discovered_devices", (device))
                continue
            device = DeviceData(discovery_info)
            #LOGGER.debug(f"Device data: {device}")
            if device.supported():
                self._discovered_devices.append(device)
        self.mac_dict = { dev.address(): dev.name() for dev in self._discovered_devices }
        LOGGER.debug(f"mac dict: {self.mac_dict}")
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
            led_count   = self._instance._led_count
            led_type    = self._instance._chip_type.name
            color_order = self._instance._color_order.name
            model_num   = self._instance._model
            data        = {CONF_MAC: self.mac, CONF_NAME: self.name, CONF_DELAY: 120}
            options     = {CONF_LEDCOUNT: led_count, CONF_LEDTYPE: led_type, CONF_COLORORDER: color_order, CONF_MODEL: model_num}
            # TODO: deal with "none" better from old devices which haven't got config data yet. Also update the function in const to not error on none.
            LOGGER.debug(f"LED Count: {led_count}, LED Type: {led_type}, Color Order: {color_order}")
            LOGGER.debug(f"instance: {self._instance}")
            #LOGGER.debug(f"instance dir: {dir(self._instance)}")
            LOGGER.debug(f"name: {self.name}")
            LOGGER.debug(f"mac: {self.mac}")

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

    # async def async_step_manual(self, user_input: "dict[str, Any] | None" = None):
    #     if user_input is not None:            
    #         self.mac = user_input[CONF_MAC]
    #         self.name = user_input["name"]
    #         await self.async_set_unique_id(format_mac(self.mac))
    #         return await self.async_step_validate()

    #     return self.async_show_form(
    #         step_id="manual", data_schema=vol.Schema(
    #             {
    #                 vol.Required(CONF_MAC): str,
    #                 vol.Required("name"): str
    #             }
    #         ), errors={})

    
    async def toggle_light(self):
        if not self._instance:
            self._instance = LEDNETWFInstance(self.mac, self.hass)
        try:
            await self._instance.update()
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
        LOGGER.debug(f"Options flow handler __init__")
        LOGGER.debug(f"Config entry: {config_entry}")
        LOGGER.debug(f"Config entry dir: {dir(config_entry)}")
        LOGGER.debug(f"Config entry options: {config_entry.options}")
        LOGGER.debug(f"Options flow handler, data: {self._data}")
        LOGGER.debug(f"Options flow handler, config entry: {self._config_entry}")
        LOGGER.debug(f"Options flow handler, config entry dir: {dir(self._config_entry)}")
        LOGGER.debug(f"Options flow handler, config entry options: {self._options}")
    
    async def async_step_init(self, _user_input=None):
        """Manage the options."""
        LOGGER.debug(f"Options flow handler step init")
        return await self.async_step_user()
    
    async def async_step_user(self, user_input=None):
        """Handle a flow initialized by the user."""
        LOGGER.debug(f"Options flow handler step user")
        errors = {}
        #options = self.config_entry.options
        model   = self._options.get("model")
        LOGGER.debug(f"Options flow handler, model: {model}")

        if user_input is not None:
            new_led_count   = user_input.get(CONF_LEDCOUNT)
            new_led_type    = user_input.get(CONF_LEDTYPE)
            new_led_type    = LedTypes_StripLight[new_led_type].value if model == 0x56 else LedTypes_RingLight[new_led_type].value
            new_color_order = user_input.get(CONF_COLORORDER)
            new_color_order = ColorOrdering[new_color_order].value
            new_delay       = user_input.get(CONF_DELAY)
            #entry_id        = self.hass.data[DOMAIN][self.config_entry.entry_id]
            LOGGER.debug(f"Options flow handler")
            LOGGER.debug(f"User input: {user_input}")
            LOGGER.debug(f"Options flow handler, new led count: {new_led_count}, new led type: {new_led_type}, new color order: {new_color_order}, new delay: {new_delay}")
            #LOGGER.debug(f"Options flow handler, device: {entry_id}")
            self._options.update(user_input)
            # return await self._update_options()
            return self.async_create_entry(title=self._config_entry.title, data=self._options)

        LOGGER.debug(f"Options flow handler, there was no user input.  Options: {self._options}")
        
        default_conf_delay = self._options.get(CONF_DELAY, self._data.get(CONF_DELAY, 120))
        ledchiplist = LedTypes_StripLight if model == 0x56 else LedTypes_RingLight # TODO: This should be more dynamic.
        ledchips_options   = [option.name for option in ledchiplist]
        colororder_options = [option.name for option in ColorOrdering]
        # if model == RING_LIGHT_MODEL:
        #     default_ledchip    = LedTypes_RingLight(self._options.get(CONF_LEDTYPE)).name
        # elif model == STRIP_LIGHT_MODEL:
        #     default_ledchip    = LedTypes_StripLight(self._options.get(CONF_LEDTYPE)).name
        #default_colororder = ColorOrdering(self._options.get(CONF_COLORORDER)).name
        LOGGER.debug(f"Options flow handler, default led chips: {self._options.get(CONF_LEDTYPE)}")

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
        # LOGGER.debug(f"Options flow handler, _update_options OPTIONS: {self._options}")
        LOGGER.debug(f"Options flow handler, _update_options, config entry DATA: {self._config_entry}")
        LOGGER.debug(f"Options flow handler, _update_options, self name: {self._config_entry.title}")
        # return self.async_create_entry(title=self._config_entry.title, data=self._data, options=self._options)
        return self.async_create_entry(self._config_entry)
