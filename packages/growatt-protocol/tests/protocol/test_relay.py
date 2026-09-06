"""The optional pass-through to the Growatt cloud."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import pytest
from growatt_protocol.relay import RelayConfig
from growatt_protocol.server import GrowattServer
from growatt_protocol.session import Record
from growatt_protocol.testing import FakeDatalogger, FakeUpstream

pytestmark = pytest.mark.asyncio

MakeServer = Callable[..., Awaitable[GrowattServer]]
WaitFor = Callable[..., Awaitable[None]]

#: A cloud that is not there. Port 1 is reserved and refuses immediately, which is the
#: connect-failure path rather than a timeout.
UNREACHABLE = RelayConfig(host="127.0.0.1", port=1, connect_timeout=0.5)


async def relaying_to(make_server: MakeServer, upstream: FakeUpstream) -> GrowattServer:
    return await make_server(
        relay=RelayConfig(host="127.0.0.1", port=upstream.port, connect_timeout=1.0)
    )


async def connected(server: GrowattServer) -> FakeDatalogger:
    device = FakeDatalogger()
    await device.connect("127.0.0.1", server.port)
    return device


async def test_records_are_forwarded_verbatim(
    make_server: MakeServer, upstream: FakeUpstream, wait_for: WaitFor
) -> None:
    server = await relaying_to(make_server, upstream)
    async with await connected(server) as device:
        sent = await device.send_data()

        await wait_for(lambda: upstream.received)
        assert b"".join(upstream.received) == sent


async def test_local_decoding_still_happens_while_relaying(
    make_server: MakeServer, upstream: FakeUpstream, records: list[Record], wait_for: WaitFor
) -> None:
    """The whole point: the cloud keeps working and we still get the data."""
    server = await relaying_to(make_server, upstream)
    async with await connected(server) as device:
        await device.send_data()

        await wait_for(lambda: records)
        assert records[0].payload.datalogger_serial == "GPG0EXAMP1"


async def test_we_stay_silent_while_the_relay_is_healthy(
    make_server: MakeServer, upstream: FakeUpstream, records: list[Record], wait_for: WaitFor
) -> None:
    """Growatt sends the ACKs; two servers answering the same record is untested ground."""
    server = await relaying_to(make_server, upstream)
    async with await connected(server) as device:
        await device.send_data()
        await wait_for(lambda: records)

        await device.expect_nothing(within=0.2)


async def test_upstream_replies_are_returned_unchanged(make_server: MakeServer) -> None:
    upstream = FakeUpstream(auto_ack=True)
    await upstream.start()
    try:
        server = await relaying_to(make_server, upstream)
        async with await connected(server) as device:
            await device.send_data()

            ack = await device.read_frame()
            assert ack.function == 0x04
    finally:
        await upstream.stop()


async def test_an_unreachable_cloud_does_not_drop_the_datalogger(
    make_server: MakeServer, records: list[Record], wait_for: WaitFor
) -> None:
    """Degrading to local acknowledgement is the whole reason this path exists."""
    server = await make_server(relay=UNREACHABLE)
    async with await connected(server) as device:
        await device.send_data()

        ack = await device.read_frame()
        assert ack.function == 0x04
        await wait_for(lambda: records)
        assert server.stats.relay_failures == 1


async def test_we_take_over_when_upstream_dies_mid_session(
    make_server: MakeServer, upstream: FakeUpstream, records: list[Record], wait_for: WaitFor
) -> None:
    server = await relaying_to(make_server, upstream)
    async with await connected(server) as device:
        await device.send_data()
        await wait_for(lambda: records)
        await device.expect_nothing(within=0.2)  # relay healthy, we are silent

        await upstream.kill()
        await wait_for(lambda: any(r is not None and r.degraded for r in server.relays.values()))

        await device.send_data()
        ack = await device.read_frame()
        assert ack.function == 0x04, "we did not take over acknowledgement"


async def test_takeover_is_sticky_for_the_rest_of_the_connection(
    make_server: MakeServer, upstream: FakeUpstream, records: list[Record], wait_for: WaitFor
) -> None:
    """Flipping back mid-stream risks a window where nobody acknowledges."""
    server = await relaying_to(make_server, upstream)
    async with await connected(server) as device:
        await device.send_data()
        await wait_for(lambda: records)

        await upstream.kill()
        await device.send_data()
        await device.read_frame()

        # Upstream is back, but this connection keeps being answered locally.
        for _ in range(2):
            await device.send_data()
            ack = await device.read_frame()
            assert ack.function == 0x04


async def test_relaying_is_off_unless_configured(make_server: MakeServer) -> None:
    """Nothing leaves the network by default."""
    server = await make_server()
    async with await connected(server) as device:
        await device.send_data()

        ack = await device.read_frame()
        assert ack.function == 0x04
        assert all(relay is None for relay in server.relays.values())
