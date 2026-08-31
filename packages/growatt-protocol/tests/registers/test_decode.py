"""Applying a profile to a record's registers."""

from __future__ import annotations

from growatt_protocol.registers import (
    PROTOCOL_II,
    PROTOCOL_II_3000,
    RegisterSpace,
    decode_registers,
)
from growatt_protocol.registers.base import (
    Profile,
    RegisterSpec,
    ValueKind,
    _assemble,
)


def test_assemble_unsigned() -> None:
    assert _assemble([0x0001], signed=False) == 1
    assert _assemble([0xFFFF], signed=False) == 65535
    assert _assemble([0x0001, 0x0002], signed=False) == 0x00010002


def test_assemble_signed_two_words() -> None:
    assert _assemble([0xFFFF, 0xFFFF], signed=True) == -1
    assert _assemble([0x8000, 0x0000], signed=True) == -(2**31)
    assert _assemble([0x0000, 0x0001], signed=True) == 1


def test_scaled_single_register() -> None:
    result = decode_registers(PROTOCOL_II, {3: 3295})
    assert result.values["input_1_voltage"] == 329.5


def test_scaled_thirty_two_bit_value() -> None:
    # 2585 W reported as tenths across two registers.
    result = decode_registers(PROTOCOL_II, {1: 0, 2: 25850})
    assert result.values["input_power"] == 2585.0


def test_negative_power_decodes_as_negative() -> None:
    """Export on a hybrid is a negative 32-bit value, not four billion watts."""
    # 0xFFFFF9F6 is -1546 in two's complement, i.e. -154.6 W in tenths.
    result = decode_registers(PROTOCOL_II, {1: 0xFFFF, 2: 0xF9F6})
    assert result.values["input_power"] == -154.6


def test_raw_values_are_not_scaled() -> None:
    result = decode_registers(PROTOCOL_II, {0: 1})
    assert result.values["status_code"] == 1


def test_grid_frequency_uses_its_own_scale() -> None:
    result = decode_registers(PROTOCOL_II, {37: 4996})
    assert result.values["grid_frequency"] == 49.96


def test_operation_hours_scale() -> None:
    """Reported in half-seconds; 7200 of them is an hour."""
    result = decode_registers(PROTOCOL_II, {57: 0, 58: 14400})
    assert result.values["operation_hours"] == 2.0


def test_unrecognised_registers_are_kept_not_dropped() -> None:
    result = decode_registers(PROTOCOL_II, {0: 1, 60000: 42})
    assert result.values["status_code"] == 1
    assert result.unknown == {60000: 42}


def test_partial_multi_register_run_is_reported_not_guessed() -> None:
    """A 32-bit value whose second word fell outside the reported group."""
    result = decode_registers(PROTOCOL_II, {1: 5})
    assert "input_power" not in result.values
    assert "input_power" in result.incomplete
    assert result.unknown == {1: 5}


def test_text_values_unpack_two_characters_per_register() -> None:
    profile = Profile.compose(
        "t",
        "test",
        holding_tables=[(RegisterSpec(0, "firmware", ValueKind.TEXT, length=3),)],
    )
    words = [
        (ord("A") << 8) | ord("B"),
        (ord("1") << 8) | ord("2"),
        (ord("3") << 8) | 0,
    ]
    result = decode_registers(profile, dict(enumerate(words)), space=RegisterSpace.HOLDING)
    assert result.values["firmware"] == "AB123"


def test_input_and_holding_are_separate_address_spaces() -> None:
    """Holding 3001 is the serial number; input 3001 is total PV power."""
    from growatt_protocol.registers import STORAGE_3000

    assert STORAGE_3000.get(3001, RegisterSpace.INPUT).name == "input_power"
    assert STORAGE_3000.get(3001, RegisterSpace.HOLDING).name == "serial_number"


def test_storage_overlay_replaces_the_base_serial_register() -> None:
    """Composing must not leave one name reachable from two different registers."""
    from growatt_protocol.registers import STORAGE_3000

    holding = STORAGE_3000.specs_for(RegisterSpace.HOLDING)
    serials = [spec for spec in holding.values() if spec.name == "serial_number"]
    assert len(serials) == 1
    assert serials[0].register == 3001


def test_three_thousand_block_decodes_with_its_own_profile() -> None:
    result = decode_registers(PROTOCOL_II_3000, {3000: 1, 3025: 4996, 3093: 345, 3094: 379})
    assert result.values == {
        "status_code": 1,
        "grid_frequency": 49.96,
        "inverter_temperature": 34.5,
        "ipm_temperature": 37.9,
    }
