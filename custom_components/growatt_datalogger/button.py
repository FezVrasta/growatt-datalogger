"""Button platform: one-shot actions on the datalogger."""

from __future__ import annotations

from growatt_protocol import CommandTimeout, commands
from homeassistant.components.button import ButtonEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from . import GrowattConfigEntry
from .const import KIND_DATALOGGER, SIGNAL_NEW_DEVICE
from .entity import GrowattEntity
from .hub import GrowattDevice, GrowattHub

SYNC_TIME = "sync_time"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: GrowattConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    hub = entry.runtime_data
    created: set[str] = set()

    @callback
    def _add(device_key: str, _names: list[str]) -> None:
        device = hub.devices.get(device_key)
        if device is None or device.kind != KIND_DATALOGGER or device_key in created:
            return
        created.add(device_key)
        async_add_entities([GrowattSyncTime(hub, device)])

    entry.async_on_unload(
        async_dispatcher_connect(hass, SIGNAL_NEW_DEVICE.format(entry_id=entry.entry_id), _add)
    )
    hub.async_replay(_add)


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

        try:
            response = await session.send_command(
                commands.set_time(self.device.serial, session.protocol, dt_util.now())
            )
        except (CommandTimeout, ConnectionError) as err:
            raise HomeAssistantError(str(err)) from err

        if not response.ok:
            raise HomeAssistantError(
                f"The datalogger rejected the clock update (result {response.result})"
            )
