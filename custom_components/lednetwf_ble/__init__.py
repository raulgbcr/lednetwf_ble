from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, Event
from homeassistant.const import CONF_MAC, EVENT_HOMEASSISTANT_STOP

from .const import DOMAIN, CONF_RESET, CONF_DELAY, CONF_LEDCOUNT, CONF_LEDTYPE, CONF_COLORORDER
from .lednetwf import LEDNETWFInstance
import logging

LOGGER = logging.getLogger(__name__)
PLATFORMS = ["light"]

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up from a config entry."""
    LOGGER.debug(f"In __init__ async_setup_entry for {entry}")
    config  = entry.data
    options = entry.options

    delay = entry.options.get(CONF_DELAY, None) or entry.data.get(CONF_DELAY, None)
    ledcount = entry.options.get(CONF_LEDCOUNT, None)
    ledtype = entry.options.get(CONF_LEDTYPE, None)
    colororder = entry.options.get(CONF_COLORORDER, None)

    LOGGER.debug("Config Reset data: %s and config delay data: %s", delay)
    LOGGER.debug("Config LED Count data: %s and config LED Type data: %s", ledcount, ledtype)
    LOGGER.debug("Config Color Order data: %s", colororder)

    instance = LEDNETWFInstance(entry.data[CONF_MAC], hass, config, options)
    #await instance.set_led_settings(ledcount, ledtype, colororder)

    # Probably call some method to set ledtype etc here.
    #await instance.set_led_count(ledcount)

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
    LOGGER.debug(f"In __init__ _async_update_listener for {entry.entry_id}")
    instance = hass.data[DOMAIN][entry.entry_id]
    options = entry.options
    LOGGER.debug(f"Options: {options}")
    LOGGER.debug(f"instance: {instance}")
    await hass.config_entries.async_reload(entry.entry_id)
    
    # if entry.title != instance.name:
    #     await hass.config_entries.async_reload(entry.entry_id)
