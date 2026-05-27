"""LocalSky coordinator — SSE-first, poll-fallback.

Subscribes to ``/api/v1/irrigation/stream`` and ``/api/v1/stream`` so HA
sees zone state + Tempest weather updates as fast as LocalSky emits them
(sub-second typical), and falls back to scheduled polling when SSE is
disabled in options or the connection can't be established. The forecast
endpoint has no SSE upstream — kept on a 5-minute poll regardless.

Music-Assistant-style dynamic entity registration: every snapshot is
diffed against the previously-seen zone set, and listeners (registered
by platforms) fire on the changed slug set so new zones added in
LocalSky's UI surface in HA without a reload.
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from datetime import timedelta
from typing import Any

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    API_PREFIX,
    DEFAULT_POLL_INTERVAL,
    DEFAULT_USE_SSE,
    DOMAIN,
    OPT_POLL_INTERVAL,
    OPT_USE_SSE,
)

_LOGGER = logging.getLogger(__name__)

# Forecast endpoint isn't event-driven server-side; refresh every 5 min.
FORECAST_REFRESH = timedelta(minutes=5)

# SSE reconnect backoff caps. We always retry — LocalSky is a LAN service
# and outages are usually short (reboot, network blip).
SSE_BACKOFF_INITIAL = 2.0
SSE_BACKOFF_MAX = 30.0


class LocalSkyCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Push-driven coordinator with poll fallback."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        session: aiohttp.ClientSession,
        base_url: str,
    ) -> None:
        self._entry = entry
        self._session = session
        self._base_url = base_url.rstrip("/")
        self._sse_tasks: list[asyncio.Task[None]] = []
        self._forecast_task: asyncio.Task[None] | None = None
        self._zone_listeners: list[Callable[[set[str]], None]] = []
        self._known_zones: set[str] = set()
        self.info: dict[str, Any] | None = None
        # SSE-mode coordinators don't poll; pure polling-mode falls back
        # to the configured interval. We pick the mode in async_setup().
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=None,
        )

    # ---- public read accessors used by entities ----

    @property
    def base_url(self) -> str:
        return self._base_url

    @property
    def use_sse(self) -> bool:
        return self._entry.options.get(OPT_USE_SSE, DEFAULT_USE_SSE)

    @property
    def poll_interval(self) -> int:
        return int(self._entry.options.get(OPT_POLL_INTERVAL, DEFAULT_POLL_INTERVAL))

    def add_zone_listener(self, cb: Callable[[set[str]], None]) -> Callable[[], None]:
        """Register a callback fired with the new zone-slug set on changes."""
        self._zone_listeners.append(cb)
        # Fire once with the current set so a late-joining platform sees today's zones.
        if self._known_zones:
            cb(set(self._known_zones))

        def _remove() -> None:
            try:
                self._zone_listeners.remove(cb)
            except ValueError:
                pass

        return _remove

    # ---- setup + teardown ----

    async def fetch_info(self) -> dict[str, Any]:
        url = f"{self._base_url}{API_PREFIX}/info"
        async with self._session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
            r.raise_for_status()
            self.info = await r.json()
            return self.info

    async def async_start(self) -> None:
        """Kick off SSE consumers + forecast poller. Polling fallback
        runs through DataUpdateCoordinator's update_interval when SSE
        is disabled in options."""
        # Seed an initial snapshot in either mode so entities can paint
        # immediately rather than wait for the first SSE event.
        await self.async_config_entry_first_refresh()

        if self.use_sse:
            # SSE drives the irrigation + tempest channels. Forecast is
            # polled. update_interval stays None so DataUpdateCoordinator
            # doesn't double-poll.
            self.update_interval = None
            self._sse_tasks.append(
                self.hass.loop.create_task(self._sse_loop("/irrigation/stream", "irrigation"))
            )
            self._sse_tasks.append(
                self.hass.loop.create_task(self._sse_loop("/stream", "tempest"))
            )
            self._forecast_task = self.hass.loop.create_task(self._forecast_poll_loop())
        else:
            # Pure polling — DataUpdateCoordinator handles the cadence.
            self.update_interval = timedelta(seconds=self.poll_interval)
            await self.async_request_refresh()

    async def async_stop(self) -> None:
        """Cancel background tasks. Idempotent."""
        for t in self._sse_tasks:
            t.cancel()
        if self._forecast_task is not None:
            self._forecast_task.cancel()
        await asyncio.gather(
            *(self._sse_tasks + ([self._forecast_task] if self._forecast_task else [])),
            return_exceptions=True,
        )
        self._sse_tasks.clear()
        self._forecast_task = None

    # ---- internal: polling fallback ----

    async def _async_update_data(self) -> dict[str, Any]:
        """Full snapshot refresh — used as fallback when SSE is off and
        as the initial-paint fetch on startup."""
        try:
            tempest, irrigation, forecast = await asyncio.gather(
                self._fetch("/snapshot"),
                self._fetch("/irrigation/snapshot"),
                self._fetch("/forecast/snapshot"),
                return_exceptions=False,
            )
        except aiohttp.ClientError as err:
            raise UpdateFailed(f"LocalSky API error: {err}") from err
        except asyncio.TimeoutError as err:
            raise UpdateFailed("LocalSky API timeout") from err

        merged = {"tempest": tempest, "irrigation": irrigation, "forecast": forecast}
        self._notify_zone_changes(merged)
        return merged

    async def _fetch(self, path: str) -> dict[str, Any]:
        url = f"{self._base_url}{API_PREFIX}{path}"
        async with self._session.get(
            url, timeout=aiohttp.ClientTimeout(total=15)
        ) as r:
            r.raise_for_status()
            return await r.json()

    # ---- internal: SSE consumers ----

    async def _sse_loop(self, path: str, kind: str) -> None:
        """Persistent SSE consumer with exponential backoff on disconnects."""
        url = f"{self._base_url}{API_PREFIX}{path}"
        backoff = SSE_BACKOFF_INITIAL
        while True:
            try:
                # sock_read=None lets the connection live indefinitely;
                # the keep-alive pings from LocalSky's Sse::keep_alive
                # produce ":\n\n" comments every 15s which we ignore.
                async with self._session.get(
                    url,
                    timeout=aiohttp.ClientTimeout(
                        total=None, sock_connect=10, sock_read=None
                    ),
                    headers={"Accept": "text/event-stream"},
                ) as resp:
                    resp.raise_for_status()
                    backoff = SSE_BACKOFF_INITIAL
                    async for snapshot in self._iter_sse_events(resp):
                        self._merge_and_publish(kind, snapshot)
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001 - any failure → reconnect
                _LOGGER.warning(
                    "LocalSky SSE %s disconnected: %s. Retrying in %.1fs",
                    path, err, backoff,
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, SSE_BACKOFF_MAX)

    async def _iter_sse_events(self, resp: aiohttp.ClientResponse):
        """Minimal SSE parser. Yields JSON-decoded `data` payloads.

        LocalSky only emits event:snapshot frames carrying the full
        snapshot in the data line, so we don't need to track event
        names or multi-line data buffers — but we do handle multi-line
        data per the SSE spec to stay correct if the protocol expands.
        """
        data_buf: list[str] = []
        async for raw in resp.content:
            line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            if line == "":
                # Dispatch on blank line.
                if data_buf:
                    try:
                        yield json.loads("\n".join(data_buf))
                    except json.JSONDecodeError:
                        _LOGGER.debug("Discarding malformed SSE frame: %r", data_buf)
                    data_buf = []
                continue
            if line.startswith(":"):
                # Keep-alive comment.
                continue
            if line.startswith("data:"):
                data_buf.append(line[5:].lstrip())

    def _merge_and_publish(self, kind: str, snapshot: dict[str, Any]) -> None:
        merged = dict(self.data) if self.data else {}
        merged[kind] = snapshot
        if kind == "irrigation":
            self._notify_zone_changes(merged)
        self.async_set_updated_data(merged)

    # ---- internal: forecast poller ----

    async def _forecast_poll_loop(self) -> None:
        while True:
            try:
                forecast = await self._fetch("/forecast/snapshot")
                merged = dict(self.data) if self.data else {}
                merged["forecast"] = forecast
                self.async_set_updated_data(merged)
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("Forecast poll failed (will retry): %s", err)
            await asyncio.sleep(FORECAST_REFRESH.total_seconds())

    # ---- dynamic entity registration ----

    def _notify_zone_changes(self, data: dict[str, Any]) -> None:
        irrigation = data.get("irrigation") or {}
        slugs = {
            z.get("slug")
            for z in irrigation.get("zones", [])
            if isinstance(z, dict) and z.get("slug")
        }
        if slugs == self._known_zones:
            return
        self._known_zones = slugs
        for cb in list(self._zone_listeners):
            try:
                cb(set(slugs))
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Zone listener raised")

    # ---- action dispatch ----

    async def dispatch_action(self, payload: dict[str, Any]) -> None:
        """POST to /api/v1/irrigation/action. Used by valve/number/switch
        entities + integration services."""
        url = f"{self._base_url}{API_PREFIX}/irrigation/action"
        async with self._session.post(
            url,
            json=payload,
            timeout=aiohttp.ClientTimeout(total=15),
        ) as r:
            r.raise_for_status()
        # Force a refresh so state updates fast when SSE isn't carrying it.
        if not self.use_sse:
            await self.async_request_refresh()
