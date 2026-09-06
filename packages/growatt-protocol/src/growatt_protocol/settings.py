"""Changing an inverter setting, and explaining it when the inverter says no.

None of this is Home Assistant's business, and while it lived on an entity class it was
only available to entities -- the ``write_register`` service, which is the path a user
takes with an *unfamiliar* register and therefore the one most likely to be refused, got
the bare status byte that https://github.com/FezVrasta/growatt-datalogger/issues/2 was
filed about. So it lives next to the register table instead, where both callers can
reach it.

Two things happen here that a plain ``send_command`` does not do:

* A charge or discharge window is written as a whole slot. Firmware validates the three
  registers as a unit, so changing one at a time can present an inverted window.
* A rejection is diagnosed rather than reported. One extra read separates "this model
  does not have that register" from "it has it and would not take this value", which are
  the two situations a user acts on differently.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Protocol

from . import commands
from .commands import CommandResponse, describe_result
from .errors import CommandTimeout
from .registers.writable import TimeSlot, slot_for

_LOGGER = logging.getLogger(__name__)

#: The inverter answered "unsupported operation". Nothing was written, so a caller is
#: free to try a different function code.
UNSUPPORTED = 1


class CommandChannel(Protocol):
    """What these helpers need from a :class:`~.session.Session`.

    A protocol rather than the class itself, so a caller can substitute anything that
    speaks commands -- and so this module does not import the transport.
    """

    datalogger_serial: str | None
    protocol: int | None

    async def send_command(self, command: commands.Command) -> CommandResponse: ...


async def write_register(
    channel: CommandChannel, register: int, value: int, *, whole_slot: bool = True
) -> CommandResponse:
    """Write one holding register.

    With ``whole_slot`` (the default), a register belonging to a charge or discharge
    window is written together with the rest of that window: the slot is read back, this
    value is substituted, and all three go out as one 0x10 range. Callers offering a raw
    register escape hatch should pass ``whole_slot=False`` -- someone poking 1080 by hand
    means that register and no other.
    """
    serial, protocol = _address(channel)
    single = commands.write_inverter(serial, protocol, register, value)

    slot = slot_for(register) if whole_slot else None
    if slot is None:
        return await channel.send_command(single)

    values = await read_slot(channel, slot)
    if values is None:
        # Without the slot's current contents there is nothing to send alongside this
        # value, and inventing the other two would overwrite a window someone set in the
        # Growatt app. Changing one register is the smaller risk.
        return await channel.send_command(single)

    values[slot.registers.index(register)] = value
    response = await channel.send_command(
        commands.write_inverter_range(serial, protocol, slot.start, values)
    )
    if response.result == UNSUPPORTED:
        # This firmware has no 0x10. Nothing was written, so falling back to the
        # single-register write cannot apply anything twice.
        _LOGGER.debug("no range writes on %s; writing register %s on its own", serial, register)
        return await channel.send_command(single)
    return response


#: Registers to read in one command, at most. Firmware commonly refuses a longer run,
#: and a refusal costs a whole round trip to learn.
MAX_RANGE_SPAN = 45

#: How far apart two wanted registers may be and still share a read. Reading across a
#: gap costs nothing but the words; reading across a gap the device does not implement
#: costs the whole range, so this stays small.
MAX_RANGE_GAP = 8


def read_ranges(
    registers: Iterable[int], *, max_span: int = MAX_RANGE_SPAN, max_gap: int = MAX_RANGE_GAP
) -> list[tuple[int, int]]:
    """Group ``registers`` into the contiguous runs worth asking for in one command."""
    wanted = sorted(set(registers))
    if not wanted:
        return []

    ranges: list[tuple[int, int]] = []
    start = previous = wanted[0]
    for register in wanted[1:]:
        if register - previous > max_gap or register - start >= max_span:
            ranges.append((start, previous))
            start = register
        previous = register
    ranges.append((start, previous))
    return ranges


async def read_registers(channel: CommandChannel, registers: Iterable[int]) -> dict[int, int]:
    """Read every register in ``registers``, in as few round trips as the gaps allow.

    Reading them one at a time is what this replaces, and on a storage inverter that was
    26 commands: a :class:`~.session.Session` serialises them behind one lock a fixed
    interval apart, so it was seconds of enforced pauses on top of 26 round trips to a
    device fronting a serial Modbus bus -- all of it before the first thing a user asked
    for could start.

    A device answers a range it does not fully implement with the echo and nothing after
    it, which would lose every register in that range. So a range that comes back empty
    is retried one register at a time: batching is an optimisation, and it must never
    return less than asking singly would.
    """
    wanted = sorted(set(registers))
    values: dict[int, int] = {}

    for start, end in read_ranges(wanted):
        got = await _read_span(channel, start, end)
        if got is None and start != end:
            _LOGGER.debug("range %s..%s came back empty; falling back to single reads", start, end)
            for register in (r for r in wanted if start <= r <= end):
                if (single := await _read_span(channel, register, register)) is not None:
                    values.update(single)
        elif got is not None:
            values.update({r: word for r, word in got.items() if r in set(wanted)})

    return values


async def _read_span(channel: CommandChannel, start: int, end: int) -> dict[int, int] | None:
    """The words for ``start..end``, or None if the device did not return them all."""
    serial, protocol = _address(channel)
    try:
        response = await channel.send_command(commands.read_inverter(serial, protocol, start, end))
    except (CommandTimeout, ConnectionError) as error:
        _LOGGER.debug("could not read %s..%s: %s", start, end, error)
        return None

    if len(response.values) != end - start + 1:
        return None
    return {start + offset: word for offset, word in enumerate(response.values)}


async def read_slot(channel: CommandChannel, slot: TimeSlot) -> list[int] | None:
    """A window's three current words, or None if the inverter did not return them all."""
    words = await _read_span(channel, slot.start, slot.registers[-1])
    if words is None:
        _LOGGER.debug("could not read the whole window at %s", slot.start)
        return None
    return [words[register] for register in slot.registers]


async def explain_rejection(
    channel: CommandChannel, register: int, response: CommandResponse, *, name: str | None = None
) -> str:
    """Say what a rejection means, having asked the inverter about the register.

    A bare status byte leaves someone with nowhere to go. One extra read separates the
    two cases a user would act on differently: a register this model does not have, and
    one it has but would not accept this change to. It delays an error that has already
    happened, which is the cheapest thing there is to delay.
    """
    subject = f"{name} (holding register {register})" if name else f"register {register}"
    message = f"The inverter rejected {subject}: {describe_result(response.result)}"

    try:
        serial, protocol = _address(channel)
        probe = await channel.send_command(commands.read_inverter(serial, protocol, register))
    except (CommandTimeout, ConnectionError, ValueError):
        return f"{message}."

    if probe.empty or probe.value is None:
        return (
            f"{message}. It does not answer a read of that register either, so this "
            "model does not have it and retrying will not help."
        )
    return (
        f"{message}. The register itself reads back as {probe.value}, so the inverter "
        "does have it and refused this particular change."
    )


def _address(channel: CommandChannel) -> tuple[str, int]:
    if channel.datalogger_serial is None or channel.protocol is None:
        raise ValueError("the datalogger has not identified itself yet")
    return channel.datalogger_serial, channel.protocol
