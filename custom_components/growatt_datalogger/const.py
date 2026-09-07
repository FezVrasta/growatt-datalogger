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

The only way to select the 0-based storage profile, whose register meanings a record
cannot distinguish from Protocol II. Set from the options flow's profile step.
"""

PROFILE_AUTO: Final = "auto"
"""Picker sentinel for "no pin". Never stored -- it is removed on save."""

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

CONF_SETTINGS_INTERVAL: Final = "settings_interval"
"""Minutes between re-reads of an inverter's settings registers. 0 turns it off.

A datalogger volunteers the holding space only when it connects, so without this the
settings shown in Home Assistant are whatever the last announce carried. Anything that
changes them elsewhere -- someone in ShinePhone, or firmware quietly discarding a change
it could not act on -- leaves the two disagreeing with nothing to correct them. See
:data:`SETTINGS_MISSES` for the cost, which is why this is a poll rather than a stream.
"""

DEFAULT_PORT: Final = 5279
DEFAULT_INCLUDE_UNKNOWN: Final = False
DEFAULT_RELAY_ENABLED: Final = False
DEFAULT_RELAY_HOST: Final = "server.growatt.com"
DEFAULT_RELAY_PORT: Final = 5279
DEFAULT_SETTINGS_INTERVAL: Final = 5

#: How many refreshes in a row a register may go unanswered before it is dropped from
#: them.
#:
#: A range read the device does not fully implement comes back empty, and the fallback is
#: to read every register in it singly -- so a family that lacks a whole block would pay
#: for the entire block, one command at a time, on every refresh forever. One or two
#: misses are the ordinary result of a datalogger hanging up mid-read, though, so a single
#: silence must not be read as "this model does not have it". Cleared whenever a register
#: does answer, and whenever the device reconnects and announces its holding space afresh.
SETTINGS_MISSES: Final = 3

# Buffered-record handling ----------------------------------------------------

BUFFERED_IGNORE: Final = "ignore"
BUFFERED_EVENT: Final = "event"
DEFAULT_BUFFERED_POLICY: Final = BUFFERED_EVENT

EVENT_BUFFERED_RECORD: Final = f"{DOMAIN}_buffered_record"

# Repairs ---------------------------------------------------------------------

ISSUE_UNCONFIDENT_PROFILE: Final = "unconfident_profile_{serial}"
#: Not keyed by serial: an encrypted connection never gets as far as saying
#: which datalogger it is.
ISSUE_ENCRYPTED_SESSION: Final = "encrypted_session"
"""Raised when a record's register layout matches no family we know.

The values still decode, so nothing looks broken from the outside -- which is exactly
why this has to be said out loud. Without it the first sign of trouble is a boost
temperature of 534 degrees, and the user has to guess that a profile even exists.
"""

LEARN_MORE_URL: Final = "https://github.com/FezVrasta/growatt-datalogger#troubleshooting"

# Dispatcher signals ----------------------------------------------------------
# Scoped by entry id. A globally-named signal would cross-talk between two config
# entries, which is a real bug in at least one integration that does it this way.

SIGNAL_NEW_DEVICE: Final = f"{DOMAIN}_new_device_{{entry_id}}"

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

VALUE_HOLDING: Final = "holding_registers"
"""The inverter's holding registers, raw, as ``{number: word}``.

The settings a write entity owns live in the holding space, and an announce carries that
whole space -- so the device volunteers the current value of every one of them on each
connection. A profile names only a handful of holding registers, though, and none of the
SPH/SPA storage block, so the named values alone leave those entities with nothing to
refresh from. Keeping the raw words is what lets them.

Fed from two places, deliberately merged rather than replaced: an announce, which is free
but only happens when the device reconnects, and the settings refresh, which asks. Both
are the device reporting its own holding space, so neither is more authoritative than the
other -- only newer, which is what :data:`VALUE_HOLDING_AT` is for.

Not gated on :data:`CONF_INCLUDE_UNKNOWN`: that option decides whether unnamed registers
become diagnostic *entities*, and a switch showing the wrong state must not depend on it.
"""

VALUE_HOLDING_AT: Final = "holding_registers_at"
"""When :data:`VALUE_HOLDING` was last reported.

A write is read back immediately, and the last announce may be hours old. Without knowing
which is newer, a freshly written switch snaps back to its pre-write value.

A refresh stamps this with the time it *started*, not the time it finished. Commands on a
connection are serialised, so a write that a user makes while a refresh is in flight lands
after every one of that refresh's reads -- and dating those reads to when they completed
would make the older words look like the newer ones and undo the write on screen.
"""

NOT_SENSORS: Final = frozenset({VALUE_HOLDING, VALUE_HOLDING_AT})
"""Coordinator values that exist for other entities to read, not to be shown.

Everything else the hub publishes becomes a sensor by name. These two are a raw register
map and its timestamp -- state for the write entities.
"""
