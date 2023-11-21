import logging
import voluptuous as vol
from typing import Any, Optional, Tuple

from .lednetwf import LEDNETWFInstance
from .const import DOMAIN

from homeassistant.const import CONF_MAC
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.components.light import (
    PLATFORM_SCHEMA,
    ATTR_BRIGHTNESS,
    ATTR_COLOR_TEMP_KELVIN,
    ATTR_MIN_COLOR_TEMP_KELVIN,
    ATTR_MAX_COLOR_TEMP_KELVIN,
    ATTR_EFFECT,
    ATTR_HS_COLOR,
    ColorMode,
    LightEntity,
    LightEntityFeature,
)
from homeassistant.util.color import (match_max_scale)
from homeassistant.helpers import device_registry

PARALLEL_UPDATES = 0

LOGGER = logging.getLogger(__name__)
PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend({
    vol.Required(CONF_MAC): cv.string
})

async def async_setup_entry(hass, config_entry, async_add_devices):
    instance = hass.data[DOMAIN][config_entry.entry_id]
    await instance.update()
    async_add_devices([LEDNETWFLight(instance, config_entry.data["name"], config_entry.entry_id)])
    # config_entry.async_on_unload(
    #     await instance.stop()
    # )

class LEDNETWFLight(LightEntity):
    def __init__(self, lednetwfinstance: LEDNETWFInstance, name: str, entry_id: str) -> None:
        self._instance = lednetwfinstance
        self._entry_id = entry_id
        self._attr_supported_color_modes = {ColorMode.COLOR_TEMP, ColorMode.HS}
        self._attr_supported_features = LightEntityFeature.EFFECT
        self._color_mode = ColorMode.COLOR_TEMP
        self._attr_name = name
        self._effect = None
        self._attr_unique_id = self._instance.mac

    @property
    def available(self):
        return self._instance.is_on != None

    @property
    def brightness(self):
        if self._instance.brightness:
            return self._instance.brightness
        return 255

    @property
    def is_on(self) -> Optional[bool]:
        return self._instance.is_on

    @property
    def max_mireds(self):
        return 100

    @property
    def min_mireds(self):
        return 1

    @property
    def color_temp_kelvin(self):
        return self._instance.color_temp_kelvin
    
    @property
    def max_color_temp_kelvin(self):
        return self._instance.max_color_temp_kelvin

    @property
    def min_color_temp_kelvin(self):
        return self._instance.min_color_temp_kelvin

    @property
    def effect_list(self):
        return self._instance.effect_list

    @property
    def effect(self):
        return self._effect

    @property
    def supported_features(self) -> int:
        """Flag supported features."""
        return self._attr_supported_features

    @property
    def supported_color_modes(self) -> int:
        """Flag supported color modes."""
        return self._attr_supported_color_modes

    @property
    def hs_color(self):
        if self._instance.hs_color:
            return self._instance.hs_color
        return None

    @property
    def color_mode(self):
        """Return the color mode of the light."""
        return self._color_mode
        
    @property
    def device_info(self):
        """Return device info."""
        return DeviceInfo(
            identifiers={
                # Serial numbers are unique identifiers within a specific domain
                (DOMAIN, self._instance.mac)
            },
            name=self.name,
            connections={(device_registry.CONNECTION_NETWORK_MAC, self._instance.mac)}
        )

    @property
    def should_poll(self):
        return False

    async def async_turn_on(self, **kwargs: Any) -> None:
        LOGGER.debug("kwargs: %s", kwargs)
        LOGGER.debug("self._color_mode: %s", self._color_mode)
        LOGGER.debug("self._effect: %s", self._effect)
        if not self.is_on:
            await self._instance.turn_on()
        
        if ATTR_BRIGHTNESS in kwargs and kwargs[ATTR_BRIGHTNESS] != self.brightness:
            self._brightness = kwargs[ATTR_BRIGHTNESS]
            await self._instance.set_brightness_local(kwargs[ATTR_BRIGHTNESS])
            # Call rgb or temp color functions in order to update the brightness (same packet)
            if self._color_mode is ColorMode.COLOR_TEMP and ATTR_COLOR_TEMP_KELVIN not in kwargs:
                await self._instance.set_color_temp_kelvin(self._instance.color_temp_kelvin, self._brightness)
            elif self._color_mode is ColorMode.HS and ATTR_HS_COLOR not in kwargs:
                await self._instance.set_hs_color(self._instance.hs_color, self._brightness)
            elif self._effect is not None and ATTR_EFFECT not in kwargs:
                await self._instance.set_effect(self._effect, self._brightness)
        
        if ATTR_COLOR_TEMP_KELVIN in kwargs:
            self._color_mode = ColorMode.COLOR_TEMP
            if kwargs[ATTR_COLOR_TEMP_KELVIN] != self.color_temp:
                self._effect = None
                await self._instance.set_color_temp_kelvin(kwargs[ATTR_COLOR_TEMP_KELVIN], self.brightness)
        elif ATTR_HS_COLOR in kwargs:
            self._color_mode = ColorMode.HS
            if kwargs[ATTR_HS_COLOR] != self.color_temp:
                self._effect = None
                await self._instance.set_hs_color(kwargs[ATTR_HS_COLOR], self.brightness)
        elif ATTR_EFFECT in kwargs:
            if kwargs[ATTR_EFFECT] != self.effect:
                self._effect = kwargs[ATTR_EFFECT]
                await self._instance.set_effect(kwargs[ATTR_EFFECT], self.brightness)

        self.async_write_ha_state()
        
    async def async_turn_off(self, **kwargs: Any) -> None:
        # Fix for turn of circle effect of HSV MODE(controller skips turn off animation if state is not changed since last turn on)
        if self._instance.brightness == 100:
            temp_brightness = 99
        else:
            temp_brightness = self._instance.brightness + 1
        if self._color_mode is ColorMode.HS and ATTR_HS_COLOR not in kwargs:
            await self._instance.set_hs_color(self._instance.hs_color, temp_brightness)
        
        # Actual turn off
        await self._instance.turn_off()
        self.async_write_ha_state()

    async def async_update(self) -> None:
        await self._instance.update()
        self.async_write_ha_state()

    async def async_set_effect(self, effect: str) -> None:
        self._effect = effect
        await self._instance.set_effect(effect, None)
        self.async_write_ha_state()
