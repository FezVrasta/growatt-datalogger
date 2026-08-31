"""Matching a device's reply to the command that is waiting for it."""

from __future__ import annotations

import asyncio

import pytest
from growatt_protocol import commands
from growatt_protocol.errors import CommandTimeout
from growatt_protocol.records import Frame
from growatt_protocol.server import (
    GrowattServer,
    ServerConfig,
)
from growatt_protocol.session import Session
from growatt_protocol.testing import FakeDatalogger
from growatt_protocol.testing.frames import build_frame

pytestmark = pytest.mark.asyncio

SERIAL = "GPG0EXAMP1"


def make_session(**kwargs: object) -> tuple[Session, list[bytes]]:
    sent: list[bytes] = []

    async def send(data: bytes) -> None:
        sent.append(data)

    session = Session(1, send=send, **kwargs)  # type: ignore[arg-type]
    session.protocol = 6
    session.datalogger_serial = SERIAL
    session.command_interval = 0
    return session, sent


def reply(function: int, register: int, tail: bytes, sequence: int, protocol: int = 6) -> Frame:
    width = 30 if protocol == 6 else 10
    body = SERIAL.encode().ljust(width, b"\x00") + register.to_bytes(2, "big")
    if function == 0x05:
        # A read reply echoes the range it was asked for before any values, and a
        # single-register read has end == start.
        body += register.to_bytes(2, "big")
    body += tail
    return Frame(build_frame(body, protocol=protocol, function=function, sequence=sequence))


async def test_a_command_resolves_when_its_reply_arrives() -> None:
    session, sent = make_session()
    task = asyncio.create_task(session.send_command(commands.read_inverter(SERIAL, 6, 3)))
    await asyncio.sleep(0)

    sequence = Frame(sent[0]).sequence
    await session.handle_frame(reply(0x05, 3, (1234).to_bytes(2, "big"), sequence))

    assert (await task).value == 1234


async def test_sequence_numbers_actually_advance() -> None:
    """A counter pinned at one leaves nothing to correlate replies against."""
    session, sent = make_session()
    seen = []

    for _ in range(3):
        task = asyncio.create_task(session.send_command(commands.read_inverter(SERIAL, 6, 3)))
        await asyncio.sleep(0)
        sequence = Frame(sent[-1]).sequence
        seen.append(sequence)
        await session.handle_frame(reply(0x05, 3, b"\x00\x01", sequence))
        await task

    assert len(set(seen)) == 3


async def test_zero_and_ffff_are_never_used() -> None:
    session, sent = make_session()
    for _ in range(20):
        task = asyncio.create_task(session.send_command(commands.read_inverter(SERIAL, 6, 3)))
        await asyncio.sleep(0)
        sequence = Frame(sent[-1]).sequence
        assert sequence not in (0, 0xFFFF)
        await session.handle_frame(reply(0x05, 3, b"\x00\x01", sequence))
        await task


async def test_a_reply_with_the_wrong_sequence_still_matches_on_the_register() -> None:
    """It is unverified that every firmware echoes the sequence, so there is a fallback.

    Commands are serialised per connection, so at most one request can be waiting on a
    given register and the fallback is unambiguous.
    """
    session, _sent = make_session()
    task = asyncio.create_task(session.send_command(commands.read_inverter(SERIAL, 6, 3)))
    await asyncio.sleep(0)

    await session.handle_frame(reply(0x05, 3, (777).to_bytes(2, "big"), sequence=0xABCD))

    assert (await task).value == 777


async def test_a_reply_nobody_awaits_is_recorded_not_dropped() -> None:
    session, _ = make_session()
    await session.handle_frame(reply(0x05, 99, b"\x00\x05", sequence=1))

    assert len(session.unsolicited) == 1
    assert session.unsolicited[0].register == 99


async def test_a_command_that_is_never_answered_times_out() -> None:
    session, _ = make_session()
    with pytest.raises(CommandTimeout, match="register 3"):
        await session.send_command(commands.read_inverter(SERIAL, 6, 3), timeout=0.05)
    # The pending entry must be cleaned up, or the sequence leaks.
    assert not session._pending


