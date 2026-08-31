"""Services for reading and writing registers directly.

These are the escape hatch. Register semantics beyond the documented banks vary by model
and firmware, so rather than guessing and shipping entities for everything, the
integration exposes the raw operations and lets someone who knows their device use them.

``write_register`` asks for confirmation outside a small allowlist. Writing the wrong
holding register on a grid-tied inverter can misconfigure it, and a deliberate speed bump
is cheap insurance against a typo in a script.
"""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
    callback,
)
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr

from .const import DOMAIN
from .hub import GrowattHub
from .protocol import commands
from .protocol.errors import CommandTimeout

_LOGGER = logging.getLogger(__name__)

SERVICE_READ_REGISTER = "read_register"
SERVICE_WRITE_REGISTER = "write_register"
SERVICE_SYNC_TIME = "sync_time"

ATTR_DEVICE_ID = "device_id"
ATTR_TARGET = "target"
ATTR_REGISTER = "register"
ATTR_END_REGISTER = "end_register"
ATTR_VALUE = "value"
ATTR_CONFIRM = "confirm"

TARGET_INVERTER = "inverter"
TARGET_DATALOGGER = "datalogger"

#: Registers safe enough not to require confirmation. Documented in the Growatt
#: specification, bounded, and reversible.
_NO_CONFIRM_REQUIRED = {3}

_READ_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_DEVICE_ID): cv.string,
        vol.Optional(ATTR_TARGET, default=TARGET_INVERTER): vol.In(
            [TARGET_INVERTER, TARGET_DATALOGGER]
        ),
        vol.Required(ATTR_REGISTER): vol.All(int, vol.Range(min=0, max=65535)),
        vol.Optional(ATTR_END_REGISTER): vol.All(int, vol.Range(min=0, max=65535)),
    }
)

_WRITE_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_DEVICE_ID): cv.string,
        vol.Optional(ATTR_TARGET, default=TARGET_INVERTER): vol.In(
            [TARGET_INVERTER, TARGET_DATALOGGER]
        ),
        vol.Required(ATTR_REGISTER): vol.All(int, vol.Range(min=0, max=65535)),
        vol.Required(ATTR_VALUE): vol.Any(int, cv.string),
        vol.Optional(ATTR_CONFIRM, default=False): cv.boolean,
    }
)

_SYNC_SCHEMA = vol.Schema({vol.Required(ATTR_DEVICE_ID): cv.string})


def _resolve(hass: HomeAssistant, device_id: str) -> tuple[GrowattHub, str]:
    """Map a Home Assistant device id to a hub and a datalogger serial."""
    registry = dr.async_get(hass)
    device = registry.async_get(device_id)
    if device is None:
        raise ServiceValidationError(f"Unknown device {device_id}")

    for entry_id in device.config_entries:
        entry = hass.config_entries.async_get_entry(entry_id)
        if entry is None or entry.domain != DOMAIN:
            continue
        hub: GrowattHub = entry.runtime_data

        for domain, key in device.identifiers:
            if domain != DOMAIN:
                continue
            known = hub.devices.get(key)
            if known is None:
                continue
            # A command always goes to a datalogger, even when it addresses the
            # inverter behind it, so resolve through the parent when necessary.
            serial = known.serial if known.parent is None else hub.devices[known.parent].serial
            return hub, serial

    raise ServiceValidationError(f"{device_id} is not a Growatt Datalogger device")


def _session(hub: GrowattHub, serial: str) -> Any:
    for session in hub.sessions:
        if session.datalogger_serial == serial:
            return session
    raise HomeAssistantError(f"Datalogger {serial} is not currently connected")


@callback
def async_register_services(hass: HomeAssistant) -> None:
    """Register the services once, for the whole domain."""

    async def _read(call: ServiceCall) -> ServiceResponse:
        hub, serial = _resolve(hass, call.data[ATTR_DEVICE_ID])
        session = _session(hub, serial)
        register = call.data[ATTR_REGISTER]
        end = call.data.get(ATTR_END_REGISTER, register)

        if call.data[ATTR_TARGET] == TARGET_DATALOGGER:
            command = commands.read_datalogger(serial, session.protocol, register)
        else:
            command = commands.read_inverter(serial, session.protocol, register, end)

        try:
            response = await session.send_command(command)
        except (CommandTimeout, ConnectionError) as err:
            raise HomeAssistantError(str(err)) from err

        return {
            "register": response.register,
            "value": response.value,
            "empty": response.empty,
        }

    async def _write(call: ServiceCall) -> ServiceResponse:
        hub, serial = _resolve(hass, call.data[ATTR_DEVICE_ID])
        session = _session(hub, serial)
        register = call.data[ATTR_REGISTER]
        value = call.data[ATTR_VALUE]
        target = call.data[ATTR_TARGET]

        if register not in _NO_CONFIRM_REQUIRED and not call.data[ATTR_CONFIRM]:
            raise ServiceValidationError(
                f"Writing register {register} needs confirm: true. Register meanings "
                "vary by model and firmware, and a wrong write can misconfigure the "
                "inverter."
            )

        if target == TARGET_DATALOGGER:
            command = commands.write_datalogger(serial, session.protocol, register, str(value))
        else:
            if not isinstance(value, int):
                raise ServiceValidationError("Inverter registers take an integer, not a string")
            command = commands.write_inverter(serial, session.protocol, register, value)

        try:
            response = await session.send_command(command)
        except (CommandTimeout, ConnectionError) as err:
            # Never retried: a blind repeat could apply a change twice. The caller can
            # read the register back to see what actually happened.
            raise HomeAssistantError(str(err)) from err

        if not response.ok:
            raise HomeAssistantError(f"The device rejected the write with result {response.result}")
        return {"register": response.register, "result": response.result}

    async def _sync_time(call: ServiceCall) -> None:
        hub, serial = _resolve(hass, call.data[ATTR_DEVICE_ID])
        session = _session(hub, serial)
        command = commands.set_time(serial, session.protocol, dt_now())
        try:
            response = await session.send_command(command)
        except (CommandTimeout, ConnectionError) as err:
            raise HomeAssistantError(str(err)) from err
        if not response.ok:
            raise HomeAssistantError(
                f"The datalogger rejected the clock update (result {response.result})"
            )

    hass.services.async_register(
        DOMAIN,
        SERVICE_READ_REGISTER,
        _read,
        schema=_READ_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_WRITE_REGISTER,
        _write,
        schema=_WRITE_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(DOMAIN, SERVICE_SYNC_TIME, _sync_time, schema=_SYNC_SCHEMA)


def dt_now():
    """Local wall-clock time.

    Local rather than UTC on purpose: the timestamps a datalogger puts in its own
    records are local, so setting its clock to UTC would silently shift every record.
    """
    from homeassistant.util import dt as dt_util

    return dt_util.now()
