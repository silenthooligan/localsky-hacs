"""Coordinator internals: SSE parsing, merge, zone diffing, auth failures."""
import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import UpdateFailed
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.localsky.const import DOMAIN
from custom_components.localsky.coordinator import LocalSkyCoordinator

ENTRY_DATA = {"host": "192.0.2.10", "port": 8090, "use_https": False}


def _coordinator(hass: HomeAssistant, session=None, options=None) -> LocalSkyCoordinator:
    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA, options=options or {})
    entry.add_to_hass(hass)
    return LocalSkyCoordinator(
        hass, entry, session or MagicMock(), "http://192.0.2.10:8090"
    )


class _FakeStream:
    """aiohttp response stand-in whose .content yields canned SSE bytes."""

    def __init__(self, lines: list[bytes]) -> None:
        self._lines = lines

    @property
    def content(self):
        async def _gen():
            for line in self._lines:
                yield line

        return _gen()


class _FakeResponse:
    """Async-context-manager response with a fixed status + json body."""

    def __init__(self, status: int = 200, body=None) -> None:
        self.status = status
        self._body = body or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def raise_for_status(self):
        if self.status >= 400:
            raise aiohttp.ClientResponseError(MagicMock(), (), status=self.status)

    async def json(self):
        return self._body


class _FakeSession:
    """Records requests; serves queued responses for get/post."""

    def __init__(self, response: _FakeResponse) -> None:
        self._response = response
        self.calls: list[tuple[str, str, dict]] = []

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        return self._response

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return self._response


@pytest.mark.asyncio
async def test_sse_parser_handles_keepalives_and_multiline(hass: HomeAssistant) -> None:
    coord = _coordinator(hass)
    stream = _FakeStream(
        [
            b":\n",  # keep-alive comment
            b"data: {\"a\":\n",
            b"data: 1}\n",
            b"\n",  # dispatch
            b"data: not-json\n",
            b"\n",  # malformed frame: skipped, parser keeps going
            b"data: {\"b\": 2}\n",
            b"\n",
        ]
    )
    events = [e async for e in coord._iter_sse_events(stream)]
    assert events == [{"a": 1}, {"b": 2}]


@pytest.mark.asyncio
async def test_merge_publishes_channels_independently(hass: HomeAssistant) -> None:
    coord = _coordinator(hass)
    coord._merge_and_publish("tempest", {"air_temp_f": 80.0})
    coord._merge_and_publish("irrigation", {"zones": [{"slug": "front"}]})
    assert coord.data["tempest"] == {"air_temp_f": 80.0}
    assert coord.data["irrigation"]["zones"][0]["slug"] == "front"


@pytest.mark.asyncio
async def test_zone_listener_fires_on_changes_only(hass: HomeAssistant) -> None:
    coord = _coordinator(hass)
    seen: list[set[str]] = []
    coord.add_zone_listener(seen.append)

    coord._merge_and_publish("irrigation", {"zones": [{"slug": "front"}]})
    coord._merge_and_publish("irrigation", {"zones": [{"slug": "front"}]})  # no change
    coord._merge_and_publish(
        "irrigation", {"zones": [{"slug": "front"}, {"slug": "back"}]}
    )
    assert seen == [{"front"}, {"front", "back"}]

    # A listener registered late immediately sees the current set.
    late: list[set[str]] = []
    coord.add_zone_listener(late.append)
    assert late == [{"front", "back"}]


@pytest.mark.asyncio
async def test_poll_refresh_merges_all_three_snapshots(hass: HomeAssistant) -> None:
    coord = _coordinator(hass)
    with patch.object(
        coord,
        "_fetch",
        new=AsyncMock(side_effect=[{"t": 1}, {"zones": []}, {"daily": []}]),
    ):
        data = await coord._async_update_data()
    assert data == {"tempest": {"t": 1}, "irrigation": {"zones": []}, "forecast": {"daily": []}}


@pytest.mark.asyncio
async def test_poll_refresh_wraps_client_errors(hass: HomeAssistant) -> None:
    coord = _coordinator(hass)
    with patch.object(
        coord,
        "_fetch",
        new=AsyncMock(side_effect=aiohttp.ClientError("boom")),
    ), pytest.raises(UpdateFailed):
        await coord._async_update_data()


@pytest.mark.asyncio
async def test_fetch_401_triggers_reauth(hass: HomeAssistant) -> None:
    """Revoked token on a poll fetch raises ConfigEntryAuthFailed, which
    is HA's signal to start the reauth flow."""
    session = _FakeSession(_FakeResponse(status=401))
    coord = _coordinator(hass, session=session)
    with pytest.raises(ConfigEntryAuthFailed):
        await coord._fetch("/snapshot")


@pytest.mark.asyncio
async def test_dispatch_action_posts_payload(hass: HomeAssistant) -> None:
    session = _FakeSession(_FakeResponse(status=200))
    coord = _coordinator(hass, session=session, options={"use_sse": True})
    await coord.dispatch_action({"kind": "run", "zone": "front", "seconds": 60})
    method, url, kwargs = session.calls[-1]
    assert method == "POST"
    assert url.endswith("/api/v1/irrigation/action")
    assert kwargs["json"]["zone"] == "front"


@pytest.mark.asyncio
async def test_dispatch_action_401_triggers_reauth(hass: HomeAssistant) -> None:
    session = _FakeSession(_FakeResponse(status=401))
    coord = _coordinator(hass, session=session)
    with pytest.raises(ConfigEntryAuthFailed):
        await coord.dispatch_action({"kind": "stop_all"})


@pytest.mark.asyncio
async def test_bearer_header_set_from_entry_token(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN, data={**ENTRY_DATA, "api_token": "tok123"}
    )
    entry.add_to_hass(hass)
    coord = LocalSkyCoordinator(hass, entry, MagicMock(), "http://192.0.2.10:8090")
    assert coord._headers == {"Authorization": "Bearer tok123"}
