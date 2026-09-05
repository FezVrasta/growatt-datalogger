"""Choosing a profile from the register ranges a record reported."""

from __future__ import annotations

from growatt_protocol.registers import (
    LEGACY_315,
    OFFGRID,
    PROTOCOL_II,
    PROTOCOL_II_3000,
    STORAGE_1000,
    STORAGE_3000,
    resolve_profile,
)


def test_protocol_ii_zero_block() -> None:
    match = resolve_profile([(0, 124)])
    assert match.profile is PROTOCOL_II
    assert match.confident


def test_legacy_block_ending_at_44() -> None:
    match = resolve_profile([(0, 44)])
    assert match.profile is LEGACY_315
    assert match.confident


def test_legacy_two_group_form() -> None:
    """The legacy map is sometimes split as 0-44 and 45-89."""
    match = resolve_profile([(0, 44), (45, 89)])
    assert match.profile is LEGACY_315


def test_three_thousand_block() -> None:
    match = resolve_profile([(3000, 3124)])
    assert match.profile is PROTOCOL_II_3000
    assert match.confident


def test_hybrid_is_distinguished_by_the_second_group_not_by_overlap() -> None:
    """A plain MOD and a hybrid both report 3000-3124.

    Several storage registers (3041, 3067) fall inside that range, so "contains a
    storage register" cannot discriminate. Only a hybrid sends a 3125+ group.
    """
    plain = resolve_profile([(3000, 3124)])
    hybrid = resolve_profile([(3000, 3124), (3125, 3249)])

    assert plain.profile is PROTOCOL_II_3000
    assert hybrid.profile is STORAGE_3000


def test_storage_on_the_thousand_block() -> None:
    match = resolve_profile([(0, 124), (1000, 1124)])
    assert match.profile is STORAGE_1000
    assert match.confident


def test_extended_protocol_ii_stays_confident() -> None:
    """MIN and MAX3 send further groups above 124; the first still ends there.

    Confidence is read off that first group alone, so a device with extended registers
    is not demoted to a guess just for reporting more of them.
    """
    match = resolve_profile([(0, 124), (125, 249)])
    assert match.profile is PROTOCOL_II
    assert match.confident


def test_zero_block_ending_off_the_documented_boundary_is_not_confident() -> None:
    """The SPH5000/ShineWiFi-S case from issue #1.

    It reports the storage layout on a single 0-134 group. Nothing in the record says
    so, but 134 is not where any family we know ends -- and admitting that is what lets
    the integration prompt instead of publishing 65 GWh of daily yield.
    """
    match = resolve_profile([(0, 134)])
    assert match.profile is PROTOCOL_II  # still decodes
    assert not match.confident
    assert "134" in match.reason


def test_offgrid_is_never_inferred() -> None:
    """An SPF reports a 0-based block indistinguishable from Protocol II."""
    assert resolve_profile([(0, 124)]).profile is not OFFGRID
    assert resolve_profile([(0, 134)]).profile is not OFFGRID


def test_offgrid_can_be_pinned() -> None:
    match = resolve_profile([(0, 124)], override="offgrid")
    assert match.profile is OFFGRID
    assert match.confident
    assert "pinned" in match.reason


def test_override_wins_over_inference() -> None:
    match = resolve_profile([(3000, 3124)], override="legacy_315")
    assert match.profile is LEGACY_315


def test_unknown_override_falls_back_to_inference() -> None:
    match = resolve_profile([(3000, 3124)], override="does_not_exist")
    assert match.profile is PROTOCOL_II_3000


def test_no_groups_is_not_confident() -> None:
    match = resolve_profile([])
    assert not match.confident
    assert "no register groups" in match.reason


def test_unrecognised_range_is_not_confident() -> None:
    match = resolve_profile([(0, 900)])
    assert not match.confident
    assert match.profile is PROTOCOL_II  # still decodes, but flagged


def test_reason_is_always_populated() -> None:
    for ranges in ([(0, 124)], [(0, 44)], [(3000, 3124)], [], [(0, 900)]):
        assert resolve_profile(ranges).reason