async def test_a_disconnect_fails_outstanding_commands_rather_than_hanging() -> None:
    session, _ = make_session()
    task = asyncio.create_task(
        session.send_command(commands.read_inverter(SERIAL, 6, 3), timeout=30)
    )
    await asyncio.sleep(0)

    session.close()

    with pytest.raises(ConnectionError):
        await task


async def test_commands_are_serialised_per_connection() -> None:
    """One at a time: these devices front a physically serial Modbus bus."""
    session, sent = make_session()
    session.command_interval = 0

    first = asyncio.create_task(session.send_command(commands.read_inverter(SERIAL, 6, 3)))
    second = asyncio.create_task(session.send_command(commands.read_inverter(SERIAL, 6, 4)))
    await asyncio.sleep(0.01)

    assert len(sent) == 1, "the second command was sent before the first was answered"

    await session.handle_frame(reply(0x05, 3, b"\x00\x01", Frame(sent[0]).sequence))
    await first
    await asyncio.sleep(0.01)

    assert len(sent) == 2
    await session.handle_frame(reply(0x05, 4, b"\x00\x02", Frame(sent[1]).sequence))
    assert (await second).value == 2


async def test_a_command_before_identification_is_refused() -> None:
    sent: list[bytes] = []

    async def send(data: bytes) -> None:
        sent.append(data)

    session = Session(1, send=send)
    with pytest.raises(ConnectionError, match="has not identified"):
        await session.send_command(commands.read_inverter(SERIAL, 6, 3))


async def test_two_connections_reading_the_same_register_do_not_collide() -> None:
    """The exact failure of correlating on the register number alone.

    Two dataloggers, both asked for register 3, answering in the other order. Keying on
    the connection means each future gets its own device's value.
    """
    server = GrowattServer(ServerConfig(host="127.0.0.1", port=0, push_time_on_announce=False))
    await server.start()
    try:
        first = FakeDatalogger(datalogger_serial="AAA0000001")
        second = FakeDatalogger(datalogger_serial="BBB0000002")
        await first.connect("127.0.0.1", server.port)
        await second.connect("127.0.0.1", server.port)

        # Announce so each session learns its serial and protocol.
        await first.send_announce()
        await second.send_announce()
        for device in (first, second):
            await device.read_frame()
        await asyncio.sleep(0.05)

        sessions = {s.datalogger_serial: s for s in server.sessions.values()}
        assert set(sessions) == {"AAA0000001", "BBB0000002"}
        for session in sessions.values():
            session.command_interval = 0

        task_a = asyncio.create_task(
            sessions["AAA0000001"].send_command(commands.read_inverter("AAA0000001", 6, 3))
        )
        task_b = asyncio.create_task(
            sessions["BBB0000002"].send_command(commands.read_inverter("BBB0000002", 6, 3))
        )
        await asyncio.sleep(0.05)

        request_a = await first.read_frame()
        request_b = await second.read_frame()

        # Answer in the opposite order, with each device's own value.
        await second.send_raw(
            build_frame(
                b"BBB0000002".ljust(30, b"\x00")
                + (3).to_bytes(2, "big")
                + (3).to_bytes(2, "big")  # reads echo the range
                + (222).to_bytes(2, "big"),
                protocol=6,
                function=0x05,
                sequence=request_b.sequence,
            )
        )
        await first.send_raw(
            build_frame(
                b"AAA0000001".ljust(30, b"\x00")
                + (3).to_bytes(2, "big")
                + (3).to_bytes(2, "big")  # reads echo the range
                + (111).to_bytes(2, "big"),
                protocol=6,
                function=0x05,
                sequence=request_a.sequence,
            )
        )

        assert (await asyncio.wait_for(task_a, 2)).value == 111
        assert (await asyncio.wait_for(task_b, 2)).value == 222

        await first.close()
        await second.close()
    finally:
        await server.stop()
