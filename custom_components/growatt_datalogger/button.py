"""Button platform: one-shot actions on a datalogger and on an inverter."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import GrowattConfigEntry
from .const import KIND_DATALOGGER, KIND_INVERTER
from .entity import GrowattEntity, async_setup_device_platform
from .hub import GrowattDevice, GrowattHub
from .services import async_sync_clock

SYNC_TIME = "sync_time"
REFRESH_SETTINGS = "refresh_settings"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: GrowattConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_setup_device_platform(hass, entry, async_add_entities, KIND_DATALOGGER, GrowattSyncTime)
    async_setup_device_platform(
        hass, entry, async_add_entities, KIND_INVERTER, GrowattRefreshSettings
    )


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


class GrowattRefreshSettings(GrowattEntity, ButtonEntity):
    """Re-read this inverter's settings registers now.

    The same read the hub runs on a timer, on demand. Worth a button of its own because
    the workaround it replaces is reloading the integration -- which people found for
    themselves, and which restarts a TCP server and rebuilds every entity to achieve what
    four Modbus reads do.
    """

    _attr_entity_category = EntityCategory.CONFIG
    _attr_name = "Refresh settings"
    _attr_icon = "mdi:refresh"

    def __init__(self, hub: GrowattHub, device: GrowattDevice) -> None:
        super().__init__(hub, device, REFRESH_SETTINGS)

    async def async_press(self) -> None:
        session = self.hub.session_for_device(self.device)
        if session is None:
            raise HomeAssistantError(f"Datalogger for {self.device.serial} is not connected")
        await self.hub.async_refresh_settings_now(self.device, session)
