"""Commands the server sends to a datalogger, and the replies it gets back.

Four kinds, distinguished by function code:

===========  =============================================  ================
Function     Meaning                                        Reply
===========  =============================================  ================
``0x19``     read a datalogger parameter                    ``0x19``
``0x18``     write a datalogger parameter (incl. the clock) ``0x18``
``0x05``     read an inverter holding register              ``0x05``
``0x06``     write one inverter holding register            ``0x06``
``0x10``     write a range of inverter holding registers    ``0x10``
===========  =============================================  ================

The body always begins with the datalogger serial, padded to the same width the device
uses in its own records: 30 bytes on protocol 06, 10 bytes on 02 and 05. Everything after
that shifts accordingly, which is why the response parsers take an offset.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import IntEnum

from .crc import append_crc
from .crypt import OBFUSCATED_PROTOCOLS, obfuscate
from .errors import RecordError
from .records import Frame, Function

#: Datalogger parameter holding the wall clock, as an ASCII timestamp.
REGISTER_TIME = 0x1F

#: Other datalogger parameters, as reported by the device in 0x19 replies.
REGISTER_UPDATE_INTERVAL = 0x04
REGISTER_SERIAL = 0x08
REGISTER_SERVER_IP = 0x11
REGISTER_SERVER_PORT = 0x12
REGISTER_TIMEZONE = 0x1E

_SERIAL_WIDTH = {2: 10, 5: 10, 6: 30}


class Target(IntEnum):
    """What a command addresses."""

    DATALOGGER = 0
    INVERTER = 1


def _serial_field(serial: str, protocol: int) -> bytes:
    width = _SERIAL_WIDTH.get(protocol)
    if width is None:
        raise RecordError(f"no serial width known for protocol {protocol}")
    encoded = serial.encode("ascii")
    if len(encoded) > width:
        raise RecordError(f"serial {serial!r} does not fit in {width} bytes")
    return encoded.ljust(width, b"\x00")


def _frame(sequence: int, protocol: int, device_id: int, function: int, body: bytes) -> bytes:
    declared = 2 + len(body)
    frame = (
        (sequence & 0xFFFF).to_bytes(2, "big")
        + b"\x00"
        + bytes([protocol])
        + declared.to_bytes(2, "big")
        + bytes([device_id, function])
        + body
    )
    if protocol in OBFUSCATED_PROTOCOLS:
        return append_crc(obfuscate(frame, protocol))
    return frame


@dataclass(frozen=True, slots=True)
class Command:
    """A command ready to be given a sequence number and sent."""

    function: int
    register: int
    body: bytes
    device_id: int = 0x01

    @property
    def response_function(self) -> int:
        """The function code the device answers with. Always the request's own."""
        return self.function

    def build(self, sequence: int, protocol: int) -> bytes:
        return _frame(sequence, protocol, self.device_id, self.function, self.body)


# ----------------------------------------------------------------------------------
# Builders
# ----------------------------------------------------------------------------------


def read_datalogger(serial: str, protocol: int, register: int) -> Command:
    """Read one datalogger parameter (0x19)."""
    body = _serial_field(serial, protocol)
    body += register.to_bytes(2, "big") + register.to_bytes(2, "big")
    return Command(Function.CONFIG_READ, register, body)


def write_datalogger(serial: str, protocol: int, register: int, value: str) -> Command:
    """Write one datalogger parameter (0x18).

    Datalogger parameters are strings, length-prefixed -- unlike inverter registers,
    which are bare 16-bit words.
    """
    encoded = value.encode("utf-8")
    body = _serial_field(serial, protocol)
    body += register.to_bytes(2, "big")
    body += len(encoded).to_bytes(2, "big") + encoded
    return Command(Function.CONFIG_WRITE, register, body)


def set_time(serial: str, protocol: int, when: datetime) -> Command:
    """Set the datalogger clock (0x18, register 0x1f).

    The device wants ``YYYY-MM-DD HH:MM:SS`` as ASCII -- 19 bytes. Sent in local time,
    since that is what the timestamps in its own records are expressed in.
    """
    text = when.replace(microsecond=0).strftime("%Y-%m-%d %H:%M:%S")
    return write_datalogger(serial, protocol, REGISTER_TIME, text)


