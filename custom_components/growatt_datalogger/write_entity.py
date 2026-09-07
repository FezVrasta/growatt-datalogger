"""Shared plumbing for the entities that write registers.

Write entities differ from sensors in where their value comes from. A telemetry record
carries input registers; these settings live in the holding space. Fortunately an
announce carries the holding space, so the device volunteers the current value of every
one of these registers each time it connects, and the entity simply reads it from the
coordinator.

That leaves an entity with nothing to say about the registers an announce does not carry,
and -- because a datalogger announces only when it reconnects -- nothing to correct it
when a setting changes anywhere else. Both are the hub's job rather than an entity's: it
reads the whole set on the first record and again on a timer, and publishes the words
where every entity here already looks for them. See
:meth:`~.hub.GrowattHub.async_refresh_settings`.

A write that the device rejects does not update the state. Optimistic updates would be
worse than useless here: showing a battery cut-off the inverter never accepted is exactly
the sort of thing someone builds an automation on. A rejection also gets one extra read
before it is reported, so the message can say whether the inverter has the register at
all rather than quoting a status byte at someone. And an *accepted* write is read back
too, because acceptance is not application: firmware will take a value and then discard
it -- arming a window that is still 00:00-00:00 is the case that turns up in practice --
and a switch that reports success for a change the inverter dropped is the worst of the
three outcomes.

Charge and discharge windows are the exception to "one entity, one register": firmware
validates a whole slot, so all three of its registers go out together. How that is done
lives in :mod:`growatt_protocol.settings`, next to
:class:`~growatt_protocol.registers.writable.TimeSlot`, rather than here -- writing a
register safely is not a property of being a Home Assistant entity.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime
from typing import Any

from growatt_protocol import CommandTimeout, commands, settings
from growatt_protocol.registers.writable import (
    WritableRegister,
    WriteKind,
    for_profile,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import KIND_INVERTER, VALUE_HOLDING, VALUE_HOLDING_AT
from .entity import GrowattEntity, async_on_new_device
from .hub import GrowattDevice, GrowattHub
from .metadata import pretty

_LOGGER = logging.getLogger(__name__)


def async_setup_write_platform(
    hass: HomeAssistant,
    entry: Any,
    async_add_entities: AddConfigEntryEntitiesCallback,
    kind: WriteKind,
    factory: Callable[[GrowattHub, GrowattDevice, WritableRegister], GrowattEntity],
) -> None:
    """Create write entities of one kind as inverters are discovered."""
    hub: GrowattHub = entry.runtime_data
    created: set[tuple[str, str]] = set()

    @callback
    def _add(device_key: str, _names: list[str]) -> None:
        device = hub.devices.get(device_key)
        if device is None or device.kind != KIND_INVERTER or device.profile is None:
            return

        specs = for_profile(device.profile, include_unverified=True)
        entities = []
        for spec in specs:
            if spec.kind is not kind:
                continue
            token = (device_key, spec.key)
            if token in created:
                continue
            created.add(token)
            entities.append(factory(hub, device, spec))

        if entities:
            async_add_entities(entities)

    async_on_new_device(hass, entry, _add)


class GrowattWriteEntity(GrowattEntity):
    """Base for an entity backed by a writable holding register."""

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, hub: GrowattHub, device: GrowattDevice, spec: WritableRegister) -> None:
        super().__init__(hub, device, spec.key)
        self.spec = spec
        self._attr_name = pretty(spec.key)
        self._attr_icon = spec.icon
        self._attr_entity_registry_enabled_default = spec.enabled_default
        self._current: Any = None
        self._current_at: datetime | None = None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        # Surfacing the provenance means a user can judge for themselves whether to
        # trust a register this project has flagged as unverified.
        return {
            "register": self.spec.register,
            "confidence": self.spec.confidence.value,
            "source": self.spec.source,
        }

    @property
    def _reported(self) -> Any | None:
        """This register's value as the device itself last reported it.

        An announce carries the holding space, which is where these settings live, so
        the device volunteers the current value every time it connects -- no command
        round-trip needed, and it refreshes itself.

        Taken from the announce's raw words rather than from the profile's named values,
        because a profile names only a handful of holding registers and none of the
        SPH/SPA storage block: charge priority, the SOC limits and all six Grid First /
        Battery First windows have no name to look up. Reading the word is what makes
        the free refresh apply to every write entity rather than to two of them. It also
        removes the double-scaling hazard that the name lookup had to guard against -- a
        named value has already been through the register table's scale, a raw word has
        not -- so every encoding can come this way.
        """
        word = ((self.coordinator.data or {}).get(VALUE_HOLDING) or {}).get(self.spec.register)
        if not isinstance(word, int):
            return None
        return self.spec.decode(word)

    @property
    def _state(self) -> Any | None:
        """What to display: whichever of the device's report and our own read is newer.

        Not simply "prefer the device". Both are the device -- one is what it announced
        when it last connected, the other is what it answered when we last read the
        register -- and an announce can be hours old while a read-back is seconds old.
        Preferring the announce unconditionally would snap a switch back to its
        pre-write value for the rest of the connection, which is exactly the confusion
        this is meant to end.
        """
        reported = self._reported
        if reported is None:
            return self._current
        if self._current is None or self._current_at is None:
            return reported
        announced = (self.coordinator.data or {}).get(VALUE_HOLDING_AT)
        if announced is not None and announced > self._current_at:
            return reported
        return self._current

    @callback
    def _remember(self, value: Any) -> None:
        """Record a value we read from the register, and when we read it."""
        self._current = value
        self._current_at = dt_util.utcnow()
        self.async_write_ha_state()

    async def _async_refresh(self) -> int | None:
        """Read this one register back, to confirm what a write actually did.

        Deliberately not the batch: after a write the caller is waiting, and one
        register is what changed.

        Returns the word the inverter answered with, or None if it did not answer.
        """
        session = self._session()
        if session is None:
            return None
        try:
            response = await session.send_command(
                commands.read_inverter(
                    session.datalogger_serial, session.protocol, self.spec.register
                )
            )
        except (CommandTimeout, ConnectionError) as err:
            _LOGGER.debug("could not read %s: %s", self.spec.key, err)
            return None

        if response.empty or response.value is None:
            # The device does not implement this register. Better an unknown value than
            # a plausible-looking wrong one.
            _LOGGER.debug(
                "%s does not implement register %s", self.device.serial, self.spec.register
            )
            return None

        word = int(response.value)
        self._remember(self.spec.decode(word))
        return word

    async def _async_write(self, value: Any) -> None:
        """Write ``value``, then read the register back to confirm."""
        session = self._session()
        if session is None:
            raise HomeAssistantError(f"Datalogger for {self.device.serial} is not connected")

        try:
            word = self.spec.encode(value)
        except ValueError as err:
            raise HomeAssistantError(str(err)) from err

        # How a register is written safely -- whole-slot writes, the range-write
        # fallback, and turning a refusal into something a user can act on -- lives in
        # growatt_protocol.settings, next to the register table rather than on an entity
        # class, so the register services get the same behaviour.
        try:
            response = await settings.write_register(session, self.spec.register, word)
        except (CommandTimeout, ConnectionError) as err:
            # Deliberately not retried: repeating a write could apply a change twice.
            raise HomeAssistantError(
                f"{self.spec.key} was not confirmed: {err}. Reload or read the register "
                "back to see whether it took effect."
            ) from err

        if not response.ok:
            raise HomeAssistantError(
                await settings.explain_rejection(
                    session, self.spec.register, response, name=self.spec.key
                )
            )

        # Accepted is not applied. Firmware will take a write and then quietly discard
        # it -- arming a window whose start and stop are both still 00:00 is the case
        # that turns up in practice -- and the read-back is the only way to tell that
        # apart from a change that stuck. Reporting success for a change the inverter
        # dropped leaves someone with a switch that says on and an inverter that is off,
        # which is worse than either an error or a refusal.
        readback = await self._async_refresh()
        if readback is None:
            # And a read-back that got no answer confirms nothing either way. Saying
            # nothing here is the same silence as success, which is the one thing this
            # must not be: a datalogger hangs up between commands often enough that the
            # confirmation is the part most likely to be lost, and the write it was
            # confirming may well have applied.
            raise HomeAssistantError(
                f"{self.spec.key} was accepted, but reading holding register "
                f"{self.spec.register} back to confirm it got no answer, so whether the "
                "change took effect is unknown. The next settings refresh will show what "
                "the inverter actually holds; the Refresh settings button asks now."
            )
        if readback != word:
            raise HomeAssistantError(
                f"The inverter accepted {self.spec.key} but holding register "
                f"{self.spec.register} still reads back as {readback}, not {word}. The "
                "change was not applied. Some firmware discards a value it cannot act on "
                "-- enabling a window that is still 00:00-00:00, for instance -- so check "
                "whether this setting depends on another one."
            )

    def _session(self) -> Any:
        return self.hub.session_for_device(self.device)
