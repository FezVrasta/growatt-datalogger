"""Connectivity binary sensors.

The only entities whose availability really is binary. Sensor values deliberately persist
overnight -- see :class:`~.entity.GrowattEntity.available` -- so this is how a user tells
"the inverter is asleep" from "the datalogger is gone".

Which makes it worth being precise about what "connected" means. A datalogger does not
hold a session open: it dials in, uploads, hangs up, and repeats. Reporting the socket
state would therefore flap dozens of times an hour on a device that is working perfectly,
and anything built on it -- an offline alert, a dashboard badge -- would be useless. So
connectivity is silence-based: a device is online while it is still delivering records,
and goes offline only after a gap long enough that no healthy datalogger would produce it.
"""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.util import dt as dt_util

from . import GrowattConfigEntry
from .const import (
    CONNECTIVITY_GRACE,
    CONNECTIVITY_INTERVAL,
    KIND_DATALOGGER,
    SIGNAL_NEW_DEVICE,
)
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
    """Whether the datalogger is still delivering records."""

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_name = "Connected"

    def __init__(self, hub: GrowattHub, device: GrowattDevice) -> None:
        super().__init__(hub, device, CONNECTED)

    @property
    def is_on(self) -> bool:
        # Deliberately ignores ``device.connected``. Letting an open socket force "on"
        # would mask the one fault that most deserves an alert: a datalogger holding a
        # connection while delivering nothing. Silence is the signal, in both directions.
        if (last := self.device.last_record) is None:
            return False
        return dt_util.utcnow() - last < CONNECTIVITY_GRACE

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        # Nothing pushes when a datalogger simply stops talking, so the grace period
        # has to be checked on a timer or the sensor would never fall to "off".
        self.async_on_remove(
            async_track_time_interval(self.hass, self._async_recheck, CONNECTIVITY_INTERVAL)
        )

    @callback
    def _async_recheck(self, _now: object) -> None:
        self.async_write_ha_state()
