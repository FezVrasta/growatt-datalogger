"""XOR obfuscation."""

from __future__ import annotations

from growatt_protocol.crypt import (
    HEADER_LENGTH,
    KEY,
    deobfuscate,
    xor_payload,
)
from hypothesis import given
from hypothesis import strategies as st


def test_key_is_the_ascii_word() -> None:
    assert KEY == b"Growatt"
    assert KEY.hex() == "47726f77617474"


@given(st.binary(min_size=HEADER_LENGTH, max_size=600))
def test_transform_is_its_own_inverse(frame: bytes) -> None:
    assert xor_payload(xor_payload(frame)) == frame


@given(st.binary(min_size=HEADER_LENGTH, max_size=600))
def test_header_is_never_touched(frame: bytes) -> None:
    assert xor_payload(frame)[:HEADER_LENGTH] == frame[:HEADER_LENGTH]


def test_keystream_restarts_at_offset_eight() -> None:
    """The byte at offset 8 is XORed with 'G', not with KEY[8 % 7].

    This is the detail that silently corrupts every payload if you index the keystream
    from the start of the frame instead of from the end of the header.
    """
    frame = bytes(HEADER_LENGTH) + bytes(len(KEY) * 2)
    body = xor_payload(frame)[HEADER_LENGTH:]

    assert body == KEY * 2
    assert body[0] == ord("G")


def test_a_zero_byte_at_offset_eight_encodes_to_0x47() -> None:
    """0x47 is the pre-encrypted zero used as the ACK payload byte."""
    assert xor_payload(bytes(HEADER_LENGTH) + b"\x00")[HEADER_LENGTH] == 0x47


def test_frames_at_or_below_header_length_are_unchanged() -> None:
    for size in range(HEADER_LENGTH + 1):
        frame = bytes(range(size))
        assert xor_payload(frame) == frame


def test_protocol_02_is_not_obfuscated() -> None:
    frame = bytes(HEADER_LENGTH) + b"payload"
    assert deobfuscate(frame, protocol=2) == frame
    assert deobfuscate(frame, protocol=5) != frame
    assert deobfuscate(frame, protocol=6) != frame
