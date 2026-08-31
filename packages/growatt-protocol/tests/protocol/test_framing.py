"""Incremental frame reassembly.

TCP fragmentation and coalescing are the failure mode that a naive one-recv-per-record
reader gets wrong, so these tests drive both directions hard.
"""

from __future__ import annotations

import pytest
from growatt_protocol.errors import FrameError
from growatt_protocol.framing import (
    Framer,
    frame_length,
)
from growatt_protocol.testing.frames import build_frame
from hypothesis import given, settings
from hypothesis import strategies as st


@pytest.mark.parametrize("protocol", [2, 5, 6])
def test_frame_length_matches_what_the_builder_produced(protocol: int) -> None:
    frame = build_frame(b"\x01\x02\x03\x04", protocol=protocol)
    assert frame_length(frame) == len(frame)


def test_frame_length_accounts_for_the_crc_only_on_05_and_06() -> None:
    body = b"abcd"
    plain = build_frame(body, protocol=2)
    encrypted = build_frame(body, protocol=6)

    # Same declared length, but 05/06 carry two extra checksum bytes.
    assert len(encrypted) == len(plain) + 2


def test_unsupported_protocol_is_rejected() -> None:
    frame = bytearray(build_frame(b"abcd", protocol=6))
    frame[3] = 0x09
    with pytest.raises(FrameError, match="unsupported"):
        frame_length(bytes(frame))


def test_impossible_declared_length_is_rejected() -> None:
    frame = bytearray(build_frame(b"abcd", protocol=6))
    frame[4:6] = (1).to_bytes(2, "big")
    with pytest.raises(FrameError, match="below the minimum"):
        frame_length(bytes(frame))


def test_single_frame_delivered_whole() -> None:
    frame = build_frame(b"hello world")
    assert Framer().feed(frame) == [frame]


def test_partial_frame_is_buffered_not_emitted() -> None:
    frame = build_frame(b"hello world")
    framer = Framer()

    assert framer.feed(frame[:-1]) == []
    assert framer.pending == frame[:-1]
    assert framer.feed(frame[-1:]) == [frame]
    assert framer.pending == b""


def test_coalesced_frames_are_split() -> None:
    frames = [build_frame(f"record-{i}".encode(), sequence=i) for i in range(3)]
    assert Framer().feed(b"".join(frames)) == frames


def test_byte_at_a_time_delivery() -> None:
    """The worst case: one byte per read."""
    frames = [build_frame(f"record-{i}".encode(), sequence=i) for i in range(3)]
    stream = b"".join(frames)

    framer = Framer()
    received = [f for byte in stream for f in framer.feed(bytes([byte]))]

    assert received == frames
    assert framer.pending == b""


@settings(max_examples=200)
@given(
    payloads=st.lists(st.binary(min_size=0, max_size=80), min_size=1, max_size=10),
    protocols=st.lists(st.sampled_from([2, 5, 6]), min_size=1, max_size=10),
    chunk_sizes=st.lists(st.integers(min_value=1, max_value=200), min_size=1, max_size=40),
)
def test_arbitrary_chunking_recovers_exactly(
    payloads: list[bytes], protocols: list[int], chunk_sizes: list[int]
) -> None:
    frames = [
        build_frame(payload, protocol=protocols[i % len(protocols)], sequence=i)
        for i, payload in enumerate(payloads)
    ]
    stream = b"".join(frames)

    framer = Framer()
    received: list[bytes] = []
    offset = 0
    index = 0
    while offset < len(stream):
        size = chunk_sizes[index % len(chunk_sizes)]
        received.extend(framer.feed(stream[offset : offset + size]))
        offset += size
        index += 1

    assert received == frames
    assert framer.pending == b""


def test_oversized_frame_is_refused_without_buffering() -> None:
    framer = Framer(max_frame=64)
    header = (1).to_bytes(2, "big") + b"\x00\x06" + (5000).to_bytes(2, "big") + b"\x01\x04"

    with pytest.raises(FrameError, match="exceeds maximum"):
        framer.feed(header)


def test_reset_discards_buffered_bytes() -> None:
    framer = Framer()
    framer.feed(build_frame(b"hello world")[:5])
    assert framer.pending != b""

    framer.reset()
    assert framer.pending == b""


def test_feed_is_eager() -> None:
    """The buffer must advance even if the caller ignores the return value.

    A generator-based feed() would silently do nothing here.
    """
    frame = build_frame(b"hello world")
    framer = Framer()

    framer.feed(frame[:4])
    assert framer.pending == frame[:4]
