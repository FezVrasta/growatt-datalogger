"""Write entities: number, switch, select, time, and the sync-time button."""

from __future__ import annotations

import asyncio

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.growatt_datalogger.const import DOMAIN
from custom_components.growatt_datalogger.registers.base import Confidence
from custom_components.growatt_datalogger.registers.writable import (
    WRITABLE,
    Encoding,
    WriteKind,
    for_profile,
)
from tests.fakes.datalogger import FakeDatalogger
from tests.fakes.frames import build_frame, build_group

SERIAL = "GPG0EXAMP1"
INVERTER = "SML0EXAMP2"


async def _settle(hass: HomeAssistant, times: int = 3) -> None:
    for _ in range(times):
        await asyncio.sleep(0.05)
        await hass.async_block_till_done()


async def _serve_reads(device: FakeDatalogger, count: int, value: int = 0) -> None:
    """Answer the read-back requests entities issue when they are first added."""
    for _ in range(count):
        try:
            request = await device.read_frame(timeout=0.5)
        except (TimeoutError, ConnectionError):
            return
        if request.function != 0x05:
            continue
        register = int.from_bytes(request.body[30:32], "big")
        body = (
            SERIAL.encode().ljust(30, b"\x00")
            + register.to_bytes(2, "big")
            + value.to_bytes(2, "big")
        )
        await device.send_raw(
            build_frame(body, protocol=6, function=0x05, sequence=request.sequence)
        )


# ----------------------------------------------------------------------------------
# The table itself
# ----------------------------------------------------------------------------------


def test_only_verified_registers_are_enabled_by_default() -> None:
    """Community-reported meanings must not be created live on someone's inverter."""
    for spec in WRITABLE:
        assert spec.enabled_default == (spec.confidence is Confidence.VERIFIED)


def test_every_writable_register_cites_a_source() -> None:
    for spec in WRITABLE:
        assert spec.source, spec.key


def test_a_non_storage_profile_gets_no_battery_registers() -> None:
    """A string inverter has no battery, so those registers must not appear."""
    keys = {spec.key for spec in for_profile("protocol_ii_3000")}
    assert "output_power_limit" in keys
    assert "battery_first_stop_soc" not in keys


def test_a_storage_profile_gets_the_battery_registers() -> None:
    keys = {spec.key for spec in for_profile("storage_3000")}
    assert "battery_first_stop_soc" in keys
    assert "charge_priority" in keys


def test_unverified_registers_are_only_offered_when_asked_for() -> None:
    default = {spec.key for spec in for_profile("storage_3000")}
    opted_in = {spec.key for spec in for_profile("storage_3000", include_unverified=True)}

    assert "load_first_stop_soc" not in default
    assert "load_first_stop_soc" in opted_in


# ----------------------------------------------------------------------------------
# Encoding
# ----------------------------------------------------------------------------------


def test_time_windows_pack_the_hour_and_minute_into_one_word() -> None:
    spec = next(s for s in WRITABLE if s.encoding is Encoding.HHMM)
    assert spec.encode("06:30:00") == (6 << 8) | 30
    assert spec.decode((23 << 8) | 45) == "23:45:00"


def test_boolean_registers_round_trip() -> None:
    spec = next(s for s in WRITABLE if s.encoding is Encoding.BOOL)
    assert spec.encode(True) == 1
    assert spec.encode(False) == 0
    assert spec.decode(1) is True


def test_select_options_round_trip() -> None:
    spec = next(s for s in WRITABLE if s.kind is WriteKind.SELECT)
    assert spec.encode("Battery first") == 1
    assert spec.decode(2) == "Grid first"


def test_an_unknown_select_word_decodes_to_none_rather_than_a_guess() -> None:
    spec = next(s for s in WRITABLE if s.kind is WriteKind.SELECT)
    assert spec.decode(99) is None


def test_an_invalid_select_option_is_refused() -> None:
    spec = next(s for s in WRITABLE if s.kind is WriteKind.SELECT)
    with pytest.raises(ValueError, match="not one of"):
        spec.encode("Nonsense")


# ----------------------------------------------------------------------------------
# End to end
# ----------------------------------------------------------------------------------


