"""Builders for synthetic Growatt frames, used by the protocol tests.

These construct frames the way a datalogger would, so tests exercise the real
obfuscate-then-checksum ordering rather than a convenient approximation.
"""

from __future__ import annotations

from datetime import datetime

from ..crc import append_crc
from ..crypt import OBFUSCATED_PROTOCOLS, xor_payload

SERIAL_WIDTH = {2: 10, 5: 10, 6: 30}


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


def _serial_field(serial: str, width: int) -> bytes:
    encoded = serial.encode("ascii")
    if len(encoded) > width:
        raise ValueError(f"serial {serial!r} does not fit in {width} bytes")
    return encoded.ljust(width, b"\x00")


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
    width = SERIAL_WIDTH[protocol]
    groups = groups if groups is not None else [build_group(3000, [1, 2, 3])]
    timestamp = timestamp or datetime(2026, 8, 31, 12, 34, 56)

    body = _serial_field(datalogger_serial, width)
    body += _serial_field(inverter_serial, width)
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
