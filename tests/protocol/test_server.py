"""End-to-end tests against a real socket and a fake datalogger."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest

from custom_components.growatt_datalogger.protocol.server import (
    GrowattServer,
    ServerConfig,
)
from custom_components.growatt_datalogger.protocol.session import Record
from tests.fakes.datalogger import FakeDatalogger

pytestmark = pytest.mark.asyncio


class Harness:
    def __init__(self) -> None:
        self.records: list[Record] = []
        self.identities: list[tuple[str, str, int]] = []
        self.connections: list[bool] = []
        self.server = GrowattServer(
            # Port 0 lets the OS pick, so tests never fight over a fixed number.
            ServerConfig(host="127.0.0.1", port=0),
            on_record=self.records.append,
            on_identify=lambda *args: self.identities.append(args),
            on_connection_change=lambda _s, up: self.connections.append(up),
        )

    async def wait_for_records(self, count: int, timeout: float = 2.0) -> None:
        async def _wait() -> None:
            while len(self.records) < count:
                await asyncio.sleep(0.01)

        await asyncio.wait_for(_wait(), timeout)


@pytest.fixture
async def harness() -> AsyncIterator[Harness]:
    h = Harness()
    await h.server.start()
    try:
        yield h
    finally:
        await h.server.stop()


async def _connect(harness: Harness, **kwargs: object) -> FakeDatalogger:
    device = FakeDatalogger(**kwargs)  # type: ignore[arg-type]
    await device.connect("127.0.0.1", harness.server.port)
    return device


async def test_a_data_record_is_acknowledged_over_the_wire(harness: Harness) -> None:
    device = await _connect(harness)
    async with device:
        sent = await device.send_data()
        ack = await device.read_frame()

        assert ack.function == 0x04
        assert ack.sequence == 1
        assert len(ack.raw) == 11  # protocol 06 ACK
        await harness.wait_for_records(1)
        assert harness.records[0].payload.datalogger_serial == "GPG0EXAMP1"
        assert sent  # the record really did go out


async def test_ping_is_echoed_verbatim(harness: Harness) -> None:
    device = await _connect(harness)
    async with device:
        sent = await device.send_ping()
        echo = await device.read_frame()
        assert echo.raw == sent


@pytest.mark.parametrize("chunk_size", [1, 3, 7])
async def test_records_fragmented_across_writes_are_reassembled(
    harness: Harness, chunk_size: int
) -> None:
    """One byte per write is the pathological case a naive reader gets wrong."""
    device = await _connect(harness, chunk_size=chunk_size)
    async with device:
        await device.send_data()
        await harness.wait_for_records(1)

        assert harness.records[0].payload.registers == {3000: 1, 3001: 2585, 3002: 3295}


async def test_records_coalesced_into_one_write_are_split(harness: Harness) -> None:
    device = await _connect(harness)
    async with device:
        await device.send_coalesced(3)
        await harness.wait_for_records(3)

        assert len(harness.records) == 3


async def test_announce_reports_the_device_identity(harness: Harness) -> None:
    device = await _connect(harness)
    async with device:
        await device.send_announce()
        await harness.wait_for_records(1)

        assert harness.identities == [("GPG0EXAMP1", "SML0EXAMP2", 6)]


@pytest.mark.parametrize("protocol", [2, 5, 6])
async def test_every_protocol_version_round_trips(harness: Harness, protocol: int) -> None:
    device = await _connect(harness, protocol=protocol)
    async with device:
        await device.send_data()
        ack = await device.read_frame()

        assert ack.protocol == protocol
        assert len(ack.raw) == (9 if protocol == 2 else 11)
        await harness.wait_for_records(1)


async def test_two_devices_are_tracked_independently(harness: Harness) -> None:
    first = await _connect(harness, datalogger_serial="AAA0000001")
    second = await _connect(harness, datalogger_serial="BBB0000002")
    async with first, second:
        await first.send_data()
        await second.send_data()
        await harness.wait_for_records(2)

        serials = {record.payload.datalogger_serial for record in harness.records}
        assert serials == {"AAA0000001", "BBB0000002"}
        assert harness.server.stats.connections_accepted == 2


async def test_connection_changes_are_reported(harness: Harness) -> None:
    device = await _connect(harness)
    await device.send_data()
    await harness.wait_for_records(1)
    await device.close()

    async def _wait() -> None:
        while harness.connections != [True, False]:
            await asyncio.sleep(0.01)

    await asyncio.wait_for(_wait(), 2.0)
    assert harness.server.stats.connections_active == 0


async def test_a_malformed_length_closes_only_that_connection(
    harness: Harness,
) -> None:
    bad = await _connect(harness)
    good = await _connect(harness)
    async with good:
        # A declared length far beyond the frame maximum.
        await bad.send_raw(
            (1).to_bytes(2, "big") + b"\x00\x06" + (60000).to_bytes(2, "big") + b"\x01\x04"
        )

        async def _wait() -> None:
            while harness.server.stats.framing_errors == 0:
                await asyncio.sleep(0.01)

        await asyncio.wait_for(_wait(), 2.0)
        await bad.close()

        # The healthy connection is untouched.
        await good.send_data()
        await harness.wait_for_records(1)


async def test_stop_releases_the_port(harness: Harness) -> None:
    port = harness.server.port
    await harness.server.stop()

    # Re-binding the same port proves the listener is really gone.
    probe = await asyncio.start_server(lambda r, w: None, "127.0.0.1", port)
    probe.close()
    await probe.wait_closed()


async def test_stop_cancels_live_connection_handlers(harness: Harness) -> None:
    device = await _connect(harness)
    await device.send_data()
    await harness.wait_for_records(1)

    await harness.server.stop()

    assert harness.server.sessions == {}
    assert not [task for task in asyncio.all_tasks() if "_handle_client" in repr(task)]
    await device.close()


async def test_a_dead_connection_is_reaped_after_the_read_timeout() -> None:
    server = GrowattServer(ServerConfig(host="127.0.0.1", port=0, read_timeout=0.1))
    await server.start()
    try:
        device = FakeDatalogger()
        await device.connect("127.0.0.1", server.port)

        async def _wait() -> None:
            while server.stats.connections_active:
                await asyncio.sleep(0.01)

        await asyncio.wait_for(_wait(), 2.0)
        await device.close()
    finally:
        await server.stop()