async def test_a_number_entity_is_created_and_writes(
    hass: HomeAssistant, setup_integration: MockConfigEntry, device: FakeDatalogger
) -> None:
    await device.send_data(groups=[build_group(3000, [1, 0, 0, 3295])])
    await device.read_frame()
    await _settle(hass)
    await _serve_reads(device, count=4, value=80)
    await _settle(hass)

    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id(
        "number", DOMAIN, f"{DOMAIN}_inverter:{INVERTER}_output_power_limit"
    )
    assert entity_id is not None

    call = asyncio.create_task(
        hass.services.async_call(
            "number",
            "set_value",
            {"entity_id": entity_id, "value": 50},
            blocking=True,
        )
    )
    await asyncio.sleep(0.05)

    request = await device.read_frame()
    assert request.function == 0x06
    assert int.from_bytes(request.body[30:32], "big") == 3
    assert int.from_bytes(request.body[32:34], "big") == 50

    body = SERIAL.encode().ljust(30, b"\x00") + (3).to_bytes(2, "big") + b"\x00\x00\x32"
    await device.send_raw(build_frame(body, protocol=6, function=0x06, sequence=request.sequence))
    await _serve_reads(device, count=1, value=50)
    await asyncio.wait_for(call, 5)


async def test_a_rejected_write_raises_and_leaves_the_state_alone(
    hass: HomeAssistant, setup_integration: MockConfigEntry, device: FakeDatalogger
) -> None:
    await device.send_data(groups=[build_group(3000, [1, 0, 0, 3295])])
    await device.read_frame()
    await _settle(hass)
    await _serve_reads(device, count=4, value=80)
    await _settle(hass)

    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id(
        "number", DOMAIN, f"{DOMAIN}_inverter:{INVERTER}_output_power_limit"
    )
    before = hass.states.get(entity_id).state

    call = asyncio.create_task(
        hass.services.async_call(
            "number", "set_value", {"entity_id": entity_id, "value": 50}, blocking=True
        )
    )
    await asyncio.sleep(0.05)

    request = await device.read_frame()
    body = SERIAL.encode().ljust(30, b"\x00") + (3).to_bytes(2, "big") + b"\x03\x00\x00"
    await device.send_raw(build_frame(body, protocol=6, function=0x06, sequence=request.sequence))

    with pytest.raises(HomeAssistantError, match="rejected"):
        await asyncio.wait_for(call, 5)
    assert hass.states.get(entity_id).state == before


async def test_write_entities_expose_their_provenance(
    hass: HomeAssistant, setup_integration: MockConfigEntry, device: FakeDatalogger
) -> None:
    """A user should be able to see where a register's meaning came from."""
    await device.send_data(groups=[build_group(3000, [1, 0, 0, 3295])])
    await device.read_frame()
    await _settle(hass)
    await _serve_reads(device, count=4, value=80)
    await _settle(hass)

    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id(
        "number", DOMAIN, f"{DOMAIN}_inverter:{INVERTER}_output_power_limit"
    )
    attributes = hass.states.get(entity_id).attributes

    assert attributes["register"] == 3
    assert attributes["confidence"] == "verified"
    assert "Protocol II" in attributes["source"]


async def test_the_sync_time_button_sets_the_clock(
    hass: HomeAssistant, setup_integration: MockConfigEntry, device: FakeDatalogger
) -> None:
    await device.send_announce()
    await device.read_frame()
    await _settle(hass)
    await _serve_reads(device, count=4)
    await _settle(hass)

    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id(
        "button", DOMAIN, f"{DOMAIN}_logger:{SERIAL}_sync_time"
    )
    assert entity_id is not None

    call = asyncio.create_task(
        hass.services.async_call("button", "press", {"entity_id": entity_id}, blocking=True)
    )
    await asyncio.sleep(0.05)

    request = await device.read_frame()
    assert request.function == 0x18
    assert int.from_bytes(request.body[30:32], "big") == 0x1F

    body = SERIAL.encode().ljust(30, b"\x00") + (0x1F).to_bytes(2, "big") + b"\x00"
    await device.send_raw(build_frame(body, protocol=6, function=0x18, sequence=request.sequence))
    await asyncio.wait_for(call, 5)
