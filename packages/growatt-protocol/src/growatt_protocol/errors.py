"""Exceptions raised by the protocol layer."""

from __future__ import annotations


class GrowattProtocolError(Exception):
    """Base class for every protocol-layer failure."""


class FrameError(GrowattProtocolError):
    """A byte stream could not be split into frames.

    Raised for structurally impossible framing -- an unsupported protocol version, a
    declared length that cannot be valid, or a frame larger than the configured
    maximum. The connection should be closed, because the stream position is no longer
    trustworthy.
    """


class RecordError(GrowattProtocolError):
    """A frame was well-framed but its payload could not be interpreted.

    Unlike :class:`FrameError` this is recoverable: the frame boundary was correct, so
    the connection can continue with the next frame.
    """


class CommandTimeout(GrowattProtocolError):
    """A command was sent but no matching response arrived in time."""
