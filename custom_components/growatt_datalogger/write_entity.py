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
validates a whole slot, so all three of its registers go out together. How that is done
lives in :mod:`growatt_protocol.settings`, next to
:class:`~growatt_protocol.registers.writable.TimeSlot`, rather than here -- writing a
register safely is not a property of being a Home Assistant entity.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

from growatt_protocol import CommandTimeout, commands, settings
from growatt_protocol.registers.writable import (
    Encoding,
    WritableRegister,
    WriteKind,
    for_profile,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import KIND_INVERTER
from .entity import GrowattEntity, async_on_new_device
from .hub import GrowattDevice, GrowattHub
from .metadata import pretty

_LOGGER = logging.getLogger(__name__)


class HoldingReader:
    """One coalesced read of a device's writable holding registers.

    Every write entity used to issue its own single-register read on the first record
    after startup. On a storage inverter that is 26 commands, and a session serialises
    them behind one lock a fixed interval apart -- close to four seconds of enforced
    pauses on top of 26 round trips to a device fronting a serial Modbus bus, all of it
    ahead of the first thing a user actually asks for.

    Nothing about that had to be sequential: which registers are wanted is known from the
    device's profile before any of them is asked for. So they are read together, in as
    few ranges as the gaps allow, and each entity takes its own word out of the result.

    One shot, like the reads it replaces: a device that will not answer leaves its
    entities unknown rather than being retried forever. The lock is what makes it one --
    every entity arrives at the same moment and the first one through does the work.
    """

    def __init__(self, registers: frozenset[int]) -> None:
        self.registers = registers
        self._values: dict[int, int] = {}
        self._read = False
        self._lock = asyncio.Lock()

    async def word(self, session: Any, register: int) -> int | None:
        """``register``'s current word, reading the whole set on the first call."""
        async with self._lock:
            if not self._read:
                self._read = True
                self._values = await settings.read_registers(session, self.registers)
                _LOGGER.debug(
                    "read %s of %s writable registers in %s commands",
                    len(self._values),
                    len(self.registers),
                    len(settings.read_ranges(self.registers)),
                )
        return self._values.get(register)


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
        # Every platform runs this; the first one to reach a device sets up the shared
        # batch its entities will all read from.
        hub.holding_reads.setdefault(
            device_key, HoldingReader(frozenset(spec.register for spec in specs))
        )

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
                self._async_first_read(),
                name=f"growatt read {self.device.serial} {self.spec.key}",
            )
        super()._handle_coordinator_update()

    async def _async_first_read(self) -> None:
        """Take this register's value from the device's one batched read."""
        session = self._session()
        reader = self.hub.holding_reads.get(self.device.key)
        if session is None or reader is None:
            return

        word = await reader.word(session, self.spec.register)
        if word is None:
            # The device does not implement this register. Better an unknown value than
            # a plausible-looking wrong one.
            _LOGGER.debug(
                "%s does not implement register %s", self.device.serial, self.spec.register
            )
            return

        self._current = self.spec.decode(word)
        self.async_write_ha_state()

    async def _async_refresh(self) -> None:
        """Read this one register back, to confirm what a write actually did.

        Deliberately not the batch: after a write the caller is waiting, and one
        register is what changed.
        """
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

        await self._async_refresh()

    def _session(self) -> Any:
        parent = self.device.parent
        if parent is None:
            return None
        return self.hub.session_for(self.hub.devices[parent].serial)
