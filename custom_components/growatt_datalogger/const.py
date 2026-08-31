"""Constants for the Growatt Datalogger integration."""

from __future__ import annotations

from datetime import timedelta
from typing import Final

from homeassistant.const import Platform

DOMAIN: Final = "growatt_datalogger"

PLATFORMS: Final = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
    Platform.TIME,
]

# Configuration ---------------------------------------------------------------

CONF_PROFILE_OVERRIDES: Final = "profile_overrides"
"""Per-inverter-serial profile pins, ``{serial: profile_key}``.

The only way to select the off-grid profile, whose register meanings a record cannot
distinguish from Protocol II.
"""

CONF_INCLUDE_UNKNOWN: Final = "include_unknown"
"""Expose registers with no known meaning as disabled diagnostic entities."""

CONF_BUFFERED_POLICY: Final = "buffered_policy"

CONF_RELAY_ENABLED: Final = "relay_enabled"
"""Mirror every connection to the Growatt cloud so ShinePhone keeps working.

Off by default. The premise of this integration is that nothing has to leave the
network; this exists for people who want the app as well.
"""

CONF_RELAY_HOST: Final = "relay_host"
CONF_RELAY_PORT: Final = "relay_port"

DEFAULT_PORT: Final = 5279
DEFAULT_INCLUDE_UNKNOWN: Final = False
DEFAULT_RELAY_ENABLED: Final = False
DEFAULT_RELAY_HOST: Final = "server.growatt.com"
DEFAULT_RELAY_PORT: Final = 5279

# Buffered-record handling ----------------------------------------------------

BUFFERED_IGNORE: Final = "ignore"
BUFFERED_EVENT: Final = "event"
DEFAULT_BUFFERED_POLICY: Final = BUFFERED_EVENT

EVENT_BUFFERED_RECORD: Final = f"{DOMAIN}_buffered_record"

# Dispatcher signals ----------------------------------------------------------
# Scoped by entry id. A globally-named signal would cross-talk between two config
# entries, which is a real bug in at least one integration that does it this way.

SIGNAL_NEW_DEVICE: Final = f"{DOMAIN}_new_device_{{entry_id}}"
SIGNAL_NEW_VALUES: Final = f"{DOMAIN}_new_values_{{entry_id}}_{{device_key}}"

# Device keys -----------------------------------------------------------------

KIND_DATALOGGER: Final = "logger"
KIND_INVERTER: Final = "inverter"

# Connectivity ----------------------------------------------------------------

#: How long a datalogger may go without delivering a record before it counts as
#: offline. Generous on purpose: a datalogger holds no long-lived TCP session. It
#: uploads, hangs up and redials every few minutes, so the socket being down says
#: nothing about whether the device is healthy -- observed hardware drops it dozens
#: of times an hour while delivering a record every nine seconds throughout. Only a
#: silence far longer than that gap is evidence of an actual outage.
CONNECTIVITY_GRACE: Final = timedelta(minutes=15)

#: The connectivity sensor's state is a function of elapsed time, so something has to
#: re-evaluate it when no record arrives to do so -- otherwise a device that vanishes
#: stays "on" forever, holding the state it had when the last record came in.
CONNECTIVITY_INTERVAL: Final = timedelta(seconds=60)

# Storage ---------------------------------------------------------------------

STORAGE_KEY: Final = f"{DOMAIN}.devices"
STORAGE_VERSION: Final = 1
STORAGE_SAVE_DELAY: Final = 30.0

# Diagnostic value names produced by the integration rather than by a register.

VALUE_LAST_RECORD: Final = "last_record"
VALUE_RECORDS: Final = "records_received"
VALUE_DECODE_ERRORS: Final = "decode_errors"
VALUE_CRC_MISMATCHES: Final = "crc_mismatches"
VALUE_BUFFERED_RECORDS: Final = "buffered_records"
VALUE_PROFILE: Final = "profile"
VALUE_RELAY_CONNECTED: Final = "cloud_relay"
