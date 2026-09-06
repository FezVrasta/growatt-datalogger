"""Diagnostics dump.

The unnamed-register list is the useful part: it is exactly what someone needs to attach
to an issue for a family this project does not yet decode.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from . import GrowattConfigEntry

#: Serial numbers identify a specific person's hardware, so they are redacted by default.
TO_REDACT = {"serial", "serial_number", "datalogger", "inverter", "key", "parent"}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: GrowattConfigEntry
) -> dict[str, Any]:
    hub = entry.runtime_data

    devices = [
        {
            "key": device.key,
            "kind": device.kind,
            "serial": device.serial,
            "parent": device.parent,
            "profile": device.profile,
            "profile_confident": device.profile_confident,
            "connected": device.connected,
            "fields": sorted(device.fields),
        }
        for device in hub.devices.values()
    ]

    sessions = [
        {
            "connection_id": session.connection_id,
            "protocol": session.protocol,
            # The clock is what a datalogger waits for before it will send anything, so
            # "announced but no data" and "clock never set" are the same report. The
            # session has always known; nothing asked it until now.
            "time_synced": session.time_synced,
            # The one failure that produces no devices at all, so nothing else in this
            # dump would hint at it. See issue #3.
            "key_exchange": session.key_exchange,
            "encrypted": session.encrypted,
            "inverter": session.inverter_serial,
            "records": session.stats.records,
            "acknowledged": session.stats.acknowledged,
            "pings": session.stats.pings,
            "decode_errors": session.stats.decode_errors,
            "encrypted_records": session.stats.encrypted_records,
            "crc_mismatches": session.stats.crc_mismatches,
            "unknown_functions": {
                f"{code:#04x}": count for code, count in session.stats.unknown_functions.items()
            },
            # Replies nothing here was waiting for. On a connection of our own that
            # means a command timed out and its answer turned up late. With the cloud
            # relay on it means something else entirely: Growatt is issuing its own
            # commands down the same socket, and ours are interleaved with them. Which
            # of those is happening changes the diagnosis completely, and nothing else
            # in this dump distinguishes them.
            "unsolicited": [
                {
                    "function": f"{reply.function:#04x}",
                    "register": reply.register,
                    "result": reply.result,
                    "empty": reply.empty,
                }
                for reply in session.unsolicited
            ],
        }
        for session in hub.sessions
    ]

    # What each device's entities actually read from. Without this, a sensor stuck on
    # "unknown" is indistinguishable from one whose value simply is not being published.
    coordinator_data = {
        key: {name: value for name, value in (coordinator.data or {}).items()}
        for key, coordinator in hub.coordinators.items()
    }

    return async_redact_data(
        {
            "port": hub.port,
            "coordinator_data": coordinator_data,
            "options": dict(entry.options),
            "devices": devices,
            "sessions": sessions,
        },
        TO_REDACT,
    )
