"""Services: direct register access, and adopting another integration's history.

The register services are the escape hatch. Register semantics beyond the documented
banks vary by model and firmware, so rather than guessing and shipping entities for
everything, the integration exposes the raw operations and lets someone who knows their
device use them.

``write_register`` asks for confirmation outside a small allowlist. Writing the wrong
holding register on a grid-tied inverter can misconfigure it, and a deliberate speed bump
is cheap insurance against a typo in a script.

``adopt_history`` is the migration path from whatever was reading the inverter before;
``docs/MIGRATION.md`` has the whole story.
"""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from growatt_protocol import CommandTimeout, commands, settings
from homeassistant.const import ATTR_UNIT_OF_MEASUREMENT
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
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .hub import GrowattHub

_LOGGER = logging.getLogger(__name__)

SERVICE_READ_REGISTER = "read_register"
SERVICE_WRITE_REGISTER = "write_register"
SERVICE_WRITE_REGISTERS = "write_registers"
SERVICE_SYNC_TIME = "sync_time"
SERVICE_ADOPT_HISTORY = "adopt_history"

ATTR_DEVICE_ID = "device_id"
ATTR_TARGET = "target"
ATTR_REGISTER = "register"
ATTR_END_REGISTER = "end_register"
ATTR_VALUE = "value"
ATTR_VALUES = "values"
ATTR_START = "start_register"
ATTR_CONFIRM = "confirm"
ATTR_TARGET_ENTITY = "target_entity"
ATTR_SOURCE_ENTITY = "source_entity_id"
ATTR_DISCARD = "discard_target_statistics"

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

_WRITE_MANY_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_DEVICE_ID): cv.string,
        vol.Required(ATTR_START): vol.All(int, vol.Range(min=0, max=65535)),
        vol.Required(ATTR_VALUES): vol.All(
            cv.ensure_list, [vol.All(int, vol.Range(min=0, max=65535))], vol.Length(min=1)
        ),
        vol.Optional(ATTR_CONFIRM, default=False): cv.boolean,
    }
)

_SYNC_SCHEMA = vol.Schema({vol.Required(ATTR_DEVICE_ID): cv.string})

