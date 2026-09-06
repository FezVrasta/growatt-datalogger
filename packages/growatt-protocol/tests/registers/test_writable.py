"""Integrity of the writable register table, and how a value becomes a word.

None of this needs Home Assistant, and it used to live in the integration's suite -- so
it only ran in the CI job that installs Home Assistant, not in the dependency-free one
that is the whole point of this package.
"""

from __future__ import annotations

import pytest
from growatt_protocol.registers import PROFILES, RegisterSpace
from growatt_protocol.registers.base import Confidence
from growatt_protocol.registers.writable import (
    TIME_SLOTS,
    WRITABLE,
    Encoding,
    WriteKind,
    for_profile,
    slot_for,
)


def test_only_verified_registers_are_enabled_by_default() -> None:
    """Community-reported meanings must not be created live on someone's inverter."""
    for spec in WRITABLE:
        assert spec.enabled_default == (spec.confidence is Confidence.VERIFIED)


def test_every_writable_register_cites_a_source() -> None:
    for spec in WRITABLE:
        assert spec.source, spec.key


def test_every_scoped_register_names_a_profile_that_exists() -> None:
    """A typo in a profile key fails silently -- the family just gets no settings."""
    for spec in WRITABLE:
        for key in spec.profiles:
            assert key in PROFILES, f"{spec.key} is scoped to unknown profile {key!r}"


@pytest.mark.parametrize("profile", sorted(PROFILES))
def test_the_read_and_write_tables_agree_on_an_address(profile: str) -> None:
    """A key that names one register to the reader and another to the writer is a bug.

    An entity prefers what the device reported over what it last wrote, so a profile
    whose holding table put ``ac_charge_enabled`` at 3049 while the writable table wrote
    1092 would have shown one register and changed another. Nothing caught that; this
    does.
    """
    holding = {
        spec.name: spec.register
        for spec in PROFILES[profile].specs_for(RegisterSpace.HOLDING).values()
    }
    for spec in for_profile(profile, include_unverified=True):
        if (reported := holding.get(spec.key)) is not None:
            assert reported == spec.register, (
                f"{profile} reads {spec.key} from holding {reported} "
                f"but writes it to {spec.register}"
            )


def test_a_family_whose_holding_space_is_unknown_is_offered_nothing() -> None:
    """OFFGRID is composed with no holding table -- the model saying so explicitly.

    It was still offered holding 0 and 3, on the strength of a default that meant "every
    profile". An SPF's holding map is not the Protocol II one.
    """
    assert PROFILES["offgrid"].specs_for(RegisterSpace.HOLDING) == {}
    assert for_profile("offgrid", include_unverified=True) == []


def test_a_non_storage_profile_gets_no_battery_registers() -> None:
    """A string inverter has no battery, so those registers must not appear."""
    keys = {spec.key for spec in for_profile("protocol_ii_3000")}
    assert "output_power_limit" in keys
    assert "battery_first_stop_soc" not in keys


def test_a_storage_profile_gets_the_battery_registers() -> None:
    keys = {spec.key for spec in for_profile("storage_1000")}
    assert "battery_first_stop_soc" in keys
    assert "charge_priority" in keys


def test_the_1000_block_is_not_offered_to_a_3000_block_hybrid() -> None:
    """A TL-XH does not have those registers, and answers a write "no such register".

    Its schedule is nine bit-packed slots at 3038-3059 instead. Offering the SPH block
    to it produced entities that could never be written, which is
    https://github.com/FezVrasta/growatt-datalogger/issues/2.
    """
    specs = for_profile("storage_3000", include_unverified=True)
    assert not [spec for spec in specs if 1000 <= spec.register < 2000]

    # ...but not left with nothing: the one storage setting whose 3000-block address is
    # settled is the same register this package already reads for these devices.
    ac_charge = next(spec for spec in specs if spec.key == "ac_charge_enabled")
    assert ac_charge.register == 3049


def test_unverified_registers_are_only_offered_when_asked_for() -> None:
    default = {spec.key for spec in for_profile("storage_1000")}
    opted_in = {spec.key for spec in for_profile("storage_1000", include_unverified=True)}

    assert "load_first_stop_soc" not in default
    assert "load_first_stop_soc" in opted_in


def test_registers_are_each_named_once() -> None:
    """A register with two keys is a transcription bug, not a design choice."""
    registers = [spec.register for spec in WRITABLE]
    assert len(registers) == len(set(registers))


