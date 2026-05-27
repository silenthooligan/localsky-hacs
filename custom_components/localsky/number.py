"""Threshold sliders for LocalSky's skip-check rules.

Three numbers map to the LocalSky ``SetThreshold`` action:

- ``number.localsky_max_wind_mph``   — skip if today's forecast peak wind exceeds this
- ``number.localsky_min_temp_f``     — skip if overnight low drops below this
- ``number.localsky_rain_skip_in``   — skip if forecast/recent rain ≥ this

Editing the slider POSTs the action to LocalSky, which persists it as
an HA ``input_number`` so the value is visible in HA's helpers UI too.
"""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import ACTION_SET_THRESHOLD, DOMAIN, THRESHOLD_KEYS, THRESHOLD_LIMITS
from .coordinator import LocalSkyCoordinator
from .util import format_base_url

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: LocalSkyCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [LocalSkyThresholdNumber(coordinator, entry, key) for key in THRESHOLD_KEYS]
    )


class LocalSkyThresholdNumber(CoordinatorEntity[LocalSkyCoordinator], NumberEntity):
    """One slider per skip-check threshold."""

    _attr_has_entity_name = True
    _attr_mode = NumberMode.SLIDER

    def __init__(
        self,
        coordinator: LocalSkyCoordinator,
        entry: ConfigEntry,
        key: str,
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._key = key
        min_v, max_v, step, unit = THRESHOLD_LIMITS[key]
        self._attr_unique_id = f"{entry.entry_id}_threshold_{key}"
        self._attr_name = key.replace("_", " ").title()
        self._attr_native_min_value = min_v
        self._attr_native_max_value = max_v
        self._attr_native_step = step
        self._attr_native_unit_of_measurement = unit
        info = coordinator.info or {}
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="LocalSky",
            manufacturer="LocalSky",
            model="LocalSky Service",
            sw_version=info.get("service_version", "unknown"),
            configuration_url=format_base_url(
                entry.data.get("host", ""),
                entry.data.get("port", 8090),
                entry.data.get("use_https", False),
            ),
        )

    @property
    def native_value(self) -> float | None:
        irrigation = (self.coordinator.data or {}).get("irrigation") or {}
        skip_check = irrigation.get("skip_check") or {}
        v = skip_check.get(self._key)
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    async def async_set_native_value(self, value: float) -> None:
        await self.coordinator.dispatch_action(
            {"kind": ACTION_SET_THRESHOLD, "key": self._key, "value": value}
        )
