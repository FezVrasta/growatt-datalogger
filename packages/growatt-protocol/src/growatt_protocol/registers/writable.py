"""Registers this integration is willing to write, and how confident it is about each.

The honest position: Growatt's published protocol documents the inverter holding
registers in the low bank (0-124) and the storage control block (1000-1118) on the
SPH/SPA family. Those are solid -- as long as they are offered to the family they belong
to. A great deal of what circulates about other registers is community folklore --
correct on someone's firmware, wrong or destructive on another's.

So every entry carries a :class:`~.base.Confidence`, and only ``VERIFIED`` entries are
created as entities by default. Everything else is opt-in per device. That is not
excessive caution: writing the wrong holding register on a grid-tied inverter can change
its grid-code behaviour.

Values here are for *holding* registers, which is a different address space from the
input registers telemetry arrives in.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .base import Confidence


class WriteKind(StrEnum):
    """How a writable register is presented."""

    NUMBER = "number"
    SWITCH = "switch"
    SELECT = "select"
    TIME = "time"


class Encoding(StrEnum):
    """How a value becomes a 16-bit word."""

    RAW = "raw"
    """The value is the word."""

    SCALED = "scaled"
    """The word is the value multiplied by ``scale``."""

    BOOL = "bool"
    """1 or 0."""

    HHMM = "hhmm"
    """Hour in the high byte, minute in the low byte.

    How Growatt encodes the boundaries of a charge or discharge window.
    """


@dataclass(frozen=True, slots=True)
class WritableRegister:
    """One register a user may change."""

    key: str
    register: int
    kind: WriteKind
    confidence: Confidence
    source: str
    """Where the meaning comes from, so a reader can judge it for themselves."""

    profiles: frozenset[str] = frozenset()
    """Profiles this applies to. Empty means every profile."""

    encoding: Encoding = Encoding.RAW
    minimum: float = 0
    maximum: float = 100
    step: float = 1
    scale: float = 1
    unit: str | None = None
    icon: str | None = None
    options: tuple[tuple[str, int], ...] = ()
    """For a select: ``((label, word), ...)``."""

    @property
    def enabled_default(self) -> bool:
        return self.confidence is Confidence.VERIFIED

    def encode(self, value: float | bool | str) -> int:
        """Turn a Home Assistant value into the word to write."""
        if self.encoding is Encoding.BOOL:
            return 1 if value else 0
        if self.encoding is Encoding.SCALED:
            return round(float(value) * self.scale)
        if self.encoding is Encoding.HHMM:
            hour, _, minute = str(value).partition(":")
            return (int(hour) << 8) | int(minute[:2])
        if self.kind is WriteKind.SELECT:
            for label, word in self.options:
                if label == value:
                    return word
            raise ValueError(f"{value!r} is not one of {[o[0] for o in self.options]}")
        return int(value)

    def decode(self, word: int) -> float | bool | str | None:
        """Inverse of :meth:`encode`, for reading a register back."""
        if self.encoding is Encoding.BOOL:
            return bool(word)
        if self.encoding is Encoding.SCALED:
            return word / self.scale
        if self.encoding is Encoding.HHMM:
            return f"{word >> 8:02d}:{word & 0xFF:02d}:00"
        if self.kind is WriteKind.SELECT:
            for label, value in self.options:
                if value == word:
                    return label
            return None
        return word


_SPEC_II = "Growatt Inverter Modbus RTU Protocol II"

#: The SPH/SPA/MIX storage control block lives at 1000-1118, and *only* there.
#:
#: A 3000-block hybrid -- a MOD or MIN TL-XH -- does not have those registers at all. Its
#: schedule is nine bit-packed slots at 3038-3059, where one word carries hour, minute,
#: priority and enable together, and its AC-charge gate is 3049. That is why this package
#: already *reads* ``ac_charge_enabled`` at 3049 for those devices rather than at 1092.
#:
#: Offering the 1000 block to both was wrong in a way that only shows up on write: a
#: read of a register the inverter does not have comes back empty and the entity simply
#: stays unknown, while a write comes back "result 2 -- no such register". That is
#: https://github.com/FezVrasta/growatt-datalogger/issues/2.
STORAGE_1000_BLOCK = frozenset({"storage_1000"})

#: Hybrids that report the 3000 block. The bit-packed schedule at 3038-3059 has no entity
#: here yet; 3049 is the one setting whose address is settled.
STORAGE_3000_BLOCK = frozenset({"storage_3000"})

WRITABLE: tuple[WritableRegister, ...] = (
    # ---- Documented in the specification -------------------------------------------
    WritableRegister(
        key="output_power_limit",
        register=3,
        kind=WriteKind.NUMBER,
        confidence=Confidence.VERIFIED,
        source=f"{_SPEC_II}, holding register 3",
        minimum=0,
        maximum=100,
        unit="%",
        icon="mdi:speedometer",
    ),
    WritableRegister(
        key="inverter_enabled",
        register=0,
        kind=WriteKind.SWITCH,
        confidence=Confidence.VERIFIED,
        source=f"{_SPEC_II}, holding register 0",
        encoding=Encoding.BOOL,
        icon="mdi:power",
    ),
    # ---- Storage control block ------------------------------------------------------
    # Documented, but only meaningful on a hybrid, so scoped to those profiles.
    WritableRegister(
        key="charge_priority",
        register=1044,
        kind=WriteKind.SELECT,
        confidence=Confidence.VERIFIED,
        source=f"{_SPEC_II}, holding register 1044",
        profiles=STORAGE_1000_BLOCK,
        options=(("Load first", 0), ("Battery first", 1), ("Grid first", 2)),
        icon="mdi:priority-high",
    ),
    WritableRegister(
        key="ac_charge_enabled",
        register=1092,
        kind=WriteKind.SWITCH,
        confidence=Confidence.VERIFIED,
        source=f"{_SPEC_II}, holding register 1092",
        profiles=STORAGE_1000_BLOCK,
        encoding=Encoding.BOOL,
        icon="mdi:battery-charging",
    ),
    WritableRegister(
        key="battery_first_stop_soc",
        register=1091,
        kind=WriteKind.NUMBER,
        confidence=Confidence.VERIFIED,
        source=f"{_SPEC_II}, holding register 1091",
        profiles=STORAGE_1000_BLOCK,
        minimum=5,
        maximum=100,
        unit="%",
        icon="mdi:battery-charging-high",
    ),
    WritableRegister(
        key="grid_first_stop_soc",
        register=1071,
        kind=WriteKind.NUMBER,
        confidence=Confidence.VERIFIED,
        source=f"{_SPEC_II}, holding register 1071",
        profiles=STORAGE_1000_BLOCK,
        minimum=5,
        maximum=100,
        unit="%",
        icon="mdi:battery-arrow-down",
    ),
    WritableRegister(
        key="grid_first_discharge_power_rate",
        register=1070,
        kind=WriteKind.NUMBER,
        confidence=Confidence.VERIFIED,
        source=f"{_SPEC_II}, holding register 1070",
        profiles=STORAGE_1000_BLOCK,
        minimum=0,
        maximum=100,
        unit="%",
        icon="mdi:transmission-tower-export",
    ),
    WritableRegister(
        key="grid_first_start_time",
        register=1080,
        kind=WriteKind.TIME,
        confidence=Confidence.VERIFIED,
        source=f"{_SPEC_II}, holding register 1080",
        profiles=STORAGE_1000_BLOCK,
        encoding=Encoding.HHMM,
        icon="mdi:clock-start",
    ),
    WritableRegister(
        key="grid_first_stop_time",
        register=1081,
        kind=WriteKind.TIME,
        confidence=Confidence.VERIFIED,
        source=f"{_SPEC_II}, holding register 1081",
        profiles=STORAGE_1000_BLOCK,
        encoding=Encoding.HHMM,
        icon="mdi:clock-end",
    ),
    WritableRegister(
        key="grid_first_enabled",
        register=1082,
        kind=WriteKind.SWITCH,
        confidence=Confidence.VERIFIED,
        source=f"{_SPEC_II}, holding register 1082",
        profiles=STORAGE_1000_BLOCK,
        encoding=Encoding.BOOL,
        icon="mdi:transmission-tower",
    ),
    WritableRegister(
        key="grid_first_start_time_2",
        register=1083,
        kind=WriteKind.TIME,
        confidence=Confidence.VERIFIED,
        source=f"{_SPEC_II}, holding register 1083",
        profiles=STORAGE_1000_BLOCK,
        encoding=Encoding.HHMM,
        icon="mdi:clock-start",
    ),
    WritableRegister(
        key="grid_first_stop_time_2",
        register=1084,
        kind=WriteKind.TIME,
        confidence=Confidence.VERIFIED,
        source=f"{_SPEC_II}, holding register 1084",
        profiles=STORAGE_1000_BLOCK,
        encoding=Encoding.HHMM,
        icon="mdi:clock-end",
    ),
    WritableRegister(
        key="grid_first_enabled_2",
        register=1085,
        kind=WriteKind.SWITCH,
        confidence=Confidence.VERIFIED,
        source=f"{_SPEC_II}, holding register 1085",
        profiles=STORAGE_1000_BLOCK,
        encoding=Encoding.BOOL,
        icon="mdi:transmission-tower",
    ),
    WritableRegister(
        key="grid_first_start_time_3",
        register=1086,
        kind=WriteKind.TIME,
        confidence=Confidence.VERIFIED,
        source=f"{_SPEC_II}, holding register 1086",
        profiles=STORAGE_1000_BLOCK,
        encoding=Encoding.HHMM,
        icon="mdi:clock-start",
    ),
    WritableRegister(
        key="grid_first_stop_time_3",
        register=1087,
        kind=WriteKind.TIME,
        confidence=Confidence.VERIFIED,
        source=f"{_SPEC_II}, holding register 1087",
        profiles=STORAGE_1000_BLOCK,
        encoding=Encoding.HHMM,
        icon="mdi:clock-end",
    ),
    WritableRegister(
        key="grid_first_enabled_3",
        register=1088,
        kind=WriteKind.SWITCH,
        confidence=Confidence.VERIFIED,
        source=f"{_SPEC_II}, holding register 1088",
        profiles=STORAGE_1000_BLOCK,
        encoding=Encoding.BOOL,
        icon="mdi:transmission-tower",
    ),
    WritableRegister(
        key="battery_first_charge_power_rate",
        register=1090,
        kind=WriteKind.NUMBER,
        confidence=Confidence.VERIFIED,
        source=f"{_SPEC_II}, holding register 1090",
        profiles=STORAGE_1000_BLOCK,
        minimum=0,
        maximum=100,
        unit="%",
        icon="mdi:battery-charging-100",
    ),
    WritableRegister(
        key="battery_first_start_time",
        register=1100,
        kind=WriteKind.TIME,
        confidence=Confidence.VERIFIED,
        source=f"{_SPEC_II}, holding register 1100",
        profiles=STORAGE_1000_BLOCK,
        encoding=Encoding.HHMM,
        icon="mdi:clock-start",
    ),
    WritableRegister(
        key="battery_first_stop_time",
        register=1101,
        kind=WriteKind.TIME,
        confidence=Confidence.VERIFIED,
        source=f"{_SPEC_II}, holding register 1101",
        profiles=STORAGE_1000_BLOCK,
        encoding=Encoding.HHMM,
        icon="mdi:clock-end",
    ),
    WritableRegister(
        key="battery_first_enabled",
        register=1102,
        kind=WriteKind.SWITCH,
        confidence=Confidence.VERIFIED,
        source=f"{_SPEC_II}, holding register 1102",
        profiles=STORAGE_1000_BLOCK,
        encoding=Encoding.BOOL,
        icon="mdi:battery-clock",
    ),
    WritableRegister(
        key="battery_first_start_time_2",
        register=1103,
        kind=WriteKind.TIME,
        confidence=Confidence.VERIFIED,
        source=f"{_SPEC_II}, holding register 1103",
        profiles=STORAGE_1000_BLOCK,
        encoding=Encoding.HHMM,
        icon="mdi:clock-start",
    ),
    WritableRegister(
        key="battery_first_stop_time_2",
        register=1104,
        kind=WriteKind.TIME,
        confidence=Confidence.VERIFIED,
        source=f"{_SPEC_II}, holding register 1104",
        profiles=STORAGE_1000_BLOCK,
        encoding=Encoding.HHMM,
        icon="mdi:clock-end",
    ),
    WritableRegister(
        key="battery_first_enabled_2",
        register=1105,
        kind=WriteKind.SWITCH,
        confidence=Confidence.VERIFIED,
        source=f"{_SPEC_II}, holding register 1105",
        profiles=STORAGE_1000_BLOCK,
        encoding=Encoding.BOOL,
        icon="mdi:battery-clock",
    ),
    WritableRegister(
        key="battery_first_start_time_3",
        register=1106,
        kind=WriteKind.TIME,
        confidence=Confidence.VERIFIED,
        source=f"{_SPEC_II}, holding register 1106",
        profiles=STORAGE_1000_BLOCK,
        encoding=Encoding.HHMM,
        icon="mdi:clock-start",
    ),
    WritableRegister(
        key="battery_first_stop_time_3",
        register=1107,
        kind=WriteKind.TIME,
        confidence=Confidence.VERIFIED,
        source=f"{_SPEC_II}, holding register 1107",
        profiles=STORAGE_1000_BLOCK,
        encoding=Encoding.HHMM,
        icon="mdi:clock-end",
    ),
    WritableRegister(
        key="battery_first_enabled_3",
        register=1108,
        kind=WriteKind.SWITCH,
        confidence=Confidence.VERIFIED,
        source=f"{_SPEC_II}, holding register 1108",
        profiles=STORAGE_1000_BLOCK,
        encoding=Encoding.BOOL,
        icon="mdi:battery-clock",
    ),
    # ---- The 3000 block -------------------------------------------------------------
    # Everything above addresses the SPH/SPA storage block. A TL-XH hybrid keeps its
    # settings elsewhere, and 3049 is the one address that is settled: the same register
    # this package already reads as ac_charge_enabled for these devices. The nine-slot
    # schedule at 3038-3059 packs hour, minute, priority and enable into single words and
    # needs its own encoding, so it is not exposed yet.
    WritableRegister(
        key="ac_charge_enabled",
        register=3049,
        kind=WriteKind.SWITCH,
        confidence=Confidence.VERIFIED,
        source=f"{_SPEC_II}, holding register 3049",
        profiles=STORAGE_3000_BLOCK,
        encoding=Encoding.BOOL,
        icon="mdi:battery-charging",
    ),
    # ---- Community-reported ---------------------------------------------------------
    # Consistent with observed behaviour, but absent from the specification. Created
    # disabled, and only when the user opts in for that device.
    WritableRegister(
        key="load_first_stop_soc",
        register=1109,
        kind=WriteKind.NUMBER,
        confidence=Confidence.COMMUNITY,
        source="community reports; not present in the published protocol",
        profiles=STORAGE_1000_BLOCK,
        minimum=5,
        maximum=100,
        unit="%",
        icon="mdi:battery-low",
    ),
)

#: The SPH/SPA time slots, as ``(start, stop, enable)`` triples of holding registers.
#:
#: Firmware validates a slot as a whole, not a register at a time. A window written one
#: register at a time passes through a moment where the start is the new value and the
#: stop is still the old one -- an inverted or overlapping window that some firmware
#: rejects outright and some acts on. And enabling a slot whose window is still
#: 00:00-00:00 is refused, which only makes sense once you see that the three registers
#: are one setting. Sending them as a single 0x10 range is what Growatt's own app does.
TIME_SEGMENTS: tuple[tuple[int, int, int], ...] = (
    (1080, 1081, 1082),
    (1083, 1084, 1085),
    (1086, 1087, 1088),
    (1100, 1101, 1102),
    (1103, 1104, 1105),
    (1106, 1107, 1108),
)


def segment_for(register: int) -> tuple[int, int, int] | None:
    """The time slot ``register`` is part of, or ``None`` if it stands alone."""
    for segment in TIME_SEGMENTS:
        if register in segment:
            return segment
    return None


def for_profile(profile_key: str, *, include_unverified: bool = False) -> list[WritableRegister]:
    """Writable registers applicable to ``profile_key``."""
    return [
        entry
        for entry in WRITABLE
        if (not entry.profiles or profile_key in entry.profiles)
        and (include_unverified or entry.confidence is Confidence.VERIFIED)
    ]
