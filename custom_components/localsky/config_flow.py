"""Config flow for LocalSky."""
from __future__ import annotations

import logging
from typing import Any

import aiohttp
import voluptuous as vol
from awesomeversion import AwesomeVersion, AwesomeVersionException
from homeassistant import config_entries
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    API_PREFIX,
    CONF_USE_HTTPS,
    DEFAULT_PORT,
    DEFAULT_USE_HTTPS,
    DOMAIN,
    MIN_API_VERSION,
    MIN_SERVICE_VERSION,
)

_LOGGER = logging.getLogger(__name__)

USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): str,
        vol.Optional(CONF_PORT, default=DEFAULT_PORT): int,
        vol.Optional(CONF_USE_HTTPS, default=DEFAULT_USE_HTTPS): bool,
    }
)


async def _probe(
    session: aiohttp.ClientSession, host: str, port: int, use_https: bool
) -> dict[str, Any]:
    """GET /api/v1/info. Validates connectivity + returns service info."""
    scheme = "https" if use_https else "http"
    url = f"{scheme}://{host}:{port}{API_PREFIX}/info"
    async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
        r.raise_for_status()
        return await r.json()


def _version_ok(reported: Any, minimum: str) -> bool:
    """Return True if `reported` parses as a version and is >= `minimum`.

    Unknown / unparseable versions fail closed: we'd rather surface a
    clear error in the config flow than silently pair against an instance
    that may not implement the endpoints this integration calls.
    """
    if not isinstance(reported, str) or not reported:
        return False
    try:
        return AwesomeVersion(reported) >= AwesomeVersion(minimum)
    except AwesomeVersionException:
        return False


class LocalSkyConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle the LocalSky integration setup flow."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            host = user_input[CONF_HOST].strip()
            port = int(user_input.get(CONF_PORT, DEFAULT_PORT))
            use_https = bool(user_input.get(CONF_USE_HTTPS, DEFAULT_USE_HTTPS))

            session = async_get_clientsession(self.hass)
            try:
                info = await _probe(session, host, port, use_https)
            except aiohttp.ClientResponseError as err:
                _LOGGER.warning("LocalSky probe HTTP %s: %s", err.status, err.message)
                errors["base"] = "cannot_connect"
            except aiohttp.ClientError as err:
                _LOGGER.warning("LocalSky probe failed: %s", err)
                errors["base"] = "cannot_connect"
            except (TimeoutError, OSError) as err:
                _LOGGER.warning("LocalSky probe timed out: %s", err)
                errors["base"] = "cannot_connect"
            except Exception as err:  # noqa: BLE001
                _LOGGER.exception("Unexpected error during LocalSky probe: %s", err)
                errors["base"] = "unknown"
            else:
                if info.get("service") != "localsky":
                    errors["base"] = "not_localsky"
                elif not _version_ok(
                    info.get("service_version"), MIN_SERVICE_VERSION
                ):
                    errors["base"] = "service_too_old"
                elif not _version_ok(info.get("api_version"), MIN_API_VERSION):
                    errors["base"] = "api_too_old"
                else:
                    # Unique ID: host:port. Lets a single HA instance pair
                    # against multiple LocalSky deployments (test bed + prod).
                    unique_id = f"{host}:{port}"
                    await self.async_set_unique_id(unique_id)
                    self._abort_if_unique_id_configured()
                    title = info.get("service_version", "LocalSky")
                    return self.async_create_entry(
                        title=f"LocalSky ({host})",
                        data={
                            CONF_HOST: host,
                            CONF_PORT: port,
                            CONF_USE_HTTPS: use_https,
                        },
                        description_placeholders={"version": title},
                    )

        return self.async_show_form(
            step_id="user",
            data_schema=USER_SCHEMA,
            errors=errors,
            description_placeholders={"default_port": str(DEFAULT_PORT)},
        )
