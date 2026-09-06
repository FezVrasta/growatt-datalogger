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

from . import profiles
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

    profiles: frozenset[str]
    """Exactly which profiles this applies to. No default, deliberately.

    It used to default to "every profile", which is fail-open, and issue #2 is what
    fail-open costs. There was a live instance of it too: :data:`~.profiles.OFFGRID` is
    composed with *no holding table at all* -- this project's own model saying it knows
    nothing about that family's holding space -- and yet an off-grid SPF was offered
    holding 0 and 3 on the strength of an unstated default. An SPF's holding map is not
    the Protocol II one.

    Requiring the field means a new entry cannot be added without someone answering the
    question, and :func:`for_profile` no longer has a branch that says yes to everything.
    """

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
#:
#: Named off :mod:`.profiles` rather than spelled as a string: a typo in a bare
#: ``"storage_1000"`` fails silently -- ``for_profile`` simply returns nothing for that
#: family, which looks exactly like a device with no writable settings.
STORAGE_1000_BLOCK = frozenset({profiles.STORAGE_1000.key})

#: Hybrids that report the 3000 block. The bit-packed schedule at 3038-3059 has no entity
#: here yet; 3049 is the one setting whose address is settled.
STORAGE_3000_BLOCK = frozenset({profiles.STORAGE_3000.key})

#: Families whose holding space is the documented Protocol II low bank, where registers 0
#: and 3 mean what the specification says they mean.
#:
#: Every family this project understands the holding space of, which is to say every one
#: except :data:`~.profiles.OFFGRID`. That profile is composed with no holding table at
#: all, so there is nothing here to base a write on: an SPF's holding map is a different
#: map, and writing register 3 on one on the strength of a Protocol II reading would be
#: exactly the kind of guess this table exists to avoid.
PROTOCOL_II_HOLDING = frozenset(
    {
        profiles.PROTOCOL_II.key,
        profiles.PROTOCOL_II_3000.key,
        profiles.STORAGE_1000.key,
        profiles.STORAGE_3000.key,
        # The legacy map declares 0 and 3 with the same two meanings.
        profiles.LEGACY_315.key,
    }
)


@dataclass(frozen=True, slots=True)
class TimeSlot:
    """One charge or discharge window: a start, a stop, and the switch that arms it.

    Firmware validates the three as a unit rather than a register at a time. A window
    written one register at a time passes through a moment where the start is the new
    value and the stop is still the old one -- an inverted or overlapping window that
    some firmware rejects outright and some acts on. And enabling a slot whose window is
    still 00:00-00:00 is refused, which only makes sense once you see that the three
    registers are one setting. Sending them as a single 0x10 range is what Growatt's own
    app does.

    So a slot is declared here once, and its three :class:`WritableRegister` entries are
    generated from it. The registers are always contiguous, in this order.
    """

    schedule: str
    """``grid_first`` or ``battery_first`` -- the key prefix its entries take."""

    number: int
    """1, 2 or 3, numbered as the Growatt app numbers them."""

    start: int
    """Holding register of the window start."""

    profiles: frozenset[str]
    enable_icon: str

    @property
    def registers(self) -> tuple[int, int, int]:
        return (self.start, self.start + 1, self.start + 2)

    @property
    def suffix(self) -> str:
        """Slot 1 goes unsuffixed, matching how the app presents it."""
        return "" if self.number == 1 else f"_{self.number}"

    def entries(self) -> tuple[WritableRegister, ...]:
        start, stop, enable = self.registers
        common = {
            "confidence": Confidence.VERIFIED,
            "profiles": self.profiles,
        }
        return (
            WritableRegister(
                key=f"{self.schedule}_start_time{self.suffix}",
                register=start,
                kind=WriteKind.TIME,
                source=f"{_SPEC_II}, holding register {start}",
                encoding=Encoding.HHMM,
                icon="mdi:clock-start",
                **common,
            ),
            WritableRegister(
                key=f"{self.schedule}_stop_time{self.suffix}",
                register=stop,
                kind=WriteKind.TIME,
                source=f"{_SPEC_II}, holding register {stop}",
                encoding=Encoding.HHMM,
                icon="mdi:clock-end",
                **common,
            ),
            WritableRegister(
                key=f"{self.schedule}_enabled{self.suffix}",
                register=enable,
                kind=WriteKind.SWITCH,
                source=f"{_SPEC_II}, holding register {enable}",
                encoding=Encoding.BOOL,
                icon=self.enable_icon,
                **common,
            ),
        )


#: Every window, spelled out rather than computed: these are documented register numbers
#: and a reader should be able to check them against the spec without doing arithmetic.
TIME_SLOTS: tuple[TimeSlot, ...] = (
    TimeSlot("grid_first", 1, 1080, STORAGE_1000_BLOCK, "mdi:transmission-tower"),
    TimeSlot("grid_first", 2, 1083, STORAGE_1000_BLOCK, "mdi:transmission-tower"),
    TimeSlot("grid_first", 3, 1086, STORAGE_1000_BLOCK, "mdi:transmission-tower"),
    TimeSlot("battery_first", 1, 1100, STORAGE_1000_BLOCK, "mdi:battery-clock"),
    TimeSlot("battery_first", 2, 1103, STORAGE_1000_BLOCK, "mdi:battery-clock"),
    TimeSlot("battery_first", 3, 1106, STORAGE_1000_BLOCK, "mdi:battery-clock"),
)


def slot_for(register: int) -> TimeSlot | None:
    """The window ``register`` is part of, or ``None`` if it stands alone."""
    for slot in TIME_SLOTS:
        if register in slot.registers:
            return slot
    return None


WRITABLE: tuple[WritableRegister, ...] = (
    # ---- Documented in the specification -------------------------------------------
    WritableRegister(
        key="output_power_limit",
        register=3,
        kind=WriteKind.NUMBER,
        confidence=Confidence.VERIFIED,
        source=f"{_SPEC_II}, holding register 3",
        profiles=PROTOCOL_II_HOLDING,
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
        profiles=PROTOCOL_II_HOLDING,
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
    # ---- Charge and discharge windows -----------------------------------------------
    # Generated from TIME_SLOTS, so a window is declared once rather than as three
    # entries here plus a separate list of which registers form one. Two sources for one
    # fact is how a slot ends up half declared -- and a slot the writer fails to
    # recognise silently loses its atomicity, which is the whole point of declaring it.
    *(entry for slot in TIME_SLOTS for entry in slot.entries()),
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


def for_profile(profile_key: str, *, include_unverified: bool = False) -> list[WritableRegister]:
    """Writable registers applicable to ``profile_key``."""
    return [
        entry
        for entry in WRITABLE
        if profile_key in entry.profiles
        and (include_unverified or entry.confidence is Confidence.VERIFIED)
    ]
