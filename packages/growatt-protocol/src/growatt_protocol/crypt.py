"""The XOR obfuscation used by Growatt protocol versions 05 and 06.

The payload is XORed with the repeating ASCII key ``Growatt``. Two details matter and
are easy to get wrong:

* The eight-byte header is **not** obfuscated. It is transmitted in clear so that a
  receiver can read the protocol version and length before it knows whether to
  deobfuscate anything.
* The keystream index restarts at zero at byte offset 8, so the byte at offset 8 is
  XORed with ``'G'``, not with ``KEY[8 % 7]``.

Protocol version 02 is not obfuscated at all. Do not apply this unconditionally.

The transform is its own inverse, so the same function encrypts outbound frames.
"""

from __future__ import annotations

KEY = b"Growatt"

HEADER_LENGTH = 8

#: Protocol versions whose payload is XOR-obfuscated and which carry a trailing CRC.
OBFUSCATED_PROTOCOLS = frozenset({5, 6})

#: Every protocol version this implementation understands.
SUPPORTED_PROTOCOLS = frozenset({2, 5, 6})


def xor_payload(frame: bytes) -> bytes:
    """Return ``frame`` with its payload XORed against the repeating key.

    The first :data:`HEADER_LENGTH` bytes are copied unchanged. Applying this twice
    returns the original frame.
    """
    if len(frame) <= HEADER_LENGTH:
        return bytes(frame)

    header = bytes(frame[:HEADER_LENGTH])
    body = memoryview(frame)[HEADER_LENGTH:]
    key_len = len(KEY)
    # Build a keystream that is at least as long as the body, then truncate. This is
    # meaningfully faster than a per-byte modulo for the sizes involved.
    repeats = -(-len(body) // key_len)
    keystream = (KEY * repeats)[: len(body)]
    return header + bytes(a ^ b for a, b in zip(body, keystream, strict=True))


def deobfuscate(frame: bytes, protocol: int) -> bytes:
    """Return the plaintext of ``frame`` for the given protocol version."""
    if protocol in OBFUSCATED_PROTOCOLS:
        return xor_payload(frame)
    return bytes(frame)


def obfuscate(frame: bytes, protocol: int) -> bytes:
    """Inverse of :func:`deobfuscate`. Identical, but named for the calling direction."""
    return deobfuscate(frame, protocol)
