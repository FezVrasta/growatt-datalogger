"""The library's tests run without Home Assistant, and without its test plugin.

They should also run when someone has both installed in one environment, which is the
normal case for anyone working on the integration too. The Home Assistant test plugin
auto-loads whenever it is importable and blocks real sockets, so the socket-based tests
here need it switched off explicitly.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import AsyncIterator, Awaitable, Callable, Generator
from importlib.util import find_spec
from pathlib import Path
from typing import NamedTuple

import pytest

# Allow running the suite straight from a checkout, before an editable install.
SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from growatt_protocol.server import GrowattServer, ServerConfig  # noqa: E402
from growatt_protocol.session import Record, Session  # noqa: E402
from growatt_protocol.testing import FakeUpstream  # noqa: E402


async def _wait_for(predicate: Callable[[], object], timeout: float = 2.0) -> None:
    async def _loop() -> None:
        while not predicate():
            await asyncio.sleep(0.01)

    await asyncio.wait_for(_loop(), timeout)


@pytest.fixture
def wait_for() -> Callable[..., Awaitable[None]]:
    """Poll until a predicate is true.

    A real socket sits between the fake device and the server, so most of what these
    tests assert on arrives with nothing to await. Polling for the condition beats a
    fixed sleep long enough to be safe on a loaded machine -- and a fixture beats an
    import, since ``tests`` is not a package.
    """
    return _wait_for


@pytest.fixture
def records() -> list[Record]:
    """Records the server decoded, in arrival order."""
    return []


SERIAL = "GPG0EXAMP1"


class SessionHarness(NamedTuple):
    """A :class:`Session` wired to lists rather than to a socket."""

    session: Session
    sent: list[bytes]
    records: list[Record]


@pytest.fixture
def make_session() -> Callable[..., SessionHarness]:
    """Build a session that sends into a list and collects what it decodes.

    ``identified`` says whether it already knows its serial and protocol. A session that
    has not seen a record refuses commands, so the command tests want it on, while the
    tests watching identification happen want it off -- which is the only thing the two
    hand-rolled builders this replaces actually disagreed about.

    The clock push is off and the inter-command pause is zero throughout: both are real
    behaviour, both are tested where they belong, and leaving them on here only buys
    wall clock.
    """

    def _make(*, identified: bool = False, **kwargs: object) -> SessionHarness:
        sent: list[bytes] = []
        records: list[Record] = []

        async def send(data: bytes) -> None:
            sent.append(data)

        kwargs.setdefault("on_record", records.append)
        session = Session(1, send=send, **kwargs)  # type: ignore[arg-type]
        session.push_time_on_announce = False
        session.command_interval = 0
        if identified:
            session.protocol = 6
            session.datalogger_serial = SERIAL
        return SessionHarness(session, sent, records)

    return _make


@pytest.fixture
async def upstream() -> AsyncIterator[FakeUpstream]:
    """A stand-in for the Growatt cloud, stopped on teardown."""
    fake = FakeUpstream()
    await fake.start()
    try:
        yield fake
    finally:
        await fake.stop()


@pytest.fixture
async def make_server(
    records: list[Record],
) -> AsyncIterator[Callable[..., Awaitable[GrowattServer]]]:
    """Start a server on an OS-assigned port, stopped on teardown.

    Every server test wants the same three things -- loopback, port 0, and no clock push
    to trip over -- and every one of them was writing its own try/finally around them.
    """
    started: list[GrowattServer] = []

    async def _make(**config: object) -> GrowattServer:
        server = GrowattServer(
            ServerConfig(host="127.0.0.1", port=0, push_time_on_announce=False, **config),  # type: ignore[arg-type]
            on_record=records.append,
        )
        await server.start()
        started.append(server)
        return server

    try:
        yield _make
    finally:
        for server in started:
            await server.stop()


if find_spec("pytest_homeassistant_custom_component") is not None:

    @pytest.fixture(autouse=True)
    def _allow_real_sockets(socket_enabled: None) -> Generator[None]:
        """This package *is* a socket server; its tests bind loopback and drive it.

        Faking the transport would leave the framing, the acknowledgement timing and the
        reassembly untested, which are the parts most likely to be wrong.
        """
        yield
