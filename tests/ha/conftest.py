"""Fixtures for the Home Assistant integration tests."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Generator

import pytest
from growatt_protocol.testing import FakeDatalogger, FakeInverter
from homeassistant.const import CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.growatt_datalogger.const import DOMAIN

SERIAL = "GPG0EXAMP1"
INVERTER = "SML0EXAMP2"


async def settle(hass: HomeAssistant, times: int = 3) -> None:
    """Let the server read a record and Home Assistant finish reacting to it.

    A real socket sits between the fake device and the integration, so there is nothing
    to await: the record has to cross the loop before ``async_block_till_done`` has
    anything to block on. Hence a sleep, and hence it living here rather than being
    retyped in every test file with its own idea of how long is long enough.
    """
    for _ in range(times):
        await asyncio.sleep(0.05)
        await hass.async_block_till_done()


def device_id(hass: HomeAssistant, entry: MockConfigEntry, key: str) -> str:
    """The Home Assistant device id for one of ours, by our own device key.

    ``async_get_device_by_identifier`` rather than ``async_get_device``: the latter is
    deprecated because identifiers are only unique *within* a config entry, and calling
    it from test code raises rather than warns.
    """
    entry_ = dr.async_get(hass).async_get_device_by_identifier((DOMAIN, key), entry.entry_id)
    assert entry_ is not None, f"no device registered for {key}"
    return entry_.id


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(
    enable_custom_integrations: None,
) -> Generator[None]:
    """Let Home Assistant load this repository's custom_components directory."""
    yield


@pytest.fixture
def config_entry() -> MockConfigEntry:
    # Port 0 asks the OS to allocate one, so the suite never collides with a real
    # service or with a parallel test run on 5279.
    return MockConfigEntry(domain=DOMAIN, data={CONF_PORT: 0}, title="Growatt")


@pytest.fixture
async def setup_integration(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> AsyncIterator[MockConfigEntry]:
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    yield config_entry


@pytest.fixture
async def device(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> AsyncIterator[FakeDatalogger]:
    """A fake datalogger already connected to the running integration."""
    logger = FakeDatalogger()
    await logger.connect("127.0.0.1", setup_integration.runtime_data.port)
    try:
        yield logger
    finally:
        await logger.close()


@pytest.fixture
async def inverter(device: FakeDatalogger) -> AsyncIterator[FakeInverter]:
    """A device that answers register commands in the background.

    Most tests want a working device rather than to play one by hand: entities read
    their registers back as they are added, and a command nobody answers holds the
    session's command lock until it times out.
    """
    async with FakeInverter(device) as fake:
        yield fake
