"""Per-zone switch — back-compat shim.

The canonical entity for LocalSky irrigation zones in v0.3.0+ is the
``valve.<zone>`` produced by ``valve.py``. This switch is preserved so
existing automations that reference ``switch.<zone>_run`` keep working
during migration; both call the same underlying coordinator action.

New installs should prefer the valve entity.
"""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    ACTION_RUN,
    ACTION_STOP,
    DEFAULT_RUN_SECONDS,
    DOMAIN,
    OPT_DEFAULT_RUN_SECONDS,
)
from .coordinator import LocalSkyCoordinator
from .util import device_info_for

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: LocalSkyCoordinator = entry.runtime_data
    seen: set[str] = set()

    @callback
    def _on_zones(slugs: set[str]) -> None:
        new = slugs - seen
        if not new:
            return
        irrigation = (coordinator.data or {}).get("irrigation") or {}
        zone_by_slug = {z["slug"]: z for z in irrigation.get("zones", []) if z.get("slug")}
        async_add_entities(
            [
                LocalSkyZoneSwitch(
                    coordinator,
                    entry,
                    slug,
                    zone_by_slug.get(slug, {}).get("name") or slug,
                )
                for slug in sorted(new)
            ]
        )
        seen.update(new)

    entry.async_on_unload(coordinator.add_zone_listener(_on_zones))


class LocalSkyZoneSwitch(CoordinatorEntity[LocalSkyCoordinator], SwitchEntity):
    """on = run zone; off = stop zone. Back-compat with v0.1/v0.2."""

    _attr_has_entity_name = True
    _attr_entity_registry_enabled_default = False  # valve is canonical; keep this off by default

    def __init__(
        self,
        coordinator: LocalSkyCoordinator,
        entry: ConfigEntry,
        slug: str,
        zone_name: str,
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._slug = slug
        self._attr_unique_id = f"{entry.entry_id}_{slug}_run"
        self._attr_name = f"{zone_name} - Run"
        self._attr_device_info = device_info_for(entry, coordinator.info, "irrigation")

    @property
    def is_on(self) -> bool | None:
        data = self.coordinator.data or {}
        for z in (data.get("irrigation") or {}).get("zones", []):
            if z.get("slug") == self._slug:
                return bool(z.get("running"))
        return None

    async def async_turn_on(self, **kwargs: Any) -> None:
        seconds = int(
            kwargs.get(
                "duration_s",
                self._entry.options.get(OPT_DEFAULT_RUN_SECONDS, DEFAULT_RUN_SECONDS),
            )
        )
        await self.coordinator.dispatch_action(
            {"kind": ACTION_RUN, "zone": self._slug, "seconds": seconds}
        )

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.dispatch_action(
            {"kind": ACTION_STOP, "zone": self._slug}
        )
