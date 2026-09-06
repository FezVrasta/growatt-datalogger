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


def device_entry(hass: HomeAssistant, entry: MockConfigEntry, key: str) -> dr.DeviceEntry:
    """The registered device for one of our device keys.

    Filtering the config entry's own devices, deliberately. The obvious call,
    ``async_get_device``, is deprecated -- identifiers are only unique *within* a config
    entry -- and raises rather than warns from test code. Its replacement,
    ``async_get_device_by_identifier``, does not exist before Home Assistant 2026.8, and
    ``registry.devices`` is a mapping on one version and an iterable of entries on the
    other. ``async_entries_for_config_entry`` is the one spelling that has meant the same
    thing throughout, and the suite has to pass on the oldest version this integration
    supports as well as the newest.
    """
    registry = dr.async_get(hass)
    for device in dr.async_entries_for_config_entry(registry, entry.entry_id):
        if (DOMAIN, key) in device.identifiers:
            return device
    raise AssertionError(f"no device registered for {key}")


def device_id(hass: HomeAssistant, entry: MockConfigEntry, key: str) -> str:
    return device_entry(hass, entry, key).id


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
