"""LocalSky HA integration.

Pattern: one config entry per LocalSky deployment. The coordinator
multiplexes SSE streams (irrigation + tempest) + a slow forecast poll
into a single data dict consumed by the platforms below.
"""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import CONF_HOST, CONF_PORT, CONF_USE_HTTPS, DEFAULT_PORT, DOMAIN
from .coordinator import LocalSkyCoordinator
from .services import async_register_services, async_unregister_services
from .util import format_base_url

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.NUMBER,
    Platform.SENSOR,
    Platform.SWITCH,
    Platform.VALVE,
    Platform.WEATHER,
]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up LocalSky from a config entry."""
    host: str = entry.data[CONF_HOST]
    port: int = entry.data.get(CONF_PORT, DEFAULT_PORT)
    use_https: bool = entry.data.get(CONF_USE_HTTPS, False)
    base_url = format_base_url(host, port, use_https)

    session = async_get_clientsession(hass)
    coordinator = LocalSkyCoordinator(hass, entry, session, base_url)

    try:
        await coordinator.fetch_info()
    except Exception as err:  # noqa: BLE001 - aiohttp + timeouts + json parse
        raise ConfigEntryNotReady(f"Cannot reach LocalSky at {base_url}: {err}") from err

    await coordinator.async_start()

    domain_data = hass.data.setdefault(DOMAIN, {})
    domain_data[entry.entry_id] = coordinator

    # Register integration-level services once, on the first entry setup.
    if len(domain_data) == 1:
        async_register_services(hass)

    # Reload entry when options change so the coordinator picks up new
    # SSE/poll preferences without a manual restart.
    entry.async_on_unload(entry.add_update_listener(_async_reload_on_options))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        domain_data = hass.data.get(DOMAIN, {})
        coordinator: LocalSkyCoordinator | None = domain_data.pop(entry.entry_id, None)
        if coordinator is not None:
            await coordinator.async_stop()
        if not domain_data:
            async_unregister_services(hass)
    return unload_ok


async def _async_reload_on_options(
    hass: HomeAssistant, entry: ConfigEntry
) -> None:
    await hass.config_entries.async_reload(entry.entry_id)
