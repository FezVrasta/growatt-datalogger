"""Shared plumbing for the entities that write registers.

Write entities differ from sensors in an awkward way: their value is not in the telemetry
stream. A record carries input registers, while these live in the holding space, so the
only way to know the current setting is to ask. That means:

* the value is read once when the device first connects, and again after each write;
* until that read succeeds the entity reports ``unknown`` rather than inventing a value;
* a write that the device rejects does not update the state.

Optimistic updates would be worse than useless here -- showing a battery cut-off the
inverter never accepted is exactly the sort of thing someone builds an automation on.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import KIND_INVERTER, SIGNAL_NEW_DEVICE
from .entity import GrowattEntity
from .hub import GrowattDevice, GrowattHub
from .metadata import pretty
from .protocol import commands
from .protocol.errors import CommandTimeout
from .registers.writable import WritableRegister, WriteKind, for_profile

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

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        # Surfacing the provenance means a user can judge for themselves whether to
        # trust a register this project has flagged as unverified.
        return {
            "register": self.spec.register,
            "confidence": self.spec.confidence.value,
            "source": self.spec.source,
        }

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        # Deliberately not awaited. Commands are serialised per connection, so awaiting
        # the initial read here would make platform setup take one command round-trip
        # per entity -- and a full command timeout each, if the device is unreachable.
        # The entity simply reports unknown until its value arrives.
        self.hass.async_create_background_task(
            self._async_refresh(),
            name=f"growatt refresh {self.device.serial} {self.spec.key}",
        )

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
        serial = self.hub.devices[parent].serial
        for session in self.hub.sessions:
            if session.datalogger_serial == serial:
                return session
        return None
