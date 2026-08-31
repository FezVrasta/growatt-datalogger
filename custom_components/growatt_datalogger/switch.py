"""Switch platform: writable on/off registers."""

from __future__ import annotations

from typing import Any

from growatt_protocol.registers.writable import WriteKind
from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import GrowattConfigEntry
from .write_entity import GrowattWriteEntity, async_setup_write_platform


async def async_setup_entry(
    hass: HomeAssistant,
    entry: GrowattConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_setup_write_platform(hass, entry, async_add_entities, WriteKind.SWITCH, GrowattSwitch)


class GrowattSwitch(GrowattWriteEntity, SwitchEntity):
    @property
    def is_on(self) -> bool | None:
        return self._state

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._async_write(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._async_write(False)
