"""The TCP server dataloggers upload to.

A thin asyncio wrapper: accept a connection, reassemble frames, hand each to a
:class:`~.session.Session`. It holds no Home Assistant state, so the whole reply protocol
can be exercised against a fake datalogger with no event loop of Home Assistant's around.

Design notes worth keeping in mind when changing this:

* Reads are bounded by a timeout rather than left to block forever. Dataloggers ping
  every few minutes; ten minutes of silence means the peer is gone, and without a
  timeout a half-open connection leaks until the process restarts.
* Writes go through :meth:`_send`, which drains with a timeout. An implementation that
  never handles backpressure ends up busy-polling a permanently-writable socket set.
* Connection handler tasks are tracked and cancelled on shutdown.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import logging
from collections.abc import Callable
from dataclasses import dataclass

from .errors import FrameError
from .framing import DEFAULT_MAX_FRAME, Framer
from .records import Frame
from .session import Record, Session, gather_cancelled

_LOGGER = logging.getLogger(__name__)

DEFAULT_PORT = 5279


@dataclass(slots=True)
class ServerConfig:
    """Everything the server needs that is not a callback."""

    # Binding every interface is the point: the datalogger connects to us.
    host: str = "0.0.0.0"
    port: int = DEFAULT_PORT

    read_timeout: float = 600.0
    """Seconds of silence before a connection is considered dead.

    Dataloggers report every few minutes at most, so this is generous. It exists to
    reap half-open connections, not to police reporting intervals.
    """

    write_timeout: float = 10.0
    max_frame: int = DEFAULT_MAX_FRAME
    read_size: int = 4096
    shutdown_timeout: float = 5.0


@dataclass(slots=True)
class ServerStats:
    connections_accepted: int = 0
    connections_active: int = 0
    framing_errors: int = 0


class GrowattServer:
    """Listens for datalogger connections and turns their frames into records."""

    def __init__(
        self,
        config: ServerConfig,
        *,
        on_record: Callable[[Record], None] | None = None,
        on_identify: Callable[[str, str, int], None] | None = None,
        on_connection_change: Callable[[Session, bool], None] | None = None,
    ) -> None:
        self.config = config
        self._on_record = on_record
        self._on_identify = on_identify
        self._on_connection_change = on_connection_change

        self.stats = ServerStats()
        self.sessions: dict[int, Session] = {}

        self._server: asyncio.Server | None = None
        self._tasks: set[asyncio.Task[None]] = set()
        self._ids = itertools.count(1)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    @property
    def port(self) -> int:
        """The port actually bound.

        Not necessarily ``config.port``: binding port 0 asks the OS to choose, which is
        what tests do to avoid fighting over a fixed number.
        """
        if self._server is None or not self._server.sockets:
            return self.config.port
        return int(self._server.sockets[0].getsockname()[1])

    async def start(self) -> None:
        """Bind and begin accepting.

        Raises ``OSError`` -- typically ``EADDRINUSE`` -- which the caller should turn
        into a retryable setup failure rather than swallowing.
        """
        self._server = await asyncio.start_server(
            self._handle_client,
            self.config.host,
            self.config.port,
            start_serving=True,
        )
        _LOGGER.info("listening for Growatt dataloggers on port %s", self.port)

    async def stop(self) -> None:
        """Stop accepting, drop every connection, and wait for the handlers.

        The order here matters and is not the obvious one. Since Python 3.12.1,
        ``Server.wait_closed()`` does not return until every connection handler has
        finished -- so awaiting it before cancelling those handlers deadlocks, because
        each one is parked on a read with a ten-minute timeout. Close the listener,
        cancel the handlers, and only then wait.
        """
        server, self._server = self._server, None
        if server is not None:
            server.close()

        for session in list(self.sessions.values()):
            session.close()
        self.sessions.clear()

        await gather_cancelled(set(self._tasks), timeout=self.config.shutdown_timeout)
        self._tasks.clear()

        if server is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(server.wait_closed(), timeout=self.config.shutdown_timeout)
        _LOGGER.debug("Growatt server stopped")

    # ------------------------------------------------------------------
    # Connections
    # ------------------------------------------------------------------

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._tasks.add(task)

        connection_id = next(self._ids)
        peer = writer.get_extra_info("peername")
        framer = Framer(max_frame=self.config.max_frame)
        session = Session(
            connection_id,
            send=lambda data: self._send(writer, data),
            on_record=self._on_record,
            on_identify=self._on_identify,
        )

        self.sessions[connection_id] = session
        self.stats.connections_accepted += 1
        self.stats.connections_active += 1
        _LOGGER.debug("connection %s opened from %s", connection_id, peer)
        self._notify(session, True)

        try:
            await self._read_loop(reader, framer, session)
        except TimeoutError:
            _LOGGER.info(
                "connection %s: no data for %.0fs, closing",
                connection_id,
                self.config.read_timeout,
            )
        except FrameError as error:
            # The stream position is no longer trustworthy, so there is nothing to
            # recover: drop the connection and let the device reconnect.
            self.stats.framing_errors += 1
            _LOGGER.warning("connection %s: %s", connection_id, error)
        except (ConnectionResetError, BrokenPipeError):
            _LOGGER.debug("connection %s reset by peer", connection_id)
        except asyncio.CancelledError:
            raise
        # Deliberately broad: one bad connection must not take the server down.
        except Exception:
            _LOGGER.exception("connection %s failed", connection_id)
        finally:
            session.close()
            self.sessions.pop(connection_id, None)
            self.stats.connections_active -= 1
            self._notify(session, False)
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
            if task is not None:
                self._tasks.discard(task)
            _LOGGER.debug("connection %s closed", connection_id)

    async def _read_loop(
        self, reader: asyncio.StreamReader, framer: Framer, session: Session
    ) -> None:
        while True:
            data = await asyncio.wait_for(
                reader.read(self.config.read_size), timeout=self.config.read_timeout
            )
            if not data:
                return

            for raw in framer.feed(data):
                await session.handle_frame(Frame(raw))

    async def _send(self, writer: asyncio.StreamWriter, data: bytes) -> None:
        writer.write(data)
        await asyncio.wait_for(writer.drain(), timeout=self.config.write_timeout)

    def _notify(self, session: Session, connected: bool) -> None:
        if self._on_connection_change is None:
            return
        try:
            self._on_connection_change(session, connected)
        # A listener belongs to the application; its bugs are not ours to propagate.
        except Exception:
            _LOGGER.exception("connection listener failed")
