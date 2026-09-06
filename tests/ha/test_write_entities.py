"""Write entities: number, switch, select, time, and the sync-time button."""

from __future__ import annotations

import asyncio
import contextlib

import pytest
from growatt_protocol.records import Frame
from growatt_protocol.registers.base import Confidence
from growatt_protocol.registers.writable import (
    TIME_SEGMENTS,
    WRITABLE,
    Encoding,
    WriteKind,
    for_profile,
    segment_for,
)
from growatt_protocol.testing import FakeDatalogger
from growatt_protocol.testing.frames import build_frame, build_group
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.growatt_datalogger.const import DOMAIN

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
            + register.to_bytes(2, "big")  # reads echo the range
            + value.to_bytes(2, "big")
        )
        await device.send_raw(
            build_frame(body, protocol=6, function=0x05, sequence=request.sequence)
        )


class _Inverter:
    """A fake inverter that answers reads and records writes, in the background.

    A storage profile creates two dozen write entities and each one reads its register
    once, so a test interested in a single write would otherwise spend itself hand
    serving the others. Accepted writes are remembered, which is what makes the read-back
    after a write return the new value the way real hardware does.
    """

    def __init__(
        self,
        device: FakeDatalogger,
        values: dict[int, int] | None = None,
        *,
        missing: set[int] | None = None,
    ) -> None:
        self.device = device
        self.values = dict(values or {})
        self.missing = missing or set()
        """Registers the model does not implement. A read of a range covering one of
        these comes back as the echo alone, which is how a device says "not here"."""

        self.result = 0
        """The status byte to answer writes with."""

        self.writes: asyncio.Queue[Frame] = asyncio.Queue()
        self._task: asyncio.Task[None] | None = None

    async def __aenter__(self) -> _Inverter:
        self._task = asyncio.create_task(self._run())
        return self

    async def __aexit__(self, *exc: object) -> None:
        assert self._task is not None
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task

    async def next_write(self, timeout: float = 10.0) -> Frame:
        return await asyncio.wait_for(self.writes.get(), timeout)

    async def _run(self) -> None:
        while True:
            try:
                request = await self.device.read_frame(timeout=0.2)
            except TimeoutError:
                continue
            except ConnectionError:
                return
            if request.function == 0x05:
                await self._answer_read(request)
            elif request.function in (0x06, 0x10):
                await self.writes.put(request)
                await self._answer_write(request)

    async def _send(self, function: int, tail: bytes, sequence: int) -> None:
        body = SERIAL.encode().ljust(30, b"\x00") + tail
        await self.device.send_raw(
            build_frame(body, protocol=6, function=function, sequence=sequence)
        )

    async def _answer_read(self, request: Frame) -> None:
        start = int.from_bytes(request.body[30:32], "big")
        end = int.from_bytes(request.body[32:34], "big")
        echo = request.body[30:34]
        if self.missing & set(range(start, end + 1)):
            await self._send(0x05, echo, request.sequence)
            return
        words = b"".join(self.values.get(r, 0).to_bytes(2, "big") for r in range(start, end + 1))
        await self._send(0x05, echo + words, request.sequence)

    async def _answer_write(self, request: Frame) -> None:
        if request.function == 0x06:
            register = int.from_bytes(request.body[30:32], "big")
            value = request.body[32:34]
            if self.result == 0:
                self.values[register] = int.from_bytes(value, "big")
            await self._send(
                0x06, request.body[30:32] + bytes([self.result]) + value, request.sequence
            )
            return

        start = int.from_bytes(request.body[30:32], "big")
        end = int.from_bytes(request.body[32:34], "big")
        if self.result == 0:
            for offset, register in enumerate(range(start, end + 1)):
                at = 34 + offset * 2
                self.values[register] = int.from_bytes(request.body[at : at + 2], "big")
        await self._send(0x10, request.body[30:34] + bytes([self.result]), request.sequence)


