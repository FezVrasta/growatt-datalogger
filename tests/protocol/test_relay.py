"""The optional pass-through to the Growatt cloud."""

from __future__ import annotations

import asyncio

import pytest

from custom_components.growatt_datalogger.protocol.relay import RelayConfig
from custom_components.growatt_datalogger.protocol.server import (
    GrowattServer,
    ServerConfig,
)
from custom_components.growatt_datalogger.protocol.session import Record
from tests.fakes.datalogger import FakeDatalogger
from tests.fakes.upstream import FakeUpstream

pytestmark = pytest.mark.asyncio


async def _wait(predicate, timeout: float = 2.0) -> None:
    async def _loop() -> None:
        while not predicate():
            await asyncio.sleep(0.01)

    await asyncio.wait_for(_loop(), timeout)


async def _server(upstream: FakeUpstream | None, records: list[Record]) -> GrowattServer:
    relay = (
        RelayConfig(host="127.0.0.1", port=upstream.port, connect_timeout=1.0)
        if upstream is not None
        # An unreachable port, to exercise the connect failure path.
        else RelayConfig(host="127.0.0.1", port=1, connect_timeout=0.5)
    )
    server = GrowattServer(
        ServerConfig(host="127.0.0.1", port=0, relay=relay),
        on_record=records.append,
    )
    await server.start()
    return server


async def test_records_are_forwarded_verbatim() -> None:
    upstream = FakeUpstream()
    await upstream.start()
    records: list[Record] = []
    server = await _server(upstream, records)
    try:
        device = FakeDatalogger()
        await device.connect("127.0.0.1", server.port)
        sent = await device.send_data()

        await _wait(lambda: upstream.received)
        assert b"".join(upstream.received) == sent

        await device.close()
    finally:
        await server.stop()
        await upstream.stop()


async def test_local_decoding_still_happens_while_relaying() -> None:
    """The whole point: the cloud keeps working and we still get the data."""
    upstream = FakeUpstream()
    await upstream.start()
    records: list[Record] = []
    server = await _server(upstream, records)
    try:
        device = FakeDatalogger()
        await device.connect("127.0.0.1", server.port)
        await device.send_data()

        await _wait(lambda: records)
        assert records[0].payload.datalogger_serial == "GPG0EXAMP1"

        await device.close()
    finally:
        await server.stop()
        await upstream.stop()


async def test_we_stay_silent_while_the_relay_is_healthy() -> None:
    """Growatt sends the ACKs; two servers answering the same record is untested ground."""
    upstream = FakeUpstream()
    await upstream.start()
    records: list[Record] = []
    server = await _server(upstream, records)
    try:
        device = FakeDatalogger()
        await device.connect("127.0.0.1", server.port)
        await device.send_data()
        await _wait(lambda: records)

        await device.expect_nothing(within=0.2)

        await device.close()
    finally:
        await server.stop()
        await upstream.stop()


async def test_upstream_replies_are_returned_unchanged() -> None:
    upstream = FakeUpstream(auto_ack=True)
    await upstream.start()
    records: list[Record] = []
    server = await _server(upstream, records)
    try:
        device = FakeDatalogger()
        await device.connect("127.0.0.1", server.port)
        await device.send_data()

        ack = await device.read_frame()
        assert ack.function == 0x04

        await device.close()
    finally:
        await server.stop()
        await upstream.stop()


async def test_an_unreachable_cloud_does_not_drop_the_datalogger() -> None:
    """Degrading to local acknowledgement is the whole reason this path exists."""
    records: list[Record] = []
    server = await _server(None, records)
    try:
        device = FakeDatalogger()
        await device.connect("127.0.0.1", server.port)
        await device.send_data()

        ack = await device.read_frame()
        assert ack.function == 0x04
        await _wait(lambda: records)
        assert server.stats.relay_failures == 1

        await device.close()
    finally:
        await server.stop()


async def test_we_take_over_when_upstream_dies_mid_session() -> None:
    upstream = FakeUpstream()
    await upstream.start()
    records: list[Record] = []
    server = await _server(upstream, records)
    try:
        device = FakeDatalogger()
        await device.connect("127.0.0.1", server.port)
        await device.send_data()
        await _wait(lambda: records)
        await device.expect_nothing(within=0.2)  # relay healthy, we are silent

        await upstream.kill()
        await _wait(lambda: any(r is not None and r.degraded for r in server.relays.values()))

        await device.send_data()
        ack = await device.read_frame()
        assert ack.function == 0x04, "we did not take over acknowledgement"

        await device.close()
    finally:
        await server.stop()
        await upstream.stop()


async def test_takeover_is_sticky_for_the_rest_of_the_connection() -> None:
    """Flipping back mid-stream risks a window where nobody acknowledges."""
    upstream = FakeUpstream()
    await upstream.start()
    records: list[Record] = []
    server = await _server(upstream, records)
    try:
        device = FakeDatalogger()
        await device.connect("127.0.0.1", server.port)
        await device.send_data()
        await _wait(lambda: records)

        await upstream.kill()
        await device.send_data()
        await device.read_frame()

        # Upstream is back, but this connection keeps being answered locally.
        for _ in range(2):
            await device.send_data()
            ack = await device.read_frame()
            assert ack.function == 0x04

        await device.close()
    finally:
        await server.stop()
        await upstream.stop()


async def test_relaying_is_off_unless_configured() -> None:
    """Nothing leaves the network by default."""
    records: list[Record] = []
    server = GrowattServer(ServerConfig(host="127.0.0.1", port=0), on_record=records.append)
    await server.start()
    try:
        device = FakeDatalogger()
        await device.connect("127.0.0.1", server.port)
        await device.send_data()

        ack = await device.read_frame()
        assert ack.function == 0x04
        assert all(relay is None for relay in server.relays.values())

        await device.close()
    finally:
        await server.stop()
