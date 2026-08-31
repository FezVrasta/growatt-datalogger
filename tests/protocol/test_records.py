"""Frame model, register-group parsing, and reply construction."""

from __future__ import annotations

from datetime import datetime

import pytest

from custom_components.growatt_datalogger.protocol.crc import check_crc
from custom_components.growatt_datalogger.protocol.errors import RecordError
from custom_components.growatt_datalogger.protocol.records import (
    Frame,
    Function,
    build_ack,
    build_ping_echo,
    parse_register_record,
)
from tests.fakes.frames import build_data_record, build_frame, build_group

# --------------------------------------------------------------------------------
# Header
# --------------------------------------------------------------------------------


def test_header_fields() -> None:
    frame = Frame(build_frame(b"abcd", protocol=6, function=0x04, device_id=0x01, sequence=0x1234))

    assert frame.sequence == 0x1234
    assert frame.protocol == 6
    assert frame.device_id == 0x01
    assert frame.function == Function.DATA
    assert frame.declared_length == 2 + 4
    assert frame.has_crc


def test_protocol_02_carries_no_crc() -> None:
    assert not Frame(build_frame(b"abcd", protocol=2)).has_crc


@pytest.mark.parametrize(("protocol", "width"), [(2, 10), (5, 10), (6, 30)])
def test_serial_field_width_by_protocol(protocol: int, width: int) -> None:
    assert Frame(build_frame(b"", protocol=protocol)).serial_width == width


def test_body_strips_header_and_crc() -> None:
    payload = b"0123456789"
    assert Frame(build_frame(payload, protocol=6)).body == payload
    assert Frame(build_frame(payload, protocol=2)).body == payload


# --------------------------------------------------------------------------------
# Register groups -- the core of the decoder
# --------------------------------------------------------------------------------


@pytest.mark.parametrize("protocol", [2, 5, 6])
def test_round_trip_of_a_telemetry_record(protocol: int) -> None:
    frame = Frame(
        build_data_record(
            protocol=protocol,
            datalogger_serial="GPG0EXAMP1",
            inverter_serial="SML0EXAMP2",
            timestamp=datetime(2026, 8, 31, 8, 48, 42),
            groups=[build_group(3000, [1, 2585, 3295])],
        )
    )
    payload = parse_register_record(frame)

    assert payload.datalogger_serial == "GPG0EXAMP1"
    assert payload.inverter_serial == "SML0EXAMP2"
    assert payload.timestamp == datetime(2026, 8, 31, 8, 48, 42)
    assert payload.registers == {3000: 1, 3001: 2585, 3002: 3295}


def test_multiple_groups_flatten_into_one_register_map() -> None:
    frame = Frame(
        build_data_record(groups=[build_group(0, list(range(125))), build_group(3000, [9, 8, 7])])
    )
    payload = parse_register_record(frame)

    assert len(payload.groups) == 2
    assert payload.groups[0].start == 0
    assert payload.groups[0].end == 124
    assert payload.groups[0].count == 125
    assert payload.registers[124] == 124
    assert payload.registers[3001] == 8


def test_first_register_value_lands_where_the_wire_format_says() -> None:
    """Protocol 06 puts the first register value at byte 79 of the frame; 02/05 at 39.

    30+30 byte serials + 6 byte timestamp + 1 count + 4 group header = byte 79 after the
    8-byte header, and 10+10+6+1+4 = byte 39 for the narrow-serial protocols. Pinning
    this catches an off-by-one in the serial widths immediately.
    """
    for protocol, expected in ((6, 79), (5, 39), (2, 39)):
        frame = Frame(build_data_record(protocol=protocol, groups=[build_group(3000, [0xBEEF])]))
        assert frame.plaintext[expected : expected + 2] == (0xBEEF).to_bytes(2, "big")


def test_group_count_of_zero_is_valid() -> None:
    payload = parse_register_record(Frame(build_data_record(groups=[])))
    assert payload.groups == ()
    assert payload.registers == {}


