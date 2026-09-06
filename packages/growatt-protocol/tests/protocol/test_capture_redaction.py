"""Redaction of captured frames.

A capture is meant to be attachable to a public issue, so "no original serial survives"
has to be a checked property, not an intention -- and it has to stay decodable, or it is
useless as a fixture.
"""

from __future__ import annotations

import pytest
from growatt_protocol.crc import append_crc, check_crc
from growatt_protocol.records import (
    Frame,
    parse_register_record,
)
from growatt_protocol.redaction import Pseudonymiser, redact
from growatt_protocol.testing.frames import build_data_record

LOGGER_SERIAL = "GPG0EXAMP1"
INVERTER_SERIAL = "SML0EXAMP2"


def _record(protocol: int = 6) -> bytes:
    return build_data_record(
        protocol=protocol,
        datalogger_serial=LOGGER_SERIAL,
        inverter_serial=INVERTER_SERIAL,
    )


@pytest.mark.parametrize("protocol", [2, 5, 6])
def test_no_original_serial_survives(protocol: int) -> None:
    clean = redact(_record(protocol), Pseudonymiser())

    assert LOGGER_SERIAL.encode() not in clean
    assert INVERTER_SERIAL.encode() not in clean
    # Also absent from the plaintext, not merely hidden by the obfuscation.
    assert LOGGER_SERIAL.encode() not in Frame(clean).plaintext
    assert INVERTER_SERIAL.encode() not in Frame(clean).plaintext


@pytest.mark.parametrize("protocol", [2, 5, 6])
def test_a_redacted_frame_still_decodes(protocol: int) -> None:
    """A capture that no longer parses would be useless as a fixture."""
    payload = parse_register_record(Frame(redact(_record(protocol), Pseudonymiser())))

    assert payload.registers == {3000: 1, 3001: 2, 3002: 3}
    assert payload.timestamp is not None


@pytest.mark.parametrize("protocol", [5, 6])
def test_the_checksum_is_recomputed(protocol: int) -> None:
    assert check_crc(redact(_record(protocol), Pseudonymiser()))


def test_replacements_keep_the_serial_length() -> None:
    payload = parse_register_record(Frame(redact(_record(), Pseudonymiser())))

    assert len(payload.datalogger_serial) == len(LOGGER_SERIAL)
    assert len(payload.inverter_serial) == len(INVERTER_SERIAL)
    assert payload.datalogger_serial.isalnum()


def test_the_same_serial_maps_consistently_across_frames() -> None:
    """One device must not look like a different one in every frame."""
    pseudonymiser = Pseudonymiser()
    first = parse_register_record(Frame(redact(_record(), pseudonymiser)))
    second = parse_register_record(Frame(redact(_record(), pseudonymiser)))

    assert first.datalogger_serial == second.datalogger_serial


def test_two_devices_stay_distinguishable() -> None:
    payload = parse_register_record(Frame(redact(_record(), Pseudonymiser())))
    assert payload.datalogger_serial != payload.inverter_serial


def test_separate_runs_do_not_share_a_mapping() -> None:
    """A fresh key per run stops two published captures being correlated."""
    first = parse_register_record(Frame(redact(_record(), Pseudonymiser())))
    second = parse_register_record(Frame(redact(_record(), Pseudonymiser())))

    assert first.datalogger_serial != second.datalogger_serial


def test_a_short_frame_is_passed_through_untouched() -> None:
    assert redact(b"\x00\x01\x00", Pseudonymiser()) == b"\x00\x01\x00"


def test_a_plaintext_frame_on_an_obfuscated_protocol_is_not_scrambled() -> None:
    """Firmware exists that sends its key-exchange handshake unobfuscated on protocol 06.

    Deobfuscating it yields random bytes, in which an 8-to-16-character upper-alphanumeric
    run turns up often enough; rewriting one corrupted the only frame that explained the
    capture. See https://github.com/FezVrasta/growatt-datalogger/issues/3.
    """
    body = b"Password&*20240730" + bytes(60)
    # Built by hand: build_frame obfuscates, and the whole point is a frame that
    # declares protocol 06 and is not obfuscated.
    header = b"\x00\x01\x00\x06" + (2 + len(body)).to_bytes(2, "big") + b"\x01\x41"
    frame = append_crc(header + body)

    clean = redact(frame, Pseudonymiser())

    # Edited in the view it is actually written in, so it stays readable...
    assert b"Password&*" in clean
    assert len(clean) == len(frame)
    # ...and the one thing in it shaped like a serial is still replaced.
    assert b"20240730" not in clean


def test_an_encrypted_body_is_passed_through_untouched() -> None:
    """There is no serial in ciphertext to replace, and rewriting it destroys evidence."""
    ciphertext = bytes((i * 37 + 11) % 256 for i in range(64))
    frame = append_crc(b"\x00\x01\x00\x06\x00\x42\x01\x04" + ciphertext)

    assert redact(frame, Pseudonymiser()) == frame
