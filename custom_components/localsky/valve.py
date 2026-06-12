"""LocalSky irrigation zones as HA ``valve`` entities.

Modern replacement for the per-zone ``switch.<zone>_run`` entities. HA's
valve platform (2024.5+) is the proper device class for water valves and
sprinkler stations — it gives Lovelace the right tile features (open/
close, position) and surfaces correctly in voice intents like "open the
front yard valve".

Each LocalSky zone slug becomes one ``valve.localsky_<slug>``. The valve
opens (zone runs) for ``default_run_seconds`` from options unless an
automation passes a duration via service data.
"""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.valve import (
    ValveDeviceClass,
    ValveEntity,
    ValveEntityFeature,
)
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
    """Register valves dynamically — every new LocalSky zone shows up
    without a config-entry reload."""
    coordinator: LocalSkyCoordinator = entry.runtime_data
    seen: set[str] = set()

    @callback
    def _on_zones(slugs: set[str]) -> None:
        new = slugs - seen
        if not new:
            return
        irrigation = (coordinator.data or {}).get("irrigation") or {}
        zone_by_slug = {z["slug"]: z for z in irrigation.get("zones", []) if z.get("slug")}
        entities = [
            LocalSkyZoneValve(coordinator, entry, slug, zone_by_slug.get(slug, {}).get("name") or slug)
            for slug in sorted(new)
        ]
        if entities:
            async_add_entities(entities)
        seen.update(new)

    entry.async_on_unload(coordinator.add_zone_listener(_on_zones))


class LocalSkyZoneValve(CoordinatorEntity[LocalSkyCoordinator], ValveEntity):
    """A LocalSky irrigation zone, exposed as a water valve."""

    _attr_has_entity_name = True
    _attr_device_class = ValveDeviceClass.WATER
    _attr_supported_features = (
        ValveEntityFeature.OPEN | ValveEntityFeature.CLOSE
    )
    _attr_reports_position = False

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
        self._attr_unique_id = f"{entry.entry_id}_{slug}_valve"
        self._attr_name = zone_name
        self._attr_device_info = device_info_for(entry, coordinator.info, "irrigation")

    def _zone(self) -> dict[str, Any] | None:
        irrigation = (self.coordinator.data or {}).get("irrigation") or {}
        for z in irrigation.get("zones", []):
            if z.get("slug") == self._slug:
                return z
        return None

    @property
    def is_closed(self) -> bool | None:
        zone = self._zone()
        if zone is None:
            return None
        return not bool(zone.get("running"))

    @property
    def available(self) -> bool:
        return super().available and self._zone() is not None

    async def async_open_valve(self, **kwargs: Any) -> None:
        seconds = int(
            kwargs.get(
                "duration_s",
                self._entry.options.get(OPT_DEFAULT_RUN_SECONDS, DEFAULT_RUN_SECONDS),
            )
        )
        await self.coordinator.dispatch_action(
            {"kind": ACTION_RUN, "zone": self._slug, "seconds": seconds}
        )

    async def async_close_valve(self, **kwargs: Any) -> None:
        await self.coordinator.dispatch_action(
            {"kind": ACTION_STOP, "zone": self._slug}
        )
