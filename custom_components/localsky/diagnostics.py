"""Diagnostics support — HA 2024.11+ standard.

Surfaced when an operator clicks "Download diagnostics" on the
integration card. Returns coordinator state + LocalSky /info without
leaking host secrets.
"""
from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .coordinator import LocalSkyCoordinator

TO_REDACT = {"token", "api_key", "ha_token", "vapid_public_key"}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    coordinator: LocalSkyCoordinator = hass.data[DOMAIN][entry.entry_id]
    data = coordinator.data or {}
    irrigation = data.get("irrigation") or {}
    return {
        "entry": {
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": dict(entry.options),
            "title": entry.title,
            "unique_id": entry.unique_id,
        },
        "coordinator": {
            "last_update_success": coordinator.last_update_success,
            "last_update_success_time": (
                coordinator.last_update_success_time.isoformat()
                if coordinator.last_update_success_time
                else None
            ),
            "use_sse": coordinator.use_sse,
            "poll_interval": coordinator.poll_interval,
            "known_zones": sorted(getattr(coordinator, "_known_zones", set())),
        },
        "info": async_redact_data(coordinator.info or {}, TO_REDACT),
        "snapshot_summary": {
            "tempest_keys": sorted((data.get("tempest") or {}).keys()),
            "irrigation_zone_count": len(irrigation.get("zones", [])),
            "irrigation_ha_reachable": irrigation.get("ha_reachable"),
            "irrigation_skip_check": irrigation.get("skip_check"),
            "irrigation_next_run_epoch": irrigation.get("next_run_epoch"),
            "forecast_keys": sorted((data.get("forecast") or {}).keys()),
        },
    }
