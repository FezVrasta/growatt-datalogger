"""Config and options flow.

Setup asks for a port and nothing else. There is no device list to fill in: dataloggers
announce themselves when they connect, so anything more would be asking the user for
information the protocol already provides.
"""

from __future__ import annotations

import asyncio
import errno
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.const import CONF_PORT
from homeassistant.helpers import config_validation as cv

from .const import (
    BUFFERED_EVENT,
    BUFFERED_IGNORE,
    CONF_BUFFERED_POLICY,
    CONF_INCLUDE_UNKNOWN,
    DEFAULT_BUFFERED_POLICY,
    DEFAULT_INCLUDE_UNKNOWN,
    DEFAULT_PORT,
    DOMAIN,
)


async def _port_is_free(port: int) -> bool:
    """Bind and immediately release, to fail at configuration time rather than later."""
    try:
        server = await asyncio.start_server(lambda r, w: None, "0.0.0.0", port)
    except OSError as err:
        if err.errno in (errno.EADDRINUSE, errno.EACCES):
            return False
        raise
    server.close()
    await server.wait_closed()
    return True


class GrowattConfigFlow(ConfigFlow, domain=DOMAIN):
    """Set up the datalogger listener."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            port = user_input[CONF_PORT]
            if await _port_is_free(port):
                return self.async_create_entry(
                    title=f"Growatt Datalogger (port {port})", data={CONF_PORT: port}
                )
            errors[CONF_PORT] = "port_in_use"

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_PORT,
                        default=(user_input or {}).get(CONF_PORT, DEFAULT_PORT),
                    ): cv.port,
                }
            ),
            errors=errors,
            description_placeholders={"port": str(DEFAULT_PORT)},
        )

    @staticmethod
    def async_get_options_flow(config_entry: Any) -> GrowattOptionsFlow:
        return GrowattOptionsFlow()


class GrowattOptionsFlow(OptionsFlowWithReload):
    """Decoding and record-handling options.

    OptionsFlowWithReload reloads the entry on save, which is what re-binds the socket
    if anything server-side changed.
    """

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)

        options = self.config_entry.options
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_INCLUDE_UNKNOWN,
                        default=options.get(CONF_INCLUDE_UNKNOWN, DEFAULT_INCLUDE_UNKNOWN),
                    ): cv.boolean,
                    vol.Required(
                        CONF_BUFFERED_POLICY,
                        default=options.get(CONF_BUFFERED_POLICY, DEFAULT_BUFFERED_POLICY),
                    ): vol.In([BUFFERED_EVENT, BUFFERED_IGNORE]),
                }
            ),
        )
