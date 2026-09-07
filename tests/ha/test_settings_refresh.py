"""Keeping Home Assistant's picture of the inverter's settings honest.

A datalogger reports its holding space when it connects and never again, so for as long
as a connection lasts the only thing that could correct a setting shown here is the user
changing it themselves. Anything else that moves one -- someone in ShinePhone, or firmware
that accepts a change and then discards it -- goes unnoticed, and the two disagree with
nothing to resolve them. That is
https://github.com/FezVrasta/growatt-datalogger/issues/2, and asking again on a timer is
what these cover.
"""

from __future__ import annotations

from datetime import timedelta

from growatt_protocol.testing import FakeDatalogger, FakeInverter, request_register
from growatt_protocol.testing.frames import build_group
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.growatt_datalogger.const import (
    CONF_SETTINGS_INTERVAL,
    SETTINGS_MISSES,
    VALUE_HOLDING_AT,
)

from .conftest import INVERTER, settle
from .test_write_entities import PROTOCOL_II_3000, STORAGE_1000, entity

#: Comfortably past the default five-minute interval.
LATER = timedelta(minutes=6)


async def quiet(hass: HomeAssistant, inverter: FakeInverter) -> None:
    """Wait until the inverter stops being asked for anything.

    A refresh is several commands a fixed interval apart, so it outlives any single
    ``settle`` -- and a test that reads an entity while one is still in flight is reading
    a value that has not arrived yet.
    """
    seen = -1
    for _ in range(20):
        if seen == len(inverter.requests):
            return
        seen = len(inverter.requests)
        await settle(hass, times=8)
    raise AssertionError("the device is still being asked for registers")


async def tick(
    hass: HomeAssistant, device: FakeDatalogger, inverter: FakeInverter, times: int = 1
) -> None:
    """Run the refresh timer ``times``, letting each round finish before the next.

    The ping is not decoration. Moving the clock forward moves it for the server too, and
    a connection that says nothing for ten minutes is reaped -- so several ticks in a row
    would otherwise close the socket the refresh is meant to be using.
    """
    for _ in range(times):
        await device.send_ping()
        await settle(hass, times=1)
        async_fire_time_changed(hass, dt_util.utcnow() + LATER)
        await quiet(hass, inverter)


def reads_of(inverter: FakeInverter, register: int) -> int:
    """How many times ``register`` has been asked for on its own."""
    return sum(
        1
        for request in inverter.requests
        if request.function == 0x05 and request_register(request) == register
    )


async def test_a_setting_changed_on_the_inverter_reaches_home_assistant(
    hass: HomeAssistant, setup_integration: MockConfigEntry, device: FakeDatalogger
) -> None:
    """The case the issue ends on: a window edited in ShinePhone, and no reconnect.

    Nothing here writes anything. The inverter's own copy is the truth by definition, and
    a Home Assistant that cannot see it changing is a Home Assistant that will happily
    show a charge window the inverter stopped using hours ago.
    """
    async with FakeInverter(device, {1100: 0x171E, 1101: 0x051E, 1102: 1}) as inverter:
        await device.send_data(groups=STORAGE_1000)
        await quiet(hass, inverter)

        entity_id = entity(hass, "time", "battery_first_start_time")
        assert hass.states.get(entity_id).state == "23:30:00"

        # Someone moves the window in the app. The datalogger stays connected throughout,
        # so nothing announces and nothing tells us.
        inverter.values[1100] = 0x0100

        await tick(hass, device, inverter)

        assert hass.states.get(entity_id).state == "01:00:00"


async def test_a_switch_the_inverter_later_dropped_stops_claiming_to_be_on(
    hass: HomeAssistant, setup_integration: MockConfigEntry, device: FakeDatalogger
) -> None:
    """The mismatch that used to be permanent.

    A write can be accepted, read back as applied, and then be undone by the inverter --
    a charge window that clashes with another one is the case the issue reports. The
    read-back is what makes the switch trustworthy at the moment of the write; only the
    refresh can make it trustworthy a minute later, and it has to win against the newer
    read-back to do it.
    """
    async with FakeInverter(device, {1100: 0x171E, 1101: 0x051E, 1102: 0}) as inverter:
        await device.send_data(groups=STORAGE_1000)
        await quiet(hass, inverter)

        entity_id = entity(hass, "switch", "battery_first_enabled")
        await hass.services.async_call("switch", "turn_on", {"entity_id": entity_id}, blocking=True)
        assert hass.states.get(entity_id).state == "on"

        # And the inverter quietly puts it back.
        inverter.values[1102] = 0

        await tick(hass, device, inverter)

        assert hass.states.get(entity_id).state == "off"


