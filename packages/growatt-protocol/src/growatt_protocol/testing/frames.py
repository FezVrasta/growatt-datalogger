"""Builders for synthetic Growatt frames, used by the protocol tests.

These construct frames the way a datalogger would, so tests exercise the real
obfuscate-then-checksum ordering rather than a convenient approximation.
"""

from __future__ import annotations

from datetime import datetime

from ..crc import append_crc
from ..crypt import OBFUSCATED_PROTOCOLS, xor_payload
from ..records import serial_width

# The frame *assembly* below is deliberately independent of `commands._frame`: a builder
# that shared code with the thing it tests could not catch a bug in it. The serial width
# is not logic, though -- it is one fact about the wire format, and a fake that padded
# differently from the parser would only ever prove itself right.


def build_frame(
    body: bytes,
    *,
    protocol: int = 6,
    function: int = 0x04,
    device_id: int = 0x01,
    sequence: int = 1,
) -> bytes:
    """Assemble a complete frame around ``body``.

    The declared length covers the device id and function bytes plus the body, but not
    the CRC -- which is what the wire format specifies.
    """
    declared = 2 + len(body)
    frame = (
        sequence.to_bytes(2, "big")
        + b"\x00"
        + bytes([protocol])
        + declared.to_bytes(2, "big")
        + bytes([device_id, function])
        + body
    )
    if protocol in OBFUSCATED_PROTOCOLS:
        # Obfuscation first, then the checksum over the obfuscated bytes.
        return append_crc(xor_payload(frame))
    return frame


def _serial_field(serial: str, protocol: int) -> bytes:
    width = serial_width(protocol)
    encoded = serial.encode("ascii")
    if len(encoded) > width:
        raise ValueError(f"serial {serial!r} does not fit in {width} bytes")
    return encoded.ljust(width, b"\x00")


def build_command_response(
    serial: str,
    *,
    function: int,
    register: int,
    tail: bytes = b"",
    protocol: int = 6,
    sequence: int = 1,
) -> bytes:
    """A device's reply to a command: serial, the register, then whatever follows.

    What ``tail`` holds is per function, and getting it wrong is the classic mistake --
    a read echoes the *range* before any values, so a reply that omits the end register
    parses as a value that looks convincingly like a real one. The helpers below spell
    each shape out so no test has to remember it.
    """
    body = _serial_field(serial, protocol) + register.to_bytes(2, "big") + tail
    return build_frame(body, protocol=protocol, function=function, sequence=sequence)


def build_read_response(
    serial: str,
    *,
    register: int,
    values: list[int] | tuple[int, ...] = (),
    end_register: int | None = None,
    protocol: int = 6,
    sequence: int = 1,
) -> bytes:
    """A 0x05 reply. Empty ``values`` is how a device says it has no such register."""
    end = register + len(values) - 1 if end_register is None and values else end_register
    tail = (end if end is not None else register).to_bytes(2, "big")
    tail += b"".join(value.to_bytes(2, "big") for value in values)
    return build_command_response(
        serial, function=0x05, register=register, tail=tail, protocol=protocol, sequence=sequence
    )


def build_write_response(
    serial: str,
    *,
    register: int,
    value: int = 0,
    result: int = 0,
    protocol: int = 6,
    sequence: int = 1,
) -> bytes:
    """A 0x06 reply: the status byte first, then the value the device settled on."""
    return build_command_response(
        serial,
        function=0x06,
        register=register,
        tail=bytes([result]) + value.to_bytes(2, "big"),
        protocol=protocol,
        sequence=sequence,
    )


def build_range_write_response(
    serial: str,
    *,
    start: int,
    end: int,
    result: int = 0,
    protocol: int = 6,
    sequence: int = 1,
) -> bytes:
    """A 0x10 reply: the range echoed back, then the status byte."""
    return build_command_response(
        serial,
        function=0x10,
        register=start,
        tail=end.to_bytes(2, "big") + bytes([result]),
        protocol=protocol,
        sequence=sequence,
    )


def build_group(start: int, values: list[int] | tuple[int, ...]) -> bytes:
    """Encode one register group: a (start, end) header then one word per register."""
    end = start + len(values) - 1
    out = start.to_bytes(2, "big") + end.to_bytes(2, "big")
    for value in values:
        out += value.to_bytes(2, "big")
    return out


def build_register_body(
    *,
    protocol: int = 6,
    datalogger_serial: str = "GPG0AAAAA1",
    inverter_serial: str = "SML0BBBBB2",
    timestamp: datetime | None = None,
    groups: list[bytes] | None = None,
) -> bytes:
    """Build the payload of a 0x03/0x04/0x50 record."""
    groups = groups if groups is not None else [build_group(3000, [1, 2, 3])]
    timestamp = timestamp or datetime(2026, 8, 31, 12, 34, 56)

    body = _serial_field(datalogger_serial, protocol)
    body += _serial_field(inverter_serial, protocol)
    body += bytes(
        [
            timestamp.year - 2000,
            timestamp.month,
            timestamp.day,
            timestamp.hour,
            timestamp.minute,
            timestamp.second,
        ]
    )
    body += bytes([len(groups)])
    for group in groups:
        body += group
    return body


def build_data_record(
    *,
    protocol: int = 6,
    function: int = 0x04,
    sequence: int = 1,
    **body_kwargs: object,
) -> bytes:
    """Convenience: a complete, valid telemetry record."""
    body = build_register_body(protocol=protocol, **body_kwargs)  # type: ignore[arg-type]
    return build_frame(body, protocol=protocol, function=function, sequence=sequence)
