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
the sort of thing someone builds an automation on.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from growatt_protocol import CommandTimeout, commands
from growatt_protocol.registers.writable import Encoding, WritableRegister, WriteKind, for_profile
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
            response = await session.send_command(
                commands.write_inverter(
                    session.datalogger_serial, session.protocol, self.spec.register, word
                )
            )
        except (CommandTimeout, ConnectionError) as err:
            # Deliberately not retried: repeating a write could apply a change twice.
            raise HomeAssistantError(
                f"{self.spec.key} was not confirmed: {err}. Reload or read the register "
                "back to see whether it took effect."
            ) from err

        if not response.ok:
            raise HomeAssistantError(
                f"The inverter rejected {self.spec.key} with result {response.result}"
            )

        await self._async_refresh()

    def _session(self) -> Any:
        parent = self.device.parent
        if parent is None:
            return None
        return self.hub.session_for(self.hub.devices[parent].serial)
