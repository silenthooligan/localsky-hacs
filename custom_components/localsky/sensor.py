"""Sensor entities exposed by the LocalSky integration."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    PERCENTAGE,
    UnitOfLength,
    UnitOfPressure,
    UnitOfSpeed,
    UnitOfTemperature,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .util import format_base_url
from .coordinator import LocalSkyCoordinator


@dataclass(frozen=True)
class LocalSkySensorDef:
    """A simple definition: where to read from coordinator.data + how to label it."""

    key: str
    name: str
    snapshot: str  # 'tempest' | 'irrigation' | 'forecast'
    path: tuple[str, ...]
    unit: str | None = None
    device_class: SensorDeviceClass | None = None
    state_class: SensorStateClass | None = SensorStateClass.MEASUREMENT
    icon: str | None = None


WEATHER_SENSORS: tuple[LocalSkySensorDef, ...] = (
    LocalSkySensorDef(
        key="air_temp_f",
        name="Air temperature",
        snapshot="tempest",
        path=("air_temp_f",),
        unit=UnitOfTemperature.FAHRENHEIT,
        device_class=SensorDeviceClass.TEMPERATURE,
    ),
    LocalSkySensorDef(
        key="feels_like_f",
        name="Feels like",
        snapshot="tempest",
        path=("feels_like_f",),
        unit=UnitOfTemperature.FAHRENHEIT,
        device_class=SensorDeviceClass.TEMPERATURE,
    ),
    LocalSkySensorDef(
        key="rh_pct",
        name="Humidity",
        snapshot="tempest",
        path=("rh_pct",),
        unit=PERCENTAGE,
        device_class=SensorDeviceClass.HUMIDITY,
    ),
    LocalSkySensorDef(
        key="dew_point_f",
        name="Dew point",
        snapshot="tempest",
        path=("dew_point_f",),
        unit=UnitOfTemperature.FAHRENHEIT,
        device_class=SensorDeviceClass.TEMPERATURE,
    ),
    LocalSkySensorDef(
        key="wind_avg_mph",
        name="Wind speed",
        snapshot="tempest",
        path=("wind_avg_mph",),
        unit=UnitOfSpeed.MILES_PER_HOUR,
        device_class=SensorDeviceClass.WIND_SPEED,
    ),
    LocalSkySensorDef(
        key="wind_gust_mph",
        name="Wind gust",
        snapshot="tempest",
        path=("wind_gust_mph",),
        unit=UnitOfSpeed.MILES_PER_HOUR,
        device_class=SensorDeviceClass.WIND_SPEED,
    ),
    LocalSkySensorDef(
        key="pressure_inhg",
        name="Pressure",
        snapshot="tempest",
        path=("pressure_inhg",),
        unit=UnitOfPressure.INHG,
        device_class=SensorDeviceClass.PRESSURE,
    ),
    LocalSkySensorDef(
        key="rain_in_today",
        name="Rain today",
        snapshot="tempest",
        path=("rain_in_today",),
        unit="in",
        device_class=SensorDeviceClass.PRECIPITATION,
        state_class=SensorStateClass.TOTAL_INCREASING,
    ),
    LocalSkySensorDef(
        key="solar_w_m2",
        name="Solar irradiance",
        snapshot="tempest",
        path=("solar_w_m2",),
        unit="W/m²",
        device_class=SensorDeviceClass.IRRADIANCE,
    ),
    LocalSkySensorDef(
        key="uv_index",
        name="UV index",
        snapshot="tempest",
        path=("uv_index",),
        device_class=None,
    ),
)


VERDICT_SENSOR = LocalSkySensorDef(
    key="verdict",
    name="Verdict (today)",
    snapshot="irrigation",
    path=("skip_check", "verdict"),
    state_class=None,
    icon="mdi:water-check",
)


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
    """Set up scalar sensors immediately, then add per-zone sensors as
    LocalSky reports new zones via the coordinator's dynamic listener.
    A zone added in LocalSky's UI surfaces in HA without reload."""
    coordinator: LocalSkyCoordinator = hass.data[DOMAIN][entry.entry_id]

    scalars: list[SensorEntity] = [
        LocalSkyScalarSensor(coordinator, entry, d) for d in WEATHER_SENSORS
    ]
    scalars.append(LocalSkyScalarSensor(coordinator, entry, VERDICT_SENSOR))
    async_add_entities(scalars)

    seen: set[str] = set()

    @callback
    def _on_zones(slugs: set[str]) -> None:
        new = slugs - seen
        if not new:
            return
        irrigation = (coordinator.data or {}).get("irrigation") or {}
        zone_by_slug = {z["slug"]: z for z in irrigation.get("zones", []) if z.get("slug")}
        new_entities: list[SensorEntity] = []
        for slug in sorted(new):
            zone_name = (zone_by_slug.get(slug) or {}).get("name") or slug
            new_entities.extend(
                [
                    LocalSkyZoneSensor(
                        coordinator, entry,
                        slug=slug, zone_name=zone_name,
                        key="bucket_mm", label="Soil bucket",
                        unit="mm", icon="mdi:water-percent",
                    ),
                    LocalSkyZoneSensor(
                        coordinator, entry,
                        slug=slug, zone_name=zone_name,
                        key="planned_run_seconds", label="Planned run",
                        unit=UnitOfTime.SECONDS,
                        device_class=SensorDeviceClass.DURATION,
                    ),
                    LocalSkyZoneSensor(
                        coordinator, entry,
                        slug=slug, zone_name=zone_name,
                        key="today_run_minutes", label="Run today",
                        unit=UnitOfTime.MINUTES,
                        device_class=SensorDeviceClass.DURATION,
                        state_class=SensorStateClass.TOTAL_INCREASING,
                    ),
                ]
            )
        if new_entities:
            async_add_entities(new_entities)
        seen.update(new)

    entry.async_on_unload(coordinator.add_zone_listener(_on_zones))


