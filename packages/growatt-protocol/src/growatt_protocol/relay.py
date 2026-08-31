"""Optional pass-through to the Growatt cloud.

With the relay on, this server sits in front of Growatt rather than replacing it: bytes
from the datalogger go upstream first, then are decoded locally, and Growatt's replies are
returned verbatim. The ShinePhone app keeps working.

Two rules make this safe.

**Forward before decoding.** The relay must never be held up by our own parsing, so bytes
go upstream before anything looks at them. A decoder bug then costs local telemetry, not
the user's cloud account.

**Exactly one side acknowledges.** When the relay is healthy Growatt sends the ACKs and we
suppress ours; two servers replying to the same record with the same sequence number is a
situation no datalogger is documented to handle. If upstream dies we take over
immediately, because no ACK at all is worse than a duplicate.

The changeover is the delicate part. Switching back to suppressed mid-stream would leave a
window where the device is acknowledged by nobody, so once we have taken over, we keep
answering for the rest of that connection and only return to relayed acknowledgement when
the datalogger next reconnects.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from dataclasses import dataclass

_LOGGER = logging.getLogger(__name__)

DEFAULT_UPSTREAM_HOST = "server.growatt.com"
DEFAULT_UPSTREAM_PORT = 5279


@dataclass(slots=True)
class RelayConfig:
    host: str = DEFAULT_UPSTREAM_HOST
    port: int = DEFAULT_UPSTREAM_PORT
    connect_timeout: float = 10.0
    max_buffer: int = 262144
    """Bytes we will hold for a slow upstream before giving up on it.

    Applying backpressure to the datalogger instead would be worse: it would stall the
    device that is trying to give us data, in order to protect a cloud service that is
    already struggling.
    """


class RelayConnection:
    """One upstream connection, paired to one datalogger connection."""

    def __init__(
        self,
        config: RelayConfig,
        downstream_send: Callable[[bytes], asyncio.Future | asyncio.Task | None],
        *,
        on_state_change: Callable[[bool], None] | None = None,
    ) -> None:
        self.config = config
        self._downstream_send = downstream_send
        self._on_state_change = on_state_change

        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._pump: asyncio.Task[None] | None = None
        self._buffered = 0

        self.connected = False
        self.degraded = False
        """True once we have taken over acknowledgement for this connection.

        Sticky by design: see the module docstring.
        """

    async def start(self) -> bool:
        """Open the upstream connection. Returns whether it succeeded."""
        try:
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(self.config.host, self.config.port),
                timeout=self.config.connect_timeout,
            )
        except (OSError, TimeoutError) as err:
            _LOGGER.warning(
                "cloud relay could not reach %s:%s (%s); acknowledging locally instead",
                self.config.host,
                self.config.port,
                err,
            )
            self._degrade()
            return False

        self.connected = True
        self._pump = asyncio.create_task(self._pump_downstream())
        _LOGGER.debug("cloud relay connected to %s:%s", self.config.host, self.config.port)
        self._notify()
        return True

    def forward(self, data: bytes) -> None:
        """Send bytes upstream. Never blocks and never raises."""
        if not self.connected or self._writer is None:
            return

        transport_buffer = self._writer.transport.get_write_buffer_size()
        if transport_buffer > self.config.max_buffer:
            _LOGGER.warning(
                "cloud relay is %s bytes behind; giving up on upstream for this session",
                transport_buffer,
            )
            self._degrade()
            return

        try:
            self._writer.write(data)
        except Exception:
            _LOGGER.debug("cloud relay write failed", exc_info=True)
            self._degrade()

    async def _pump_downstream(self) -> None:
        """Return everything Growatt sends back to the datalogger, unchanged."""
        assert self._reader is not None
        try:
            while True:
                data = await self._reader.read(4096)
                if not data:
                    _LOGGER.info("cloud relay: upstream closed the connection")
                    break
                result = self._downstream_send(data)
                if asyncio.isfuture(result) or asyncio.iscoroutine(result):
                    await result
        except asyncio.CancelledError:
            raise
        except Exception:
            _LOGGER.debug("cloud relay read failed", exc_info=True)
        finally:
            self._degrade()

    def _degrade(self) -> None:
        """Stop relying on upstream and start acknowledging locally."""
        if self.degraded:
            return
        self.degraded = True
        self.connected = False
        _LOGGER.info("cloud relay degraded; this connection is now acknowledged locally")
        self._notify()

    def _notify(self) -> None:
        if self._on_state_change is None:
            return
        try:
            self._on_state_change(self.connected)
        except Exception:
            _LOGGER.exception("relay state listener failed")

    async def close(self) -> None:
        self.connected = False
        if self._pump is not None:
            self._pump.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._pump
            self._pump = None
        if self._writer is not None:
            self._writer.close()
            with contextlib.suppress(Exception):
                await self._writer.wait_closed()
            self._writer = None
