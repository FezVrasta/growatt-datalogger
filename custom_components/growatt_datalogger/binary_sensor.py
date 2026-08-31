"""Connectivity binary sensors.

The only entities whose availability really is binary. Sensor values deliberately persist
overnight -- see :class:`~.entity.GrowattEntity.available` -- so this is how a user tells
"the inverter is asleep" from "the datalogger is gone".
"""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import GrowattConfigEntry
from .const import KIND_DATALOGGER, SIGNAL_NEW_DEVICE
from .entity import GrowattEntity
from .hub import GrowattDevice, GrowattHub

CONNECTED = "connected"


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
        if device is None or device.kind != KIND_DATALOGGER:
            return
        if device_key in created:
            return
        created.add(device_key)
        async_add_entities([GrowattConnectivity(hub, device)])

    entry.async_on_unload(
        async_dispatcher_connect(hass, SIGNAL_NEW_DEVICE.format(entry_id=entry.entry_id), _add)
    )
    hub.async_replay(_add)


class GrowattConnectivity(GrowattEntity, BinarySensorEntity):
    """Whether the datalogger currently holds a TCP connection to us."""

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_name = "Connected"

    def __init__(self, hub: GrowattHub, device: GrowattDevice) -> None:
        super().__init__(hub, device, CONNECTED)

    @property
    def is_on(self) -> bool:
        return self.device.connected
