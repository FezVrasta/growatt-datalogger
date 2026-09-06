"""A fake datalogger that speaks the real protocol over a real socket.

Deliberately able to misbehave in the ways a real device does: fragmenting a record
across many small writes, and coalescing several records into one. Those are the two
behaviours that break implementations which assume one read equals one record, so the
tests need to reproduce them rather than assume they do not happen.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import datetime

from ..framing import Framer
from ..records import Frame, serial_width
from .frames import build_data_record, build_frame, build_group


class FakeDatalogger:
    """An asyncio client that behaves like a ShineLan/ShineWiFi datalogger."""

    def __init__(
        self,
        *,
        datalogger_serial: str = "GPG0EXAMP1",
        inverter_serial: str = "SML0EXAMP2",
        protocol: int = 6,
        chunk_size: int | None = None,
    ) -> None:
        self.datalogger_serial = datalogger_serial
        self.inverter_serial = inverter_serial
        self.protocol = protocol
        self.chunk_size = chunk_size
        """When set, every write is split into pieces of this many bytes."""

        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._framer = Framer()
        self._sequence = 0

    async def connect(self, host: str, port: int) -> None:
        self._reader, self._writer = await asyncio.open_connection(host, port)

    async def close(self) -> None:
        if self._writer is not None:
            self._writer.close()
            with contextlib.suppress(ConnectionResetError, BrokenPipeError):
                await self._writer.wait_closed()
            self._writer = None

    async def __aenter__(self) -> FakeDatalogger:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    # ------------------------------------------------------------------
    # Sending
    # ------------------------------------------------------------------

    def _next_sequence(self) -> int:
        self._sequence += 1
        return self._sequence

    async def send_raw(self, data: bytes) -> None:
        assert self._writer is not None, "not connected"
        if self.chunk_size is None:
            self._writer.write(data)
            await self._writer.drain()
            return

        for offset in range(0, len(data), self.chunk_size):
            self._writer.write(data[offset : offset + self.chunk_size])
            await self._writer.drain()
            # Yield so the server actually gets each piece separately, which is the
            # point of fragmenting in the first place.
            await asyncio.sleep(0)

    def build_record(
        self,
        *,
        function: int = 0x04,
        groups: list[bytes] | None = None,
        timestamp: datetime | None = None,
    ) -> bytes:
        return build_data_record(
            protocol=self.protocol,
            function=function,
            sequence=self._next_sequence(),
            datalogger_serial=self.datalogger_serial,
            inverter_serial=self.inverter_serial,
            timestamp=timestamp or datetime(2026, 8, 31, 12, 0, 0),
            groups=groups if groups is not None else [build_group(3000, [1, 2585, 3295])],
        )

    async def send_data(self, **kwargs: object) -> bytes:
        record = self.build_record(**kwargs)  # type: ignore[arg-type]
        await self.send_raw(record)
        return record

    async def send_announce(self, **kwargs: object) -> bytes:
        return await self.send_data(function=0x03, **kwargs)  # type: ignore[arg-type]

    async def send_buffered(self, **kwargs: object) -> bytes:
        return await self.send_data(function=0x50, **kwargs)  # type: ignore[arg-type]

    async def send_ping(self) -> bytes:
        body = (
            self.datalogger_serial.encode().ljust(serial_width(self.protocol), b"\x00")
            + b"\x00\x00"
        )
        frame = build_frame(
            body,
            protocol=self.protocol,
            function=0x16,
            sequence=self._next_sequence(),
        )
        await self.send_raw(frame)
        return frame

    async def send_coalesced(self, count: int = 3) -> list[bytes]:
        """Write several records in a single TCP write."""
        records = [self.build_record() for _ in range(count)]
        await self.send_raw(b"".join(records))
        return records

    # ------------------------------------------------------------------
    # Receiving
    # ------------------------------------------------------------------

    async def read_frame(self, timeout: float = 2.0) -> Frame:
        """Wait for one frame from the server."""
        assert self._reader is not None, "not connected"

        async def _read() -> Frame:
            while True:
                data = await self._reader.read(4096)  # type: ignore[union-attr]
                if not data:
                    raise ConnectionError("server closed the connection")
                frames = self._framer.feed(data)
                if frames:
                    self._pending = frames[1:]
                    return Frame(frames[0])

        if getattr(self, "_pending", None):
            return Frame(self._pending.pop(0))
        return await asyncio.wait_for(_read(), timeout)

    async def expect_nothing(self, within: float = 0.15) -> None:
        """Assert the server stays silent, e.g. for a command response."""
        assert self._reader is not None, "not connected"
        try:
            data = await asyncio.wait_for(self._reader.read(4096), within)
        except TimeoutError:
            return
        raise AssertionError(f"expected no reply, got {data!r}")
