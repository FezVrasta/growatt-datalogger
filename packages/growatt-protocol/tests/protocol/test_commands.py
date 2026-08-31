"""Command construction and response parsing."""

from __future__ import annotations

from datetime import datetime

import pytest
from growatt_protocol import commands
from growatt_protocol.commands import (
    REGISTER_TIME,
    parse_command_response,
)
from growatt_protocol.crc import check_crc
from growatt_protocol.errors import RecordError
from growatt_protocol.records import Frame
from growatt_protocol.testing.frames import build_frame

SERIAL = "GPG0EXAMP1"


def _decode(frame: bytes) -> bytes:
    """The plaintext body of a frame we built."""
    return Frame(frame).body


@pytest.mark.parametrize("protocol", [2, 5, 6])
def test_serial_field_width_matches_the_protocol(protocol: int) -> None:
    command = commands.read_inverter(SERIAL, protocol, 3)
    width = 30 if protocol == 6 else 10
    assert command.body[:width].rstrip(b"\x00").decode() == SERIAL
    assert len(command.body) == width + 4


@pytest.mark.parametrize("protocol", [2, 5, 6])
def test_built_frames_are_well_formed(protocol: int) -> None:
    raw = commands.read_inverter(SERIAL, protocol, 3).build(0x1234, protocol)
    frame = Frame(raw)

    assert frame.sequence == 0x1234
    assert frame.protocol == protocol
    assert frame.function == 0x05
    assert frame.declared_length == 2 + len(commands.read_inverter(SERIAL, protocol, 3).body)
    if protocol != 2:
        assert check_crc(raw)


def test_read_inverter_sends_a_start_and_end_register() -> None:
    command = commands.read_inverter(SERIAL, 6, 3)
    assert command.body[30:34] == (3).to_bytes(2, "big") + (3).to_bytes(2, "big")

    ranged = commands.read_inverter(SERIAL, 6, 3, 7)
    assert ranged.body[30:34] == (3).to_bytes(2, "big") + (7).to_bytes(2, "big")


def test_read_inverter_rejects_an_inverted_range() -> None:
    with pytest.raises(ValueError, match="inverted register range"):
        commands.read_inverter(SERIAL, 6, 10, 5)


def test_write_inverter_has_no_length_field() -> None:
    """Unlike a datalogger write, the value is a bare 16-bit word."""
    command = commands.write_inverter(SERIAL, 6, 3, 100)
    assert command.body[30:32] == (3).to_bytes(2, "big")
    assert command.body[32:34] == (100).to_bytes(2, "big")
    assert len(command.body) == 34


def test_write_inverter_rejects_an_out_of_range_value() -> None:
    with pytest.raises(ValueError, match="16-bit"):
        commands.write_inverter(SERIAL, 6, 3, 70000)


def test_write_datalogger_length_prefixes_its_string() -> None:
    command = commands.write_datalogger(SERIAL, 6, 8, "hello")
    assert command.body[30:32] == (8).to_bytes(2, "big")
    assert command.body[32:34] == (5).to_bytes(2, "big")
    assert command.body[34:39] == b"hello"


def test_set_time_encodes_the_expected_ascii_form() -> None:
    command = commands.set_time(SERIAL, 6, datetime(2026, 8, 31, 12, 34, 56))

    assert command.function == 0x18
    assert command.register == REGISTER_TIME
    assert command.body[30:32] == (0x1F).to_bytes(2, "big")
    assert command.body[32:34] == (19).to_bytes(2, "big")
    assert command.body[34:53] == b"2026-08-31 12:34:56"


def test_set_time_drops_sub_second_precision() -> None:
    command = commands.set_time(SERIAL, 6, datetime(2026, 8, 31, 12, 34, 56, 987654))
    assert command.body[34:53] == b"2026-08-31 12:34:56"


def test_write_inverter_range() -> None:
    command = commands.write_inverter_range(SERIAL, 6, 100, [1, 2, 3])
    assert command.body[30:34] == (100).to_bytes(2, "big") + (102).to_bytes(2, "big")
    assert command.body[34:40] == b"\x00\x01\x00\x02\x00\x03"


def test_write_inverter_range_rejects_an_empty_list() -> None:
    with pytest.raises(ValueError, match="no values"):
        commands.write_inverter_range(SERIAL, 6, 100, [])


