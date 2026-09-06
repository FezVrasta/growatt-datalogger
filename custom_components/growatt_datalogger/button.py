"""Button platform: one-shot actions on the datalogger."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import GrowattConfigEntry
from .const import KIND_DATALOGGER
from .entity import GrowattEntity, async_setup_device_platform
from .hub import GrowattDevice, GrowattHub
from .services import async_sync_clock

SYNC_TIME = "sync_time"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: GrowattConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_setup_device_platform(hass, entry, async_add_entities, KIND_DATALOGGER, GrowattSyncTime)


class GrowattSyncTime(GrowattEntity, ButtonEntity):
    """Set the datalogger clock to Home Assistant's local time.

    Local rather than UTC: the timestamps the device puts in its own records are local,
    so setting the clock to UTC would silently shift every one of them.
    """

    _attr_entity_category = EntityCategory.CONFIG
    _attr_name = "Sync time"
    _attr_icon = "mdi:clock-check-outline"

    def __init__(self, hub: GrowattHub, device: GrowattDevice) -> None:
        super().__init__(hub, device, SYNC_TIME)

    async def async_press(self) -> None:
        session = self.hub.session_for(self.device.serial)
        if session is None:
            raise HomeAssistantError(f"{self.device.serial} is not connected")
        await async_sync_clock(session, self.device.serial)
