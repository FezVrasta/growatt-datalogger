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
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime

from .crc import check_crc
from .errors import RecordError
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

    def _handle_command_response(self, frame: Frame) -> None:
        """Placeholder until the command layer lands.

        Kept as a distinct branch so these frames are never acknowledged by accident.
        """
        _LOGGER.debug(
            "connection %s: unsolicited %#04x response",
            self.connection_id,
            frame.function,
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Release everything tied to the connection."""
        self.closed = True

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