async def _storage_device(hass: HomeAssistant, device: FakeDatalogger) -> None:
    """Announce a device that resolves to the SPH/SPA storage profile."""
    await device.send_data(groups=[build_group(1000, [0] * 60)])
    await _settle(hass)


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
    keys = {spec.key for spec in for_profile("storage_1000")}
    assert "battery_first_stop_soc" in keys
    assert "charge_priority" in keys


def test_the_1000_block_is_not_offered_to_a_3000_block_hybrid() -> None:
    """A TL-XH does not have those registers, and answers a write "no such register".

    Its schedule is nine bit-packed slots at 3038-3059 instead. Offering the SPH block
    to it produced entities that could never be written, which is
    https://github.com/FezVrasta/growatt-datalogger/issues/2.
    """
    specs = for_profile("storage_3000", include_unverified=True)
    assert not [spec for spec in specs if 1000 <= spec.register < 2000]

    # ...but not left with nothing: the one storage setting whose 3000-block address is
    # settled is the same register this package already reads for these devices.
    ac_charge = next(spec for spec in specs if spec.key == "ac_charge_enabled")
    assert ac_charge.register == 3049


def test_unverified_registers_are_only_offered_when_asked_for() -> None:
    default = {spec.key for spec in for_profile("storage_1000")}
    opted_in = {spec.key for spec in for_profile("storage_1000", include_unverified=True)}

    assert "load_first_stop_soc" not in default
    assert "load_first_stop_soc" in opted_in


def test_registers_are_each_named_once() -> None:
    """A register with two keys is a transcription bug, not a design choice."""
    registers = [spec.register for spec in WRITABLE]
    assert len(registers) == len(set(registers))


@pytest.mark.parametrize("profile", ["protocol_ii", "storage_1000", "storage_3000"])
def test_a_key_means_one_register_within_a_profile(profile: str) -> None:
    """One key may span profiles -- ac_charge_enabled is 1092 or 3049 -- but never two
    entities on the same device, which would collide on their unique id."""
    keys = [spec.key for spec in for_profile(profile, include_unverified=True)]
    assert len(keys) == len(set(keys))


def test_every_time_slot_register_is_written_as_part_of_its_segment() -> None:
    """Firmware validates a window as a whole, so no member may be left out."""
    boundaries = {spec.register for spec in WRITABLE if spec.encoding is Encoding.HHMM}
    assert boundaries  # the table would otherwise have quietly lost its schedule
    for register in boundaries:
        assert segment_for(register) is not None, register

    # And each slot is complete: two boundaries and the switch that arms them, all three
    # of which have to exist for a segment write to have something to send.
    named = {spec.register for spec in WRITABLE}
    for segment in TIME_SEGMENTS:
        assert len(segment) == 3
        assert set(segment) <= named, segment
        assert set(segment[:2]) <= boundaries, segment


def test_a_register_outside_a_window_stands_alone() -> None:
    assert segment_for(3) is None
    assert segment_for(1092) is None


def test_grid_first_and_battery_first_scheduling_is_exposed() -> None:
    """The full storage priority-set block, not just SOC and the slot-1 window.

    See https://github.com/FezVrasta/growatt-datalogger/issues/1.
    """
    keys = {spec.key for spec in for_profile("storage_1000")}
    for key in (
        "grid_first_discharge_power_rate",
        "grid_first_start_time",
        "grid_first_stop_time",
        "grid_first_enabled",
        "grid_first_start_time_2",
        "grid_first_stop_time_2",
        "grid_first_enabled_2",
        "grid_first_start_time_3",
        "grid_first_stop_time_3",
        "grid_first_enabled_3",
        "battery_first_charge_power_rate",
        "battery_first_enabled",
        "battery_first_start_time_2",
        "battery_first_stop_time_2",
        "battery_first_enabled_2",
        "battery_first_start_time_3",
        "battery_first_stop_time_3",
        "battery_first_enabled_3",
    ):
        assert key in keys, key


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
    # The read that turns the rejection into an explanation: the register is there, so
    # the message should say the inverter refused this value rather than lacks the
    # register.
    await _serve_reads(device, count=1, value=80)

    with pytest.raises(HomeAssistantError, match="reads back as 80"):
        await asyncio.wait_for(call, 5)
    assert hass.states.get(entity_id).state == before


