"""CRC-16/MODBUS.

Growatt frames using protocol version 05 or 06 carry a two-byte checksum after the
declared payload. The algorithm is the standard Modbus CRC-16 (reflected polynomial
0xA001, initial value 0xFFFF, no final XOR), but note that Growatt transmits it
**big-endian**, which is the opposite of Modbus RTU on a serial line.

This is a pure-Python replacement for the ``libscrc`` C extension, which is the only
compiled dependency other Growatt tooling needs. A 256-entry table is built once at
import; frames are under a kilobyte and arrive every few minutes, so this is far
faster than it needs to be.
"""

from __future__ import annotations

_POLY = 0xA001


def _build_table() -> tuple[int, ...]:
    table = []
    for byte in range(256):
        crc = byte
        for _ in range(8):
            crc = (crc >> 1) ^ _POLY if crc & 1 else crc >> 1
        table.append(crc)
    return tuple(table)


_TABLE = _build_table()


def modbus_crc(data: bytes) -> int:
    """Return the CRC-16/MODBUS of ``data`` as an unsigned 16-bit integer."""
    crc = 0xFFFF
    for byte in data:
        crc = (crc >> 8) ^ _TABLE[(crc ^ byte) & 0xFF]
    return crc


def append_crc(frame: bytes) -> bytes:
    """Return ``frame`` with its CRC appended big-endian, as Growatt transmits it."""
    return frame + modbus_crc(frame).to_bytes(2, "big")


def check_crc(frame: bytes) -> bool:
    """Whether ``frame``'s trailing two bytes match a CRC over everything before them.

    Callers should treat a mismatch as advisory. Some dataloggers have been reported to
    fail this check on every single record while still emitting perfectly decodable
    payloads, so refusing such records loses all data from that device. Count the
    mismatch, log it, and decode anyway; frame length is the real validity gate.
    """
    if len(frame) < 3:
        return False
    return modbus_crc(frame[:-2]) == int.from_bytes(frame[-2:], "big")
