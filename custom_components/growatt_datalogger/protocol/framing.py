"""Incremental reassembly of Growatt frames from a TCP byte stream.

TCP is a stream, not a message queue. A datalogger's records arrive split across several
segments, or several records arrive coalesced into one. Treating each ``recv()`` as
exactly one record -- which is a common shortcut in this problem space -- corrupts data
as soon as either happens.

Feed arbitrary chunks to :meth:`Framer.feed` and it yields whole frames.

Frame length is derived from the header::

    total = 6 + declared_length + (2 if protocol in (5, 6) else 0)

``declared_length`` counts the device-id and function-code bytes plus the payload, but
excludes the trailing CRC that protocol 05/06 appends.
"""

from __future__ import annotations

from .crypt import OBFUSCATED_PROTOCOLS, SUPPORTED_PROTOCOLS
from .errors import FrameError

#: Largest frame we will assemble. Real records are well under 1 KB; this exists so a
#: corrupt or hostile length field cannot make us buffer without bound.
DEFAULT_MAX_FRAME = 8192

#: Bytes of header needed before the total frame length can be computed.
_LENGTH_PREFIX = 6

#: Smallest meaningful declared length: the device id and function code alone.
_MIN_DECLARED_LENGTH = 2


def frame_length(header: bytes) -> int:
    """Return the total on-the-wire length of the frame beginning with ``header``.

    ``header`` must be at least :data:`_LENGTH_PREFIX` bytes.

    Raises:
        FrameError: if the protocol version is unsupported or the declared length is
            structurally impossible.
    """
    if len(header) < _LENGTH_PREFIX:
        raise ValueError(f"need at least {_LENGTH_PREFIX} bytes to compute frame length")

    protocol = header[3]
    if protocol not in SUPPORTED_PROTOCOLS:
        raise FrameError(f"unsupported Growatt protocol version 0x{protocol:02x}")

    declared = int.from_bytes(header[4:6], "big")
    if declared < _MIN_DECLARED_LENGTH:
        raise FrameError(f"declared frame length {declared} is below the minimum")

    crc_length = 2 if protocol in OBFUSCATED_PROTOCOLS else 0
    return _LENGTH_PREFIX + declared + crc_length


class Framer:
    """Stateful reassembler. One instance per connection, per direction."""

    def __init__(self, max_frame: int = DEFAULT_MAX_FRAME) -> None:
        if max_frame < _LENGTH_PREFIX + _MIN_DECLARED_LENGTH:
            raise ValueError("max_frame is too small to hold any valid frame")
        self.max_frame = max_frame
        self._buffer = bytearray()

    @property
    def pending(self) -> bytes:
        """Bytes buffered so far that do not yet form a complete frame."""
        return bytes(self._buffer)

    def reset(self) -> None:
        """Discard buffered bytes. Use after an error, before reusing the instance."""
        self._buffer.clear()

    def feed(self, chunk: bytes) -> list[bytes]:
        """Add ``chunk`` to the buffer and return every frame it completes.

        Frames are returned in arrival order. Any trailing partial frame stays buffered
        for the next call.

        This is deliberately eager rather than a generator: the buffer must be updated
        even when the caller does not consume the result, and an error must surface at
        the call site rather than at some later iteration.

        Raises:
            FrameError: on an unsupported protocol version, an impossible declared
                length, or a frame exceeding ``max_frame``. The connection should be
                closed: once the length field is untrustworthy, so is the stream
                position. Frames completed before the bad one are lost with the
                exception, which is the right trade -- a stream we can no longer
                position ourselves in has no salvageable remainder.
        """
        self._buffer.extend(chunk)
        frames: list[bytes] = []

        while len(self._buffer) >= _LENGTH_PREFIX:
            total = frame_length(self._buffer)

            if total > self.max_frame:
                raise FrameError(f"frame length {total} exceeds maximum {self.max_frame}")

            if len(self._buffer) < total:
                break

            frames.append(bytes(self._buffer[:total]))
            del self._buffer[:total]

        return frames
