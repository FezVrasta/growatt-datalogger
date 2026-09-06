"""Shared plumbing for the entities that write registers.

Write entities differ from sensors in where their value comes from. A telemetry record
carries input registers; these settings live in the holding space. Fortunately an
announce carries holding registers, so for most of them the device volunteers the current
value every time it connects, and the entity simply reads it from the coordinator.

For a register no announce reports, the entity asks for it directly -- but only once a
record has arrived, because entities are added during setup, before any datalogger has
connected. Reading at add time talks to nothing, and as a one-shot it would never retry,
leaving the entity unknown for good.

A write that the device rejects does not update the state. Optimistic updates would be
worse than useless here: showing a battery cut-off the inverter never accepted is exactly
the sort of thing someone builds an automation on. A rejection also gets one extra read
before it is reported, so the message can say whether the inverter has the register at
all rather than quoting a status byte at someone.

Charge and discharge windows are the exception to "one entity, one register": firmware
validates a whole slot, so all three of its registers go out together. See
:data:`~growatt_protocol.registers.writable.TIME_SEGMENTS`.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from growatt_protocol import CommandTimeout, commands
from growatt_protocol.registers.writable import (
    Encoding,
    WritableRegister,
    WriteKind,
    for_profile,
    segment_for,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import KIND_INVERTER, SIGNAL_NEW_DEVICE
from .entity import GrowattEntity
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

        entities = []
        for spec in for_profile(device.profile, include_unverified=True):
            if spec.kind is not kind:
                continue
            token = (device_key, spec.key)
            if token in created:
                continue
            created.add(token)
            entities.append(factory(hub, device, spec))

        if entities:
            async_add_entities(entities)

    entry.async_on_unload(
        async_dispatcher_connect(hass, SIGNAL_NEW_DEVICE.format(entry_id=entry.entry_id), _add)
    )
    hub.async_replay(_add)


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
        self._refresh_requested = False

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

        An announce carries holding registers, which is where these settings live, so
        for most of them the device volunteers the current value every time it connects
        -- no command round-trip needed, and it refreshes itself.

        Only unscaled encodings are taken this way. A scaled one would already have been
        divided by the register table, and running it through :meth:`decode` again would
        scale it twice.
        """
        if self.spec.encoding not in (Encoding.RAW, Encoding.BOOL):
            return None
        value = (self.coordinator.data or {}).get(self.spec.key)
        if not isinstance(value, int):
            return None
        return self.spec.decode(value)

    @property
    def _state(self) -> Any | None:
        """What to display: the device's own report, else our last read or write."""
        reported = self._reported
        return self._current if reported is None else reported

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        # No read here. Entities are added during setup, before any datalogger has
        # connected, so a read at this point has nothing to talk to and -- being a
        # one-shot -- would never be retried, leaving the entity unknown for good. The
        # value comes from the announce instead, and _handle_coordinator_update asks
        # explicitly only for the registers an announce does not carry.
        self._refresh_requested = False

    @callback
    def _handle_coordinator_update(self) -> None:
        # A record has arrived, so the device is connected and a command can be sent.
        if not self._refresh_requested and self._reported is None and self._current is None:
            self._refresh_requested = True
            self.hass.async_create_background_task(
                self._async_refresh(),
                name=f"growatt read {self.device.serial} {self.spec.key}",
            )
        super()._handle_coordinator_update()

    async def _async_refresh(self) -> None:
        """Read the register back. Leaves the value unknown if it cannot be read."""
        session = self._session()
        if session is None:
            return
        try:
            response = await session.send_command(
                commands.read_inverter(
                    session.datalogger_serial, session.protocol, self.spec.register
                )
            )
        except (CommandTimeout, ConnectionError) as err:
            _LOGGER.debug("could not read %s: %s", self.spec.key, err)
            return

        if response.empty or response.value is None:
            # The device does not implement this register. Better an unknown value than
            # a plausible-looking wrong one.
            _LOGGER.debug(
                "%s does not implement register %s", self.device.serial, self.spec.register
            )
            return

        self._current = self.spec.decode(int(response.value))
        self.async_write_ha_state()

    async def _async_write(self, value: Any) -> None:
        """Write ``value``, then read the register back to confirm."""
        session = self._session()
        if session is None:
            raise HomeAssistantError(f"Datalogger for {self.device.serial} is not connected")

        try:
            word = self.spec.encode(value)
        except ValueError as err:
            raise HomeAssistantError(str(err)) from err

        try:
            response = await self._send_write(session, word)
        except (CommandTimeout, ConnectionError) as err:
            # Deliberately not retried: repeating a write could apply a change twice.
            raise HomeAssistantError(
                f"{self.spec.key} was not confirmed: {err}. Reload or read the register "
                "back to see whether it took effect."
            ) from err

        if not response.ok:
            raise HomeAssistantError(await self._explain_rejection(session, response))

        await self._async_refresh()

    async def _send_write(self, session: Any, word: int) -> Any:
        """Send the write -- as a whole time slot, where the register belongs to one.

        Growatt firmware validates a charge or discharge window as a unit. Enabling one
        whose boundaries are still 00:00-00:00 is refused, and setting the boundaries one
        register at a time passes through a moment where the start has moved and the stop
        has not, which is an inverted window some firmware refuses and some acts on.
        Sending the three registers as one 0x10 range never presents a half-changed slot.
        """
        single = commands.write_inverter(
            session.datalogger_serial, session.protocol, self.spec.register, word
        )
        segment = segment_for(self.spec.register)
        if segment is None:
            return await session.send_command(single)

        values = await self._read_segment(session, segment)
        if values is None:
            # Without the slot's current contents there is nothing to send alongside this
            # value, and inventing the other two would overwrite a window someone set in
            # the Growatt app. Changing one register is the smaller risk.
            return await session.send_command(single)

        values[segment.index(self.spec.register)] = word
        response = await session.send_command(
            commands.write_inverter_range(
                session.datalogger_serial, session.protocol, segment[0], values
            )
        )
        if response.result == 1:
            # "Unsupported operation" -- this firmware has no 0x10. Nothing was written,
            # so falling back to the single-register write cannot apply anything twice.
            _LOGGER.debug(
                "%s does not implement range writes; writing %s on its own",
                self.device.serial,
                self.spec.key,
            )
            return await session.send_command(single)
        return response

    async def _read_segment(self, session: Any, segment: tuple[int, ...]) -> list[int] | None:
        """The slot's current words, or None if the inverter did not return all of them."""
        try:
            response = await session.send_command(
                commands.read_inverter(
                    session.datalogger_serial, session.protocol, segment[0], segment[-1]
                )
            )
        except (CommandTimeout, ConnectionError) as err:
            _LOGGER.debug("could not read the time slot at %s: %s", segment[0], err)
            return None

        if len(response.values) != len(segment):
            _LOGGER.debug(
                "%s answered a read of %s..%s with %s words, not %s",
                self.device.serial,
                segment[0],
                segment[-1],
                len(response.values),
                len(segment),
            )
            return None
        return list(response.values)

    async def _explain_rejection(self, session: Any, response: Any) -> str:
        """Say what a rejection means, having asked the inverter about the register.

        A bare status byte leaves someone with nowhere to go -- issue #2 was filed
        against "result 2" for exactly that reason. One extra read separates the two
        cases a user would act on differently: a register this model does not have, and
        one it has but would not accept this change to. It delays an error that has
        already happened, which is the cheapest thing there is to delay.
        """
        message = (
            f"The inverter rejected {self.spec.key} (holding register "
            f"{self.spec.register}): {commands.describe_result(response.result)}"
        )
        try:
            probe = await session.send_command(
                commands.read_inverter(
                    session.datalogger_serial, session.protocol, self.spec.register
                )
            )
        except (CommandTimeout, ConnectionError):
            return f"{message}."

        if probe.empty or probe.value is None:
            return (
                f"{message}. It does not answer a read of that register either, so this "
                "model does not have it and retrying will not help."
            )
        return (
            f"{message}. The register itself reads back as {probe.value}, so the "
            "inverter does have it and refused this particular change."
        )

    def _session(self) -> Any:
        parent = self.device.parent
        if parent is None:
            return None
        return self.hub.session_for(self.hub.devices[parent].serial)