_ADOPT_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_TARGET_ENTITY): cv.entity_id,
        vol.Required(ATTR_SOURCE_ENTITY): cv.entity_id,
        vol.Optional(ATTR_DISCARD, default=False): cv.boolean,
    }
)


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
    if (session := hub.session_for(serial)) is not None:
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
            # whole_slot=False deliberately: this is the raw escape hatch, and someone
            # poking 1080 by hand means that register and no other. The entities are
            # where a charge window is treated as one setting.
            command = commands.write_inverter(serial, session.protocol, register, value)

        try:
            response = await session.send_command(command)
        except (CommandTimeout, ConnectionError) as err:
            # Never retried: a blind repeat could apply a change twice. The caller can
            # read the register back to see what actually happened.
            raise HomeAssistantError(str(err)) from err

        if not response.ok:
            if target == TARGET_DATALOGGER:
                raise HomeAssistantError(
                    f"The datalogger rejected the write to register {register}: "
                    f"{commands.describe_result(response.result)}"
                )
            # An unfamiliar register is exactly where "result 2" is least useful, so the
            # raw service gets the same diagnosis the entities do.
            raise HomeAssistantError(await settings.explain_rejection(session, register, response))
        return {"register": response.register, "result": response.result}

    async def _write_many(call: ServiceCall) -> ServiceResponse:
        """Write a contiguous run of holding registers in one operation (0x10).

        The reason this exists separately from write_register: a 32-bit quantity spans
        two registers, and writing it as two single-register calls leaves a window in
        which the inverter holds half of the old value and half of the new one. Sending
        the run as one command removes that window.
        """
        hub, serial = _resolve(hass, call.data[ATTR_DEVICE_ID])
        session = _session(hub, serial)
        start = call.data[ATTR_START]
        values = call.data[ATTR_VALUES]

        # Always confirmed, with no allowlist. A multi-register write touches a range
        # rather than one documented setting, so there is no small set of these that is
        # safe enough to wave through.
        if not call.data[ATTR_CONFIRM]:
            raise ServiceValidationError(
                f"Writing {len(values)} registers from {start} needs confirm: true."
            )

        try:
            response = await session.send_command(
                commands.write_inverter_range(serial, session.protocol, start, values)
            )
        except (CommandTimeout, ConnectionError) as err:
            raise HomeAssistantError(str(err)) from err

        if not response.ok:
            raise HomeAssistantError(
                f"The inverter rejected the write to {len(values)} registers from "
                f"{start}: {commands.describe_result(response.result)}"
            )
        return {
            "start_register": response.register,
            "count": len(values),
            "result": response.result,
        }

    async def _sync_time(call: ServiceCall) -> None:
        hub, serial = _resolve(hass, call.data[ATTR_DEVICE_ID])
        await async_sync_clock(_session(hub, serial), serial)

    async def _adopt_history(call: ServiceCall) -> ServiceResponse:
        """Hand a Growatt entity the entity id of the one that used to read this inverter.

        Statistics and history are keyed by entity id, not by entity, and Home Assistant
        already migrates both when an entity is renamed. So the whole migration is one
        rename: the new sensor takes the old sensor's id, and the Energy Dashboard,
        every automation and every card go on pointing at something that exists.

        The old entity has to be gone first -- an id can only have one owner -- which is
        also what leaves its statistics behind to be picked up. Recorder refuses to move
        the *target's* own series onto an id that already has one, and that refusal is
        the desired outcome: the long series is kept and the new entity continues it.
        What is left orphaned is the handful of days the target recorded under its own
        id, which is exactly the period the source was recording in parallel.
        """
        # Recorder is an *after* dependency, never a hard one: this is the only thing
        # here that needs it, and an integration that reads an inverter has no business
        # refusing to load on a system that keeps no history.
        from homeassistant.components.recorder import get_instance
        from homeassistant.components.recorder.statistics import (
            STATISTIC_UNIT_TO_UNIT_CONVERTER,
            async_list_statistic_ids,
        )

        target: str = call.data[ATTR_TARGET_ENTITY]
        source: str = call.data[ATTR_SOURCE_ENTITY]

        registry = er.async_get(hass)
        entry = registry.async_get(target)
        if entry is None or entry.platform != DOMAIN:
            raise ServiceValidationError(f"{target} is not a Growatt Datalogger entity")
        if source == target:
            raise ServiceValidationError(f"{target} already has that id")
        if source.split(".")[0] != target.split(".")[0]:
            raise ServiceValidationError(
                f"{source} and {target} are in different domains. An entity can only "
                "take an id from its own domain."
            )
        if registry.async_is_registered(source) or not hass.states.async_available(source):
            raise ServiceValidationError(
                f"{source} still exists. Delete the old integration -- or the YAML "
                "entity -- first: an id has one owner, and its history is only free to "
                "be adopted once nothing holds it."
            )

        if "recorder" not in hass.config.components:
            raise HomeAssistantError("The recorder is not running, so there is no history to adopt")

        # The target's unit has to be the one its statistics already compile in, not
        # merely its native unit, because that is what recorder compares against. A
        # mismatch it cannot convert does not fail loudly -- it silently stops compiling
        # long-term statistics for the entity -- so it is worth refusing up front.
        state = hass.states.get(target)
        if state is None:
            raise ServiceValidationError(
                f"{target} has no state. A disabled entity cannot adopt anything."
            )
        target_unit = state.attributes.get(ATTR_UNIT_OF_MEASUREMENT)

        found = {
            meta["statistic_id"]: meta for meta in await async_list_statistic_ids(hass, {source})
        }
        if (source_meta := found.get(source)) is None:
            raise ServiceValidationError(
                f"No long-term statistics exist for {source}, so there is nothing to "
                "adopt. Check the spelling against Developer tools > Statistics. To "
                "reuse the id alone, rename the entity in its settings instead."
            )

        source_unit = source_meta["statistics_unit_of_measurement"]
        if source_unit != target_unit:
            converter = STATISTIC_UNIT_TO_UNIT_CONVERTER.get(source_unit)
            if converter is None or converter is not STATISTIC_UNIT_TO_UNIT_CONVERTER.get(
                target_unit
            ):
                raise ServiceValidationError(
                    f"{source} recorded {source_unit} and {target} reports "
                    f"{target_unit}, which are not convertible. Adopting the series "
                    "would stop long-term statistics for this entity."
                )

        try:
            registry.async_update_entity(target, new_entity_id=source)
        except ValueError as err:
            raise ServiceValidationError(str(err)) from err

        _LOGGER.info("%s adopted the history of %s", target, source)

        if call.data[ATTR_DISCARD]:
            # Whatever the target compiled under its own id is now unreachable: no
            # entity answers to that id any more. Dropping it is optional because it is
            # still data, and Developer tools > Statistics can do it later by hand.
            get_instance(hass).async_clear_statistics([target])

        return {
            "entity_id": source,
            "previous_entity_id": target,
            "statistics_unit_of_measurement": source_unit,
            "orphaned_statistics": None if call.data[ATTR_DISCARD] else target,
        }

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
    hass.services.async_register(
        DOMAIN,
        SERVICE_WRITE_REGISTERS,
        _write_many,
        schema=_WRITE_MANY_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(DOMAIN, SERVICE_SYNC_TIME, _sync_time, schema=_SYNC_SCHEMA)
    hass.services.async_register(
        DOMAIN,
        SERVICE_ADOPT_HISTORY,
        _adopt_history,
        schema=_ADOPT_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )


async def async_sync_clock(session: Any, serial: str) -> None:
    """Set ``serial``'s clock to Home Assistant's local time.

    Shared with the Sync time button rather than written twice: the two had drifted into
    the same six steps and the same user-facing sentence, which is one string too many to
    keep in step by hand.

    Local time rather than UTC on purpose -- the timestamps a datalogger puts in its own
    records are local, so setting its clock to UTC would silently shift every record.
    """
    try:
        response = await session.send_command(
            commands.set_time(serial, session.protocol, dt_util.now())
        )
    except (CommandTimeout, ConnectionError) as err:
        raise HomeAssistantError(str(err)) from err

    if not response.ok:
        raise HomeAssistantError(
            f"The datalogger rejected the clock update: {commands.describe_result(response.result)}"
        )