async def test_the_refresh_is_dated_to_when_it_started(
    hass: HomeAssistant, setup_integration: MockConfigEntry, device: FakeDatalogger
) -> None:
    """Otherwise it can undo a write that was made while it was in flight.

    Commands on a connection are serialised, so a write issued during a refresh lands
    after every one of that refresh's reads. Dating those reads to when they arrived would
    make words the inverter answered *before* the write look newer than the write, and the
    entity would show the value the user had just replaced.
    """
    hub = setup_integration.runtime_data
    async with FakeInverter(device, {1100: 0x171E}) as inverter:
        assert inverter is not None
        await device.send_data(groups=STORAGE_1000)
        await quiet(hass, inverter)

        inverter_device = hub.devices[f"inverter:{INVERTER}"]
        session = hub.session_for_device(inverter_device)

        started = dt_util.utcnow()
        await hub.async_refresh_settings_now(inverter_device, session)
        elapsed = dt_util.utcnow() - started

        # The reads are serialised a fixed interval apart, so a refresh takes real time --
        # which is what makes the two stamps distinguishable at all.
        assert elapsed > timedelta(seconds=0.2)
        stamp = hub.coordinators[inverter_device.key].data[VALUE_HOLDING_AT]
        assert stamp - started < elapsed / 2


async def test_a_register_the_inverter_never_answers_is_given_up_on(
    hass: HomeAssistant, setup_integration: MockConfigEntry, device: FakeDatalogger
) -> None:
    """A range read the device does not implement costs a command per register.

    The fallback that makes batching safe -- a range that comes back empty is retried one
    register at a time -- is exactly what makes an unimplemented register expensive on a
    timer. Asked for a few times, then left alone until the device reconnects.
    """
    async with FakeInverter(device, missing={1109}) as inverter:
        await device.send_data(groups=STORAGE_1000)
        await quiet(hass, inverter)

        await tick(hass, device, inverter, times=SETTINGS_MISSES + 2)
        settled = reads_of(inverter, 1109)
        assert settled <= SETTINGS_MISSES

        await tick(hass, device, inverter, times=2)
        assert reads_of(inverter, 1109) == settled, "kept asking for a register that never answers"

        # A reconnect is a fresh chance: the silence is usually a datalogger hanging up
        # mid-read rather than a model that lacks the register.
        await device.send_announce(groups=[build_group(1000, [0] * 60)])
        await settle(hass)
        await quiet(hass, inverter)
        await tick(hass, device, inverter)
        assert reads_of(inverter, 1109) > settled


async def test_the_refresh_can_be_turned_off(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    """Zero minutes means no timer at all, not a timer that fires immediately."""
    config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(config_entry, options={CONF_SETTINGS_INTERVAL: 0})
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    logger = FakeDatalogger()
    await logger.connect("127.0.0.1", config_entry.runtime_data.port)
    try:
        async with FakeInverter(logger) as inverter:
            await logger.send_data(groups=STORAGE_1000)
            await quiet(hass, inverter)
            # The first record still reads the settings once; entities would otherwise
            # have nothing to show for the registers an announce does not carry.
            first = len(inverter.requests)

            await tick(hass, logger, inverter, times=3)
            assert len(inverter.requests) == first
    finally:
        await logger.close()


async def test_the_refresh_button_reads_the_settings_now(
    hass: HomeAssistant, setup_integration: MockConfigEntry, device: FakeDatalogger
) -> None:
    """What people were reloading the whole integration to do."""
    async with FakeInverter(device, {3: 40}) as inverter:
        await device.send_data(groups=PROTOCOL_II_3000)
        await quiet(hass, inverter)

        inverter.values[3] = 70
        await hass.services.async_call(
            "button",
            "press",
            {"entity_id": entity(hass, "button", "refresh_settings")},
            blocking=True,
        )
        await settle(hass)

        assert float(hass.states.get(entity(hass, "number", "output_power_limit")).state) == 70
