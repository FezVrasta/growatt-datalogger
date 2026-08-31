"""Time platform: charge and discharge window boundaries.

Growatt encodes these as one register with the hour in the high byte and the minute in
the low byte, so seconds are always zero.
"""

from __future__ import annotations

from datetime import time as dt_time

from growatt_protocol.registers.writable import WriteKind
from homeassistant.components.time import TimeEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import GrowattConfigEntry
from .write_entity import GrowattWriteEntity, async_setup_write_platform


async def async_setup_entry(
    hass: HomeAssistant,
    entry: GrowattConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_setup_write_platform(hass, entry, async_add_entities, WriteKind.TIME, GrowattTime)


class GrowattTime(GrowattWriteEntity, TimeEntity):
    @property
    def native_value(self) -> dt_time | None:
        if not isinstance(self._state, str):
            return None
        hour, minute, _ = self._state.split(":")
        return dt_time(int(hour), int(minute))

    async def async_set_value(self, value: dt_time) -> None:
        await self._async_write(value.strftime("%H:%M:%S"))
