"""Config-flow tests: manual pairing, token step, zeroconf, reauth."""
from ipaddress import ip_address
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.localsky.const import CONF_API_TOKEN, DOMAIN

from .conftest import INFO_AUTH, INFO_OPEN, INFO_TOO_OLD

USER_INPUT = {"host": "192.0.2.10", "port": 8090, "use_https": False}


def _zeroconf_info(props: dict | None = None) -> ZeroconfServiceInfo:
    return ZeroconfServiceInfo(
        ip_address=ip_address("192.0.2.10"),
        ip_addresses=[ip_address("192.0.2.10")],
        hostname="localsky.local.",
        name="LocalSky (localsky)._localsky._tcp.local.",
        port=8090,
        type="_localsky._tcp.local.",
        properties=props
        or {
            "uuid": INFO_OPEN["uuid"],
            "version": "0.2.0",
            "auth": "disabled",
        },
    )


@pytest.mark.asyncio
async def test_user_flow_open_instance(hass: HomeAssistant) -> None:
    """No-auth instance pairs straight through."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM

    with patch(
        "custom_components.localsky.config_flow._probe",
        new=AsyncMock(return_value=INFO_OPEN),
    ), patch(
        "custom_components.localsky.async_setup_entry",
        new=AsyncMock(return_value=True),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], USER_INPUT
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "LocalSky (192.0.2.10)"
    assert result["data"]["host"] == "192.0.2.10"
    assert CONF_API_TOKEN not in result["data"]
    assert result["result"].unique_id == INFO_OPEN["uuid"]


@pytest.mark.asyncio
async def test_user_flow_auth_required(hass: HomeAssistant) -> None:
    """auth_required instance demands a valid token before creating."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    with patch(
        "custom_components.localsky.config_flow._probe",
        new=AsyncMock(return_value=INFO_AUTH),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], USER_INPUT
        )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "auth"

    # Wrong token re-renders the form with invalid_auth.
    with patch(
        "custom_components.localsky.config_flow._validate_token",
        new=AsyncMock(return_value=False),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_API_TOKEN: "lsk_bad"}
        )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}

    with patch(
        "custom_components.localsky.config_flow._validate_token",
        new=AsyncMock(return_value=True),
    ), patch(
        "custom_components.localsky.async_setup_entry",
        new=AsyncMock(return_value=True),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_API_TOKEN: "lsk_good"}
        )
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_API_TOKEN] == "lsk_good"


@pytest.mark.asyncio
async def test_user_flow_rejects_old_service(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    with patch(
        "custom_components.localsky.config_flow._probe",
        new=AsyncMock(return_value=INFO_TOO_OLD),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], USER_INPUT
        )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "service_too_old"}


@pytest.mark.asyncio
async def test_zeroconf_flow(hass: HomeAssistant) -> None:
    """Discovery prefills + confirms; uuid is the unique id."""
    with patch(
        "custom_components.localsky.config_flow._probe",
        new=AsyncMock(return_value=INFO_OPEN),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": config_entries.SOURCE_ZEROCONF},
            data=_zeroconf_info(),
        )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "zeroconf_confirm"

    with patch(
        "custom_components.localsky.async_setup_entry",
        new=AsyncMock(return_value=True),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {}
        )
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["result"].unique_id == INFO_OPEN["uuid"]


@pytest.mark.asyncio
async def test_zeroconf_dedupes_on_uuid(hass: HomeAssistant) -> None:
    """A second discovery of the same uuid aborts."""
    MockConfigEntry(
        domain=DOMAIN,
        unique_id=INFO_OPEN["uuid"],
        data=USER_INPUT,
    ).add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_ZEROCONF},
        data=_zeroconf_info(),
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


@pytest.mark.asyncio
async def test_reauth_flow(hass: HomeAssistant) -> None:
    """401 path: reauth swaps in a fresh token."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=INFO_AUTH["uuid"],
        data={**USER_INPUT, CONF_API_TOKEN: "lsk_old"},
    )
    entry.add_to_hass(hass)

    result = await entry.start_reauth_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"

    with patch(
        "custom_components.localsky.config_flow._validate_token",
        new=AsyncMock(return_value=True),
    ), patch(
        "custom_components.localsky.async_setup_entry",
        new=AsyncMock(return_value=True),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_API_TOKEN: "lsk_new"}
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_API_TOKEN] == "lsk_new"


@pytest.mark.asyncio
async def test_zeroconf_adopts_legacy_host_keyed_entry(hass: HomeAssistant) -> None:
    """Pre-0.6 entries were keyed host:port; discovery adopts the uuid
    onto them instead of offering the same instance as new."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=USER_INPUT,
        unique_id="192.0.2.10:8090",
        title="LocalSky (192.0.2.10)",
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_ZEROCONF},
        data=_zeroconf_info(),
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert entry.unique_id == INFO_OPEN["uuid"]
    # The entry itself is otherwise untouched.
    assert entry.data["host"] == "192.0.2.10"


@pytest.mark.asyncio
async def test_user_flow_adopts_legacy_host_keyed_entry(hass: HomeAssistant) -> None:
    """Manually re-adding a legacy instance upgrades the existing entry
    instead of creating a duplicate."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=USER_INPUT,
        unique_id="192.0.2.10:8090",
        title="LocalSky (192.0.2.10)",
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    with patch(
        "custom_components.localsky.config_flow._probe",
        new=AsyncMock(return_value=INFO_OPEN),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], USER_INPUT
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert entry.unique_id == INFO_OPEN["uuid"]
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1
