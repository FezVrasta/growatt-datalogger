"""Write entities end to end: number, switch, select, time, and the sync-time button.

The table these are built from is the library's, and its invariants are tested there --
``packages/growatt-protocol/tests/registers/test_writable.py``. What is left here needs
Home Assistant: that an entity appears, shows the right value, and puts the right frames
on the wire when someone changes it.
"""

from __future__ import annotations

import pytest
from growatt_protocol.registers.writable import for_profile
from growatt_protocol.testing import FakeDatalogger, FakeInverter, request_register, request_values
from growatt_protocol.testing.frames import build_group
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.growatt_datalogger.const import DOMAIN

from .conftest import INVERTER, SERIAL, settle

#: A record whose register groups resolve to the Protocol II 3000-block profile.
PROTOCOL_II_3000 = [build_group(3000, [1, 0, 0, 3295])]

#: A record carrying the 1000 storage block, which resolves to the SPH/SPA profile and
#: so creates the charge and discharge window entities.
STORAGE_1000 = [build_group(1000, [0] * 60)]


def entity(hass: HomeAssistant, domain: str, key: str, device: str = f"inverter:{INVERTER}") -> str:
    entity_id = er.async_get(hass).async_get_entity_id(domain, DOMAIN, f"{DOMAIN}_{device}_{key}")
    assert entity_id is not None, f"no {domain} entity for {key}"
    return entity_id


async def test_a_number_entity_is_created_and_writes(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    device: FakeDatalogger,
    inverter: FakeInverter,
) -> None:
    await device.send_data(groups=PROTOCOL_II_3000)
    await settle(hass)

    entity_id = entity(hass, "number", "output_power_limit")
    await hass.services.async_call(
        "number", "set_value", {"entity_id": entity_id, "value": 50}, blocking=True
    )

    request = await inverter.wait_for(0x06, 3)
    assert int.from_bytes(request.body[32:34], "big") == 50
    assert inverter.values[3] == 50
    assert float(hass.states.get(entity_id).state) == 50


async def test_a_device_reads_its_writable_registers_in_one_batch(
    hass: HomeAssistant, setup_integration: MockConfigEntry, device: FakeDatalogger
) -> None:
    """27 registers, a handful of commands -- not 27 of them.

    Each write entity used to read its own register, and a session serialises commands a
    fixed interval apart, so a storage inverter spent seconds of enforced pauses on
    startup before anything a user asked for could begin.
    """
    async with FakeInverter(device) as inverter:
        # Two records: the first creates the entities, and they subscribe to the
        # coordinator after it has already published, so the second is the first update
        # they see -- and the first moment they know the device is reachable.
        await device.send_data(groups=STORAGE_1000)
        await settle(hass)
        await device.send_data(groups=STORAGE_1000)

        # The batch runs in the background; its last range covers the battery-first
        # windows, so seeing that request means it has finished.
        await inverter.wait_for(0x05, 1080)
        await settle(hass)

        reads = [r for r in inverter.requests if r.function == 0x05]
        wanted = {spec.register for spec in for_profile("storage_1000", include_unverified=True)}

        assert len(wanted) == 27
        assert len(reads) <= 6, f"{len(reads)} reads for {len(wanted)} registers"
        # And every register still got a value: fewer commands, not less data.
        covered = set()
        for request in reads:
            start = request_register(request)
            end = int.from_bytes(request.body[32:34], "big")
            covered |= set(range(start, end + 1))
        assert wanted <= covered

    entity_id = entity(hass, "time", "grid_first_start_time")
    assert hass.states.get(entity_id).state not in (None, "unknown")


async def test_a_rejected_write_raises_and_leaves_the_state_alone(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    device: FakeDatalogger,
    inverter: FakeInverter,
) -> None:
    inverter.values[3] = 80
    await device.send_data(groups=PROTOCOL_II_3000)
    await settle(hass)

    entity_id = entity(hass, "number", "output_power_limit")
    before = hass.states.get(entity_id).state

    # The register exists, so the message should say the inverter refused this value
    # rather than that it lacks the register.
    inverter.result = 3
    with pytest.raises(HomeAssistantError, match="reads back as 80"):
        await hass.services.async_call(
            "number", "set_value", {"entity_id": entity_id, "value": 50}, blocking=True
        )

    assert hass.states.get(entity_id).state == before


