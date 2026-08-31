"""Integrity of the register tables themselves.

These are data-quality checks. The tables are generated from an upstream source that has
already been shown to contain transcription slips, so the point is to make a bad entry
fail the build rather than surface as a sensor reading 6553.5 degrees.
"""

from __future__ import annotations

import pytest

from custom_components.growatt_datalogger.registers import (
    PROFILES,
    RegisterSpace,
    ValueKind,
)
from custom_components.growatt_datalogger.registers.base import Profile

PROFILE_CASES = list(PROFILES.values())
CASE_IDS = [profile.key for profile in PROFILE_CASES]

SPACE_CASES = [(profile, space) for profile in PROFILE_CASES for space in RegisterSpace]
SPACE_IDS = [f"{profile.key}-{space.value}" for profile, space in SPACE_CASES]


@pytest.mark.parametrize("profile", PROFILE_CASES, ids=CASE_IDS)
def test_profile_is_not_empty(profile: Profile) -> None:
    assert len(profile) > 0


@pytest.mark.parametrize(("profile", "space"), SPACE_CASES, ids=SPACE_IDS)
def test_specs_are_keyed_by_their_own_start_register(
    profile: Profile, space: RegisterSpace
) -> None:
    for number, spec in profile.specs_for(space).items():
        assert spec.register == number


@pytest.mark.parametrize(("profile", "space"), SPACE_CASES, ids=SPACE_IDS)
def test_no_two_specs_claim_the_same_register(profile: Profile, space: RegisterSpace) -> None:
    """A multi-register run must not overlap another spec.

    Composing a storage overlay onto a base map replaces entries by start register, but
    a *partial* overlap means two specs disagree about what a register holds, which is a
    data error rather than a deliberate override.
    """
    owner: dict[int, str] = {}
    for spec in profile.specs_for(space).values():
        for number in spec.registers:
            previous = owner.get(number)
            assert previous is None, (
                f"{profile.key}/{space.value}: register {number} claimed by both "
                f"{previous!r} and {spec.name!r}"
            )
            owner[number] = spec.name


@pytest.mark.parametrize(("profile", "space"), SPACE_CASES, ids=SPACE_IDS)
def test_no_duplicate_names_within_a_profile(profile: Profile, space: RegisterSpace) -> None:
    names = [spec.name for spec in profile.specs_for(space).values()]
    duplicates = {name for name in names if names.count(name) > 1}
    assert not duplicates, f"{profile.key}/{space.value} repeats {sorted(duplicates)}"


@pytest.mark.parametrize(("profile", "space"), SPACE_CASES, ids=SPACE_IDS)
def test_spec_fields_are_sane(profile: Profile, space: RegisterSpace) -> None:
    for spec in profile.specs_for(space).values():
        assert 0 <= spec.register <= 0xFFFF, spec
        assert spec.length >= 1, spec
        assert spec.scale > 0, spec
        assert spec.name.isidentifier(), f"{spec.name!r} is not a valid identifier"
        assert spec.name == spec.name.lower(), spec


@pytest.mark.parametrize(("profile", "space"), SPACE_CASES, ids=SPACE_IDS)
def test_raw_values_are_unscaled(profile: Profile, space: RegisterSpace) -> None:
    """A RAW spec with a scale other than 1 would silently be ignored by the decoder."""
    for spec in profile.specs_for(space).values():
        if spec.kind in (ValueKind.RAW, ValueKind.BITFIELD):
            assert spec.scale == 1, spec


@pytest.mark.parametrize(("profile", "space"), SPACE_CASES, ids=SPACE_IDS)
def test_multi_register_numeric_values_are_signed(profile: Profile, space: RegisterSpace) -> None:
    """Growatt encodes 32-bit quantities as two's complement; 16-bit ones as unsigned.

    Power flow is genuinely bidirectional on a hybrid, so an unsigned 32-bit read turns
    a small export into roughly 4.29 billion watts.
    """
    for spec in profile.specs_for(space).values():
        if spec.kind is ValueKind.SCALED and spec.length == 2:
            assert spec.signed, spec
        if spec.kind is ValueKind.SCALED and spec.length == 1:
            assert not spec.signed, spec


def test_the_known_upstream_slip_is_not_present() -> None:
    """The 3000-block table must not contain 0-block register numbers.

    Upstream's TL-XH table places input_4_energy_today/total at registers 71 and 73,
    which is a copy from the 0-based map. The importer drops them; this pins that, since
    reintroducing them would attach a PV energy counter to whatever a 0-block device
    happens to report at 71.
    """
    from custom_components.growatt_datalogger.registers.tables import protocol_ii

    stray = [spec for spec in protocol_ii.INPUT_REGISTERS_3000 if spec.register < 3000]
    assert not stray, f"0-block registers leaked into the 3000 table: {stray}"


def test_the_two_protocol_ii_blocks_are_genuinely_different_layouts() -> None:
    """They are not one map rebased on the other, and must not be treated as such.

    The 0-block carries eight MPPT inputs and the 3000-block four, so total output power
    is register 35 in one and 3023 in the other -- a difference of 2988, not 3000.
    """
    from custom_components.growatt_datalogger.registers.tables import protocol_ii

    zero = {spec.name: spec.register for spec in protocol_ii.INPUT_REGISTERS}
    three = {spec.name: spec.register for spec in protocol_ii.INPUT_REGISTERS_3000}

    shared = set(zero) & set(three)
    offsets = {three[name] - zero[name] for name in shared}
    assert len(offsets) > 1, "the two blocks differ by a constant; re-check the import"
