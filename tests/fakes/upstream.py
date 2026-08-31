"""A stand-in for server.growatt.com.

Records everything it receives and can be killed mid-session, which is the only way to
exercise the relay's degraded path without waiting for the real cloud to have an outage.
"""

from __future__ import annotations

import asyncio
import contextlib


class FakeUpstream:
    """A TCP server that captures what a relay forwards to it."""

    def __init__(self, *, auto_ack: bool = False) -> None:
        self.received: list[bytes] = []
        self.connections = 0
        self.auto_ack = auto_ack
        """Reply to each record the way the real cloud would, so the relay stays healthy."""

        self._server: asyncio.Server | None = None
        self._writers: list[asyncio.StreamWriter] = []

    @property
    def port(self) -> int:
        assert self._server is not None and self._server.sockets
        return int(self._server.sockets[0].getsockname()[1])

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.connections += 1
        self._writers.append(writer)
        try:
            while True:
                data = await reader.read(4096)
                if not data:
                    return
                self.received.append(data)
                if self.auto_ack:
                    writer.write(b"\x00\x01\x00\x06\x00\x03\x01\x04\x47\x00\x00")
                    await writer.drain()
        except (ConnectionResetError, BrokenPipeError, asyncio.CancelledError):
            return

    async def kill(self) -> None:
        """Drop every connection but stay listening, as an outage would."""
        for writer in self._writers:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
        self._writers.clear()

    async def stop(self) -> None:
        await self.kill()
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(Exception):
                await self._server.wait_closed()
            self._server = None
