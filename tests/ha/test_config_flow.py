"""Config and options flow."""

from __future__ import annotations

import asyncio

from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.growatt_datalogger.const import (
    BUFFERED_IGNORE,
    CONF_BUFFERED_POLICY,
    CONF_INCLUDE_UNKNOWN,
    DEFAULT_PORT,
    DOMAIN,
)


async def _free_port() -> int:
    """Ask the OS for an unused port, then release it.

    The flow validates with cv.port, which rejects 0, so the test cannot lean on the
    usual bind-to-zero trick and needs a concrete number.
    """
    server = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
    port = int(server.sockets[0].getsockname()[1])
    server.close()
    await server.wait_closed()
    return port


async def test_user_flow_creates_an_entry(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    port = await _free_port()
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_PORT: port})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {CONF_PORT: port}


async def test_the_default_port_is_the_growatt_one(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["data_schema"]({})[CONF_PORT] == DEFAULT_PORT


async def test_port_in_use_is_reported_on_the_form(hass: HomeAssistant) -> None:
    """Fail at configuration time rather than after the entry exists."""
    blocker = await asyncio.start_server(lambda r, w: None, "0.0.0.0", 0)
    port = blocker.sockets[0].getsockname()[1]

    try:
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_PORT: port}
        )

        assert result["type"] is FlowResultType.FORM
        assert result["errors"] == {CONF_PORT: "port_in_use"}
    finally:
        blocker.close()
        await blocker.wait_closed()


async def test_only_one_entry_is_allowed(hass: HomeAssistant) -> None:
    """Two entries would fight over the same port."""
    MockConfigEntry(domain=DOMAIN, data={CONF_PORT: DEFAULT_PORT}).add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "single_instance_allowed"


async def test_options_flow(hass: HomeAssistant, setup_integration: MockConfigEntry) -> None:
    result = await hass.config_entries.options.async_init(setup_integration.entry_id)
    assert result["type"] is FlowResultType.FORM

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_INCLUDE_UNKNOWN: True, CONF_BUFFERED_POLICY: BUFFERED_IGNORE},
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert setup_integration.options[CONF_INCLUDE_UNKNOWN] is True
    assert setup_integration.options[CONF_BUFFERED_POLICY] == BUFFERED_IGNORE
