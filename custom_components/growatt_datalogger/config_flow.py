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
from growatt_protocol.registers import PROFILES
from homeassistant.config_entries import (
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.const import CONF_PORT
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.selector import (
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .const import (
    BUFFERED_EVENT,
    BUFFERED_IGNORE,
    CONF_BUFFERED_POLICY,
    CONF_INCLUDE_UNKNOWN,
    CONF_PROFILE_OVERRIDES,
    CONF_RELAY_ENABLED,
    CONF_RELAY_HOST,
    CONF_RELAY_PORT,
    DEFAULT_BUFFERED_POLICY,
    DEFAULT_INCLUDE_UNKNOWN,
    DEFAULT_PORT,
    DEFAULT_RELAY_ENABLED,
    DEFAULT_RELAY_HOST,
    DEFAULT_RELAY_PORT,
    DOMAIN,
    KIND_INVERTER,
    PROFILE_AUTO,
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

    def _inverter_serials(self) -> list[str]:
        """Serials of the inverters seen so far, for the profile picker.

        Read from the hub rather than from options, because the whole point is to offer
        a profile for a device the user has never configured -- it announced itself.
        """
        hub = getattr(self.config_entry, "runtime_data", None)
        if hub is None:
            return []
        return sorted(
            device.serial for device in hub.devices.values() if device.kind == KIND_INVERTER
        )

    def _merged(self, user_input: dict[str, Any]) -> ConfigFlowResult:
        """Save one step's fields without dropping the ones the other step owns."""
        return self.async_create_entry(data={**self.config_entry.options, **user_input})

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        # Only offer the profile step when there is something to pin. On a fresh install
        # no datalogger has connected yet, and a menu with one real choice is friction.
        if not self._inverter_serials():
            return await self.async_step_settings()
        return self.async_show_menu(step_id="init", menu_options=["settings", "profiles"])

    async def async_step_profiles(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pin the register profile per inverter.

        Needed because a few families cannot be told apart from the record alone. An SPF,
        and at least one SPH with a ShineWiFi-S, report a 0-based block whose registers
        mean something entirely different from Protocol II's -- so when the integration
        says it is unsure, this is the only way to resolve it.
        """
        serials = self._inverter_serials()
        if user_input is not None:
            # "auto" means *remove* the pin, not store a sentinel: resolve_profile takes
            # an override or None, and a serial mapped to "auto" would match no profile
            # and silently fall through to inference anyway.
            overrides = {
                serial: choice
                for serial, choice in user_input.items()
                if serial in serials and choice != PROFILE_AUTO
            }
            return self._merged({CONF_PROFILE_OVERRIDES: overrides})

        current = self.config_entry.options.get(CONF_PROFILE_OVERRIDES) or {}
        options = [SelectOptionDict(value=PROFILE_AUTO, label="Detect automatically")] + [
            SelectOptionDict(value=key, label=f"{key} -- {profile.description}")
            for key, profile in PROFILES.items()
        ]
        return self.async_show_form(
            step_id="profiles",
            data_schema=vol.Schema(
                {
                    vol.Required(serial, default=current.get(serial, PROFILE_AUTO)): SelectSelector(
                        SelectSelectorConfig(options=options, mode=SelectSelectorMode.DROPDOWN)
                    )
                    for serial in serials
                }
            ),
        )

    async def async_step_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self._merged(user_input)

        options = self.config_entry.options
        return self.async_show_form(
            step_id="settings",
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
                    vol.Required(
                        CONF_RELAY_ENABLED,
                        default=options.get(CONF_RELAY_ENABLED, DEFAULT_RELAY_ENABLED),
                    ): cv.boolean,
                    vol.Required(
                        CONF_RELAY_HOST,
                        default=options.get(CONF_RELAY_HOST, DEFAULT_RELAY_HOST),
                    ): cv.string,
                    vol.Required(
                        CONF_RELAY_PORT,
                        default=options.get(CONF_RELAY_PORT, DEFAULT_RELAY_PORT),
                    ): cv.port,
                }
            ),
        )
