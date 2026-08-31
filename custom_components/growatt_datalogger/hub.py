"""Ties the protocol server to Home Assistant's device and entity model.

Dataloggers announce themselves; there is no list to configure up front. So devices and
entities are created as records arrive, and persisted so that a restart at night does not
empty every solar dashboard until sunrise.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from growatt_protocol import GrowattServer, Record, RelayConfig, ServerConfig, Session
from growatt_protocol.registers import RegisterSpace, decode_registers, resolve_profile
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .const import (
    BUFFERED_IGNORE,
    CONF_BUFFERED_POLICY,
    CONF_INCLUDE_UNKNOWN,
    CONF_PROFILE_OVERRIDES,
    CONF_RELAY_ENABLED,
    CONF_RELAY_HOST,
    CONF_RELAY_PORT,
    DEFAULT_BUFFERED_POLICY,
    DEFAULT_INCLUDE_UNKNOWN,
    DEFAULT_RELAY_HOST,
    DEFAULT_RELAY_PORT,
    DOMAIN,
    EVENT_BUFFERED_RECORD,
    KIND_DATALOGGER,
    KIND_INVERTER,
    SIGNAL_NEW_DEVICE,
    STORAGE_KEY,
    STORAGE_SAVE_DELAY,
    STORAGE_VERSION,
    VALUE_BUFFERED_RECORDS,
    VALUE_CRC_MISMATCHES,
    VALUE_DECODE_ERRORS,
    VALUE_LAST_RECORD,
    VALUE_PROFILE,
    VALUE_RECORDS,
)

_LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class GrowattDevice:
    """One device -- a datalogger or an inverter -- and the values it reports."""

    key: str
    kind: str
    serial: str
    parent: str | None = None
    profile: str | None = None
    profile_confident: bool = True
    fields: set[str] = field(default_factory=set)
    #: Whether a TCP session is open right now. Diagnostic only -- this flaps
    #: constantly on real hardware, so it is not the connectivity signal.
    connected: bool = False
    #: When this device last delivered a record. The basis of connectivity.
    last_record: datetime | None = None

    @property
    def name(self) -> str:
        label = "Datalogger" if self.kind == KIND_DATALOGGER else "Inverter"
        return f"Growatt {label} {self.serial}"


def device_key(kind: str, serial: str) -> str:
    return f"{kind}:{serial}"


class GrowattCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Push coordinator for one device.

    ``update_interval`` is left unset, so Home Assistant never polls: the socket handler
    calls :meth:`async_set_updated_data` when a record lands. Using a coordinator anyway
    -- rather than a bespoke dispatcher -- gets listener management, per-device isolation
    and the standard entity base class for free.
    """

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, device: GrowattDevice) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} {device.key}",
            always_update=False,
        )
        self.device = device
        self.data = {}


