#!/usr/bin/env python3
"""Capture a datalogger session, with serial numbers redacted.

Point a datalogger at this instead of at Home Assistant, and it records every frame in
both directions while forwarding to a real server. The output is a JSONL file that can be
attached to an issue.

Serial numbers identify a specific person's hardware, so they are replaced before anything
is written, by :mod:`growatt_protocol.redaction` -- deterministically and at the same
length, so the capture still decodes and still looks like one device.

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
import json
import sys
import time
from pathlib import Path

from growatt_protocol import Framer
from growatt_protocol.redaction import Pseudonymiser, redact


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
