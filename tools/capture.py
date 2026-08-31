#!/usr/bin/env python3
"""Capture a datalogger session, with serial numbers redacted.

Point a datalogger at this instead of at Home Assistant, and it records every frame in
both directions while forwarding to a real server. The output is a JSONL file that can be
attached to an issue.

Serial numbers identify a specific person's hardware, so they are replaced before anything
is written. The replacement is deterministic (an HMAC of the serial, truncated and
re-encoded to the same length and alphabet), which matters: a capture with mangled serials
would not decode, and one with randomly different serials in each frame would look like
several devices. Because the serial sits inside the obfuscated body, the frame is
deobfuscated, edited, re-obfuscated, and its checksum recomputed -- so a redacted capture
is still a valid, decodable session.

Usage::

    # Forward to the Growatt cloud, as a datalogger normally would
    python tools/capture.py --out session.jsonl

    # Or forward to a Home Assistant already running the integration
    python tools/capture.py --upstream 192.168.1.10 --upstream-port 5279 --out session.jsonl

Then point the datalogger's server address at the machine running this, on port 5279.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import hmac
import json
import re
import secrets
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from custom_components.growatt_datalogger.protocol.crc import append_crc
from custom_components.growatt_datalogger.protocol.crypt import (
    OBFUSCATED_PROTOCOLS,
    xor_payload,
)
from custom_components.growatt_datalogger.protocol.framing import Framer

#: Growatt serials are upper-case alphanumeric.
_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
_SERIAL_RE = re.compile(rb"[A-Z0-9]{8,16}")


class Pseudonymiser:
    """Replaces serials with stable, same-shape stand-ins."""

    def __init__(self, key: bytes | None = None) -> None:
        # A fresh key per run, so two captures cannot be correlated to each other.
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

    # Only the first ~80 bytes of a payload hold serials; searching the whole record
    # would rewrite register values that happen to look like ASCII.
    for match in _SERIAL_RE.finditer(bytes(plain[8 : min(end, 8 + 80)])):
        start = 8 + match.start()
        plain[start : start + len(match.group())] = pseudonymiser.replace(match.group())

    if not obfuscated:
        return bytes(plain)
    return append_crc(xor_payload(bytes(plain[:end])))


class Capture:
    def __init__(self, path: Path, pseudonymiser: Pseudonymiser) -> None:
        self.path = path
        self.pseudonymiser = pseudonymiser
        self.started = time.monotonic()
        self.frames = 0
        self._handle = path.open("w", encoding="utf-8")

    def write(self, direction: str, frame: bytes) -> None:
        clean = redact(frame, self.pseudonymiser)
        self._handle.write(
            json.dumps(
                {
                    "t": round(time.monotonic() - self.started, 3),
                    "dir": direction,
                    "hex": clean.hex(),
                }
            )
            + "\n"
        )
        self._handle.flush()
        self.frames += 1

    def close(self) -> None:
        self._handle.close()


async def _relay(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    capture: Capture,
    direction: str,
) -> None:
    framer = Framer()
    try:
        while True:
            data = await reader.read(4096)
            if not data:
                return
            writer.write(data)
            await writer.drain()
            try:
                for frame in framer.feed(data):
                    capture.write(direction, frame)
            except Exception as err:  # a malformed frame is itself worth recording
                print(f"  framing error ({err}); capture continues", file=sys.stderr)
                framer.reset()
    except (ConnectionResetError, BrokenPipeError, asyncio.CancelledError):
        return


async def _serve(args: argparse.Namespace, capture: Capture) -> None:
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        print(f"datalogger connected from {peer}")
        try:
            up_reader, up_writer = await asyncio.open_connection(args.upstream, args.upstream_port)
        except OSError as err:
            print(f"  cannot reach {args.upstream}:{args.upstream_port} ({err})")
            writer.close()
            return

        try:
            await asyncio.gather(
                _relay(reader, up_writer, capture, "up"),
                _relay(up_reader, writer, capture, "down"),
            )
        finally:
            for handle_ in (writer, up_writer):
                handle_.close()
                with contextlib.suppress(Exception):
                    await handle_.wait_closed()
            print(f"datalogger disconnected; {capture.frames} frames captured")

    server = await asyncio.start_server(handle, args.host, args.port)
    print(
        f"listening on {args.host}:{args.port}, forwarding to {args.upstream}:{args.upstream_port}"
    )
    print(f"writing to {capture.path}. Press Ctrl-C to stop.")
    async with server:
        await server.serve_forever()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=5279)
    parser.add_argument("--upstream", default="server.growatt.com")
    parser.add_argument("--upstream-port", type=int, default=5279)
    parser.add_argument("--out", type=Path, default=Path("capture.jsonl"))
    parser.add_argument(
        "--show-mapping",
        action="store_true",
        help="print the serial substitutions on exit (do NOT share this)",
    )
    args = parser.parse_args()

    pseudonymiser = Pseudonymiser()
    capture = Capture(args.out, pseudonymiser)

    try:
        asyncio.run(_serve(args, capture))
    except KeyboardInterrupt:
        print(f"\nstopped after {capture.frames} frames")
    finally:
        capture.close()
        if args.show_mapping:
            print("serial substitutions:", pseudonymiser.mapping, file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
