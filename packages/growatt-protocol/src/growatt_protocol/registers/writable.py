"""Registers this integration is willing to write, and how confident it is about each.

The honest position: Growatt's published protocol documents the inverter holding
registers in the low bank (0-124) and the storage control block (1000-1118). Those are
solid. A great deal of what circulates about other registers is community folklore --
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

STORAGE_PROFILES = frozenset({"storage_1000", "storage_3000"})

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
        profiles=STORAGE_PROFILES,
        options=(("Load first", 0), ("Battery first", 1), ("Grid first", 2)),
        icon="mdi:priority-high",
    ),
    WritableRegister(
        key="ac_charge_enabled",
        register=1092,
        kind=WriteKind.SWITCH,
        confidence=Confidence.VERIFIED,
        source=f"{_SPEC_II}, holding register 1092",
        profiles=STORAGE_PROFILES,
        encoding=Encoding.BOOL,
        icon="mdi:battery-charging",
    ),
    WritableRegister(
        key="battery_first_stop_soc",
        register=1091,
        kind=WriteKind.NUMBER,
        confidence=Confidence.VERIFIED,
        source=f"{_SPEC_II}, holding register 1091",
        profiles=STORAGE_PROFILES,
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
        profiles=STORAGE_PROFILES,
        minimum=5,
        maximum=100,
        unit="%",
        icon="mdi:battery-arrow-down",
    ),
    WritableRegister(
        key="battery_first_start_time",
        register=1100,
        kind=WriteKind.TIME,
        confidence=Confidence.VERIFIED,
        source=f"{_SPEC_II}, holding register 1100",
        profiles=STORAGE_PROFILES,
        encoding=Encoding.HHMM,
        icon="mdi:clock-start",
    ),
    WritableRegister(
        key="battery_first_stop_time",
        register=1101,
        kind=WriteKind.TIME,
        confidence=Confidence.VERIFIED,
        source=f"{_SPEC_II}, holding register 1101",
        profiles=STORAGE_PROFILES,
        encoding=Encoding.HHMM,
        icon="mdi:clock-end",
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
        profiles=STORAGE_PROFILES,
        minimum=5,
        maximum=100,
        unit="%",
        icon="mdi:battery-low",
    ),
)

BY_KEY: dict[str, WritableRegister] = {entry.key: entry for entry in WRITABLE}


def for_profile(profile_key: str, *, include_unverified: bool = False) -> list[WritableRegister]:
    """Writable registers applicable to ``profile_key``."""
    return [
        entry
        for entry in WRITABLE
        if (not entry.profiles or profile_key in entry.profiles)
        and (include_unverified or entry.confidence is Confidence.VERIFIED)
    ]
