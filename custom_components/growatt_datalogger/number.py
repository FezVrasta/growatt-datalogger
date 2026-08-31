"""Number platform: writable setpoints such as power limits and SOC thresholds."""

from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import GrowattConfigEntry
from .hub import GrowattDevice, GrowattHub
from .registers.writable import WritableRegister, WriteKind
from .write_entity import GrowattWriteEntity, async_setup_write_platform


async def async_setup_entry(
    hass: HomeAssistant,
    entry: GrowattConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_setup_write_platform(hass, entry, async_add_entities, WriteKind.NUMBER, GrowattNumber)


class GrowattNumber(GrowattWriteEntity, NumberEntity):
    _attr_mode = NumberMode.BOX

    def __init__(self, hub: GrowattHub, device: GrowattDevice, spec: WritableRegister) -> None:
        super().__init__(hub, device, spec)
        self._attr_native_min_value = spec.minimum
        self._attr_native_max_value = spec.maximum
        self._attr_native_step = spec.step
        self._attr_native_unit_of_measurement = spec.unit

    @property
    def native_value(self) -> float | None:
        return self._current

    async def async_set_native_value(self, value: float) -> None:
        await self._async_write(value)