async def test_a_charge_window_is_written_as_a_whole_slot(
    hass: HomeAssistant, setup_integration: MockConfigEntry, device: FakeDatalogger
) -> None:
    """Moving one boundary sends the slot's three registers, not just the one.

    Firmware validates a window as a unit, so a single-register write can present it with
    a start that has moved past a stop that has not.
    """
    async with FakeInverter(device, {1080: 0x0600, 1081: 0x0800, 1082: 1}) as inverter:
        await device.send_data(groups=STORAGE_1000)
        await settle(hass)

        await hass.services.async_call(
            "time",
            "set_value",
            {"entity_id": entity(hass, "time", "grid_first_start_time"), "time": "07:30:00"},
            blocking=True,
        )

        write = await inverter.wait_for(0x10)
        assert request_register(write) == 1080
        assert int.from_bytes(write.body[32:34], "big") == 1082
        # The new start, and the stop and enable switch exactly as the inverter had them.
        assert request_values(write) == ((7 << 8) | 30, 0x0800, 1)


async def test_a_rejection_says_whether_the_inverter_has_the_register(
    hass: HomeAssistant, setup_integration: MockConfigEntry, device: FakeDatalogger
) -> None:
    """The message issue #2 needed: "result 2" alone leaves nowhere to go.

    https://github.com/FezVrasta/growatt-datalogger/issues/2
    """
    async with FakeInverter(device, missing={1080, 1081, 1082}, result=2) as inverter:
        assert inverter is not None
        await device.send_data(groups=STORAGE_1000)
        await settle(hass)

        with pytest.raises(HomeAssistantError) as raised:
            await hass.services.async_call(
                "switch",
                "turn_on",
                {"entity_id": entity(hass, "switch", "grid_first_enabled")},
                blocking=True,
            )

    message = str(raised.value)
    assert "holding register 1082" in message
    assert "no such register" in message
    assert "does not have it" in message


async def test_write_entities_expose_their_provenance(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    device: FakeDatalogger,
    inverter: FakeInverter,
) -> None:
    """A user should be able to see where a register's meaning came from."""
    await device.send_data(groups=PROTOCOL_II_3000)
    await settle(hass)

    attributes = hass.states.get(entity(hass, "number", "output_power_limit")).attributes

    assert attributes["register"] == 3
    assert attributes["confidence"] == "verified"
    assert "Protocol II" in attributes["source"]


async def test_the_sync_time_button_sets_the_clock(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    device: FakeDatalogger,
    inverter: FakeInverter,
) -> None:
    await device.send_announce()
    await settle(hass)

    await hass.services.async_call(
        "button",
        "press",
        {"entity_id": entity(hass, "button", "sync_time", f"logger:{SERIAL}")},
        blocking=True,
    )

    request = await inverter.wait_for(0x18)
    assert request_register(request) == 0x1F


async def test_a_write_entity_takes_its_value_from_the_announce(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    device: FakeDatalogger,
    inverter: FakeInverter,
) -> None:
    """No command round-trip needed for a register the device already reports.

    These settings live in the holding space, which is exactly what an announce carries,
    so the device volunteers them on every connection.
    """
    await device.send_data(groups=PROTOCOL_II_3000)
    await settle(hass)
    # Holding register 3 is the output power limit; the announce reports it as 100%.
    await device.send_announce(groups=[build_group(0, [1, 0, 0, 100])])
    await settle(hass)

    assert float(hass.states.get(entity(hass, "number", "output_power_limit")).state) == 100


async def test_a_switch_is_known_once_the_device_reports_it(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    device: FakeDatalogger,
    inverter: FakeInverter,
) -> None:
    """An unknown switch renders as two buttons rather than a toggle, so this matters."""
    await device.send_data(groups=PROTOCOL_II_3000)
    await settle(hass)
    await device.send_announce(groups=[build_group(0, [1, 0, 0, 100])])
    await settle(hass)

    assert hass.states.get(entity(hass, "switch", "inverter_enabled")).state == "on"


#: The 1000 storage block as an SPA reports it in its announce: Battery First slot 1
#: runs 23:30 to 05:30 and is armed, slot 2 runs 11:00 to 12:00 and is not.
BATTERY_FIRST = {1100: 0x171E, 1101: 0x051E, 1102: 1, 1103: 0x0B00, 1104: 0x0C00, 1105: 0}

