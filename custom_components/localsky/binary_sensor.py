"""Binary sensors: per-zone running + LocalSky-driven diagnostics
(ha_reachable, iu_suspended). Manifest-first (Phase 2 architecture);
the legacy hardcoded zone-running path stays as a fallback for older
LocalSky deployments that don't publish /sensors/manifest.
"""
from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import LocalSkyCoordinator
from .util import device_info_for


def _walk(data: Any, path: tuple[str, ...]) -> Any:
    cur = data
    for p in path:
        if cur is None:
            return None
        if isinstance(cur, dict):
            cur = cur.get(p)
        else:
            return None
    return cur


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: LocalSkyCoordinator = entry.runtime_data
    manifest = await coordinator.fetch_manifest()

    if manifest is not None:
        # Manifest-driven path: every binary_sensor descriptor becomes
        # a ManifestBinarySensor. Adding a new diagnostic in LocalSky's
        # manifest.rs surfaces as a new HA binary_sensor with no HACS
        # code change.
        entities: list[BinarySensorEntity] = [LocalSkyAnyZoneRunning(coordinator, entry)]
        seen_ids: set[str] = set()
        for desc in manifest.get("entities", []):
            if desc.get("platform") != "binary_sensor":
                continue
            if desc["id"] in seen_ids:
                continue
            seen_ids.add(desc["id"])
            entities.append(ManifestBinarySensor(coordinator, entry, desc))
        async_add_entities(entities)

        @callback
        def _on_zones(_slugs: set[str]) -> None:
            hass.async_create_task(
                _async_refresh_manifest_binaries(
                    entry, coordinator, async_add_entities, seen_ids
                )
            )

        entry.async_on_unload(coordinator.add_zone_listener(_on_zones))
        return

    # ── Fallback for LocalSky < manifest support ──
    async_add_entities([LocalSkyAnyZoneRunning(coordinator, entry)])

    seen: set[str] = set()

    @callback
    def _on_zones_legacy(slugs: set[str]) -> None:
        new = slugs - seen
        if not new:
            return
        irrigation = (coordinator.data or {}).get("irrigation") or {}
        zone_by_slug = {z["slug"]: z for z in irrigation.get("zones", []) if z.get("slug")}
        async_add_entities(
            [
                LocalSkyZoneRunningBinary(
                    coordinator,
                    entry,
                    slug,
                    zone_by_slug.get(slug, {}).get("name") or slug,
                )
                for slug in sorted(new)
            ]
        )
        seen.update(new)

    entry.async_on_unload(coordinator.add_zone_listener(_on_zones_legacy))


async def _async_refresh_manifest_binaries(
    entry: ConfigEntry,
    coordinator: LocalSkyCoordinator,
    async_add_entities: AddEntitiesCallback,
    seen_ids: set[str],
) -> None:
    manifest = await coordinator.fetch_manifest()
    if manifest is None:
        return
    new_entities: list[BinarySensorEntity] = []
    for desc in manifest.get("entities", []):
        if desc.get("platform") != "binary_sensor":
            continue
        if desc["id"] in seen_ids:
            continue
        seen_ids.add(desc["id"])
        new_entities.append(ManifestBinarySensor(coordinator, entry, desc))
    if new_entities:
        async_add_entities(new_entities)


class ManifestBinarySensor(CoordinatorEntity[LocalSkyCoordinator], BinarySensorEntity):
    """Binary sensor from a manifest descriptor (per-zone running,
    diagnostic ha_reachable / iu_suspended, etc.)."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: LocalSkyCoordinator,
        entry: ConfigEntry,
        desc: dict[str, Any],
    ) -> None:
        super().__init__(coordinator)
        self._desc = desc
        self._snapshot = desc.get("snapshot", "")
        self._path: tuple[str, ...] = tuple(desc.get("path", []))
        self._zone_slug: str | None = desc.get("zone_slug")
        self._attr_unique_id = f"{entry.entry_id}_{desc['id']}"
        self._attr_name = desc.get("name") or desc["id"]
        if dc := desc.get("device_class"):
            self._attr_device_class = dc
        if icon := desc.get("icon"):
            self._attr_icon = icon
        self._attr_device_info = device_info_for(entry, coordinator.info, self._snapshot)

    @property
    def is_on(self) -> bool | None:
        snap = (self.coordinator.data or {}).get(self._snapshot)
        if self._zone_slug is not None:
            if not isinstance(snap, dict):
                return None
            zone = next(
                (z for z in snap.get("zones") or [] if isinstance(z, dict) and z.get("slug") == self._zone_slug),
                None,
            )
            v = _walk(zone, self._path)
        else:
            v = _walk(snap, self._path)
        return None if v is None else bool(v)


class _LocalSkyBaseBinary(CoordinatorEntity[LocalSkyCoordinator], BinarySensorEntity):
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: LocalSkyCoordinator,
        entry: ConfigEntry,
        group: str | None = None,
    ) -> None:
        super().__init__(coordinator)
        self._attr_device_info = device_info_for(entry, coordinator.info, group)


class LocalSkyZoneRunningBinary(_LocalSkyBaseBinary):
    """on = this zone is actively running."""

    _attr_device_class = BinarySensorDeviceClass.RUNNING

    def __init__(
        self,
        coordinator: LocalSkyCoordinator,
        entry: ConfigEntry,
        slug: str,
        zone_name: str,
    ) -> None:
        super().__init__(coordinator, entry, group="irrigation")
        self._slug = slug
        self._attr_unique_id = f"{entry.entry_id}_{slug}_running"
        self._attr_name = f"{zone_name} - Running"

    @property
    def is_on(self) -> bool | None:
        data = self.coordinator.data or {}
        for z in (data.get("irrigation") or {}).get("zones", []):
            if z.get("slug") == self._slug:
                return bool(z.get("running"))
        return None


class LocalSkyAnyZoneRunning(_LocalSkyBaseBinary):
    """on = any LocalSky zone is currently running."""

    _attr_device_class = BinarySensorDeviceClass.RUNNING

    def __init__(self, coordinator: LocalSkyCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, group="irrigation")
        self._attr_unique_id = f"{entry.entry_id}_any_running"
        self._attr_name = "Any zone running"

    @property
    def is_on(self) -> bool | None:
        data = self.coordinator.data or {}
        zones = (data.get("irrigation") or {}).get("zones", [])
        if not zones:
            return None
        return any(z.get("running") for z in zones)
