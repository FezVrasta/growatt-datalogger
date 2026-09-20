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

Refusing the optimistic update is not the same as putting the control right, and it took
issue #2 to notice the difference. The front end moves a control the moment someone moves
it and corrects itself when a new state arrives; a state that did not change produces no
event, so after a failed write there is nothing to correct it with. Hence
:meth:`~GrowattWriteEntity._republish`, and hence every path that raises going through
it.

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

from growatt_protocol import CommandTimeout, settings
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

    @callback
    def _republish(self) -> None:
        """Say what this register holds, even though it has not changed.

        For after a write that did not take. The front end sets a control to the value
        someone typed the moment they type it, and puts it right again when a new state
        arrives -- but none ever does, because the register still holds exactly what it
        held, and Home Assistant sends no event for a state that did not change. So the
        dial stays on a number the inverter never accepted until something else happens
        to move it, which is what issue #2 describes as the value getting stuck: an error
        appears, and the control goes on showing the value the error was about.

        ``force_update`` is what makes an unchanged state an event rather than a silent
        reassertion.
        """
        self._attr_force_update = True
        try:
            self.async_write_ha_state()
        finally:
            self._attr_force_update = False

    async def _async_refresh(self) -> int | None:
        """Read this one register back, to confirm what a write actually did.

        Deliberately not the batch: after a write the caller is waiting, and one
        register is what changed.

        Returns the word the inverter answered with, or None if it did not answer.
        """
        session = self._session()
        if session is None:
            return None
        word = await settings.read_back(session, self.spec.register)
        if word is None:
            _LOGGER.debug(
                "%s did not answer a read of register %s", self.device.serial, self.spec.register
            )
            return None

        self._remember(self.spec.decode(word))
        return word

    async def _async_write(self, value: Any) -> None:
        """Write ``value``, and put the entity right if it did not take.

        Every path out of here that raises leaves the front end holding a value the
        inverter does not have, so every one of them ends at :meth:`_republish`.
        """
        try:
            await self._async_write_and_confirm(value)
        except HomeAssistantError:
            self._republish()
            raise

    async def _async_write_and_confirm(self, value: Any) -> None:
        """Write ``value``, then read the register back to confirm."""
        session = self._session()
        if session is None:
            raise HomeAssistantError(f"Datalogger for {self.device.serial} is not connected")

        try:
            word = self.spec.encode(value)
        except ValueError as err:
            raise HomeAssistantError(str(err)) from err

        # Where to start looking for somebody else's writes, should this one appear not
        # to have applied. Taken before the write rather than after, because the command
        # that overwrites a register is as likely to be in flight when this one goes out
        # as to arrive after it.
        mark = session.unsolicited_mark

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
            #
            # So the whole block is re-read in the background rather than being left for
            # the timer. Telling someone to press a button to find out what their
            # inverter holds is work this can do for them, and by the time they have
            # read the message it is usually already done.
            self.hub.async_refresh_settings(self.device.key)
            raise HomeAssistantError(
                f"{self.spec.key} was accepted, but the inverter never answered a read of "
                f"holding register {self.spec.register} to confirm it, so whether the change "
                "took effect is unknown. These settings are being re-read now, and will "
                "show what the inverter actually holds."
            )
        if readback != word:
            raise HomeAssistantError(
                f"The inverter accepted {self.spec.key} but holding register "
                f"{self.spec.register} still reads back as {readback}, not {word}. The "
                f"change was not applied. {self._why_unchanged(session, mark)}"
            )

    def _why_unchanged(self, session: Any, since: int) -> str:
        """Why a register the inverter accepted a write for still reads as it did.

        Two explanations, and from the register alone they are identical. One is firmware
        declining to act on a value it has no way to use. The other is the relay: with it
        on, Growatt issues its own commands down the same socket, and a write of theirs
        landing in the same moment puts the old value back before this read-back asks.
        The connection is the only thing that knows which, and the remedies are nothing
        alike, so it should not be left to a user to guess.
        """
        others = session.written_elsewhere(self.spec.register, since=since)
        if not others:
            return (
                "Some firmware discards a value it cannot act on -- enabling a window "
                "that is still 00:00-00:00, for instance -- so check whether this "
                "setting depends on another one."
            )
        return (
            f"While the change was being made, {len(others)} "
            f"write{'' if len(others) == 1 else 's'} covering this register arrived on "
            "this connection that this integration did not ask for. That is the Growatt "
            "cloud: with the relay on it commands the same datalogger over the same "
            "connection, and it can put the old value back. Turning the relay off in the "
            "integration's options would settle whether that is what happened here."
        )

    def _session(self) -> Any:
        return self.hub.session_for_device(self.device)
