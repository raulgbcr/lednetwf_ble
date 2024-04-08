from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, Event
from homeassistant.const import CONF_MAC, EVENT_HOMEASSISTANT_STOP
from homeassistant.const import Platform


from .const import DOMAIN, CONF_RESET, CONF_DELAY, CONF_LEDCOUNT, CONF_LEDTYPE, CONF_COLORORDER
from .lednetwf import LEDNETWFInstance
import logging

LOGGER = logging.getLogger(__name__)
PLATFORMS: list[Platform] = [
    Platform.LIGHT,
    Platform.NUMBER
]

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up from a config entry."""
    config     = entry.data
    options    = entry.options
    delay      = entry.options.get(CONF_DELAY, None) or entry.data.get(CONF_DELAY, None)
    ledcount   = entry.options.get(CONF_LEDCOUNT, None)
    ledtype    = entry.options.get(CONF_LEDTYPE, None)
    colororder = entry.options.get(CONF_COLORORDER, None)
    instance   = LEDNETWFInstance(entry.data[CONF_MAC], hass, config, options)
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = instance

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    async def _async_stop(event: Event) -> None:
        """Close the connection."""
        await instance.stop()

    entry.async_on_unload(
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _async_stop)
    )
    return True

async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        instance = hass.data[DOMAIN][entry.entry_id]
        await instance.stop()
    hass.data[DOMAIN].pop(entry.entry_id)
    return unload_ok

async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle options update."""
    instance = hass.data[DOMAIN][entry.entry_id]
    await instance.set_led_settings(entry.options)
    await hass.config_entries.async_reload(entry.entry_id)
