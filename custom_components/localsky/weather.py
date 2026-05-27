"""Single weather entity built from LocalSky's Tempest snapshot.

Drops the need for a separate WeatherFlow integration in HA when
LocalSky is the source-of-truth (LocalSky already ingests Tempest UDP
broadcasts directly). Daily forecast pulled from the forecast snapshot
when present.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from homeassistant.components.weather import (
    Forecast,
    WeatherEntity,
    WeatherEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    UnitOfPrecipitationDepth,
    UnitOfPressure,
    UnitOfSpeed,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import LocalSkyCoordinator
from .util import format_base_url

_LOGGER = logging.getLogger(__name__)


# Tempest precip_type to HA condition. Tempest reports 0=none/1=rain/2=hail.
# We blend with cloud-cover heuristics from solar irradiance only when
# precip is none, since LocalSky doesn't yet expose a cloud-cover field.
def _condition_from_snapshot(tempest: dict[str, Any]) -> str | None:
    precip_type = tempest.get("precip_type")
    rain_in_hr = float(tempest.get("rain_intensity_in_hr") or 0)
    lightning = int(tempest.get("lightning_strikes_last_hour") or 0)
    if precip_type == 2:
        return "hail"
    if lightning > 0:
        return "lightning-rainy" if rain_in_hr > 0 else "lightning"
    if precip_type == 1 or rain_in_hr > 0:
        if rain_in_hr >= 0.3:
            return "pouring"
        return "rainy"
    solar = float(tempest.get("solar_w_m2") or 0)
    wind = float(tempest.get("wind_avg_mph") or 0)
    if wind >= 18:
        return "windy"
    if solar > 600:
        return "sunny"
    if solar > 200:
        return "partlycloudy"
    if solar > 30:
        return "cloudy"
    return "clear-night"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: LocalSkyCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([LocalSkyWeather(coordinator, entry)])


class LocalSkyWeather(CoordinatorEntity[LocalSkyCoordinator], WeatherEntity):
    """Backed by LocalSky's live Tempest + forecast snapshots."""

    _attr_has_entity_name = True
    _attr_name = "Weather"
    _attr_native_temperature_unit = UnitOfTemperature.FAHRENHEIT
    _attr_native_pressure_unit = UnitOfPressure.INHG
    _attr_native_wind_speed_unit = UnitOfSpeed.MILES_PER_HOUR
    _attr_native_precipitation_unit = UnitOfPrecipitationDepth.INCHES
    _attr_supported_features = WeatherEntityFeature.FORECAST_DAILY

    def __init__(self, coordinator: LocalSkyCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_weather"
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

    def _tempest(self) -> dict[str, Any]:
        return (self.coordinator.data or {}).get("tempest") or {}

    @property
    def condition(self) -> str | None:
        return _condition_from_snapshot(self._tempest())

    @property
    def native_temperature(self) -> float | None:
        v = self._tempest().get("air_temp_f")
        return float(v) if v is not None else None

    @property
    def native_apparent_temperature(self) -> float | None:
        v = self._tempest().get("feels_like_f")
        return float(v) if v is not None else None

    @property
    def native_dew_point(self) -> float | None:
        v = self._tempest().get("dew_point_f")
        return float(v) if v is not None else None

    @property
    def humidity(self) -> float | None:
        v = self._tempest().get("rh_pct")
        return float(v) if v is not None else None

    @property
    def native_pressure(self) -> float | None:
        v = self._tempest().get("pressure_inhg")
        return float(v) if v is not None else None

    @property
    def native_wind_speed(self) -> float | None:
        v = self._tempest().get("wind_avg_mph")
        return float(v) if v is not None else None

    @property
    def native_wind_gust_speed(self) -> float | None:
        v = self._tempest().get("wind_gust_mph")
        return float(v) if v is not None else None

    @property
    def wind_bearing(self) -> float | None:
        v = self._tempest().get("wind_dir_deg")
        return float(v) if v is not None else None

    @property
    def uv_index(self) -> float | None:
        v = self._tempest().get("uv_index")
        return float(v) if v is not None else None

    async def async_forecast_daily(self) -> list[Forecast] | None:
        forecast = (self.coordinator.data or {}).get("forecast") or {}
        days = forecast.get("daily") or forecast.get("days") or []
        out: list[Forecast] = []
        for d in days[:7]:
            ts = d.get("epoch") or d.get("date_epoch")
            if isinstance(ts, (int, float)):
                dt = datetime.fromtimestamp(int(ts), tz=timezone.utc)
            else:
                continue
            out.append(
                Forecast(
                    datetime=dt.isoformat(),
                    native_temperature=_as_float(d.get("temp_max_f")),
                    native_templow=_as_float(d.get("temp_min_f")),
                    native_precipitation=_as_float(d.get("precip_in")),
                    precipitation_probability=_as_int(d.get("precip_prob_pct")),
                    native_wind_speed=_as_float(d.get("wind_max_mph")),
                    condition=d.get("condition"),
                )
            )
        return out or None


def _as_float(v: Any) -> float | None:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _as_int(v: Any) -> int | None:
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None
