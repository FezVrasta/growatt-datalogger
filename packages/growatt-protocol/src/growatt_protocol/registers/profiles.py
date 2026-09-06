"""Inverter profiles and the rules for picking one from a record.

Because a record states the register ranges it carries, most of the family question
answers itself. A group ending at register 124 is the Protocol II input block; one
starting at 3000 is the newer block; one at 1000 is a storage block. That is enough to
pick a profile without any of the plausibility-scoring guesswork that offset-table
implementations need.

The exception is the storage layout on a 0-based block, which the off-grid SPF series
reports and which at least one SPH with a ShineWiFi-S reports too. Its meanings are
entirely different -- register 13 is battery charge power there and PV3 power under
Protocol II. Nothing in the record identifies it positively, so such a device must be
recognised out of band and pinned by the user.

What the record *can* do is say when it does not look like anything we know. The group
that starts at register 0 ends at a documented boundary on every family we support: 124
for Protocol II, 44 or 89 for the legacy map. A first group ending anywhere else is a
layout we have not been shown, and saying so is the difference between a user seeing a
prompt to pin a profile and a user seeing a boost temperature of 534 degrees.
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

#: The part of the storage holding overlay that addresses the 3000 block.
#:
#: ``storage.HOLDING_REGISTERS`` is generated from an upstream table that has one storage
#: device type and so does not distinguish the two storage families, but the families do
#: not agree: AC charge enable is holding 3049 on a TL-XH and holding 1092 on an SPH.
#: Applying the whole overlay to both left the read side saying 3049 and the write side
#: saying 1092 *for the same profile*, unchecked -- and an entity prefers what the device
#: reported, so it would have displayed one register while writing another.
#:
#: The split is made here rather than in the generated file because it is this project's
#: knowledge, not upstream's, and because composition decisions already live here.
STORAGE_HOLDING_3000 = tuple(spec for spec in storage.HOLDING_REGISTERS if spec.register >= 3000)
STORAGE_HOLDING_SHARED = tuple(spec for spec in storage.HOLDING_REGISTERS if spec.register < 3000)

STORAGE_1000 = Profile.compose(
    "storage_1000",
    "Storage on the 0-based block with a 1000 battery overlay (SPH, SPA, MIX)",
    input_tables=[protocol_ii.INPUT_REGISTERS, storage.INPUT_REGISTERS_1000],
    # No 3000-block overlay: an SPH cannot report those registers, so all it did was
    # contradict the writable table. Its serial comes from Protocol II holding 23.
    holding_tables=[protocol_ii.HOLDING_REGISTERS, STORAGE_HOLDING_SHARED],
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
    "Storage on a 0-based block (off-grid SPF, and some SPH via ShineWiFi-S)",
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


#: Where the group starting at register 0 ends on each 0-based family. Protocol II
#: devices with extended registers (MIN, MAX3) send *further* groups above 124 -- the
#: first one still ends there -- so this is read off that group alone rather than from
#: the highest register in the record.
PROTOCOL_II_BLOCK_END = 124
LEGACY_BLOCK_ENDS = frozenset({44, 89})


def _zero_block_end(ranges: Sequence[tuple[int, int]]) -> int | None:
    """Where the group that starts at register 0 ends, if the record has one."""
    for start, end in ranges:
        if start == 0:
            return end
    return None


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

    # A 0-based block. Where its first group ends distinguishes the legacy map, which
    # stops at 44 (or 89 across two groups), from Protocol II, which stops at 124.
    end = _zero_block_end(ranges)
    if end is None:
        return ProfileMatch(
            FALLBACK_PROFILE, "no 0-based group to identify the family", confident=False
        )
    if end in LEGACY_BLOCK_ENDS:
        return ProfileMatch(LEGACY_315, f"0-based block ending at {end}", True)
    if end == PROTOCOL_II_BLOCK_END:
        return ProfileMatch(PROTOCOL_II, f"0-based block ending at {end}", True)

    # Anything else is a layout we have not been shown. It still decodes as Protocol II,
    # because a visibly wrong number is more useful than no number at all -- but it is
    # emphatically a guess, and an SPH reporting the storage layout on a 0-134 block
    # lands here. Claiming confidence over that is what shipped 65 GWh of daily yield.
    return ProfileMatch(
        FALLBACK_PROFILE,
        f"0-based block ends at {end}, which matches no known layout; "
        f"assuming {FALLBACK_PROFILE.key}",
        confident=False,
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
