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
    CONF_RESET,
    CONF_DELAY,
    CONF_LEDCOUNT,
    CONF_LEDTYPE,
    CONF_COLORORDER
)

LOGGER = logging.getLogger(__name__)
#DATA_SCHEMA = vol.Schema({("host"): str})
#MANUAL_MAC = "manual"

class DeviceData(BluetoothData):
    def __init__(self, discovery_info) -> None:
        self._discovery = discovery_info
        LOGGER.debug("Discovered bluetooth devices, DeviceData, : %s , %s", self._discovery.address, self._discovery.name)
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
    def _start_update(self, service_info: BluetoothServiceInfo) -> None:
        """Update from BLE advertisement data."""
        LOGGER.debug("Parsing BLE advertisement data: %s", service_info)
        
@config_entries.HANDLERS.register(DOMAIN)        
class LEDNETWFFlowHandler(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1
    CONNECTION_CLASS = config_entries.CONN_CLASS_LOCAL_POLL

    def __init__(self) -> None:
        self.mac = None
        #self._device = None
        self._instance = None
        self.name = None
        #self._discovery_info: BluetoothServiceInfoBleak | None = None
        self._discovered_device: DeviceData | None = None
        self._discovered_devices = []

    async def async_step_bluetooth(self, discovery_info: BluetoothServiceInfoBleak) -> FlowResult:
        """Handle the bluetooth discovery step."""
        LOGGER.debug("Discovered bluetooth devices, step bluetooth, : %s , %s", discovery_info.address, discovery_info.name)
        await self.async_set_unique_id(discovery_info.address)
        self._abort_if_unique_id_configured()
        device = DeviceData(discovery_info)
        self.device_data = DeviceData(discovery_info)
        self.mac = self.device_data.address()
        self.context["title_placeholders"] = {"name": human_readable_name(None, device.name(), device.address())}
        if self.device_data.supported():
            self._discovered_devices.append(self.device_data)
            return await self.async_step_bluetooth_confirm()
        else:
            return self.async_abort(reason="not_supported")

    async def async_step_bluetooth_confirm(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Confirm discovery."""
        LOGGER.debug("Discovered bluetooth devices, step bluetooth confirm, : %s", user_input)
        self._set_confirm_only()
        return await self.async_step_user()
    
    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Handle the user step to pick discovered device."""
        if user_input is not None:
            #self.mac = user_input[CONF_MAC]
            # if "title_placeholders" in self.context:
            #     self.name = self.context["title_placeholders"]["name"]
            # if 'source' in self.context.keys() and self.context['source'] == "user":
            #     LOGGER.debug(f"User context.  discovered devices: {self._discovered_devices}")
            #     for each in self._discovered_devices:
            #       LOGGER.debug(f"Address: {each.address()}")
            #       if each.address() == self.mac:
            #         self.name = each.get_device_name()
            #if self.name is None: self.name = self.mac
            #await self.async_set_unique_id(self.mac, raise_on_progress=False)
            #self._abort_if_unique_id_configured()
            
            return await self.async_step_validate()

        # current_addresses = self._async_current_ids()
        # for discovery_info in async_discovered_service_info(self.hass):
        #     self.mac = discovery_info.address
        #     if self.mac in current_addresses:
        #         LOGGER.debug("Device %s in current_addresses", (self.mac))
        #         continue
        #     if (device for device in self._discovered_devices if device.address == self.mac) == ([]):
        #         LOGGER.debug("Device %s in discovered_devices", (device))
        #         continue
        #     device = DeviceData(discovery_info)
        #     if device.supported():
        #         self._discovered_devices.append(device)
        
        # if not self._discovered_devices:
        #     return await self.async_step_manual()

        # LOGGER.debug("Discovered supported devices: %s - %s", self._discovered_devices[0].name(), self._discovered_devices[0].address())

        mac_dict = { dev.address(): dev.name() for dev in self._discovered_devices }
#        mac_dict[MANUAL_MAC] = "Manually add a MAC address"
        return self.async_show_form(
            step_id="user", data_schema=vol.Schema(
                {
                    vol.Required(CONF_MAC): vol.In(mac_dict),
#                    vol.Required("name"): str
                }
            ),
            errors={})

    async def async_step_validate(self, user_input: "dict[str, Any] | None" = None):
        if user_input is not None:
            LOGGER.debug(f"async step validarte with User input: {user_input}")
            led_count = self._instance._led_count
            led_type = self._instance._chip_type
            color_order = self._instance._color_order
            options = {CONF_LEDCOUNT: led_count, CONF_LEDTYPE: led_type, CONF_COLORORDER: color_order}
            LOGGER.debug(f"LED Count: {led_count}, LED Type: {led_type}, Color Order: {color_order}")
            if "flicker" in user_input:
                if user_input["flicker"]:
                    #return self.async_create_entry(title=self.device_data.human_readable_name(), data={CONF_MAC: self.mac, "name": self.device_data.human_readable_name(), "user_input":user_input})
                    return self.async_create_entry(title=self.device_data.human_readable_name(), data={CONF_MAC: self.mac, "name": self.device_data.human_readable_name()}, options=options)
                    #return self.async_create_entry(title=DOMAIN, data=user_input)
                return self.async_abort(reason="cannot_validate")
            if "retry" in user_input and not user_input["retry"]:
                return self.async_abort(reason="cannot_connect")

        error = await self.toggle_light()
        LOGGER.debug(f"Error: {error}")
        if error:
            return self.async_show_form(
                step_id="validate", data_schema=vol.Schema(
                    {
                        vol.Required("retry"): bool
                    }
                ), errors={"base": "connect"})
        
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
    
    # async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None):
    #     """Handle reconfiguration."""
    #     LOGGER.debug(f"Inside reconfigure flow.  Data: {user_input}")
    #     if user_input is not None:
    #         LOGGER.debug(f"Inside reconfigure flow. Was passed data: {user_input}")
    #         return
    #     return self.async_show_form(
    #         step_id="reconfigure", data_schema=vol.Schema(
    #             {
    #                 vol.Optional("ledcount"): int,
    #                 vol.Optional("ledtype"): int,
    #                 vol.Optional("colororder"): int,
    #             }
    #         ), errors={})
    
    async def toggle_light(self):
        if not self._instance:
            self._instance = LEDNETWFInstance(self.mac, False, 120, self.hass)

        try:
            await self._instance.update()
            for n in range(3):
                await self._instance.turn_on()
                await asyncio.sleep(1)
                await self._instance.turn_off()
                await asyncio.sleep(1)
            # led_config = {"num_leds":self._instance.led_count, "led_type":self._instance.led_type, "color_order":self._instance.color_order}
            # LOGGER.debug(f"CF: Config: {led_config}")
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
        LOGGER.debug(f"Options flow handler __init__")
        LOGGER.debug(f"Config entry: {config_entry}")
        self.config_entry = config_entry
        LOGGER.debug(f"Options flow handler, config entry: {config_entry}")
        LOGGER.debug(f"Options flow handler, config entry dir: {dir(config_entry)}")
        LOGGER.debug(f"Options flow handler, config entry options: {config_entry.options}")
        self.options = dict(config_entry.options)# if config_entry.options is not None else {}

    async def async_step_init(self, _user_input=None):
        """Manage the options."""
        LOGGER.debug(f"Options flow handler step init")
        return await self.async_step_user()
    
    async def async_step_user(self, user_input=None):
        """Handle a flow initialized by the user."""
        LOGGER.debug(f"Options flow handler step user")
        ledcount = self.config_entry.options.get(CONF_LEDCOUNT)# if self.config_entry.options else 0
        ledtype = self.config_entry.options.get(CONF_LEDTYPE)
        colororder = self.config_entry.options.get(CONF_COLORORDER)
        errors = {}
        options = self.config_entry.options# or {CONF_RESET: False,CONF_DELAY: 120, CONF_LEDCOUNT: 0, CONF_LEDTYPE: 0, CONF_COLORORDER: 0}

        if user_input is not None:
            LOGGER.debug(f"Options flow handler")
            LOGGER.debug(f"User input: {user_input}")
            new_led_count = user_input.get(CONF_LEDCOUNT)
            new_led_type = user_input.get(CONF_LEDTYPE)
            new_color_order = user_input.get(CONF_COLORORDER)
            new_delay = user_input.get(CONF_DELAY)
            LOGGER.debug(f"Options flow handler, new led count: {new_led_count}, new led type: {new_led_type}, new color order: {new_color_order}, new delay: {new_delay}")
            device = self.hass.data[DOMAIN][self.config_entry.entry_id]
            LOGGER.debug(f"Options flow handler, device: {device}")
            self.options.update(user_input)
            return await self._update_options()
        
            # return self.async_create_entry(title="", data={CONF_RESET: False, CONF_DELAY: user_input[CONF_DELAY]},
            #                                options={CONF_LEDCOUNT: user_input[CONF_LEDCOUNT], CONF_LEDTYPE: user_input[CONF_LEDTYPE],
            #                                         CONF_COLORORDER: user_input[CONF_COLORORDER]})
            # return self.async_create_entry(title="", data={CONF_RESET: False, CONF_DELAY: user_input[CONF_DELAY],
            #                                                CONF_LEDCOUNT: user_input[CONF_LEDCOUNT], CONF_LEDTYPE: user_input[CONF_LEDTYPE],
            #                                                CONF_COLORORDER: user_input[CONF_COLORORDER]})        
        LOGGER.debug(f"Options flow handler, there was no user input.  Options: {options}")
        data_schema = vol.Schema(
            {
                vol.Optional(CONF_DELAY,
                             default=self.config_entry.options.get(
                                 CONF_DELAY, self.config_entry.data.get(CONF_DELAY, 120))
                            ): int,
                vol.Optional(CONF_LEDCOUNT, default=options.get(CONF_LEDCOUNT)): int,
                vol.Optional(CONF_LEDTYPE, default=options.get(CONF_LEDTYPE)): int,
                vol.Optional(CONF_COLORORDER, default=options.get(CONF_COLORORDER)): int,
            }
        )
        return self.async_show_form(
            step_id="user",
            data_schema=data_schema,
            errors=errors
        )
            
            
    async def _update_options(self):
        """Update config entry options."""
        LOGGER.debug(f"Options flow handler, _update_options: {self.options}")
        return self.async_create_entry(title=DOMAIN, data=self.options)

        #     vol.Schema(
        #         {
        #             vol.Optional(CONF_DELAY, default=options.get(CONF_DELAY)): int,
        #             vol.Optional(CONF_LEDCOUNT, default=0): int,
        #             vol.Optional(CONF_LEDTYPE, default=0): int,
        #             vol.Optional(CONF_COLORORDER, default=0): int,
        #         }
        #     ), errors=errors
        # )
