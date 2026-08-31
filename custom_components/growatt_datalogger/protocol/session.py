"""Per-connection state machine for one datalogger.

Owns the reply behaviour a datalogger expects. The rule that matters most is that a
record's acknowledgement goes out **before** anything tries to decode it: the device's
retransmit timer is short, and a slow or throwing decoder must never be able to delay an
ACK. Decode failures are caught and reported, never allowed to reach the socket loop.

This module is transport-agnostic. It is handed a ``send`` coroutine and calls it; the
asyncio plumbing lives in :mod:`.server`. That is what lets the whole reply protocol be
tested against an in-memory list.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime

from . import commands
from .commands import Command, CommandResponse, parse_command_response
from .crc import check_crc
from .errors import CommandTimeout, GrowattProtocolError, RecordError
from .records import (
    COMMAND_RESPONSE_FUNCTIONS,
    METER_FUNCTIONS,
    REGISTER_RECORD_FUNCTIONS,
    Frame,
    Function,
    RecordPayload,
    build_ack,
    build_ping_echo,
    parse_register_record,
)

_LOGGER = logging.getLogger(__name__)

SendCallback = Callable[[bytes], Awaitable[None]]

#: Seconds to wait for a device to answer a command.
DEFAULT_COMMAND_TIMEOUT = 8.0

#: Pause between consecutive commands on one connection. These are small single-threaded
#: devices fronting a serial Modbus bus; giving them a moment between requests avoids
#: NAKs that would otherwise look like timeouts.
DEFAULT_COMMAND_INTERVAL = 0.15

#: Attempts for a read. Reads are idempotent, so retrying is free.
READ_ATTEMPTS = 3

#: Pause between acknowledging an announce and setting the device's clock, matching the
#: gap a datalogger sees when talking to the vendor's own server.
DEFAULT_TIME_SYNC_DELAY = 1.0

#: A key identifying an outstanding command. The connection id rather than the serial:
#: a device that reconnects gets a new id, so a reply from the previous socket can never
#: satisfy a request made on the new one.
PendingKey = tuple[int, int, int]  # (connection_id, response function, sequence)


@dataclass(slots=True)
class SessionStats:
    """Counters worth exposing as diagnostics."""

    records: int = 0
    acknowledged: int = 0
    pings: int = 0
    decode_errors: int = 0
    crc_mismatches: int = 0
    unknown_functions: dict[int, int] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Record:
    """A successfully decoded telemetry record, handed to the application."""

    frame: Frame
    payload: RecordPayload
    buffered: bool
    """True for a 0x50 replay of historical data.

    These carry a past timestamp. Writing them to live state would corrupt long-term
    statistics and invent power spikes, so the application must treat them differently
    rather than merging them with live telemetry.
    """

    @property
    def ranges(self) -> tuple[tuple[int, int], ...]:
        return tuple((group.start, group.end) for group in self.payload.groups)


class Session:
    """Handles the frames of a single datalogger connection."""

    def __init__(
        self,
        connection_id: int,
        send: SendCallback,
        *,
        on_record: Callable[[Record], None] | None = None,
        on_identify: Callable[[str, str, int], None] | None = None,
        suppress_replies: bool = False,
        now: Callable[[], datetime] = datetime.now,
    ) -> None:
        self.connection_id = connection_id
        self._send = send
        self._on_record = on_record
        self._on_identify = on_identify
        self.stats = SessionStats()

        self.suppress_replies = suppress_replies
        """Set when an upstream relay is answering on our behalf.

        Two servers acknowledging the same record with the same sequence number is a
        situation no datalogger is documented to handle, so exactly one of us replies.
        """

        self._now = now
        self.datalogger_serial: str | None = None
        self.inverter_serial: str | None = None
        self.protocol: int | None = None
        self.last_seen: datetime | None = None
        self.closed = False

        self.command_timeout = DEFAULT_COMMAND_TIMEOUT
        self.command_interval = DEFAULT_COMMAND_INTERVAL

        self.push_time_on_announce = True
        self.time_sync_delay = DEFAULT_TIME_SYNC_DELAY
        self.time_synced = False
        self._time_task: asyncio.Task[None] | None = None

        self._sequence = 0
        self._pending: dict[PendingKey, asyncio.Future[CommandResponse]] = {}
        self._pending_by_register: dict[tuple[int, int, int], asyncio.Future[CommandResponse]] = {}
        self._command_lock = asyncio.Lock()
        self._unsolicited: deque[CommandResponse] = deque(maxlen=32)

    # ------------------------------------------------------------------
    # Frame handling
    # ------------------------------------------------------------------

    async def handle_frame(self, frame: Frame) -> None:
        """Reply to ``frame`` if it needs one, then decode it if it carries data."""
        self.last_seen = self._now()
        self.protocol = frame.protocol
        self.stats.records += 1

        if frame.has_crc and not check_crc(frame.raw):
            # Advisory, deliberately. Some dataloggers fail this on every record while
            # emitting perfectly decodable payloads; refusing them loses all data from
            # that device. Frame length is the real validity gate.
            self.stats.crc_mismatches += 1
            _LOGGER.debug(
                "connection %s: CRC mismatch on function %#04x, decoding anyway",
                self.connection_id,
                frame.function,
            )

        function = frame.function

        if function == Function.PING:
            self.stats.pings += 1
            await self._reply(build_ping_echo(frame))
            return

        if function in COMMAND_RESPONSE_FUNCTIONS:
            # Replies to commands we issued. Acknowledging one would be a protocol
            # error; correlation is the command layer's job.
            self._handle_command_response(frame)
            return

        if function == Function.IGNORED:
            return

        if function in REGISTER_RECORD_FUNCTIONS:
            await self._reply(build_ack(frame))
            self._decode(frame)
            if function == Function.ANNOUNCE:
                self._schedule_time_sync()
            return

        if function in METER_FUNCTIONS:
            # Smart meters use an ASCII key/value log rather than register groups. The
            # device still expects an acknowledgement, so send one and stop there until
            # that format is implemented.
            await self._reply(build_ack(frame))
            self._note_unknown(function)
            return

        # An unrecognised function. Acknowledge it -- a datalogger that gets no reply
        # will retransmit and eventually drop the connection, and silence is a worse
        # failure than a possibly-unnecessary ACK -- but count it so it surfaces.
        self._note_unknown(function)
        await self._reply(build_ack(frame))

    async def _reply(self, data: bytes) -> None:
        if self.suppress_replies:
            return
        await self._send(data)
        self.stats.acknowledged += 1

    def _note_unknown(self, function: int) -> None:
        count = self.stats.unknown_functions.get(function, 0)
        self.stats.unknown_functions[function] = count + 1
        if count == 0:
            _LOGGER.info(
                "connection %s: unhandled function %#04x from %s",
                self.connection_id,
                function,
                self.datalogger_serial or "an unidentified device",
            )

    def _decode(self, frame: Frame) -> None:
        """Parse a record and hand it on. Never raises into the socket loop."""
        try:
            payload = parse_register_record(frame)
        except RecordError as error:
            self.stats.decode_errors += 1
            _LOGGER.warning(
                "connection %s: could not decode a %#04x record: %s",
                self.connection_id,
                frame.function,
                error,
            )
            return

        self.datalogger_serial = payload.datalogger_serial
        if payload.inverter_serial:
            self.inverter_serial = payload.inverter_serial

        if frame.function == Function.ANNOUNCE and self._on_identify is not None:
            self._on_identify(payload.datalogger_serial, payload.inverter_serial, frame.protocol)

        if self._on_record is not None:
            record = Record(
                frame=frame,
                payload=payload,
                buffered=frame.function == Function.BUFFERED,
            )
            try:
                self._on_record(record)
            # Deliberately broad: an application bug must not drop the connection.
            except Exception:
                _LOGGER.exception("connection %s: record handler failed", self.connection_id)

    # ------------------------------------------------------------------
    # Time sync
    # ------------------------------------------------------------------

    def _schedule_time_sync(self) -> None:
        """Set the device's clock after it announces itself.

        A datalogger expects the server to do this. Left unset, it announces, waits,
        gives up and reconnects -- announcing and pinging forever without ever sending a
        telemetry record. Nothing in the exchange says the clock is what it is waiting
        for; the symptom is simply that no data arrives.

        Sent as a task rather than awaited inline, because the read loop is what feeds
        the reply to the command we are about to issue: awaiting it here would deadlock
        until the timeout.
        """
        if self.suppress_replies or not self.push_time_on_announce:
            # In relay mode the cloud sets the clock, and two servers doing it would
            # race over the same sequence space.
            return
        if self._time_task is not None and not self._time_task.done():
            return
        self._time_task = asyncio.create_task(self._push_time())

    async def _push_time(self) -> None:
        try:
            # A short pause after the acknowledgement, matching the gap a datalogger
            # sees when talking to the vendor's own server.
            await asyncio.sleep(self.time_sync_delay)
            if self.closed or self.datalogger_serial is None or self.protocol is None:
                return
            response = await self.send_command(
                commands.set_time(self.datalogger_serial, self.protocol, self._now())
            )
        except asyncio.CancelledError:
            raise
        except (CommandTimeout, ConnectionError, GrowattProtocolError) as error:
            _LOGGER.debug("connection %s: could not set the clock: %s", self.connection_id, error)
            return
        except Exception:
            _LOGGER.exception("connection %s: clock update failed", self.connection_id)
            return

        if response.ok:
            self.time_synced = True
            _LOGGER.debug("connection %s: clock set", self.connection_id)
        else:
            _LOGGER.warning(
                "connection %s: device rejected the clock update (result %s)",
                self.connection_id,
                response.result,
            )

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    def _next_sequence(self) -> int:
        """Allocate a sequence number not currently outstanding.

        A real counter, unlike implementations that leave it pinned at 1 and then have
        nothing to correlate replies against. Zero and 0xFFFF are avoided because
        devices have been seen using them as sentinels.
        """
        for _ in range(0xFFFE):
            self._sequence = (self._sequence % 0xFFFD) + 1
            if not any(key[2] == self._sequence for key in self._pending):
                return self._sequence
        raise RuntimeError("no free sequence numbers")

    async def send_command(
        self, command: Command, *, timeout: float | None = None
    ) -> CommandResponse:
        """Send ``command`` and wait for the device's reply.

        One command in flight per connection. These are single-threaded devices fronting
        a physically serial Modbus bus, so pipelining buys nothing and costs correctness:
        serialising also makes the register fallback below unambiguous.
        """
        if self.closed:
            raise ConnectionError("the datalogger is not connected")
        if self.protocol is None or self.datalogger_serial is None:
            raise ConnectionError("the datalogger has not identified itself yet")

        async with self._command_lock:
            response = await self._send_once(command, timeout or self.command_timeout)
            if self.command_interval:
                await asyncio.sleep(self.command_interval)
            return response

    async def _send_once(self, command: Command, timeout: float) -> CommandResponse:
        sequence = self._next_sequence()
        loop = asyncio.get_running_loop()
        future: asyncio.Future[CommandResponse] = loop.create_future()

        key = (self.connection_id, command.response_function, sequence)
        # It is not established that every firmware echoes the request's sequence, so a
        # second index on the register number acts as a fallback. Because commands are
        # serialised, at most one request can match it at a time. The DEBUG line below
        # fires when the fallback is used; once that is seen (or not) on real hardware,
        # one of the two indexes can go.
        register_key = (self.connection_id, command.response_function, command.register)
        self._pending[key] = future
        self._pending_by_register[register_key] = future

        try:
            assert self.protocol is not None
            await self._send(command.build(sequence, self.protocol))
            return await asyncio.wait_for(future, timeout)
        except TimeoutError:
            raise CommandTimeout(
                f"no reply to {command.function:#04x} for register {command.register} "
                f"(sequence {sequence})"
            ) from None
        finally:
            self._pending.pop(key, None)
            self._pending_by_register.pop(register_key, None)

    def _handle_command_response(self, frame: Frame) -> None:
        """Match a reply to the command that is waiting for it."""
        try:
            response = parse_command_response(frame)
        except RecordError as error:
            _LOGGER.warning(
                "connection %s: unparseable %#04x response: %s",
                self.connection_id,
                frame.function,
                error,
            )
            return

        future = self._pending.get((self.connection_id, frame.function, frame.sequence))
        if future is None and response.register is not None:
            future = self._pending_by_register.get(
                (self.connection_id, frame.function, response.register)
            )
            if future is not None:
                _LOGGER.debug(
                    "connection %s: %#04x reply carried sequence %s, matched on "
                    "register %s instead",
                    self.connection_id,
                    frame.function,
                    frame.sequence,
                    response.register,
                )

        if future is None:
            # Nothing is waiting. Either a late reply to a command that already timed
            # out, or -- in relay mode -- a reply to something the cloud asked for.
            self._unsolicited.append(response)
            _LOGGER.debug(
                "connection %s: unsolicited %#04x reply for register %s",
                self.connection_id,
                frame.function,
                response.register,
            )
            return

        if not future.done():
            future.set_result(response)

    @property
    def unsolicited(self) -> tuple[CommandResponse, ...]:
        """Recent replies nothing was waiting for, for diagnostics."""
        return tuple(self._unsolicited)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Release everything tied to the connection.

        Outstanding commands are failed rather than abandoned. A caller left awaiting a
        future that will never resolve would hang until its own timeout -- during
        shutdown, that means Home Assistant waiting on a dead socket.
        """
        self.closed = True
        if self._time_task is not None and not self._time_task.done():
            self._time_task.cancel()
        for future in list(self._pending.values()):
            if not future.done():
                future.set_exception(ConnectionError("the datalogger disconnected"))
        self._pending.clear()
        self._pending_by_register.clear()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"Session(id={self.connection_id}, "
            f"datalogger={self.datalogger_serial!r}, protocol={self.protocol})"
        )


async def gather_cancelled(tasks: set[asyncio.Task[None]], timeout: float = 5.0) -> None:
    """Cancel ``tasks`` and wait for them, bounded by ``timeout``.

    Connection handlers are spawned by ``asyncio.start_server`` and are not awaited by
    ``Server.wait_closed()`` on every Python version, so shutdown has to track and
    cancel them explicitly or the event loop is left with stragglers.
    """
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.wait(tasks, timeout=timeout)