# ----------------------------------------------------------------------------------
# Responses
# ----------------------------------------------------------------------------------


def _response(function: int, tail: bytes, *, protocol: int = 6, register: int = 3) -> Frame:
    width = 30 if protocol == 6 else 10
    body = SERIAL.encode().ljust(width, b"\x00") + register.to_bytes(2, "big") + tail
    return Frame(build_frame(body, protocol=protocol, function=function))


def test_inverter_read_response() -> None:
    # serial | start | end | value
    tail = (3).to_bytes(2, "big") + (1234).to_bytes(2, "big")
    response = parse_command_response(_response(0x05, tail))

    assert response.register == 3
    assert response.end_register == 3
    assert response.value == 1234
    assert not response.empty


def test_a_read_reply_echoes_the_range_before_its_values() -> None:
    """The trap this format sets, pinned.

    Reading the word straight after the start register yields the *end* register, which
    on a single-register read is indistinguishable from a plausible value: asking for
    register 3 comes back as 3. Found against real hardware, where a power limit of 100%
    read back as 3.
    """
    tail = (3).to_bytes(2, "big") + (100).to_bytes(2, "big")
    response = parse_command_response(_response(0x05, tail))

    assert response.value == 100, "the echoed end register was mistaken for the value"


def test_a_range_read_returns_every_value() -> None:
    tail = (5).to_bytes(2, "big") + b"\x00\x0a\x00\x14\x00\x1e"
    response = parse_command_response(_response(0x05, tail))

    assert response.register == 3
    assert response.end_register == 5
    assert response.values == (10, 20, 30)
    assert response.value == 10


def test_an_unimplemented_register_reads_back_empty() -> None:
    """Devices answer an unknown register with the range echo and nothing after it."""
    echo_only = parse_command_response(_response(0x05, (3).to_bytes(2, "big")))
    assert echo_only.empty
    assert echo_only.value is None

    nothing = parse_command_response(_response(0x05, b""))
    assert nothing.empty


def test_inverter_write_response_keeps_both_result_and_value() -> None:
    """Both fields matter, and it is easy to write a parser that loses one."""
    response = parse_command_response(_response(0x06, b"\x00" + (100).to_bytes(2, "big")))
    assert response.result == 0
    assert response.value == 100
    assert response.ok


def test_a_rejected_write_is_not_ok() -> None:
    response = parse_command_response(_response(0x06, b"\x01" + (0).to_bytes(2, "big")))
    assert response.result == 1
    assert not response.ok


def test_datalogger_write_response() -> None:
    response = parse_command_response(_response(0x18, b"\x00", register=0x1F))
    assert response.register == 0x1F
    assert response.result == 0
    assert response.ok


def test_datalogger_read_response_decodes_its_string() -> None:
    payload = b"ShineLan-X"
    tail = len(payload).to_bytes(2, "big") + payload
    response = parse_command_response(_response(0x19, tail, register=8))
    assert response.value == "ShineLan-X"


def test_datalogger_read_tolerates_a_high_byte() -> None:
    """An SSID with a stray non-UTF-8 byte must not make the whole reply undecodable."""
    tail = (4).to_bytes(2, "big") + b"wi\xfai"
    response = parse_command_response(_response(0x19, tail, register=8))
    assert isinstance(response.value, str)


def test_multi_register_write_response() -> None:
    tail = (102).to_bytes(2, "big") + b"\x00"
    response = parse_command_response(_response(0x10, tail, register=100))
    assert response.register == 100
    assert response.result == 0


@pytest.mark.parametrize("protocol", [2, 5, 6])
def test_responses_parse_on_every_protocol(protocol: int) -> None:
    tail = (3).to_bytes(2, "big") + (99).to_bytes(2, "big")
    response = parse_command_response(_response(0x05, tail, protocol=protocol))
    assert response.value == 99


def test_a_truncated_response_is_refused() -> None:
    width = 30
    body = SERIAL.encode().ljust(width, b"\x00")
    frame = Frame(build_frame(body, protocol=6, function=0x05))
    with pytest.raises(RecordError, match="too short"):
        parse_command_response(frame)


def test_a_non_command_function_is_refused() -> None:
    with pytest.raises(RecordError, match="not a command response"):
        parse_command_response(_response(0x04, b"\x00\x00"))
