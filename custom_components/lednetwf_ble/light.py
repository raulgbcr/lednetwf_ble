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
    # ATTR_BRIGHTNESS_STEP_PCT,
    ATTR_COLOR_TEMP_KELVIN,
    # ATTR_MIN_COLOR_TEMP_KELVIN,
    # ATTR_MAX_COLOR_TEMP_KELVIN,
    ATTR_EFFECT,
    EFFECT_OFF,
    ATTR_HS_COLOR,
    # ATTR_FLASH,
    # FLASH_SHORT,
    # FLASH_LONG,
    ColorMode,
    LightEntity,
    LightEntityFeature,
)
from homeassistant.util.color import match_max_scale
from homeassistant.helpers import device_registry

LOGGER = logging.getLogger(__name__)
PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend({vol.Required(CONF_MAC): cv.string})


async def async_setup_entry(hass, config_entry, async_add_devices):
    instance = hass.data[DOMAIN][config_entry.entry_id]
    await instance.update()
    async_add_devices(
        [LEDNETWFLight(instance, config_entry.data["name"], config_entry.entry_id)]
    )
    #config_entry.async_on_unload(await instance.stop())


class LEDNETWFLight(LightEntity):
    def __init__(
        self, lednetwfinstance: LEDNETWFInstance, name: str, entry_id: str
    ) -> None:
        self._instance = lednetwfinstance
        self._entry_id = entry_id
        self._attr_supported_color_modes = {ColorMode.BRIGHTNESS, ColorMode.COLOR_TEMP, ColorMode.HS}
        self._attr_supported_features = LightEntityFeature.EFFECT
        self._attr_brightness_step_pct = 10
        self._attr_name = name
        self._attr_unique_id = self._instance.mac
        self._color_temp_kelvin: self._instance._color_temp_kelvin
        self._instance.local_callback = self.light_local_callback
        
    @property
    def available(self):
        return self._instance.is_on != None

    @property
    def brightness(self):
        return self._instance.brightness
    @property
    def brightness_step_pct(self):
        return self._attr_brightness_step_pct
    
    @property
    def is_on(self) -> Optional[bool]:
        return self._instance.is_on

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
        return self._instance._effect

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
        """Return the hs color value."""
        return self._instance.hs_color

    @property
    def color_mode(self):
        """Return the color mode of the light."""
        return self._instance._color_mode

    @property
    def device_info(self):
        """Return device info."""
        return DeviceInfo(
            identifiers={
                # Serial numbers are unique identifiers within a specific domain
                (DOMAIN, self._instance.mac)
            },
            name=self.name,
            connections={(device_registry.CONNECTION_NETWORK_MAC, self._instance.mac)},
        )

    @property
    def should_poll(self):
        return False

    async def async_turn_on(self, **kwargs: Any) -> None:
        LOGGER.debug("async_turn_on called")
        LOGGER.debug("kwargs: %s", kwargs)

        if not self.is_on:
            await self._instance.turn_on()

        on_brightness = kwargs.get(ATTR_BRIGHTNESS)
        if on_brightness is None and self._instance.brightness is not None:
            on_brightness = self._instance.brightness
        elif on_brightness is None and self._instance.brightness is None:
            on_brightness = 255


        if ATTR_COLOR_TEMP_KELVIN not in kwargs and ATTR_HS_COLOR not in kwargs and ATTR_EFFECT not in kwargs:
            # i.e. only a brightness change
            if self._instance._effect is not None and self._instance._effect is not EFFECT_OFF:
                # Before HA 2024.2

                # effect check go first because of the way HA handles brightness changes
                # if there is no color mode set, the brightness slider in the UI gets disabled
                # so we bodge it by keep the color mode set while adjusting the effect brightness.

                #HA 2024.2 changes this, setting color mode to "brightness" should allow to change effects brightness as well as introduces the predefined EFFECT_OFF status
                kwargs[ATTR_EFFECT] = self._instance.effect
            elif self._instance._color_mode is ColorMode.COLOR_TEMP:
                kwargs[ATTR_COLOR_TEMP_KELVIN] = self._instance.color_temp_kelvin
            elif self._instance._color_mode is ColorMode.HS:
                kwargs[ATTR_HS_COLOR] = self._instance.hs_color

        if ATTR_COLOR_TEMP_KELVIN in kwargs:
            self._instance._color_mode = ColorMode.COLOR_TEMP
            self._instance._effect = None
            await self._instance.set_color_temp_kelvin(kwargs[ATTR_COLOR_TEMP_KELVIN], on_brightness)
        elif ATTR_HS_COLOR in kwargs:
            await self._instance.set_hs_color(kwargs[ATTR_HS_COLOR], on_brightness)
        elif ATTR_EFFECT in kwargs:
            await self._instance.set_effect(kwargs[ATTR_EFFECT], on_brightness)

        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        # Fix for turn of circle effect of HSV MODE(controller skips turn off animation if state is not changed since last turn on)
        if self._instance.brightness == 255:
            temp_brightness = 254
        else:
            temp_brightness = self._instance.brightness + 1
        if self._instance._color_mode is ColorMode.HS and ATTR_HS_COLOR not in kwargs:
            await self._instance.set_hs_color(self._instance.hs_color, temp_brightness)

        # Actual turn off
        await self._instance.turn_off()
        self.async_write_ha_state()

    async def async_update(self) -> None:
        LOGGER.debug("async update called")
        await self._instance.update()
        self.async_write_ha_state()
    
    def light_local_callback(self):
        self.async_write_ha_state()

    def update_ha_state(self) -> None:
        LOGGER.debug("update_ha_state called")
        if self.hs_color is None and self.color_temp_kelvin is None:
            self._color_mode = ColorMode.BRIGHTNESS #2024.2 We can use brightness color mode so even when we don't know the state of the light the brightness can be controlled 
        elif self.hs_color is not None:
            self._color_mode = ColorMode.HS
        elif self.color_temp_kelvin is not None:
            self._color_mode = ColorMode.COLOR_TEMP
        self.available = self._instance.is_on != None
        self.async_write_ha_state()
