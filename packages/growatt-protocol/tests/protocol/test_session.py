"""Session reply behaviour, driven directly rather than over a socket."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import pytest
from growatt_protocol.records import Frame, Function
from growatt_protocol.session import Record, Session
from growatt_protocol.testing.frames import build_data_record, build_frame, build_group

pytestmark = pytest.mark.asyncio

MakeSession = Callable[..., object]


async def test_data_record_is_acknowledged_and_decoded(make_session: MakeSession) -> None:
    session, sent, records = make_session()
    await session.handle_frame(Frame(build_data_record()))

    assert len(sent) == 1
    assert len(records) == 1
    assert records[0].payload.registers == {3000: 1, 3001: 2, 3002: 3}
    assert not records[0].buffered


async def test_acknowledgement_is_sent_before_the_record_is_decoded() -> None:
    """The ordering invariant.

    A datalogger retransmits quickly if it is not acknowledged, so a slow record handler
    must not be able to delay the ACK. Here the handler blocks; the ACK must already be
    on the wire by the time it runs.
    """
    order: list[str] = []
    started = asyncio.Event()

    async def send(data: bytes) -> None:
        order.append("ack")

    def on_record(record: Record) -> None:
        order.append("decode")
        started.set()

    session = Session(1, send=send, on_record=on_record)
    task = asyncio.create_task(session.handle_frame(Frame(build_data_record())))
    await asyncio.wait_for(started.wait(), 1.0)
    await task

    assert order == ["ack", "decode"]


async def test_ping_is_echoed_byte_for_byte(make_session: MakeSession) -> None:
    session, sent, _ = make_session()
    raw = build_frame(b"GPG0EXAMP1" + bytes(22), protocol=6, function=Function.PING)

    await session.handle_frame(Frame(raw))

    assert sent == [raw]
    assert session.stats.pings == 1


async def test_command_responses_are_never_acknowledged(make_session: MakeSession) -> None:
    """Acknowledging a reply to our own command would be a protocol error."""
    session, sent, _ = make_session()

    for function in (0x05, 0x06, 0x10, 0x18, 0x19):
        await session.handle_frame(Frame(build_frame(b"\x00" * 40, function=function)))

    assert sent == []


async def test_ignored_function_gets_no_reply(make_session: MakeSession) -> None:
    session, sent, _ = make_session()
    await session.handle_frame(Frame(build_frame(b"\x00" * 8, function=0x29)))
    assert sent == []


async def test_unknown_function_is_acknowledged_and_counted(make_session: MakeSession) -> None:
    """Silence would make the device retransmit and eventually drop the link."""
    session, sent, _ = make_session()

    await session.handle_frame(Frame(build_frame(b"\x00" * 8, function=0x7A)))
    await session.handle_frame(Frame(build_frame(b"\x00" * 8, function=0x7A)))

    assert len(sent) == 2
    assert session.stats.unknown_functions == {0x7A: 2}


async def test_smart_meter_record_is_acknowledged_but_not_decoded(
    make_session: MakeSession,
) -> None:
    session, sent, records = make_session()
    await session.handle_frame(Frame(build_frame(b"\x00" * 60, function=0x20)))

    assert len(sent) == 1
    assert records == []


async def test_buffered_records_are_flagged(make_session: MakeSession) -> None:
    session, _, records = make_session()
    await session.handle_frame(Frame(build_data_record(function=0x50)))

    assert records[0].buffered


async def test_announce_identifies_the_device() -> None:
    seen: list[tuple[str, str, int]] = []

    async def send(data: bytes) -> None:
        pass

    session = Session(1, send=send, on_identify=lambda *args: seen.append(args))
    session.push_time_on_announce = False
    await session.handle_frame(Frame(build_data_record(function=0x03)))

    assert seen == [("GPG0AAAAA1", "SML0BBBBB2", 6)]
    assert session.datalogger_serial == "GPG0AAAAA1"


async def test_crc_mismatch_is_counted_but_the_record_still_decodes(
    make_session: MakeSession,
) -> None:
    """A device that fails CRC on every record must not be silently dropped."""
    session, sent, records = make_session()
    corrupted = bytearray(build_data_record())
    corrupted[-1] ^= 0xFF

    await session.handle_frame(Frame(bytes(corrupted)))

    assert session.stats.crc_mismatches == 1
    assert len(sent) == 1
    assert len(records) == 1


async def test_undecodable_record_is_still_acknowledged(make_session: MakeSession) -> None:
    """The device gets its ACK even when we cannot make sense of the payload."""
    session, sent, records = make_session()
    body = build_data_record()[8:-2] + b"\xff\xff"  # trailing junk after the groups
    frame = Frame(build_frame(body, protocol=6, function=0x04))

    await session.handle_frame(frame)

    assert len(sent) == 1
    assert records == []
    assert session.stats.decode_errors == 1


async def test_a_throwing_record_handler_does_not_break_the_session() -> None:
    async def send(data: bytes) -> None:
        pass

    def explode(record: Record) -> None:
        raise RuntimeError("handler bug")

    session = Session(1, send=send, on_record=explode)
    await session.handle_frame(Frame(build_data_record()))  # must not raise

    assert session.stats.records == 1


async def test_suppressed_replies_send_nothing(make_session: MakeSession) -> None:
    """Used when an upstream relay is acknowledging on our behalf."""
    session, sent, records = make_session(suppress_replies=True)
    await session.handle_frame(Frame(build_data_record()))

    assert sent == []
    assert len(records) == 1  # still decoded locally


async def test_ranges_are_exposed_for_profile_resolution(make_session: MakeSession) -> None:
    session, _, records = make_session()
    await session.handle_frame(
        Frame(
            build_data_record(groups=[build_group(3000, [0] * 125), build_group(3125, [0] * 125)])
        )
    )

    assert records[0].ranges == ((3000, 3124), (3125, 3249))
