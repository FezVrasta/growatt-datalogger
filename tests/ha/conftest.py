"""Fixtures for the Home Assistant integration tests."""

from __future__ import annotations

from collections.abc import AsyncIterator, Generator

import pytest
from homeassistant.const import CONF_PORT
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.growatt_datalogger.const import DOMAIN
from tests.fakes.datalogger import FakeDatalogger


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
