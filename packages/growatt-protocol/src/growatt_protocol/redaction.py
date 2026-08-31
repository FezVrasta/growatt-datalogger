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

#: Growatt serials are upper-case alphanumeric.
_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"

_SERIAL_RE = re.compile(rb"[A-Z0-9]{8,16}")

#: Only the first stretch of a payload holds serials. Searching the whole record would
#: rewrite register values that happen to look like ASCII.
_SEARCH_WINDOW = 80


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


def redact(frame: bytes, pseudonymiser: Pseudonymiser) -> bytes:
    """Return ``frame`` with its serials replaced and its checksum fixed."""
    if len(frame) < 8:
        return frame

    protocol = frame[3]
    obfuscated = protocol in OBFUSCATED_PROTOCOLS

    plain = bytearray(xor_payload(frame) if obfuscated else frame)
    end = len(plain) - (2 if obfuscated else 0)

    window = bytes(plain[8 : min(end, 8 + _SEARCH_WINDOW)])
    for match in _SERIAL_RE.finditer(window):
        start = 8 + match.start()
        plain[start : start + len(match.group())] = pseudonymiser.replace(match.group())

    if not obfuscated:
        return bytes(plain)
    return append_crc(xor_payload(bytes(plain[:end])))