def test_unset_device_clock_yields_no_timestamp_rather_than_an_error() -> None:
    body = bytearray(Frame(build_data_record()).body)
    body[60:66] = bytes(6)  # all-zero date: month 0 and day 0 are not a real date
    frame = Frame(build_frame(bytes(body), protocol=6, function=0x04))

    assert parse_register_record(frame).timestamp is None


@pytest.mark.parametrize("function", [Function.ANNOUNCE, Function.DATA, Function.BUFFERED])
def test_all_register_bearing_functions_parse(function: int) -> None:
    frame = Frame(build_data_record(function=function))
    assert parse_register_record(frame).registers


def test_non_register_function_is_refused() -> None:
    frame = Frame(build_data_record(function=Function.PING))
    with pytest.raises(RecordError, match="does not carry register groups"):
        parse_register_record(frame)


def test_group_declaring_more_registers_than_it_carries_is_refused() -> None:
    body = bytearray(Frame(build_data_record(groups=[build_group(3000, [1, 2, 3])])).body)
    # Widen the declared range without adding the data for it.
    body[69:71] = (3010).to_bytes(2, "big")
    frame = Frame(build_frame(bytes(body), protocol=6, function=0x04))

    with pytest.raises(RecordError, match="only 3 are present"):
        parse_register_record(frame)


def test_inverted_group_range_is_refused() -> None:
    body = bytearray(Frame(build_data_record(groups=[build_group(3000, [1, 2, 3])])).body)
    body[69:71] = (2999).to_bytes(2, "big")
    frame = Frame(build_frame(bytes(body), protocol=6, function=0x04))

    with pytest.raises(RecordError, match="inverted range"):
        parse_register_record(frame)


def test_trailing_bytes_after_the_groups_are_refused() -> None:
    """Leftover payload means our model of the record is wrong. Say so."""
    body = Frame(build_data_record()).body + b"\x00\x00"
    frame = Frame(build_frame(body, protocol=6, function=0x04))

    with pytest.raises(RecordError, match="consumed"):
        parse_register_record(frame)


def test_truncated_group_header_is_refused() -> None:
    body = Frame(build_data_record(groups=[build_group(3000, [1])])).body[:-4]
    frame = Frame(build_frame(body, protocol=6, function=0x04))

    with pytest.raises(RecordError):
        parse_register_record(frame)


# --------------------------------------------------------------------------------
# Replies
# --------------------------------------------------------------------------------


def test_ack_for_protocol_02() -> None:
    frame = Frame(build_frame(b"abcd", protocol=2, function=0x04, device_id=0x01, sequence=7))
    ack = build_ack(frame)

    assert ack == bytes.fromhex("0007") + bytes.fromhex("0002") + bytes.fromhex("0003") + bytes(
        [0x01, 0x04, 0x00]
    )
    assert len(ack) == 9


def test_ack_for_protocol_06() -> None:
    frame = Frame(build_frame(b"abcd", protocol=6, function=0x04, device_id=0x01, sequence=7))
    ack = build_ack(frame)

    assert len(ack) == 11
    assert ack[:8] == bytes.fromhex("0007") + b"\x00\x06" + b"\x00\x03" + bytes([0x01, 0x04])
    assert ack[8] == 0x47  # a zero payload byte, pre-encrypted
    assert check_crc(ack)


def test_ack_echoes_sequence_device_and_function() -> None:
    frame = Frame(build_frame(b"abcd", protocol=6, function=0x50, device_id=0x22, sequence=0xABCD))
    ack = build_ack(frame)

    assert ack[0:2] == (0xABCD).to_bytes(2, "big")
    assert ack[6] == 0x22
    assert ack[7] == 0x50


def test_ping_echo_is_byte_identical() -> None:
    raw = build_frame(b"GPG0EXAMP1" + bytes(22), protocol=6, function=0x16)
    assert build_ping_echo(Frame(raw)) == raw
