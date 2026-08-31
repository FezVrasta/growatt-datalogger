"""Config entry lifecycle and end-to-end entity creation."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from growatt_protocol.testing import FakeDatalogger
from growatt_protocol.testing.frames import build_group
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.growatt_datalogger.const import (
    CONNECTIVITY_GRACE,
    CONNECTIVITY_INTERVAL,
    DOMAIN,
    KIND_DATALOGGER,
)
from custom_components.growatt_datalogger.hub import device_key


async def _settle(hass: HomeAssistant, times: int = 3) -> None:
    """Let the socket handler and the resulting state writes drain."""
    for _ in range(times):
        await asyncio.sleep(0.05)
        await hass.async_block_till_done()


async def test_setup_and_unload(hass: HomeAssistant, setup_integration: MockConfigEntry) -> None:
    assert setup_integration.state is ConfigEntryState.LOADED
    assert setup_integration.runtime_data.port > 0

    assert await hass.config_entries.async_unload(setup_integration.entry_id)
    await hass.async_block_till_done()
    assert setup_integration.state is ConfigEntryState.NOT_LOADED


async def test_unload_releases_the_port(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """A leaked listener would make the integration un-setupable until a restart."""
    port = setup_integration.runtime_data.port

    assert await hass.config_entries.async_unload(setup_integration.entry_id)
    await hass.async_block_till_done()

    probe = await asyncio.start_server(lambda r, w: None, "127.0.0.1", port)
    probe.close()
    await probe.wait_closed()


async def test_port_in_use_is_retryable(hass: HomeAssistant, config_entry: MockConfigEntry) -> None:
    """Bound port -> SETUP_RETRY, so a reload racing TIME_WAIT recovers by itself."""
    blocker = await asyncio.start_server(lambda r, w: None, "0.0.0.0", 0)
    port = blocker.sockets[0].getsockname()[1]

    entry = MockConfigEntry(domain=DOMAIN, data={CONF_PORT: port})
    entry.add_to_hass(hass)
    try:
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert entry.state is ConfigEntryState.SETUP_RETRY
    finally:
        blocker.close()
        await blocker.wait_closed()


async def test_a_record_creates_devices_and_sensors(
    hass: HomeAssistant, setup_integration: MockConfigEntry, device: FakeDatalogger
) -> None:
    await device.send_data(groups=[build_group(3000, [1, 0, 25850, 3295, 7, 0, 2585])])
    await _settle(hass)

    entity_registry = er.async_get(hass)
    entries = er.async_entries_for_config_entry(entity_registry, setup_integration.entry_id)
    assert entries, "no entities were created"

    unique_ids = {entry.unique_id for entry in entries}
    assert f"{DOMAIN}_inverter:SML0EXAMP2_input_1_voltage" in unique_ids
    assert f"{DOMAIN}_logger:GPG0EXAMP1_connected" in unique_ids


async def test_device_topology_uses_via_device(
    hass: HomeAssistant, setup_integration: MockConfigEntry, device: FakeDatalogger
) -> None:
    """The inverter hangs off the datalogger, so losing the logger greys the branch."""
    await device.send_data()
    await _settle(hass)

    registry = dr.async_get(hass)
    logger = registry.async_get_device({(DOMAIN, "logger:GPG0EXAMP1")})
    inverter = registry.async_get_device({(DOMAIN, "inverter:SML0EXAMP2")})

    assert logger is not None
    assert inverter is not None
    assert inverter.via_device_id == logger.id


async def test_decoded_values_reach_entity_state(
    hass: HomeAssistant, setup_integration: MockConfigEntry, device: FakeDatalogger
) -> None:
    # register 3003 is PV1 voltage in tenths of a volt.
    await device.send_data(groups=[build_group(3000, [1, 0, 0, 3295])])
    await _settle(hass)

    entity_registry = er.async_get(hass)
    entity_id = entity_registry.async_get_entity_id(
        "sensor", DOMAIN, f"{DOMAIN}_inverter:SML0EXAMP2_input_1_voltage"
    )
    assert entity_id is not None

    state = hass.states.get(entity_id)
    assert state is not None
    assert float(state.state) == 329.5


async def test_a_second_inverter_appears_without_a_reload(
    hass: HomeAssistant, setup_integration: MockConfigEntry, device: FakeDatalogger
) -> None:
    await device.send_data()
    await _settle(hass)

    other = FakeDatalogger(datalogger_serial="GPG0OTHER1", inverter_serial="SML0OTHER2")
    await other.connect("127.0.0.1", setup_integration.runtime_data.port)
    try:
        await other.send_data()
        await _settle(hass)

        registry = dr.async_get(hass)
        assert registry.async_get_device({(DOMAIN, "inverter:SML0OTHER2")}) is not None
    finally:
        await other.close()


async def _connectivity_entity(hass: HomeAssistant) -> str:
    entity_id = er.async_get(hass).async_get_entity_id(
        "binary_sensor", DOMAIN, f"{DOMAIN}_logger:GPG0EXAMP1_connected"
    )
    assert entity_id is not None
    return entity_id


async def test_connectivity_survives_the_reconnect_gap(
    hass: HomeAssistant, setup_integration: MockConfigEntry, device: FakeDatalogger
) -> None:
    """Hanging up is normal, not an outage.

    A datalogger uploads and closes the socket, over and over. Real hardware was
    observed doing it dozens of times an hour while delivering a record every nine
    seconds throughout, so a sensor that followed the socket would have been alarming
    constantly about a device that was working perfectly.
    """
    await device.send_data()
    await _settle(hass)

    entity_id = await _connectivity_entity(hass)
    assert hass.states.get(entity_id).state == "on"

    await device.close()
    await _settle(hass, times=5)
    assert hass.states.get(entity_id).state == "on"


async def test_connectivity_goes_off_after_prolonged_silence(
    hass: HomeAssistant, setup_integration: MockConfigEntry, device: FakeDatalogger
) -> None:
    """The grace period has to end, and end without anything pushing to end it.

    Ages the record rather than freezing the clock: freezegun patches
    ``time.monotonic``, which is the asyncio event loop's own clock, so a frozen test
    deadlocks the socket server the moment anything awaits.
    """
    await device.send_data()
    await _settle(hass)
    entity_id = await _connectivity_entity(hass)

    await device.close()
    await _settle(hass)
    assert hass.states.get(entity_id).state == "on"

    hub = setup_integration.runtime_data
    logger = hub.devices[device_key(KIND_DATALOGGER, "GPG0EXAMP1")]
    logger.last_record = dt_util.utcnow() - (CONNECTIVITY_GRACE + timedelta(minutes=1))

    # Nothing arrives to announce the silence, so only the interval timer can notice it.
    async_fire_time_changed(hass, dt_util.utcnow() + CONNECTIVITY_INTERVAL)
    await hass.async_block_till_done()

    assert hass.states.get(entity_id).state == "off"


async def test_buffered_records_fire_an_event_and_do_not_touch_live_state(
    hass: HomeAssistant, setup_integration: MockConfigEntry, device: FakeDatalogger
) -> None:
    """They carry a past timestamp; merging them would invent power spikes."""
    events = []
    hass.bus.async_listen(f"{DOMAIN}_buffered_record", events.append)

    await device.send_data(groups=[build_group(3000, [1, 0, 0, 3295])])
    await _settle(hass)

    entity_registry = er.async_get(hass)
    entity_id = entity_registry.async_get_entity_id(
        "sensor", DOMAIN, f"{DOMAIN}_inverter:SML0EXAMP2_input_1_voltage"
    )
    live = hass.states.get(entity_id).state

    await device.send_buffered(groups=[build_group(3000, [1, 0, 0, 9999])])
    await _settle(hass)

    assert hass.states.get(entity_id).state == live, "buffered data overwrote live state"
    assert len(events) == 1
    assert events[0].data["values"]["input_1_voltage"] == 999.9


async def test_devices_survive_a_restart_before_any_packet(
    hass: HomeAssistant, setup_integration: MockConfigEntry, device: FakeDatalogger
) -> None:
    """Without this, a restart after sunset empties every dashboard until sunrise."""
    await device.send_data()
    await _settle(hass)
    await device.close()

    # Force the debounced store write out, then reload.
    await setup_integration.runtime_data.async_flush_storage()
    await hass.config_entries.async_reload(setup_integration.entry_id)
    await hass.async_block_till_done()

    registry = dr.async_get(hass)
    assert registry.async_get_device({(DOMAIN, "inverter:SML0EXAMP2")}) is not None


@pytest.mark.parametrize("chunk_size", [1, 5])
async def test_fragmented_records_still_produce_state(
    hass: HomeAssistant, setup_integration: MockConfigEntry, chunk_size: int
) -> None:
    logger = FakeDatalogger(chunk_size=chunk_size)
    await logger.connect("127.0.0.1", setup_integration.runtime_data.port)
    try:
        await logger.send_data(groups=[build_group(3000, [1, 0, 0, 3295])])
        await _settle(hass, times=5)

        entity_registry = er.async_get(hass)
        entity_id = entity_registry.async_get_entity_id(
            "sensor", DOMAIN, f"{DOMAIN}_inverter:SML0EXAMP2_input_1_voltage"
        )
        assert entity_id is not None
        assert float(hass.states.get(entity_id).state) == 329.5
    finally:
        await logger.close()


async def test_an_announce_does_not_wipe_the_telemetry(
    hass: HomeAssistant, setup_integration: MockConfigEntry, device: FakeDatalogger
) -> None:
    """The two record kinds feed the same device from different register spaces.

    A telemetry record carries input registers; an announce carries holding registers.
    Publishing either as a wholesale replacement means each wipes the other, and since a
    datalogger announces on every reconnect, the visible symptom is telemetry that
    populates once and then goes 'unknown' for good.
    """
    await device.send_data(groups=[build_group(3000, [1, 0, 0, 3295])])
    await _settle(hass)

    entity_registry = er.async_get(hass)
    entity_id = entity_registry.async_get_entity_id(
        "sensor", DOMAIN, f"{DOMAIN}_inverter:SML0EXAMP2_input_1_voltage"
    )
    assert entity_id is not None
    assert float(hass.states.get(entity_id).state) == 329.5

    # An announce arrives, as it does on every reconnect.
    await device.send_announce(groups=[build_group(0, [1, 0, 0])])
    await _settle(hass)

    assert hass.states.get(entity_id).state != "unknown", "the announce wiped telemetry"
    assert float(hass.states.get(entity_id).state) == 329.5


async def test_only_telemetry_decides_the_profile(
    hass: HomeAssistant, setup_integration: MockConfigEntry, device: FakeDatalogger
) -> None:
    """An announce must not change which profile a device is decoded with.

    A profile describes the input registers. An announce carries holding registers over
    ranges that are not comparable -- a string inverter's announce includes a 3125+
    group, which looks exactly like a hybrid's battery block. Letting it vote makes the
    profile flip on every reconnect and creates battery entities for a device that has
    no battery.
    """
    await device.send_data(groups=[build_group(3000, [1, 0, 0, 3295])])
    await _settle(hass)

    hub = setup_integration.runtime_data
    inverter = hub.devices["inverter:SML0EXAMP2"]
    assert inverter.profile == "protocol_ii_3000"

    # An announce whose ranges would otherwise resolve to a storage profile.
    await device.send_announce(groups=[build_group(3000, [0] * 125), build_group(3125, [0] * 125)])
    await _settle(hass)

    assert inverter.profile == "protocol_ii_3000", "the announce changed the profile"
