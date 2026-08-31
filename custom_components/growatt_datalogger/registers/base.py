"""Register specification model and the decoder that applies it.

A :class:`RegisterSpec` says what one Modbus register (or a run of them) means. A
:class:`Profile` is a set of specs covering one inverter family, keyed by register
number.

This is the half of the problem the wire format does not solve. A telemetry record tells
us *which* registers it carries -- see :mod:`..protocol.records` -- but not what they
mean, and families disagree: register 13 is PV3 power under Protocol II, grid frequency
on the legacy map, and battery charge power on an off-grid unit. So meaning is looked up
as ``(profile, register)``.

Like the protocol package, this module imports nothing outside the standard library.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum


class RegisterSpace(StrEnum):
    """Which Modbus register file a number refers to.

    These are separate address spaces: holding register 3001 is the inverter serial
    number while input register 3001 is total PV power. The record's function code says
    which one it carries -- 0x03 announces holding registers, 0x04 and 0x50 carry input
    registers -- so a lookup keyed on the number alone would confuse the two.
    """

    INPUT = "input"
    HOLDING = "holding"


class ValueKind(StrEnum):
    """How a register's raw words become a value."""

    RAW = "raw"
    """Unsigned integer, no scaling. Status codes, counters, enumerations."""

    SCALED = "scaled"
    """Numeric value divided by :attr:`RegisterSpec.scale`."""

    TEXT = "text"
    """ASCII packed two characters per register, high byte first."""

    BITFIELD = "bitfield"
    """Unsigned integer whose bits are flags. Kept raw; interpreted by metadata."""


class Confidence(StrEnum):
    """How well established a register's meaning is.

    Used to decide whether an entity is created enabled, created disabled, or gated
    behind an explicit opt-in. Writing to a register whose meaning is guessed can
    misconfigure a grid-tied inverter, so this is not cosmetic.
    """

    VERIFIED = "verified"
    """Documented in a Growatt protocol specification."""

    COMMUNITY = "community"
    """Widely reported and consistent with observed behaviour, but not in the spec."""

    UNVERIFIED = "unverified"
    """Plausible but unconfirmed. Never enabled by default."""


@dataclass(frozen=True, slots=True)
class RegisterSpec:
    """The meaning of one register, or of a run of ``length`` registers."""

    register: int
    name: str
    kind: ValueKind = ValueKind.SCALED
    length: int = 1
    scale: float = 10.0
    signed: bool = False
    """Interpret the assembled words as two's complement.

    Growatt encodes 32-bit quantities as signed -- power flow can legitimately be
    negative -- while 16-bit telemetry is unsigned. Set per spec rather than inferred
    from length, so the exceptions are visible.
    """

    confidence: Confidence = Confidence.VERIFIED

    @property
    def registers(self) -> range:
        """Every register number this spec consumes."""
        return range(self.register, self.register + self.length)


@dataclass(frozen=True, slots=True)
class Profile:
    """A named set of register meanings for one inverter family."""

    key: str
    description: str
    input_specs: Mapping[int, RegisterSpec] = field(repr=False)
    holding_specs: Mapping[int, RegisterSpec] = field(repr=False)

    @classmethod
    def compose(
        cls,
        key: str,
        description: str,
        *,
        input_tables: Iterable[Iterable[RegisterSpec]] = (),
        holding_tables: Iterable[Iterable[RegisterSpec]] = (),
    ) -> Profile:
        """Build a profile from register tables.

        Later tables override earlier ones, which is how a storage overlay extends a
        base inverter map. Override happens on **both** the register number and the
        value name: a storage device reports its serial from holding register 3001 while
        the base map reads it from register 23, so keeping both would emit one name from
        two addresses and let whichever the device happened to report win.

        Partial overlaps -- two specs whose multi-register runs intersect without sharing
        a start -- are a data error rather than a deliberate override, and are caught by
        the register tests instead of being silently resolved here.
        """

        def merge(tables: Iterable[Iterable[RegisterSpec]]) -> dict[int, RegisterSpec]:
            specs: dict[int, RegisterSpec] = {}
            register_for_name: dict[str, int] = {}
            for table in tables:
                for spec in table:
                    previous = register_for_name.get(spec.name)
                    if previous is not None and previous != spec.register:
                        del specs[previous]
                    specs[spec.register] = spec
                    register_for_name[spec.name] = spec.register
            return specs

        return cls(
            key=key,
            description=description,
            input_specs=merge(input_tables),
            holding_specs=merge(holding_tables),
        )

    def specs_for(self, space: RegisterSpace) -> Mapping[int, RegisterSpec]:
        return self.input_specs if space is RegisterSpace.INPUT else self.holding_specs

    def get(self, register: int, space: RegisterSpace = RegisterSpace.INPUT) -> RegisterSpec | None:
        return self.specs_for(space).get(register)

    def __len__(self) -> int:
        return len(self.input_specs) + len(self.holding_specs)


def _assemble(words: list[int], *, signed: bool) -> int:
    """Combine big-endian 16-bit words into one integer."""
    value = 0
    for word in words:
        value = (value << 16) | word
    if signed:
        bits = 16 * len(words)
        if value >= 1 << (bits - 1):
            value -= 1 << bits
    return value


@dataclass(frozen=True, slots=True)
class DecodedValues:
    """The result of applying a profile to a record's registers."""

    values: dict[str, float | int | str]
    """Named, scaled values ready for presentation."""

    unknown: dict[int, int]
    """Registers the profile has no spec for, kept raw.

    These are not an error. They are the raw material for extending coverage: a device
    reporting registers we cannot name is exactly what a diagnostics dump should show.
    """

    incomplete: tuple[str, ...]
    """Names whose spec spans registers the record did not fully contain."""


def decode_registers(
    profile: Profile,
    registers: Mapping[int, int],
    space: RegisterSpace = RegisterSpace.INPUT,
) -> DecodedValues:
    """Apply ``profile`` to the raw ``{register: word}`` map from a record.

    ``space`` must match what the record carried: input registers for a 0x04 or 0x50
    telemetry record, holding registers for a 0x03 announce.

    Scaling is applied here, in Python. Some implementations publish raw integers and
    push the division into a presentation-layer template; doing it once at the source
    keeps every consumer honest and means the stored value is the real quantity.
    """
    values: dict[str, float | int | str] = {}
    incomplete: list[str] = []
    consumed: set[int] = set()

    for start, spec in profile.specs_for(space).items():
        if start not in registers:
            continue

        words: list[int] = []
        for number in spec.registers:
            word = registers.get(number)
            if word is None:
                break
            words.append(word)

        if len(words) != spec.length:
            # A run that straddles the end of a reported group. Record it rather than
            # emitting a value built from a partial read.
            incomplete.append(spec.name)
            continue

        consumed.update(spec.registers)

        if spec.kind is ValueKind.TEXT:
            chars = []
            for word in words:
                chars.append(chr(word >> 8))
                chars.append(chr(word & 0xFF))
            values[spec.name] = "".join(chars).rstrip("\x00").strip()
            continue

        raw = _assemble(words, signed=spec.signed)

        if spec.kind in (ValueKind.RAW, ValueKind.BITFIELD):
            values[spec.name] = raw
        else:
            values[spec.name] = round(raw / spec.scale, 3)

    unknown = {number: word for number, word in registers.items() if number not in consumed}
    return DecodedValues(values=values, unknown=unknown, incomplete=tuple(sorted(incomplete)))
