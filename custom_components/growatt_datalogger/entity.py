"""Shared entity base."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, KIND_DATALOGGER
from .hub import GrowattCoordinator, GrowattDevice, GrowattHub


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
                info["via_device"] = (DOMAIN, self.device.parent)
        return info

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
