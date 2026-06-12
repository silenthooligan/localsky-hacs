"""Manifest-driven entities: creation, value walking, zone paths."""
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.localsky.const import DOMAIN
from custom_components.localsky.coordinator import LocalSkyCoordinator

from .conftest import INFO_OPEN

ENTRY_DATA = {"host": "192.0.2.10", "port": 8090, "use_https": False}

MANIFEST = {
    "entities": [
        {
            "id": "air_temp_f",
            "platform": "sensor",
            "name": "Air temperature",
            "snapshot": "tempest",
            "path": ["air_temp_f"],
            "unit": "°F",
            "device_class": "temperature",
        },
        {
            "id": "rain_in_today",
            "platform": "sensor",
            "name": "Rain today",
            "snapshot": "tempest",
            "path": ["rain_in_today"],
            "unit": "in",
        },
        {
            "id": "front_soil_moisture",
            "platform": "sensor",
            "name": "Front - Soil moisture",
            "snapshot": "irrigation",
            "path": ["soil_pct"],
            "zone_slug": "front",
            "unit": "%",
        },
        # Non-sensor platforms must be ignored by sensor setup.
        {
            "id": "front_running",
            "platform": "binary_sensor",
            "name": "Front running",
            "snapshot": "irrigation",
            "path": ["running"],
            "zone_slug": "front",
        },
    ]
}

DATA = {
    "tempest": {"air_temp_f": 84.2, "rain_in_today": 0.37},
    "irrigation": {
        "zones": [{"slug": "front", "name": "Front", "soil_pct": 41.5}],
        "skip_check": {"verdict": "run"},
    },
    "forecast": {"daily": []},
}


async def _setup(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN, data=ENTRY_DATA, unique_id=INFO_OPEN["uuid"]
    )
    entry.add_to_hass(hass)

    async def _start(self: LocalSkyCoordinator) -> None:
        self.async_set_updated_data(DATA)

    with patch.object(
        LocalSkyCoordinator, "fetch_info", new=AsyncMock(return_value=INFO_OPEN)
    ), patch.object(
        LocalSkyCoordinator, "fetch_manifest", new=AsyncMock(return_value=MANIFEST)
    ), patch.object(
        LocalSkyCoordinator, "async_start", new=_start
    ), patch.object(
        LocalSkyCoordinator, "async_stop", new=AsyncMock()
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


@pytest.mark.asyncio
async def test_manifest_sensors_created_with_values(hass: HomeAssistant) -> None:
    await _setup(hass)
    temp = hass.states.get("sensor.localsky_air_temperature")
    assert temp is not None
    assert float(temp.state) == 84.2
    assert temp.attributes["unit_of_measurement"] == "°F"

    rain = hass.states.get("sensor.localsky_rain_today")
    assert rain is not None
    assert float(rain.state) == 0.37


@pytest.mark.asyncio
async def test_zone_scoped_path_resolves_through_zones_list(hass: HomeAssistant) -> None:
    await _setup(hass)
    soil = hass.states.get("sensor.localsky_front_soil_moisture")
    assert soil is not None
    assert float(soil.state) == 41.5


@pytest.mark.asyncio
async def test_sensor_setup_ignores_other_platform_descriptors(hass: HomeAssistant) -> None:
    await _setup(hass)
    # The binary_sensor descriptor must not materialize as a sensor.
    assert hass.states.get("sensor.localsky_front_running") is None


@pytest.mark.asyncio
async def test_values_update_with_coordinator_data(hass: HomeAssistant) -> None:
    entry = await _setup(hass)
    coordinator: LocalSkyCoordinator = entry.runtime_data
    updated = {**DATA, "tempest": {**DATA["tempest"], "air_temp_f": 70.1}}
    coordinator.async_set_updated_data(updated)
    await hass.async_block_till_done()
    assert float(hass.states.get("sensor.localsky_air_temperature").state) == 70.1