class _LocalSkyBaseSensor(CoordinatorEntity[LocalSkyCoordinator], SensorEntity):
    """Common base: device-registry binding + availability."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: LocalSkyCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._entry = entry
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


class LocalSkyScalarSensor(_LocalSkyBaseSensor):
    """Scalar from a snapshot at a fixed JSON path."""

    def __init__(
        self,
        coordinator: LocalSkyCoordinator,
        entry: ConfigEntry,
        spec: LocalSkySensorDef,
    ) -> None:
        super().__init__(coordinator, entry)
        self._spec = spec
        self._attr_unique_id = f"{entry.entry_id}_{spec.key}"
        self._attr_name = spec.name
        self._attr_native_unit_of_measurement = spec.unit
        self._attr_device_class = spec.device_class
        self._attr_state_class = spec.state_class
        self._attr_icon = spec.icon

    @property
    def native_value(self) -> Any:
        data = self.coordinator.data
        if not data:
            return None
        return _walk(data.get(self._spec.snapshot), self._spec.path)


class LocalSkyZoneSensor(_LocalSkyBaseSensor):
    """Per-zone scalar (bucket_mm, planned_run_seconds, etc.)."""

    def __init__(
        self,
        coordinator: LocalSkyCoordinator,
        entry: ConfigEntry,
        *,
        slug: str,
        zone_name: str,
        key: str,
        label: str,
        unit: str | None = None,
        device_class: SensorDeviceClass | None = None,
        state_class: SensorStateClass | None = SensorStateClass.MEASUREMENT,
        icon: str | None = None,
    ) -> None:
        super().__init__(coordinator, entry)
        self._slug = slug
        self._key = key
        self._attr_unique_id = f"{entry.entry_id}_{slug}_{key}"
        self._attr_name = f"{zone_name} - {label}"
        self._attr_native_unit_of_measurement = unit
        self._attr_device_class = device_class
        self._attr_state_class = state_class
        self._attr_icon = icon

    @property
    def native_value(self) -> Any:
        data = self.coordinator.data or {}
        irrigation = data.get("irrigation") or {}
        for z in irrigation.get("zones", []):
            if z.get("slug") == self._slug:
                return z.get(self._key)
        return None
