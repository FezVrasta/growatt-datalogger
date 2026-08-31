"""Select platform: writable enumerated registers such as charge priority."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
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
    async_setup_write_platform(hass, entry, async_add_entities, WriteKind.SELECT, GrowattSelect)


class GrowattSelect(GrowattWriteEntity, SelectEntity):
    def __init__(self, hub: GrowattHub, device: GrowattDevice, spec: WritableRegister) -> None:
        super().__init__(hub, device, spec)
        self._attr_options = [label for label, _ in spec.options]

    @property
    def current_option(self) -> str | None:
        # None when the device reported a word outside the known options, rather than
        # guessing at a label.
        return self._current

    async def async_select_option(self, option: str) -> None:
        await self._async_write(option)
