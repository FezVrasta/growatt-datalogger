"""Inverter profiles and the rules for picking one from a record.

Because a record states the register ranges it carries, most of the family question
answers itself. A group ending at register 124 is the Protocol II input block; one
starting at 3000 is the newer block; one at 1000 is a storage block. That is enough to
pick a profile without any of the plausibility-scoring guesswork that offset-table
implementations need.

The exception is the off-grid SPF series, which reports a 0-based block whose meanings
are entirely different -- register 13 is battery charge power there and PV3 power under
Protocol II. Nothing in the record distinguishes the two, so an off-grid device must be
identified out of band and pinned by the user.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from .base import Profile, RegisterSpace, RegisterSpec
from .tables import legacy_315, offgrid, protocol_ii, storage

PROTOCOL_II = Profile.compose(
    "protocol_ii",
    "Protocol II, 0-based input block (MIN, TL-X, MAX, MID)",
    input_tables=[protocol_ii.INPUT_REGISTERS],
    holding_tables=[protocol_ii.HOLDING_REGISTERS],
)

PROTOCOL_II_3000 = Profile.compose(
    "protocol_ii_3000",
    "Protocol II, 3000-based input block (MOD, TL-XH)",
    input_tables=[protocol_ii.INPUT_REGISTERS_3000],
    holding_tables=[protocol_ii.HOLDING_REGISTERS],
)

STORAGE_1000 = Profile.compose(
    "storage_1000",
    "Storage on the 0-based block with a 1000 battery overlay (SPH, SPA, MIX)",
    input_tables=[protocol_ii.INPUT_REGISTERS, storage.INPUT_REGISTERS_1000],
    holding_tables=[protocol_ii.HOLDING_REGISTERS, storage.HOLDING_REGISTERS],
)

STORAGE_3000 = Profile.compose(
    "storage_3000",
    "Storage on the 3000-based block with a battery overlay (TL-XH hybrid)",
    input_tables=[protocol_ii.INPUT_REGISTERS_3000, storage.INPUT_REGISTERS_3000],
    holding_tables=[protocol_ii.HOLDING_REGISTERS, storage.HOLDING_REGISTERS],
)

LEGACY_315 = Profile.compose(
    "legacy_315",
    "Legacy RS485 RTU protocol (-S, MTL-S)",
    input_tables=[legacy_315.INPUT_REGISTERS],
    holding_tables=[legacy_315.HOLDING_REGISTERS],
)

OFFGRID = Profile.compose(
    "offgrid",
    "Off-grid SPF series",
    input_tables=[offgrid.INPUT_REGISTERS],
)

PROFILES: dict[str, Profile] = {
    profile.key: profile
    for profile in (
        PROTOCOL_II,
        PROTOCOL_II_3000,
        STORAGE_1000,
        STORAGE_3000,
        LEGACY_315,
        OFFGRID,
    )
}

#: Used when a record's ranges match nothing known. Its registers still decode, but the
#: caller should treat the result as provisional and surface the unknown registers.
FALLBACK_PROFILE = PROTOCOL_II

#: Profiles that cannot be inferred from a record and must be chosen by the user.
MANUAL_ONLY = frozenset({OFFGRID.key})


@dataclass(frozen=True, slots=True)
class ProfileMatch:
    """The outcome of profile resolution."""

    profile: Profile
    reason: str
    confident: bool
    """False when the record's ranges did not identify a family.

    An unconfident match still decodes -- a wrong guess is visible as implausible values
    rather than as silence -- but the integration should say so, keep the entities as
    diagnostics, and invite the user to pin the profile.
    """


def _highest_end(ranges: Sequence[tuple[int, int]], below: int) -> int | None:
    ends = [end for start, end in ranges if start < below]
    return max(ends) if ends else None


def resolve_profile(
    ranges: Iterable[tuple[int, int]],
    *,
    override: str | None = None,
) -> ProfileMatch:
    """Pick a profile from the ``(start, end)`` register ranges a record reported.

    ``override`` is a profile key set by the user; it always wins, and is the only way
    to select :data:`OFFGRID`.
    """
    if override:
        profile = PROFILES.get(override)
        if profile is not None:
            return ProfileMatch(profile, f"pinned to {override} by configuration", True)

    ranges = list(ranges)
    if not ranges:
        return ProfileMatch(FALLBACK_PROFILE, "record reported no register groups", confident=False)

    starts = {start for start, _ in ranges}
    has_storage_1000 = any(1000 <= start < 2000 for start in starts)
    has_3000 = any(start >= 3000 for start in starts)

    if has_3000:
        # Both a plain MOD and a hybrid report the 3000-3124 group, and several storage
        # registers (3041, 3067..) fall inside it -- so "contains a storage register"
        # does not discriminate. What does is the second group: only a hybrid sends
        # 3125 and above, where the battery energy counters and SOC live.
        if any(start >= 3125 for start in starts):
            return ProfileMatch(STORAGE_3000, "3000-block record with a 3125+ storage group", True)
        return ProfileMatch(PROTOCOL_II_3000, "3000-block record", True)

    if has_storage_1000:
        return ProfileMatch(STORAGE_1000, "record includes the 1000 storage block", True)

    # A 0-based block. Its highest register distinguishes the legacy map, which stops at
    # 44 (or 89 across two groups), from Protocol II, which runs to 124.
    end = _highest_end(ranges, below=1000)
    if end is None:
        return ProfileMatch(
            FALLBACK_PROFILE, "no 0-based group to identify the family", confident=False
        )
    if end <= 89:
        return ProfileMatch(LEGACY_315, f"0-based block ending at {end}", True)
    if end <= 249:
        return ProfileMatch(PROTOCOL_II, f"0-based block ending at {end}", True)

    return ProfileMatch(
        FALLBACK_PROFILE, f"unrecognised 0-based block ending at {end}", confident=False
    )


def all_spec_names(space: RegisterSpace = RegisterSpace.INPUT) -> set[str]:
    """Every value name any profile can produce. Used to check metadata coverage."""
    return {
        spec.name for profile in PROFILES.values() for spec in profile.specs_for(space).values()
    }


def specs_by_name(name: str) -> list[RegisterSpec]:
    """Every spec across every profile that produces ``name``."""
    return [
        spec
        for profile in PROFILES.values()
        for space in RegisterSpace
        for spec in profile.specs_for(space).values()
        if spec.name == name
    ]