@pytest.mark.parametrize("profile", sorted(PROFILES))
def test_a_key_means_one_register_within_a_profile(profile: str) -> None:
    """One key may span profiles -- ac_charge_enabled is 1092 or 3049 -- but never two
    entities on the same device, which would collide on their unique id."""
    keys = [spec.key for spec in for_profile(profile, include_unverified=True)]
    assert len(keys) == len(set(keys))


def test_grid_first_and_battery_first_scheduling_is_exposed() -> None:
    """The full storage priority-set block, not just SOC and the slot-1 window.

    See https://github.com/FezVrasta/growatt-datalogger/issues/1.
    """
    keys = {spec.key for spec in for_profile("storage_1000")}
    for key in (
        "grid_first_discharge_power_rate",
        "grid_first_start_time",
        "grid_first_stop_time",
        "grid_first_enabled",
        "grid_first_start_time_2",
        "grid_first_stop_time_2",
        "grid_first_enabled_2",
        "grid_first_start_time_3",
        "grid_first_stop_time_3",
        "grid_first_enabled_3",
        "battery_first_charge_power_rate",
        "battery_first_enabled",
        "battery_first_start_time_2",
        "battery_first_stop_time_2",
        "battery_first_enabled_2",
        "battery_first_start_time_3",
        "battery_first_stop_time_3",
        "battery_first_enabled_3",
    ):
        assert key in keys, key


# ----------------------------------------------------------------------------------
# Time slots
# ----------------------------------------------------------------------------------


def test_a_slot_generates_exactly_its_three_registers() -> None:
    """The window's boundaries and the switch that arms them, in register order."""
    slot = TIME_SLOTS[0]
    start, stop, enable = slot.entries()

    assert (start.register, stop.register, enable.register) == slot.registers
    assert start.kind is WriteKind.TIME
    assert stop.kind is WriteKind.TIME
    assert enable.kind is WriteKind.SWITCH
    assert start.encoding is Encoding.HHMM
    assert enable.encoding is Encoding.BOOL


def test_slot_one_goes_unsuffixed_and_the_others_are_numbered() -> None:
    """Matching how the Growatt app numbers them, which is what a user is looking at."""
    keys = [entry.key for slot in TIME_SLOTS for entry in slot.entries()]
    assert "grid_first_start_time" in keys
    assert "grid_first_start_time_2" in keys
    assert "grid_first_start_time_4" not in keys


def test_every_window_boundary_belongs_to_a_slot() -> None:
    """A boundary the writer cannot place is one written on its own, silently."""
    boundaries = [spec for spec in WRITABLE if spec.encoding is Encoding.HHMM]
    assert boundaries  # the table would otherwise have quietly lost its schedule
    for spec in boundaries:
        assert slot_for(spec.register) is not None, spec.key


def test_a_register_outside_a_window_stands_alone() -> None:
    assert slot_for(3) is None
    assert slot_for(1092) is None


def test_slots_do_not_overlap() -> None:
    registers = [register for slot in TIME_SLOTS for register in slot.registers]
    assert len(registers) == len(set(registers))


# ----------------------------------------------------------------------------------
# Encoding
# ----------------------------------------------------------------------------------


def test_time_windows_pack_the_hour_and_minute_into_one_word() -> None:
    spec = next(s for s in WRITABLE if s.encoding is Encoding.HHMM)
    assert spec.encode("06:30:00") == (6 << 8) | 30
    assert spec.decode((23 << 8) | 45) == "23:45:00"


def test_boolean_registers_round_trip() -> None:
    spec = next(s for s in WRITABLE if s.encoding is Encoding.BOOL)
    assert spec.encode(True) == 1
    assert spec.encode(False) == 0
    assert spec.decode(1) is True


def test_select_options_round_trip() -> None:
    spec = next(s for s in WRITABLE if s.kind is WriteKind.SELECT)
    assert spec.encode("Battery first") == 1
    assert spec.decode(2) == "Grid first"


def test_an_unknown_select_word_decodes_to_none_rather_than_a_guess() -> None:
    spec = next(s for s in WRITABLE if s.kind is WriteKind.SELECT)
    assert spec.decode(99) is None


def test_an_invalid_select_option_is_refused() -> None:
    spec = next(s for s in WRITABLE if s.kind is WriteKind.SELECT)
    with pytest.raises(ValueError, match="not one of"):
        spec.encode("Nonsense")