class GrowattHub:
    """Owns the server, the device registry contents, and the coordinators."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, port: int) -> None:
        self.hass = hass
        self.entry = entry
        self.devices: dict[str, GrowattDevice] = {}
        self.coordinators: dict[str, GrowattCoordinator] = {}
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, f"{STORAGE_KEY}.{entry.entry_id}"
        )
        options = {**entry.data, **entry.options}
        relay = (
            RelayConfig(
                host=options.get(CONF_RELAY_HOST, DEFAULT_RELAY_HOST),
                port=options.get(CONF_RELAY_PORT, DEFAULT_RELAY_PORT),
            )
            if options.get(CONF_RELAY_ENABLED)
            else None
        )
        self._server = GrowattServer(
            ServerConfig(port=port, relay=relay),
            on_record=self._handle_record,
            on_connection_change=self._handle_connection_change,
        )

    # ------------------------------------------------------------------
    # Options
    # ------------------------------------------------------------------

    @property
    def _options(self) -> dict[str, Any]:
        return {**self.entry.data, **self.entry.options}

    @property
    def include_unknown(self) -> bool:
        return bool(self._options.get(CONF_INCLUDE_UNKNOWN, DEFAULT_INCLUDE_UNKNOWN))

    @property
    def buffered_policy(self) -> str:
        return str(self._options.get(CONF_BUFFERED_POLICY, DEFAULT_BUFFERED_POLICY))

    def _profile_override(self, serial: str) -> str | None:
        overrides = self._options.get(CONF_PROFILE_OVERRIDES) or {}
        return overrides.get(serial)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    @property
    def port(self) -> int:
        return self._server.port

    @property
    def sessions(self) -> list[Session]:
        """Live connections, for diagnostics."""
        return list(self._server.sessions.values())

    def session_for(self, serial: str) -> Session | None:
        """The connection to talk to a datalogger on.

        A datalogger can hold more than one connection open at a time -- it opens a new
        one before the old has been reaped -- and the older one may be dead in the sense
        that matters here: it still accepts writes but nothing answers on it. Commands
        sent there simply time out. Pick the most recently active connection instead of
        whichever happens to come first.
        """
        candidates = [
            session
            for session in self._server.sessions.values()
            if session.datalogger_serial == serial and not session.closed
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda s: (s.last_seen is not None, s.last_seen))

    async def async_start(self) -> None:
        """Restore known devices, then bind. Raises OSError if the port is taken."""
        await self._async_restore()
        await self._server.start()

    async def async_stop(self) -> None:
        await self._server.stop()

    async def _async_restore(self) -> None:
        """Recreate devices seen previously, before any packet arrives.

        Without this, restarting after sunset leaves every entity missing until the
        inverter wakes up, which breaks dashboards and any automation that reads them.
        """
        stored = await self._store.async_load()
        if not stored:
            return

        for raw in stored.get("devices", []):
            device = GrowattDevice(
                key=raw["key"],
                kind=raw["kind"],
                serial=raw["serial"],
                parent=raw.get("parent"),
                profile=raw.get("profile"),
                profile_confident=raw.get("profile_confident", True),
                fields=set(raw.get("fields", [])),
            )
            self.devices[device.key] = device
            self.coordinators[device.key] = GrowattCoordinator(self.hass, self.entry, device)
        _LOGGER.debug("restored %s device(s) from storage", len(self.devices))

    @callback
    def _schedule_save(self) -> None:
        self._store.async_delay_save(self._save_data, STORAGE_SAVE_DELAY)

    async def async_flush_storage(self) -> None:
        """Write pending device state immediately rather than on the debounce.

        Saves are normally delayed, since the device and field set changes rarely and
        every record would otherwise touch the disk. This forces it out when the delay
        is not acceptable -- shutting down, or a test that reloads straight away.
        """
        await self._store.async_save(self._save_data())

    @callback
    def _save_data(self) -> dict[str, Any]:
        return {
            "devices": [
                {
                    "key": device.key,
                    "kind": device.kind,
                    "serial": device.serial,
                    "parent": device.parent,
                    "profile": device.profile,
                    "profile_confident": device.profile_confident,
                    "fields": sorted(device.fields),
                }
                for device in self.devices.values()
            ]
        }

    # ------------------------------------------------------------------
    # Records
    # ------------------------------------------------------------------

    @callback
    def _handle_record(self, record: Record) -> None:
        """Turn one decoded record into entity state. Runs on the event loop."""
        payload = record.payload
        logger_key = device_key(KIND_DATALOGGER, payload.datalogger_serial)
        logger_device = self._ensure_device(logger_key, KIND_DATALOGGER, payload.datalogger_serial)
        logger_device.connected = True
        # Any record we could decode -- telemetry, announce, even a buffered catch-up --
        # is proof the datalogger is alive and talking, so it counts towards liveness
        # regardless of what happens to the values below.
        logger_device.last_record = dt_util.utcnow()

        if record.buffered and self.buffered_policy == BUFFERED_IGNORE:
            return

        match = resolve_profile(
            record.ranges, override=self._profile_override(payload.inverter_serial)
        )
        space = RegisterSpace.HOLDING if record.frame.function == 0x03 else RegisterSpace.INPUT
        decoded = decode_registers(match.profile, payload.registers, space)

        if record.buffered:
            # These carry a past timestamp. Writing them to live state would invent
            # power spikes and corrupt long-term statistics, so they are surfaced as an
            # event for anyone who wants them and counted, but never merged with live
            # telemetry.
            self._bump(logger_key, VALUE_BUFFERED_RECORDS)
            self.hass.bus.async_fire(
                EVENT_BUFFERED_RECORD,
                {
                    "datalogger": payload.datalogger_serial,
                    "inverter": payload.inverter_serial,
                    "recorded_at": payload.timestamp.isoformat() if payload.timestamp else None,
                    "values": decoded.values,
                },
            )
            return

        values: dict[str, Any] = dict(decoded.values)
        if self.include_unknown:
            values.update({f"register_{number}": word for number, word in decoded.unknown.items()})
        values[VALUE_LAST_RECORD] = dt_util.utcnow()
        values[VALUE_PROFILE] = match.profile.key

        inverter_serial = payload.inverter_serial or payload.datalogger_serial
        inverter_key = device_key(KIND_INVERTER, inverter_serial)
        inverter = self._ensure_device(
            inverter_key, KIND_INVERTER, inverter_serial, parent=logger_key
        )

        _LOGGER.debug(
            "record fn=%#04x space=%s profile=%s: %d named values, %d unnamed registers",
            record.frame.function,
            space.value,
            match.profile.key,
            len(decoded.values),
            len(decoded.unknown),
        )

        # Only telemetry gets a vote on the profile. A profile says what the *input*
        # registers mean, and an announce carries holding registers whose ranges are not
        # comparable: on a string inverter the announce includes a 3125+ group, which
        # makes the record look like a hybrid's. Letting it vote flips the profile on
        # every announce, which churns the stored value and creates battery entities for
        # a device that has no battery.
        if space is RegisterSpace.INPUT and inverter.profile != match.profile.key:
            _LOGGER.info(
                "%s: using profile %s (%s)",
                inverter_serial,
                match.profile.key,
                match.reason,
            )
            inverter.profile = match.profile.key
            inverter.profile_confident = match.confident
            self._schedule_save()

        # Parent first. Publishing the inverter's values creates its entities, and
        # creating those registers the inverter device with a via_device pointing at
        # the datalogger -- which Home Assistant rejects if that device does not exist
        # yet. The datalogger's own stats entities are what bring it into being.
        self._publish_logger_stats(logger_key)
        self._publish(inverter_key, values)

    @callback
    def _handle_connection_change(self, session: Session, connected: bool) -> None:
        serial = session.datalogger_serial
        if serial is None:
            return
        key = device_key(KIND_DATALOGGER, serial)
        if (device := self.devices.get(key)) is not None:
            device.connected = connected
            self._publish_logger_stats(key)

    def _publish_logger_stats(self, key: str) -> None:
        totals = {
            VALUE_RECORDS: 0,
            VALUE_DECODE_ERRORS: 0,
            VALUE_CRC_MISMATCHES: 0,
        }
        for session in self._server.sessions.values():
            if session.datalogger_serial != self.devices[key].serial:
                continue
            totals[VALUE_RECORDS] += session.stats.records
            totals[VALUE_DECODE_ERRORS] += session.stats.decode_errors
            totals[VALUE_CRC_MISMATCHES] += session.stats.crc_mismatches

        # Deliberately the stored time rather than "now": this also runs when a session
        # merely opens or closes, and stamping those would report a fresh record for a
        # datalogger that had connected and then said nothing.
        stamp = self.devices[key].last_record
        extra = {VALUE_LAST_RECORD: stamp} if stamp is not None else {}
        self._publish(key, {**totals, **extra})

    def _bump(self, key: str, name: str) -> None:
        current = (self.coordinators[key].data or {}).get(name, 0)
        self._publish(key, {name: current + 1})

    # ------------------------------------------------------------------
    # Devices and entities
    # ------------------------------------------------------------------

    def _ensure_device(
        self, key: str, kind: str, serial: str, parent: str | None = None
    ) -> GrowattDevice:
        if (device := self.devices.get(key)) is not None:
            if parent and device.parent != parent:
                device.parent = parent
                self._schedule_save()
            return device

        device = GrowattDevice(key=key, kind=kind, serial=serial, parent=parent)
        self.devices[key] = device
        self.coordinators[key] = GrowattCoordinator(self.hass, self.entry, device)
        self._schedule_save()
        _LOGGER.info("discovered %s", device.name)
        return device

    def _publish(self, key: str, values: dict[str, Any]) -> None:
        """Merge values into a device's coordinator, announcing any new field names.

        Merged rather than replaced, because one device is fed from more than one kind
        of record and they carry different things. An announce carries holding
        registers -- firmware, serial number, power limit -- while a telemetry record
        carries input registers. Replacing the dataset each time means every announce
        wipes the telemetry and every telemetry record wipes the identity, leaving
        whichever arrived last readable and everything else 'unknown'.
        """
        device = self.devices[key]
        new_fields = set(values) - device.fields
        if new_fields:
            device.fields |= new_fields
            self._schedule_save()

        coordinator = self.coordinators[key]
        coordinator.async_set_updated_data({**(coordinator.data or {}), **values})

        if new_fields:
            # Announce after the data is in place so a newly created entity's first
            # read already has a value.
            async_dispatcher_send(
                self.hass,
                SIGNAL_NEW_DEVICE.format(entry_id=self.entry.entry_id),
                key,
                sorted(new_fields),
            )

    @callback
    def async_replay(self, add: Any) -> None:
        """Give a platform the devices and fields that already exist.

        Covers both restored-from-storage devices and the window between the hub
        starting and a platform finishing setup.
        """
        for key, device in self.devices.items():
            if device.fields:
                add(key, sorted(device.fields))

    def last_seen(self, key: str) -> datetime | None:
        data = self.coordinators[key].data or {}
        value = data.get(VALUE_LAST_RECORD)
        return value if isinstance(value, datetime) else None