def read_inverter(serial: str, protocol: int, start: int, end: int | None = None) -> Command:
    """Read one inverter holding register, or a contiguous range (0x05)."""
    end = start if end is None else end
    if end < start:
        raise ValueError(f"inverted register range {start}..{end}")
    body = _serial_field(serial, protocol)
    body += start.to_bytes(2, "big") + end.to_bytes(2, "big")
    return Command(Function.INVERTER_READ, start, body)


def write_inverter(serial: str, protocol: int, register: int, value: int) -> Command:
    """Write one inverter holding register (0x06).

    Note the asymmetry with :func:`write_datalogger`: there is no length field here, the
    value is simply a 16-bit word.
    """
    if not 0 <= value <= 0xFFFF:
        raise ValueError(f"{value} does not fit in a 16-bit register")
    body = _serial_field(serial, protocol)
    body += register.to_bytes(2, "big") + value.to_bytes(2, "big")
    return Command(Function.INVERTER_WRITE, register, body)


def write_inverter_range(serial: str, protocol: int, start: int, values: list[int]) -> Command:
    """Write a contiguous run of inverter holding registers (0x10)."""
    if not values:
        raise ValueError("no values to write")
    end = start + len(values) - 1
    body = _serial_field(serial, protocol)
    body += start.to_bytes(2, "big") + end.to_bytes(2, "big")
    for value in values:
        if not 0 <= value <= 0xFFFF:
            raise ValueError(f"{value} does not fit in a 16-bit register")
        body += value.to_bytes(2, "big")
    return Command(Function.INVERTER_WRITE_MULTI, start, body)


# ----------------------------------------------------------------------------------
# Responses
# ----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CommandResponse:
    """A device's reply to a command."""

    function: int
    register: int | None
    value: int | str | None = None
    result: int | None = None
    """The device's status byte for a write. Zero means accepted."""

    empty: bool = False
    """A read that returned nothing, which is how a device reports an unknown register."""

    @property
    def ok(self) -> bool:
        return self.result in (None, 0)


def parse_command_response(frame: Frame) -> CommandResponse:
    """Interpret a 0x05, 0x06, 0x10, 0x18 or 0x19 reply.

    Offsets are relative to the end of the serial field, which is 20 bytes wider on
    protocol 06.
    """
    function = frame.function
    body = frame.body
    width = frame.serial_width

    if len(body) < width + 2:
        raise RecordError(f"{function:#04x} response is too short to hold a register")

    register = int.from_bytes(body[width : width + 2], "big")
    rest = body[width + 2 :]

    if function == Function.INVERTER_READ:
        # A device that does not implement a register answers with nothing after the
        # register number, rather than with an error.
        if len(rest) < 2:
            return CommandResponse(function, register, empty=True)
        return CommandResponse(function, register, value=int.from_bytes(rest[:2], "big"))

    if function == Function.INVERTER_WRITE:
        # Both fields are present and both matter: an implementation that overwrites one
        # with the other loses the device's acceptance status.
        if not rest:
            raise RecordError("0x06 response carries no result byte")
        result = rest[0]
        value = int.from_bytes(rest[1:3], "big") if len(rest) >= 3 else None
        return CommandResponse(function, register, value=value, result=result)

    if function == Function.INVERTER_WRITE_MULTI:
        # The register field here is the start of the range; the end follows.
        if len(rest) < 3:
            raise RecordError("0x10 response is truncated")
        return CommandResponse(function, register, result=rest[2])

    if function == Function.CONFIG_WRITE:
        if not rest:
            raise RecordError("0x18 response carries no result byte")
        return CommandResponse(function, register, result=rest[0])

    if function == Function.CONFIG_READ:
        if len(rest) < 2:
            return CommandResponse(function, register, empty=True)
        length = int.from_bytes(rest[:2], "big")
        text = rest[2 : 2 + length]
        # ISO-8859-1 rather than UTF-8: these fields carry SSIDs and hostnames, and a
        # stray high byte should not make the whole reply undecodable.
        return CommandResponse(function, register, value=text.decode("ISO-8859-1").rstrip("\x00"))

    raise RecordError(f"{function:#04x} is not a command response")
