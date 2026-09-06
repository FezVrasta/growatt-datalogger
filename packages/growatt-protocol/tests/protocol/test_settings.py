"""Changing a setting, and reading the ones an application needs up front."""

from __future__ import annotations

import pytest
from growatt_protocol import settings
from growatt_protocol.commands import Command, CommandResponse
from growatt_protocol.records import serial_width

SERIAL = "GPG0EXAMP1"
WIDTH = serial_width(6)


class FakeChannel:
    """A device that answers commands from a dict, without a socket in the way."""

    def __init__(
        self,
        values: dict[int, int] | None = None,
        *,
        missing: set[int] | None = None,
        result: int = 0,
    ) -> None:
        self.datalogger_serial = SERIAL
        self.protocol = 6
        self.values = dict(values or {})
        self.missing = set(missing or ())
        self.result = result
        self.commands: list[Command] = []

    @property
    def reads(self) -> list[tuple[int, int]]:
        """The ``(start, end)`` of every range read, in order."""
        return [
            (
                int.from_bytes(c.body[WIDTH : WIDTH + 2], "big"),
                int.from_bytes(c.body[WIDTH + 2 : WIDTH + 4], "big"),
            )
            for c in self.commands
            if c.function == 0x05
        ]

    async def send_command(self, command: Command) -> CommandResponse:
        self.commands.append(command)
        start = int.from_bytes(command.body[WIDTH : WIDTH + 2], "big")

        if command.function == 0x05:
            end = int.from_bytes(command.body[WIDTH + 2 : WIDTH + 4], "big")
            if self.missing & set(range(start, end + 1)):
                # How a real device says "not here": the echo, and nothing after it.
                return CommandResponse(0x05, start, end_register=end, empty=True)
            words = tuple(self.values.get(r, 0) for r in range(start, end + 1))
            return CommandResponse(0x05, start, value=words[0], end_register=end, values=words)

        return CommandResponse(command.function, start, result=self.result)


# ----------------------------------------------------------------------------------
# Grouping
# ----------------------------------------------------------------------------------


def test_neighbouring_registers_share_a_read() -> None:
    assert settings.read_ranges([1080, 1081, 1082]) == [(1080, 1082)]


def test_a_wide_gap_splits_the_read() -> None:
    """Reading across a gap the device does not implement loses the whole range."""
    assert settings.read_ranges([0, 3, 1044]) == [(0, 3), (1044, 1044)]


def test_a_long_run_is_split_at_the_span_limit() -> None:
    """Firmware commonly refuses a longer run, and a refusal costs a round trip."""
    ranges = settings.read_ranges(range(0, 100))
    assert len(ranges) > 1
    assert all(end - start < settings.MAX_RANGE_SPAN for start, end in ranges)


def test_nothing_wanted_asks_for_nothing() -> None:
    assert settings.read_ranges([]) == []


# ----------------------------------------------------------------------------------
# Batched reads
# ----------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_whole_setting_block_is_read_in_a_few_commands() -> None:
    wanted = [0, 3, 1044, 1070, 1071, *range(1080, 1089), 1090, 1091, 1092, *range(1100, 1110)]
    channel = FakeChannel({register: register for register in wanted})

    values = await settings.read_registers(channel, wanted)

    assert values == {register: register for register in wanted}
    assert len(channel.reads) == 4, channel.reads


@pytest.mark.asyncio
async def test_a_range_the_device_cannot_answer_falls_back_to_single_reads() -> None:
    """Batching is an optimisation; it must never return less than asking singly would.

    A device answers a range it does not fully implement with the echo alone, so one
    unimplemented register in the middle would otherwise lose every register around it.
    """
    channel = FakeChannel({1080: 11, 1081: 22, 1082: 33}, missing={1084})

    values = await settings.read_registers(channel, [1080, 1081, 1082, 1084])

    assert values == {1080: 11, 1081: 22, 1082: 33}
    assert (1080, 1084) in channel.reads, "the batch should have been tried first"
    assert (1080, 1080) in channel.reads, "and then retried one at a time"


@pytest.mark.asyncio
async def test_registers_nobody_asked_for_are_not_returned() -> None:
    """Reading across a gap is free; reporting the gap's contents is not."""
    channel = FakeChannel({1080: 11, 1081: 22, 1082: 33})

    assert await settings.read_registers(channel, [1080, 1082]) == {1080: 11, 1082: 33}


# ----------------------------------------------------------------------------------
# Writes
# ----------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_window_boundary_is_written_with_the_rest_of_its_slot() -> None:
    channel = FakeChannel({1080: 0x0600, 1081: 0x0800, 1082: 1})

    await settings.write_register(channel, 1081, 0x0900)

    write = channel.commands[-1]
    assert write.function == 0x10
    assert write.body[WIDTH + 4 :] == b"\x06\x00\x09\x00\x00\x01"


@pytest.mark.asyncio
async def test_a_raw_write_is_left_alone() -> None:
    """The escape hatch means that register and no other."""
    channel = FakeChannel({1080: 0x0600, 1081: 0x0800, 1082: 1})

    await settings.write_register(channel, 1081, 0x0900, whole_slot=False)

    assert [c.function for c in channel.commands] == [0x06]


@pytest.mark.asyncio
async def test_a_register_outside_a_window_is_written_on_its_own() -> None:
    channel = FakeChannel()

    await settings.write_register(channel, 3, 80)

    assert [c.function for c in channel.commands] == [0x06]


@pytest.mark.asyncio
async def test_a_firmware_without_range_writes_falls_back() -> None:
    """A device with no 0x10 wrote nothing, so retrying as 0x06 is safe."""
    channel = FakeChannel({1080: 1, 1081: 2, 1082: 3}, result=settings.UNSUPPORTED)

    await settings.write_register(channel, 1081, 9)

    assert [c.function for c in channel.commands] == [0x05, 0x10, 0x06]


# ----------------------------------------------------------------------------------
# Rejections
# ----------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_rejection_names_the_register_and_what_the_code_means() -> None:
    channel = FakeChannel({1082: 0})
    response = CommandResponse(0x06, 1082, result=2)

    message = await settings.explain_rejection(channel, 1082, response, name="grid_first_enabled")

    assert "grid_first_enabled (holding register 1082)" in message
    assert "no such register" in message
    assert "reads back as 0" in message


@pytest.mark.asyncio
async def test_a_rejection_says_so_when_the_register_is_not_there_either() -> None:
    channel = FakeChannel(missing={1082})
    response = CommandResponse(0x06, 1082, result=2)

    message = await settings.explain_rejection(channel, 1082, response)

    assert "does not have it" in message
