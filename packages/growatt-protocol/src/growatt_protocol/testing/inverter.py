"""A register bank that answers commands behind a :class:`~.datalogger.FakeDatalogger`.

Reads and writes are what an application does at every interesting moment -- on startup,
on every user action -- and a device that does not answer them blocks the command lock
until each one times out. So a test that hand-serves them spends most of itself on
protocol clerking rather than on the behaviour it set out to check, and the clerking is
subtly wrong in a different way in each test file: a read echoes its *range* before any
values, and a reply that omits the end register parses as a value that looks convincingly
real.

This answers from a dict in the background, remembers accepted writes so a read-back
returns the new value the way real hardware does, and records every request for the test
to assert on afterwards.
"""

from __future__ import annotations

import asyncio
import contextlib
from types import TracebackType

from ..records import Frame
from .datalogger import FakeDatalogger
from .frames import (
    build_command_response,
    build_range_write_response,
    build_read_response,
    build_write_response,
)


def request_register(frame: Frame) -> int:
    """The register a command addresses -- the start of the range, for a range command."""
    width = frame.serial_width
    return int.from_bytes(frame.body[width : width + 2], "big")


def request_values(frame: Frame) -> tuple[int, ...]:
    """The words a 0x10 range write carries."""
    width = frame.serial_width
    payload = frame.body[width + 4 :]
    return tuple(
        int.from_bytes(payload[i : i + 2], "big")
        for i in range(0, len(payload) - len(payload) % 2, 2)
    )


class FakeInverter:
    """Answers register commands for ``device`` until the context exits.

    Despite the name it also answers the datalogger's own parameter writes (0x18), which
    is what a clock update is -- one responder rather than two, since a test driving an
    inverter still has a datalogger setting its clock underneath.
    """

    def __init__(
        self,
        device: FakeDatalogger,
        values: dict[int, int] | None = None,
        *,
        missing: set[int] | None = None,
        discard: set[int] | None = None,
        result: int = 0,
    ) -> None:
        self.device = device
        self.values = dict(values or {})
        """The register bank. Unlisted registers read as zero."""

        self.missing = set(missing or ())
        """Registers the model does not implement.

        A read of any range covering one of these comes back as the echo alone, which is
        how a real device says "not here" -- it does not answer with an error.
        """

        self.discard = set(discard or ())
        """Registers that answer "accepted" and then keep their old value.

        The third outcome of a write, and the one a fake that always stores what it is
        told cannot express. Real firmware does this: it will take a value it has no way
        to act on -- arming a charge window whose start and stop are both still 00:00 is
        the case that turns up -- report success, and go on reading back as it was.
        """

        self.result = result
        """The status byte to answer writes with. Zero accepts."""

        self.requests: list[Frame] = []
        """Every command seen, in order."""

        self._arrived = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def __aenter__(self) -> FakeInverter:
        self._task = asyncio.create_task(self._run())
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        assert self._task is not None
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task

    async def wait_for(
        self, function: int, register: int | None = None, *, timeout: float = 5.0
    ) -> Frame:
        """The first matching request, whether it has already arrived or is still to come.

        Looking backwards as well as forwards matters: the responder answers as soon as a
        request lands, so by the time an awaited service call returns, the request the
        test wants to assert on is already in the past.
        """
        index = 0
        async with asyncio.timeout(timeout):
            while True:
                while index < len(self.requests):
                    frame = self.requests[index]
                    index += 1
                    if frame.function == function and (
                        register is None or request_register(frame) == register
                    ):
                        return frame
                self._arrived.clear()
                await self._arrived.wait()

    async def _run(self) -> None:
        while True:
            try:
                request = await self.device.read_frame(timeout=0.2)
            except TimeoutError:
                continue
            except ConnectionError:
                return
            answer = self._answer(request)
            if answer is None:
                # An acknowledgement or ping echo coming back from the server. Not ours.
                continue
            self.requests.append(request)
            self._arrived.set()
            await self.device.send_raw(answer)

    def _answer(self, request: Frame) -> bytes | None:
        serial = self.device.datalogger_serial
        protocol = self.device.protocol
        common = {"protocol": protocol, "sequence": request.sequence}
        register = request_register(request) if len(request.body) >= request.serial_width + 2 else 0

        if request.function == 0x05:
            width = request.serial_width
            end = int.from_bytes(request.body[width + 2 : width + 4], "big")
            if self.missing & set(range(register, end + 1)):
                return build_read_response(serial, register=register, end_register=end, **common)
            values = [self.values.get(r, 0) for r in range(register, end + 1)]
            return build_read_response(
                serial, register=register, values=values, end_register=end, **common
            )

        if request.function == 0x06:
            width = request.serial_width
            value = int.from_bytes(request.body[width + 2 : width + 4], "big")
            if self.result == 0 and register not in self.discard:
                self.values[register] = value
            return build_write_response(
                serial, register=register, value=value, result=self.result, **common
            )

        if request.function == 0x10:
            width = request.serial_width
            end = int.from_bytes(request.body[width + 2 : width + 4], "big")
            if self.result == 0:
                for offset, value in enumerate(request_values(request)):
                    if register + offset not in self.discard:
                        self.values[register + offset] = value
            return build_range_write_response(
                serial, start=register, end=end, result=self.result, **common
            )

        if request.function == 0x18:
            # A datalogger parameter -- the clock, usually. One status byte.
            return build_command_response(
                serial, function=0x18, register=register, tail=bytes([self.result]), **common
            )

        return None
