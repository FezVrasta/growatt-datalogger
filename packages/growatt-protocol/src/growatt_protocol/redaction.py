"""Replacing serial numbers in captured frames, so a capture can be shared.

A packet capture is the only practical way to add support for hardware nobody working on
this owns, but it identifies a specific person's equipment. Redaction has to leave the
capture *usable*: mangled serials would stop it decoding, and serials that differ from
frame to frame would look like several devices.

So replacements are deterministic, the same length, and drawn from the same alphabet.
Because a serial sits inside the obfuscated body, a frame is deobfuscated, edited,
re-obfuscated and its checksum recomputed -- a redacted capture is still a valid session.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets

from .crc import append_crc
from .crypt import OBFUSCATED_PROTOCOLS, xor_payload
from .errors import RecordError
from .records import serial_width

#: Growatt serials are upper-case alphanumeric.
_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"

_SERIAL_RE = re.compile(rb"[A-Z0-9]{8,16}")

#: Only the first stretch of a payload holds serials. Searching the whole record would
#: rewrite register values that happen to look like ASCII.
_SEARCH_WINDOW = 80

#: How much of the *serial field* must be printable or NUL before a view is treated as
#: plaintext. Judged there rather than over the whole window, which also holds register
#: values and timestamps: a serial padded with NULs is entirely readable, ciphertext is
#: not, and random bytes clear this about as often as never.
_TEXT_RATIO = 0.9


class Pseudonymiser:
    """Replaces serials with stable, same-shape stand-ins."""

    def __init__(self, key: bytes | None = None) -> None:
        # A fresh key per run, so two published captures cannot be correlated to each
        # other or back to the hardware.
        self.key = key or secrets.token_bytes(32)
        self._seen: dict[bytes, bytes] = {}

    def replace(self, serial: bytes) -> bytes:
        if serial in self._seen:
            return self._seen[serial]

        digest = hmac.new(self.key, serial, hashlib.sha256).digest()
        replacement = bytes(
            _ALPHABET[digest[i] % len(_ALPHABET)].encode()[0] for i in range(len(serial))
        )
        self._seen[serial] = replacement
        return replacement

    @property
    def mapping(self) -> dict[str, str]:
        """The substitutions made. Do not publish this alongside the capture."""
        return {
            original.decode("ascii", "replace"): new.decode()
            for original, new in self._seen.items()
        }


def _looks_like_text(window: bytes) -> bool:
    """Whether a window plausibly holds a serial rather than ciphertext.

    A serial field is ASCII padded with NULs, so the start of a real record body is
    overwhelmingly printable. Ciphertext, and plaintext XORed by mistake, are not.
    """
    if not window:
        return False
    readable = sum(1 for byte in window if byte == 0 or 0x20 <= byte < 0x7F)
    return readable / len(window) >= _TEXT_RATIO


def redact(frame: bytes, pseudonymiser: Pseudonymiser) -> bytes:
    """Return ``frame`` with its serials replaced and its checksum fixed.

    Which of the two views holds the serial is decided by looking, not by the protocol
    number. Protocol 06 is *usually* obfuscated, but firmware exists that sends its
    session-key handshake as plaintext on protocol 06 and encrypts everything else --
    and assuming the obfuscation rewrote random 8-to-16-byte runs of a frame it could
    not read, corrupting the one frame that explained the capture.
    """
    if len(frame) < 8:
        return frame

    obfuscated = frame[3] in OBFUSCATED_PROTOCOLS
    deobfuscated = bytearray(xor_payload(frame)) if obfuscated else None
    verbatim = bytearray(frame)

    # The CRC is not part of the payload, and on an obfuscated frame the deobfuscated
    # copy has garbage where it was.
    end = len(frame) - (2 if obfuscated else 0)

    try:
        width = serial_width(frame[3])
    except RecordError:
        return frame

    for plain, is_view_obfuscated in ((deobfuscated, True), (verbatim, False)):
        if plain is None:
            continue
        if not _looks_like_text(bytes(plain[8 : min(end, 8 + width)])):
            continue
        window = bytes(plain[8 : min(end, 8 + _SEARCH_WINDOW)])
        for match in _SERIAL_RE.finditer(window):
            start = 8 + match.start()
            plain[start : start + len(match.group())] = pseudonymiser.replace(match.group())
        if not is_view_obfuscated:
            return bytes(plain)
        return append_crc(xor_payload(bytes(plain[:end])))

    # Neither view is readable, so there is no serial here to replace -- the body is
    # encrypted with something this package does not implement. Passing it through
    # untouched is what keeps such a capture worth sending.
    return frame
