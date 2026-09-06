"""Shared entity base, and the two ways a platform gets told about a device."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, KIND_DATALOGGER, SIGNAL_NEW_DEVICE
from .hub import GrowattCoordinator, GrowattDevice, GrowattHub


def async_on_new_device(
    hass: HomeAssistant, entry: Any, handler: Callable[[str, list[str]], None]
) -> None:
    """Call ``handler`` for every device this entry knows, now and as more appear.

    Both halves matter and forgetting either is a silent bug: the dispatcher covers
    devices that announce themselves later, and the replay covers the ones that were
    restored from storage before this platform was set up. The signal is scoped by entry
    id, since a globally named one would cross-talk between entries.
    """
    entry.async_on_unload(
        async_dispatcher_connect(hass, SIGNAL_NEW_DEVICE.format(entry_id=entry.entry_id), handler)
    )
    entry.runtime_data.async_replay(handler)


def async_setup_device_platform(
    hass: HomeAssistant,
    entry: Any,
    async_add_entities: AddConfigEntryEntitiesCallback,
    kind: str,
    factory: Callable[[GrowattHub, GrowattDevice], GrowattEntity],
) -> None:
    """Create one entity per device of ``kind``, as devices are discovered.

    The dedupe set is the point: a device is announced on every record, and without it
    the platform would add the same entity again on each one.
    """
    hub: GrowattHub = entry.runtime_data
    created: set[str] = set()

    @callback
    def _add(device_key: str, _names: list[str]) -> None:
        device = hub.devices.get(device_key)
        if device is None or device.kind != kind or device_key in created:
            return
        created.add(device_key)
        async_add_entities([factory(hub, device)])

    async_on_new_device(hass, entry, _add)


class GrowattEntity(CoordinatorEntity[GrowattCoordinator]):
    """Base for every entity this integration creates."""

    _attr_has_entity_name = True

    def __init__(self, hub: GrowattHub, device: GrowattDevice, value: str) -> None:
        super().__init__(hub.coordinators[device.key])
        self.hub = hub
        self.device = device
        self.value_name = value

        # Deliberately excludes the profile. Correcting a device's profile must not
        # orphan every entity and recreate them with a "_2" suffix.
        self._attr_unique_id = f"{DOMAIN}_{device.key}_{value}"

    @property
    def device_info(self) -> DeviceInfo:
        info = DeviceInfo(
            identifiers={(DOMAIN, self.device.key)},
            manufacturer="Growatt",
            name=self.device.name,
            serial_number=self.device.serial,
        )
        if self.device.kind == KIND_DATALOGGER:
            info["model"] = "Datalogger"
        else:
            info["model"] = self.device.profile or "Inverter"
            if self.device.parent:
                # Gives the correct topology: the inverter hangs off the datalogger, so
                # losing the logger greys out the whole branch.
                info.update(self._via_parent())
        return info

    def _via_parent(self) -> dict[str, Any]:
        """Point this device at its datalogger, however the running version wants it.

        ``via_device`` is deprecated and removed in Home Assistant 2027.8; the
        ``via_device_id`` that replaces it, and the registry lookup that resolves one,
        did not exist before 2026.8. This integration is installed through HACS onto
        whatever version someone happens to be running, so it speaks both for as long as
        both exist -- and detects which by asking the registry rather than by comparing
        version numbers, since the capability is the thing that matters.
        """
        identifier = (DOMAIN, self.device.parent)
        registry = dr.async_get(self.hub.hass)
        by_identifier = getattr(registry, "async_get_device_by_identifier", None)
        if by_identifier is None:
            return {"via_device": identifier}

        # A parent that is somehow not registered yet degrades to a flat topology. The
        # hub publishes the datalogger's entities before the inverter's precisely so it
        # is there by now, but that is a better outcome than an entity refusing to exist.
        parent = by_identifier(identifier, self.hub.entry.entry_id)
        return {"via_device_id": parent.id} if parent is not None else {}

    @property
    def available(self) -> bool:
        """Always available while the integration is loaded.

        Deliberately not the usual coordinator behaviour. Solar inverters stop reporting
        every night and off-grid units sleep; marking their entities unavailable would
        make every dashboard and history graph gap daily. Freshness is exposed as data
        instead -- see the ``last_record`` timestamp and the connectivity binary sensor --
        so a user who wants to alert on staleness can, without everyone else losing
        their overnight values.
        """
        return True

    @property
    def _value(self) -> object | None:
        return (self.coordinator.data or {}).get(self.value_name)