async def test_a_charge_window_is_written_as_a_whole_slot(
    hass: HomeAssistant, setup_integration: MockConfigEntry, device: FakeDatalogger
) -> None:
    """Moving one boundary sends the slot's three registers, not just the one.

    Firmware validates a window as a unit, so a single-register write can present it with
    a start that has moved past a stop that has not.
    """
    async with _Inverter(device, {1080: 0x0600, 1081: 0x0800, 1082: 1}) as inverter:
        await _storage_device(hass, device)

        entity_id = er.async_get(hass).async_get_entity_id(
            "time", DOMAIN, f"{DOMAIN}_inverter:{INVERTER}_grid_first_start_time"
        )
        assert entity_id is not None

        await hass.services.async_call(
            "time", "set_value", {"entity_id": entity_id, "time": "07:30:00"}, blocking=True
        )

        write = await inverter.next_write()
        assert write.function == 0x10
        assert int.from_bytes(write.body[30:32], "big") == 1080
        assert int.from_bytes(write.body[32:34], "big") == 1082
        # The new start, and the stop and enable switch exactly as the inverter had them.
        words = [int.from_bytes(write.body[i : i + 2], "big") for i in range(34, 40, 2)]
        assert words == [(7 << 8) | 30, 0x0800, 1]


async def test_a_rejection_says_whether_the_inverter_has_the_register(
    hass: HomeAssistant, setup_integration: MockConfigEntry, device: FakeDatalogger
) -> None:
    """The message issue #2 needed: "result 2" alone leaves nowhere to go.

    https://github.com/FezVrasta/growatt-datalogger/issues/2
    """
    async with _Inverter(device, missing={1080, 1081, 1082}) as inverter:
        inverter.result = 2
        await _storage_device(hass, device)

        entity_id = er.async_get(hass).async_get_entity_id(
            "switch", DOMAIN, f"{DOMAIN}_inverter:{INVERTER}_grid_first_enabled"
        )
        assert entity_id is not None

        with pytest.raises(HomeAssistantError) as raised:
            await hass.services.async_call(
                "switch", "turn_on", {"entity_id": entity_id}, blocking=True
            )

    message = str(raised.value)
    assert "holding register 1082" in message
    assert "no such register" in message
    assert "does not have it" in message


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


async def test_a_write_entity_takes_its_value_from_the_announce(
    hass: HomeAssistant, setup_integration: MockConfigEntry, device: FakeDatalogger
) -> None:
    """No command round-trip needed for a register the device already reports.

    These settings live in the holding space, which is exactly what an announce carries,
    so the device volunteers them on every connection.
    """
    await device.send_data(groups=[build_group(3000, [1, 0, 0, 3295])])
    await _settle(hass)
    # Holding register 3 is the output power limit; the announce reports it as 100%.
    await device.send_announce(groups=[build_group(0, [1, 0, 0, 100])])
    await _settle(hass)

    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id(
        "number", DOMAIN, f"{DOMAIN}_inverter:{INVERTER}_output_power_limit"
    )
    assert entity_id is not None
    assert float(hass.states.get(entity_id).state) == 100


async def test_a_switch_is_known_once_the_device_reports_it(
    hass: HomeAssistant, setup_integration: MockConfigEntry, device: FakeDatalogger
) -> None:
    """An unknown switch renders as two buttons rather than a toggle, so this matters."""
    await device.send_data(groups=[build_group(3000, [1, 0, 0, 3295])])
    await _settle(hass)
    await device.send_announce(groups=[build_group(0, [1, 0, 0, 100])])
    await _settle(hass)

    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id(
        "switch", DOMAIN, f"{DOMAIN}_inverter:{INVERTER}_inverter_enabled"
    )
    assert entity_id is not None
    assert hass.states.get(entity_id).state == "on"
