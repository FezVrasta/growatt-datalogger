"""CRC-16/MODBUS."""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from custom_components.growatt_datalogger.protocol.crc import (
    append_crc,
    check_crc,
    modbus_crc,
)

_POLY = 0xA001


def _reference_crc(data: bytes) -> int:
    """Bit-by-bit reference, independent of the table-driven implementation."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ _POLY if crc & 1 else crc >> 1
    return crc


def test_standard_check_vector() -> None:
    # The canonical CRC-16/MODBUS check value.
    assert modbus_crc(b"123456789") == 0x4B37


def test_empty_input_is_the_initial_value() -> None:
    assert modbus_crc(b"") == 0xFFFF


@given(st.binary(max_size=600))
def test_table_matches_bitwise_reference(data: bytes) -> None:
    assert modbus_crc(data) == _reference_crc(data)


@given(st.binary(min_size=1, max_size=600))
def test_append_then_check_round_trips(data: bytes) -> None:
    assert check_crc(append_crc(data))


def test_crc_is_appended_big_endian() -> None:
    """Growatt sends the checksum MSB first, unlike Modbus RTU on a serial line.

    Getting this backwards produces frames a datalogger silently rejects, so pin it.
    """
    payload = b"\x01\x02\x03\x04"
    crc = modbus_crc(payload)
    framed = append_crc(payload)

    assert framed[-2:] == crc.to_bytes(2, "big")
    assert framed[-2] == (crc >> 8) & 0xFF
    assert framed[-1] == crc & 0xFF


def test_check_crc_rejects_a_corrupted_payload() -> None:
    framed = bytearray(append_crc(b"\x10\x20\x30\x40"))
    framed[0] ^= 0xFF
    assert not check_crc(bytes(framed))


def test_check_crc_rejects_undersized_input() -> None:
    assert not check_crc(b"")
    assert not check_crc(b"\x00\x01")
