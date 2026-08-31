"""Constants for the Growatt Datalogger integration."""

from __future__ import annotations

from typing import Final

from homeassistant.const import Platform

DOMAIN: Final = "growatt_datalogger"

PLATFORMS: Final = [Platform.BINARY_SENSOR, Platform.SENSOR]

# Configuration ---------------------------------------------------------------

CONF_PROFILE_OVERRIDES: Final = "profile_overrides"
"""Per-inverter-serial profile pins, ``{serial: profile_key}``.

The only way to select the off-grid profile, whose register meanings a record cannot
distinguish from Protocol II.
"""

CONF_INCLUDE_UNKNOWN: Final = "include_unknown"
"""Expose registers with no known meaning as disabled diagnostic entities."""

CONF_BUFFERED_POLICY: Final = "buffered_policy"

DEFAULT_PORT: Final = 5279
DEFAULT_INCLUDE_UNKNOWN: Final = False

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
