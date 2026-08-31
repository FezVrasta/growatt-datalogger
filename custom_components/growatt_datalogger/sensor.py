"""Sensor platform.

Entities are created as values appear rather than up front, because a datalogger's field
set is not known until it reports. The dispatcher signal is scoped by entry id: a global
name would cross-talk between two config entries.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import RestoreSensor
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import GrowattConfigEntry
from .const import (
    SIGNAL_NEW_DEVICE,
    VALUE_CRC_MISMATCHES,
    VALUE_DECODE_ERRORS,
    VALUE_RECORDS,
)
from .entity import GrowattEntity
from .metadata import UNKNOWN_REGISTER_META, ValueMeta, describe, pretty

#: Names handled by another platform, so the sensor platform must skip them.
_NOT_SENSORS: frozenset[str] = frozenset()

#: Counters the hub maintains, which must not be restored from a previous run -- they
#: count this process's records, so a restored value would jump backwards and confuse
#: the statistics engine.
_VOLATILE = frozenset({VALUE_RECORDS, VALUE_DECODE_ERRORS, VALUE_CRC_MISMATCHES})


async def async_setup_entry(
    hass: HomeAssistant,
    entry: GrowattConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    hub = entry.runtime_data

    @callback
    def _add(device_key: str, names: list[str]) -> None:
        device = hub.devices.get(device_key)
        if device is None:
            return
        async_add_entities(
            GrowattSensor(hub, device, name) for name in names if name not in _NOT_SENSORS
        )

    entry.async_on_unload(
        async_dispatcher_connect(hass, SIGNAL_NEW_DEVICE.format(entry_id=entry.entry_id), _add)
    )
    # Covers devices restored from storage and any record that arrived while the
    # platform was still setting up.
    hub.async_replay(_add)


class GrowattSensor(GrowattEntity, RestoreSensor):
    """One decoded value."""

    def __init__(self, hub: Any, device: Any, value: str) -> None:
        super().__init__(hub, device, value)

        meta = describe(value)
        if meta is None:
            # An unnamed register, exposed only when the user asked for unknowns.
            # Presenting a bare integer as a normal sensor would be misleading, so it
            # is a disabled diagnostic.
            meta = UNKNOWN_REGISTER_META
            self._attr_name = value.replace("_", " ")
        else:
            self._attr_name = pretty(value)

        self._meta: ValueMeta = meta
        self._attr_device_class = meta.device_class
        self._attr_state_class = meta.state_class
        self._attr_native_unit_of_measurement = meta.unit
        self._attr_icon = meta.icon
        self._attr_suggested_display_precision = meta.precision
        self._attr_entity_category = meta.category
        self._attr_entity_registry_enabled_default = meta.enabled_default

    async def async_added_to_hass(self) -> None:
        """Restore the previous value so a restart is invisible.

        Paired with the hub restoring devices from storage, this means an overnight
        restart leaves dashboards intact instead of blank until sunrise.
        """
        await super().async_added_to_hass()

        if self._value is not None or self.value_name in _VOLATILE:
            return
        if (last := await self.async_get_last_sensor_data()) is not None:
            self._attr_native_value = last.native_value

    @property
    def native_value(self) -> Any:
        value = self._value
        if value is None:
            # Fall through to whatever was restored, rather than reporting unknown.
            return self._attr_native_value
        return value
