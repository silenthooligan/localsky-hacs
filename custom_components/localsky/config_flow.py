"""Config flow for LocalSky.

Pairing paths:
  - zeroconf: LocalSky announces ``_localsky._tcp.local.`` with a stable
    ``uuid`` TXT record; discovery dedupes on it and prefills host/port.
  - user: manual host/port entry.

Both probe ``GET /api/v1/info`` first. When the instance reports
``auth_required`` (LocalSky 0.2.0+ with an owner account), an extra step
asks for an API token (created in LocalSky under Settings -> Account)
and validates it against ``GET /api/v1/auth/session``. A 401 later in
the coordinator triggers the reauth flow to swap in a fresh token.
"""
from __future__ import annotations

import logging
from typing import Any

import aiohttp
import voluptuous as vol
from awesomeversion import AwesomeVersion, AwesomeVersionException
from homeassistant import config_entries
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.data_entry_flow import AbortFlow, FlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo

from .const import (
    API_PREFIX,
    CONF_API_TOKEN,
    CONF_USE_HTTPS,
    DEFAULT_POLL_INTERVAL,
    DEFAULT_PORT,
    DEFAULT_RUN_SECONDS,
    DEFAULT_USE_HTTPS,
    DEFAULT_USE_SSE,
    DOMAIN,
    MIN_API_VERSION,
    MIN_SERVICE_VERSION,
    OPT_DEFAULT_RUN_SECONDS,
    OPT_POLL_INTERVAL,
    OPT_USE_SSE,
)
from .util import format_base_url

_LOGGER = logging.getLogger(__name__)

USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): str,
        vol.Optional(CONF_PORT, default=DEFAULT_PORT): int,
        vol.Optional(CONF_USE_HTTPS, default=DEFAULT_USE_HTTPS): bool,
    }
)

AUTH_SCHEMA = vol.Schema({vol.Required(CONF_API_TOKEN): str})


async def _probe(
    session: aiohttp.ClientSession, host: str, port: int, use_https: bool
) -> dict[str, Any]:
    """GET /api/v1/info. Validates connectivity + returns service info."""
    url = f"{format_base_url(host, port, use_https)}{API_PREFIX}/info"
    async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
        r.raise_for_status()
        return await r.json()


async def _validate_token(
    session: aiohttp.ClientSession,
    host: str,
    port: int,
    use_https: bool,
    token: str,
) -> bool:
    """True when the API token authenticates against /auth/session."""
    url = f"{format_base_url(host, port, use_https)}{API_PREFIX}/auth/session"
    async with session.get(
        url,
        timeout=aiohttp.ClientTimeout(total=10),
        headers={"Authorization": f"Bearer {token}"},
    ) as r:
        return r.status == 200


def _version_ok(reported: Any, minimum: str) -> bool:
    """Return True if `reported` parses as a version and is >= `minimum`.

    Pre-release / build suffixes are stripped before comparison so e.g.
    ``0.2.0-alpha.1`` satisfies ``>= 0.2.0`` — the SemVer ordering would
    treat the suffix as "earlier than", but for our compatibility-floor
    purposes we want to accept anything in the 0.2.0 family.

    Unknown / unparseable versions fail closed: we'd rather surface a
    clear error in the config flow than silently pair against an instance
    that may not implement the endpoints this integration calls.
    """
    if not isinstance(reported, str) or not reported:
        return False
    base = reported.split("-")[0].split("+")[0]
    try:
        return AwesomeVersion(base) >= AwesomeVersion(minimum)
    except AwesomeVersionException:
        return False


def _info_error(info: dict[str, Any]) -> str | None:
    """Compatibility gate shared by every pairing path."""
    if info.get("service") != "localsky":
        return "not_localsky"
    if not _version_ok(info.get("service_version"), MIN_SERVICE_VERSION):
        return "service_too_old"
    if not _version_ok(info.get("api_version"), MIN_API_VERSION):
        return "api_too_old"
    return None


class LocalSkyConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle the LocalSky integration setup flow."""

    VERSION = 1

    def __init__(self) -> None:
        self._host: str = ""
        self._port: int = DEFAULT_PORT
        self._use_https: bool = DEFAULT_USE_HTTPS
        self._info: dict[str, Any] = {}

    @staticmethod
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> "LocalSkyOptionsFlow":
        return LocalSkyOptionsFlow(config_entry)

    # ---- shared helpers ----

    def _adopt_legacy_entry(self, unique_id: str) -> bool:
        """Claim `unique_id` for an entry from before uuid identity.

        Entries created by pre-0.6 flows are keyed host:port, so the
        uuid dedupe above never matches them and discovery keeps
        offering an already-configured instance as new. Match on
        host+port instead and rewrite the entry's unique_id in place;
        everything else about the entry (and therefore every entity id)
        stays untouched.
        """
        for entry in self._async_current_entries(include_ignore=False):
            if entry.unique_id == unique_id:
                continue
            if (
                entry.data.get(CONF_HOST) == self._host
                and entry.data.get(CONF_PORT, DEFAULT_PORT) == self._port
            ):
                self.hass.config_entries.async_update_entry(
                    entry, unique_id=unique_id
                )
                _LOGGER.info(
                    "Adopted instance uuid onto existing LocalSky entry for %s:%s",
                    self._host,
                    self._port,
                )
                return True
        return False

    async def _set_unique_id_from_info(self) -> None:
        """Prefer the stable instance uuid; fall back to host:port."""
        unique_id = self._info.get("uuid") or f"{self._host}:{self._port}"
        await self.async_set_unique_id(str(unique_id))
        self._abort_if_unique_id_configured(
            updates={CONF_HOST: self._host, CONF_PORT: self._port}
        )
        if self._adopt_legacy_entry(str(unique_id)):
            raise AbortFlow("already_configured")

    def _entry_data(self, token: str | None) -> dict[str, Any]:
        data: dict[str, Any] = {
            CONF_HOST: self._host,
            CONF_PORT: self._port,
            CONF_USE_HTTPS: self._use_https,
        }
        if token:
            data[CONF_API_TOKEN] = token
        return data

    def _create(self, token: str | None) -> FlowResult:
        version = self._info.get("service_version", "LocalSky")
        return self.async_create_entry(
            title=f"LocalSky ({self._host})",
            data=self._entry_data(token),
            description_placeholders={"version": version},
        )

    # ---- user-initiated pairing ----

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            self._host = user_input[CONF_HOST].strip()
            self._port = int(user_input.get(CONF_PORT, DEFAULT_PORT))
            self._use_https = bool(user_input.get(CONF_USE_HTTPS, DEFAULT_USE_HTTPS))

            session = async_get_clientsession(self.hass)
            try:
                self._info = await _probe(
                    session, self._host, self._port, self._use_https
                )
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
                if (reason := _info_error(self._info)) is not None:
                    errors["base"] = reason
                else:
                    await self._set_unique_id_from_info()
                    if self._info.get("auth_required"):
                        return await self.async_step_auth()
                    return self._create(None)

        return self.async_show_form(
            step_id="user",
            data_schema=USER_SCHEMA,
            errors=errors,
            description_placeholders={"default_port": str(DEFAULT_PORT)},
        )

    # ---- API token step (auth-required instances) ----

    async def async_step_auth(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            token = user_input[CONF_API_TOKEN].strip()
            session = async_get_clientsession(self.hass)
            try:
                valid = await _validate_token(
                    session, self._host, self._port, self._use_https, token
                )
            except (aiohttp.ClientError, TimeoutError, OSError) as err:
                _LOGGER.warning("LocalSky token validation failed: %s", err)
                errors["base"] = "cannot_connect"
            else:
                if valid:
                    return self._create(token)
                errors["base"] = "invalid_auth"

        return self.async_show_form(
            step_id="auth",
            data_schema=AUTH_SCHEMA,
            errors=errors,
            description_placeholders={"host": self._host},
        )

    # ---- zeroconf discovery ----

    async def async_step_zeroconf(
        self, discovery_info: ZeroconfServiceInfo
    ) -> FlowResult:
        host = str(discovery_info.host)
        port = discovery_info.port or DEFAULT_PORT
        props = discovery_info.properties or {}

        self._host = host
        self._port = int(port)
        self._use_https = False

        # Dedupe on the announced uuid before any network round-trip.
        if uuid := props.get("uuid"):
            await self.async_set_unique_id(str(uuid))
            self._abort_if_unique_id_configured(
                updates={CONF_HOST: self._host, CONF_PORT: self._port}
            )
            if self._adopt_legacy_entry(str(uuid)):
                return self.async_abort(reason="already_configured")

        session = async_get_clientsession(self.hass)
        try:
            self._info = await _probe(session, self._host, self._port, False)
        except (aiohttp.ClientError, TimeoutError, OSError):
            return self.async_abort(reason="cannot_connect")
        if _info_error(self._info) is not None:
            return self.async_abort(reason="not_localsky")
        if not props.get("uuid"):
            await self._set_unique_id_from_info()

        self.context["title_placeholders"] = {"host": self._host}
        return await self.async_step_zeroconf_confirm()

    async def async_step_zeroconf_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        if user_input is not None:
            if self._info.get("auth_required"):
                return await self.async_step_auth()
            return self._create(None)
        return self.async_show_form(
            step_id="zeroconf_confirm",
            description_placeholders={
                "host": self._host,
                "version": str(self._info.get("service_version", "?")),
            },
        )

    # ---- reauth (coordinator saw a 401) ----

    async def async_step_reauth(
        self, entry_data: dict[str, Any]
    ) -> FlowResult:
        self._host = entry_data.get(CONF_HOST, "")
        self._port = int(entry_data.get(CONF_PORT, DEFAULT_PORT))
        self._use_https = bool(entry_data.get(CONF_USE_HTTPS, DEFAULT_USE_HTTPS))
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            token = user_input[CONF_API_TOKEN].strip()
            session = async_get_clientsession(self.hass)
            try:
                valid = await _validate_token(
                    session, self._host, self._port, self._use_https, token
                )
            except (aiohttp.ClientError, TimeoutError, OSError):
                errors["base"] = "cannot_connect"
            else:
                if valid:
                    entry = self._get_reauth_entry()
                    return self.async_update_reload_and_abort(
                        entry,
                        data={**entry.data, CONF_API_TOKEN: token},
                    )
                errors["base"] = "invalid_auth"

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=AUTH_SCHEMA,
            errors=errors,
            description_placeholders={"host": self._host},
        )


class LocalSkyOptionsFlow(config_entries.OptionsFlow):
    """User-tunable options surfaced on the integration card."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        # HA 2024.11+ deprecates assigning self.config_entry directly;
        # the base class exposes self.config_entry via the entry context.
        self._entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        opts = self._entry.options
        schema = vol.Schema(
            {
                vol.Optional(
                    OPT_USE_SSE,
                    default=opts.get(OPT_USE_SSE, DEFAULT_USE_SSE),
                ): bool,
                vol.Optional(
                    OPT_POLL_INTERVAL,
                    default=opts.get(OPT_POLL_INTERVAL, DEFAULT_POLL_INTERVAL),
                ): vol.All(vol.Coerce(int), vol.Range(min=5, max=600)),
                vol.Optional(
                    OPT_DEFAULT_RUN_SECONDS,
                    default=opts.get(OPT_DEFAULT_RUN_SECONDS, DEFAULT_RUN_SECONDS),
                ): vol.All(vol.Coerce(int), vol.Range(min=60, max=7200)),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
