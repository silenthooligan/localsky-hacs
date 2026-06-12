"""Entry lifecycle: setup, runtime_data, services, unload, not-ready."""
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.localsky.const import DOMAIN
from custom_components.localsky.coordinator import LocalSkyCoordinator

from .conftest import INFO_OPEN

ENTRY_DATA = {"host": "192.0.2.10", "port": 8090, "use_https": False}

SERVICES = ("run_zone", "stop_zone", "stop_all")


def _entry(uid: str = INFO_OPEN["uuid"]) -> MockConfigEntry:
    return MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA, unique_id=uid)


def _patched_network():
    """Silence every coordinator network touchpoint for lifecycle tests."""
    return (
        patch.object(LocalSkyCoordinator, "fetch_info", new=AsyncMock(return_value=INFO_OPEN)),
        patch.object(LocalSkyCoordinator, "fetch_manifest", new=AsyncMock(return_value=None)),
        patch.object(LocalSkyCoordinator, "async_start", new=AsyncMock()),
        patch.object(LocalSkyCoordinator, "async_stop", new=AsyncMock()),
    )


@pytest.mark.asyncio
async def test_setup_populates_runtime_data_and_services(hass: HomeAssistant) -> None:
    entry = _entry()
    entry.add_to_hass(hass)
    p1, p2, p3, p4 = _patched_network()
    with p1, p2, p3, p4:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        assert entry.state is ConfigEntryState.LOADED
        assert isinstance(entry.runtime_data, LocalSkyCoordinator)
        for svc in SERVICES:
            assert hass.services.has_service(DOMAIN, svc)


@pytest.mark.asyncio
async def test_unload_stops_coordinator_and_drops_services(hass: HomeAssistant) -> None:
    entry = _entry()
    entry.add_to_hass(hass)
    p1, p2, p3, p4 = _patched_network()
    with p1, p2, p3, p4:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        coordinator = entry.runtime_data

        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()

        assert entry.state is ConfigEntryState.NOT_LOADED
        coordinator.async_stop.assert_awaited()
        for svc in SERVICES:
            assert not hass.services.has_service(DOMAIN, svc)


@pytest.mark.asyncio
async def test_services_survive_until_last_entry_unloads(hass: HomeAssistant) -> None:
    first = _entry()
    second = _entry(uid="99999999-2222-4333-8444-555555555555")
    first.add_to_hass(hass)
    second.add_to_hass(hass)
    p1, p2, p3, p4 = _patched_network()
    with p1, p2, p3, p4:
        assert await hass.config_entries.async_setup(first.entry_id)
        assert await hass.config_entries.async_setup(second.entry_id)
        await hass.async_block_till_done()

        assert await hass.config_entries.async_unload(first.entry_id)
        await hass.async_block_till_done()
        # One LocalSky still loaded: services must remain callable.
        for svc in SERVICES:
            assert hass.services.has_service(DOMAIN, svc)

        assert await hass.config_entries.async_unload(second.entry_id)
        await hass.async_block_till_done()
        for svc in SERVICES:
            assert not hass.services.has_service(DOMAIN, svc)


@pytest.mark.asyncio
async def test_unreachable_server_defers_setup(hass: HomeAssistant) -> None:
    entry = _entry()
    entry.add_to_hass(hass)
    with patch.object(
        LocalSkyCoordinator,
        "fetch_info",
        new=AsyncMock(side_effect=OSError("connection refused")),
    ):
        assert not await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_RETRY
