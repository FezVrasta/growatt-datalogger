"""Growatt frame model, register-group parsing, and reply construction.

The important idea in this module is that telemetry records are **self-describing**. A
0x03/0x04/0x50 payload is::

    datalogger serial | inverter serial | 6-byte timestamp | 1-byte group count
    | group 1 | group 2 | ...

and each group is::

    2-byte start register | 2-byte end register | 2 bytes per register in [start, end]

So the record states which Modbus registers it carries. Decoding therefore does not need
a per-inverter table of byte offsets; it reads the register numbers off the wire and
looks their meaning up by number. This is what lets a few register tables replace dozens
of model-specific layout files, and it makes a malformed record fail loudly instead of
silently decoding into plausible-looking nonsense.

Serial field width is the one thing that varies by protocol version: 10 bytes for
protocol 02 and 05, 30 bytes for protocol 06.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import IntEnum
from functools import cached_property

from .crc import append_crc
from .crypt import HEADER_LENGTH, OBFUSCATED_PROTOCOLS, deobfuscate
from .errors import RecordError


class Function(IntEnum):
    """Frame function codes (header byte 7)."""

    ANNOUNCE = 0x03
    """Device announce. Carries holding registers and identifies the inverter."""

    DATA = 0x04
    """Live telemetry. Carries input registers."""

    PING = 0x16
    """Heartbeat. Must be echoed back byte-for-byte."""

    CONFIG_WRITE = 0x18
    """Datalogger parameter write, including time sync."""

    CONFIG_READ = 0x19
    """Datalogger parameter read."""

    INVERTER_READ = 0x05
    INVERTER_WRITE = 0x06
    INVERTER_WRITE_MULTI = 0x10

    BUFFERED = 0x50
    """Historical data replayed after an outage. Same payload shape as DATA."""

    METER_1B = 0x1B
    METER_1E = 0x1E
    METER_20 = 0x20

    SESSION_KEY = 0x41
    """Session-key handshake, on firmware that encrypts the rest of the connection.

    The first frame of the connection, before the ping: the device offers a password and
    a firmware date, and the server answers with a key. Every body after it is ciphertext
    padded to a 16-byte boundary, so the XOR this package implements produces nonsense
    and a record decodes as an absurd register range.

    Neither the key exchange nor the cipher is implemented. What matters is recognising
    it, so the failure reads as "this connection is encrypted" rather than as a corrupt
    register group -- see :attr:`~..session.Session.encrypted`.
    https://github.com/FezVrasta/growatt-datalogger/issues/3
    """

    IGNORED = 0x29
    """Observed in the wild; expects no reply."""


#: Functions whose payload is a series of register groups.
REGISTER_RECORD_FUNCTIONS = frozenset({Function.ANNOUNCE, Function.DATA, Function.BUFFERED})

#: Smart-meter functions. These carry an ASCII key/value log rather than register
#: groups and need a separate parser.
METER_FUNCTIONS = frozenset({Function.METER_1B, Function.METER_1E, Function.METER_20})

#: Functions that are replies to a command we sent. They must not be acknowledged.
COMMAND_RESPONSE_FUNCTIONS = frozenset(
    {
        Function.INVERTER_READ,
        Function.INVERTER_WRITE,
        Function.INVERTER_WRITE_MULTI,
        Function.CONFIG_READ,
        Function.CONFIG_WRITE,
    }
)

#: Serial field width in bytes, by protocol version. Protocol 06 pads both serial
#: fields to 30 bytes; 02 and 05 use 10.
#:
#: Every offset in a body is measured from the end of this field, so this is the one
#: table the whole package reads it from -- :mod:`.commands` when it builds a request and
#: when it parses the reply, :mod:`.testing.frames` when it fakes one. A second copy that
#: drifted would put the reader and the writer at different offsets on one protocol only,
#: which is the kind of thing that passes every test on protocol 06 and fails in the
#: field on a ShineWiFi-S.
SERIAL_WIDTH = {2: 10, 5: 10, 6: 30}


def serial_width(protocol: int) -> int:
    """Bytes a serial number is padded to on ``protocol``."""
    try:
        return SERIAL_WIDTH[protocol]
    except KeyError:
        raise RecordError(f"no serial width known for protocol {protocol}") from None


_TIMESTAMP_LENGTH = 6


@dataclass(frozen=True, slots=True)
class RegisterGroup:
    """One contiguous run of Modbus registers as reported by the device."""

    start: int
    end: int
    values: tuple[int, ...]
    """Raw unsigned 16-bit values, one per register from ``start`` to ``end``."""

    @property
    def count(self) -> int:
        return self.end - self.start + 1

    def as_mapping(self) -> dict[int, int]:
        return {self.start + i: v for i, v in enumerate(self.values)}


# Note: no slots on the two classes below. They use functools.cached_property, which
# stores into the instance __dict__ that slots would remove. Frozen is still fine --
# cached_property writes to __dict__ directly rather than going through __setattr__.
@dataclass(frozen=True)
class RecordPayload:
    """The decoded body of a register-bearing record."""

    datalogger_serial: str
    inverter_serial: str
    timestamp: datetime | None
    groups: tuple[RegisterGroup, ...]

    @cached_property
    def registers(self) -> dict[int, int]:
        """All reported registers, flattened to ``{register_number: raw_value}``."""
        merged: dict[int, int] = {}
        for group in self.groups:
            merged.update(group.as_mapping())
        return merged


@dataclass(frozen=True)
class Frame:
    """A single complete frame, still in its on-the-wire form."""

    raw: bytes

    @property
    def sequence(self) -> int:
        return int.from_bytes(self.raw[0:2], "big")

    @property
    def protocol(self) -> int:
        return self.raw[3]

    @property
    def declared_length(self) -> int:
        return int.from_bytes(self.raw[4:6], "big")

    @property
    def device_id(self) -> int:
        return self.raw[6]

    @property
    def function(self) -> int:
        return self.raw[7]

    @property
    def has_crc(self) -> bool:
        return self.protocol in OBFUSCATED_PROTOCOLS

    @cached_property
    def plaintext(self) -> bytes:
        """The frame with obfuscation removed. Header bytes are unchanged."""
        return deobfuscate(self.raw, self.protocol)

    @cached_property
    def body(self) -> bytes:
        """Plaintext payload after the 8-byte header, with any trailing CRC removed."""
        end = len(self.plaintext) - (2 if self.has_crc else 0)
        return self.plaintext[HEADER_LENGTH:end]

    @property
    def serial_width(self) -> int:
        return serial_width(self.protocol)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"Frame(seq={self.sequence}, protocol={self.protocol:#04x}, "
            f"device_id={self.device_id:#04x}, function={self.function:#04x}, "
            f"len={len(self.raw)})"
        )


def _read_serial(body: bytes, offset: int, width: int) -> str:
    field = body[offset : offset + width]
    if len(field) < width:
        raise RecordError("record truncated inside a serial number field")
    # The serial is ASCII, right-padded with NULs (protocol 06) or occupying the whole
    # field (protocol 02/05). Spaces have been observed too.
    return field.split(b"\x00", 1)[0].decode("ascii", errors="replace").strip()


def _read_timestamp(body: bytes, offset: int) -> datetime | None:
    field = body[offset : offset + _TIMESTAMP_LENGTH]
    if len(field) < _TIMESTAMP_LENGTH:
        raise RecordError("record truncated inside the timestamp field")

    year, month, day, hour, minute, second = field
    try:
        # The year is an offset from 2000. Devices with an unset clock report zeroes,
        # which is not an error worth dropping the whole record over.
        return datetime(2000 + year, month, day, hour, minute, second)
    except ValueError:
        return None


def parse_register_record(frame: Frame) -> RecordPayload:
    """Parse a 0x03, 0x04 or 0x50 record into its serials, timestamp and registers.

    Raises:
        RecordError: if the payload is truncated, if a group declares an invalid range,
            or if the groups do not exactly consume the payload.
    """
    if frame.function not in REGISTER_RECORD_FUNCTIONS:
        raise RecordError(f"function {frame.function:#04x} does not carry register groups")

    body = frame.body
    width = frame.serial_width

    datalogger_serial = _read_serial(body, 0, width)
    inverter_serial = _read_serial(body, width, width)

    offset = width * 2
    timestamp = _read_timestamp(body, offset)
    offset += _TIMESTAMP_LENGTH

    if offset >= len(body):
        raise RecordError("record ended before the register-group count")
    group_count = body[offset]
    offset += 1

    groups: list[RegisterGroup] = []
    for index in range(group_count):
        if offset + 4 > len(body):
            raise RecordError(f"record ended inside the header of register group {index + 1}")

        start = int.from_bytes(body[offset : offset + 2], "big")
        end = int.from_bytes(body[offset + 2 : offset + 4], "big")
        offset += 4

        if end < start:
            raise RecordError(
                f"register group {index + 1} declares an inverted range {start}..{end}"
            )

        count = end - start + 1
        needed = count * 2
        chunk = body[offset : offset + needed]
        if len(chunk) < needed:
            raise RecordError(
                f"register group {index + 1} declares {count} registers "
                f"but only {len(chunk) // 2} are present"
            )
        offset += needed

        values = tuple(int.from_bytes(chunk[i : i + 2], "big") for i in range(0, needed, 2))
        groups.append(RegisterGroup(start=start, end=end, values=values))

    # The groups should account for the whole payload. A mismatch means our
    # understanding of the record is wrong -- surface it rather than returning a
    # partial decode that looks fine.
    if offset != len(body):
        raise RecordError(f"register groups consumed {offset} bytes of a {len(body)}-byte payload")

    return RecordPayload(
        datalogger_serial=datalogger_serial,
        inverter_serial=inverter_serial,
        timestamp=timestamp,
        groups=tuple(groups),
    )


def build_ack(frame: Frame) -> bytes:
    """Build the acknowledgement for a record.

    The datalogger retransmits, then drops the connection, if a data record is not
    acknowledged promptly, so this must never wait on decoding.

    The reply echoes the request's sequence, protocol, device id and function, declares
    a length of 3 (device id + function + one payload byte), and carries a single zero
    payload byte. For obfuscated protocols that zero is pre-encrypted to ``0x47``, which
    is simply ``0x00 ^ ord('G')`` -- the first byte of the keystream at offset 8.
    """
    header = frame.raw[0:2] + b"\x00" + bytes([frame.protocol])
    header += (3).to_bytes(2, "big") + bytes([frame.device_id, frame.function])

    if frame.protocol in OBFUSCATED_PROTOCOLS:
        return append_crc(header + b"\x47")
    return header + b"\x00"


def build_ping_echo(frame: Frame) -> bytes:
    """Build the reply to a ping: the received frame, unchanged.

    Returned verbatim rather than rebuilt, so no re-encryption or CRC recomputation can
    introduce a difference the device might reject.
    """
    return frame.raw