#: Those six registers as a holding group, which is what an announce carries.
BATTERY_FIRST_GROUP = [build_group(1100, [BATTERY_FIRST[r] for r in sorted(BATTERY_FIRST)])]


async def test_the_storage_block_takes_its_values_from_the_announce(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    device: FakeDatalogger,
    inverter: FakeInverter,
) -> None:
    """The whole settings block refreshes itself, not just the two registers with names.

    A profile names four holding registers; the SPH/SPA storage block is not among them,
    so reading the announce by *name* left every window and SOC limit showing whatever a
    one-shot read found at startup, for as long as the integration stayed loaded. That is
    what "reload the integration and the values are right" looks like from outside --
    https://github.com/FezVrasta/growatt-datalogger/issues/2.
    """
    await device.send_data(groups=STORAGE_1000)
    await settle(hass)
    await device.send_announce(groups=BATTERY_FIRST_GROUP)
    await settle(hass)

    assert hass.states.get(entity(hass, "switch", "battery_first_enabled")).state == "on"
    assert hass.states.get(entity(hass, "switch", "battery_first_enabled_2")).state == "off"
    assert hass.states.get(entity(hass, "time", "battery_first_start_time")).state == "23:30:00"
    assert hass.states.get(entity(hass, "time", "battery_first_start_time_2")).state == "11:00:00"


async def test_a_holding_register_does_not_overwrite_the_input_register_of_the_same_number(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    config_entry: MockConfigEntry,
    device: FakeDatalogger,
    inverter: FakeInverter,
) -> None:
    """Two address files, two namespaces.

    Holding 1100 is a discharge window's start time on an SPA; input 1100 is telemetry.
    Published under one ``register_1100`` name they take turns, and the value depends on
    which record happened to arrive last -- which is how two diagnostics dumps from one
    device came to disagree about the same register.
    """
    hass.config_entries.async_update_entry(
        config_entry, options={**config_entry.options, "include_unknown": True}
    )
    await hass.async_block_till_done()

    await device.send_data(groups=[build_group(1000, [0] * 60), build_group(1100, [4242] * 6)])
    await settle(hass)
    await device.send_announce(groups=BATTERY_FIRST_GROUP)
    await settle(hass)

    data = setup_integration.runtime_data.coordinators[f"inverter:{INVERTER}"].data
    assert data["register_1100"] == 4242
    assert data["holding_1100"] == 0x171E


async def test_a_write_the_inverter_drops_is_reported_rather_than_shown_as_success(
    hass: HomeAssistant, setup_integration: MockConfigEntry, device: FakeDatalogger
) -> None:
    """Accepted is not applied.

    Firmware will take a value it cannot act on, answer "accepted", and go on reading
    back as it was -- arming a window that is still 00:00-00:00 is the case in practice.
    A switch that reports success for a change the inverter dropped is worse than either
    an error or an outright refusal.
    """
    async with FakeInverter(device, discard={1108}) as inverter:
        assert inverter is not None
        await device.send_data(groups=STORAGE_1000)
        await settle(hass)

        entity_id = entity(hass, "switch", "battery_first_enabled_3")
        with pytest.raises(HomeAssistantError, match="reads back as 0"):
            await hass.services.async_call(
                "switch", "turn_on", {"entity_id": entity_id}, blocking=True
            )

        assert hass.states.get(entity_id).state == "off"


async def test_a_write_is_not_undone_by_an_older_announce(
    hass: HomeAssistant, setup_integration: MockConfigEntry, device: FakeDatalogger
) -> None:
    """The read-back is newer than the last announce, and has to win.

    Otherwise a switch snaps straight back to its pre-write value and stays there until
    the datalogger next reconnects.
    """
    async with FakeInverter(device, dict(BATTERY_FIRST)) as inverter:
        assert inverter is not None
        await device.send_data(groups=STORAGE_1000)
        await settle(hass)
        await device.send_announce(groups=BATTERY_FIRST_GROUP)
        await settle(hass)

        entity_id = entity(hass, "switch", "battery_first_enabled_2")
        assert hass.states.get(entity_id).state == "off"

        await hass.services.async_call("switch", "turn_on", {"entity_id": entity_id}, blocking=True)

        assert hass.states.get(entity_id).state == "on"
