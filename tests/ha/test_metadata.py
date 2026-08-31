"""Every decodable value must have presentation metadata.

This is the check that stops a register table growing a new entry that quietly becomes a
unitless, classless sensor nobody notices is wrong.
"""

from __future__ import annotations

import pytest
from growatt_protocol.registers import RegisterSpace, all_spec_names
from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass

from custom_components.growatt_datalogger import metadata
from custom_components.growatt_datalogger.const import (
    VALUE_BUFFERED_RECORDS,
    VALUE_CRC_MISMATCHES,
    VALUE_DECODE_ERRORS,
    VALUE_LAST_RECORD,
    VALUE_PROFILE,
    VALUE_RECORDS,
)

ALL_NAMES = sorted(all_spec_names(RegisterSpace.INPUT) | all_spec_names(RegisterSpace.HOLDING))

INTERNAL_NAMES = [
    VALUE_LAST_RECORD,
    VALUE_RECORDS,
    VALUE_DECODE_ERRORS,
    VALUE_CRC_MISMATCHES,
    VALUE_BUFFERED_RECORDS,
    VALUE_PROFILE,
]


def test_the_name_list_is_not_empty() -> None:
    """Guards against a refactor making the coverage test pass vacuously."""
    assert len(ALL_NAMES) > 100


@pytest.mark.parametrize("name", ALL_NAMES)
def test_every_register_value_has_metadata(name: str) -> None:
    assert metadata.describe(name) is not None, f"no metadata rule covers {name!r}"


@pytest.mark.parametrize("name", INTERNAL_NAMES)
def test_every_internal_value_has_metadata(name: str) -> None:
    assert metadata.describe(name) is not None


@pytest.mark.parametrize("name", ALL_NAMES)
def test_a_unit_implies_a_state_class(name: str) -> None:
    """A unit-bearing sensor with no state class is excluded from statistics."""
    meta = metadata.describe(name)
    assert meta is not None
    if meta.unit is not None:
        assert meta.state_class is not None, name


@pytest.mark.parametrize("name", ALL_NAMES)
def test_a_device_class_implies_a_unit(name: str) -> None:
    """Home Assistant rejects most device classes that arrive without a unit."""
    meta = metadata.describe(name)
    assert meta is not None
    if meta.device_class is not None:
        assert meta.unit is not None, name


def test_energy_values_are_classified_for_the_energy_dashboard() -> None:
    """The single biggest reason to use this over a generic MQTT bridge."""
    for name in ALL_NAMES:
        if not name.endswith(("_energy_today", "_energy_total")):
            continue
        meta = metadata.describe(name)
        assert meta is not None
        if meta.unit == "kvarh":
            continue  # reactive energy has no Home Assistant device class
        assert meta.device_class is SensorDeviceClass.ENERGY, name
        assert meta.state_class is SensorStateClass.TOTAL_INCREASING, name
        assert meta.unit == "kWh", name


def test_lifetime_generation_is_present_and_correct() -> None:
    meta = metadata.describe("output_energy_total")
    assert meta is not None
    assert meta.device_class is SensorDeviceClass.ENERGY
    assert meta.state_class is SensorStateClass.TOTAL_INCREASING
    assert meta.enabled_default


def test_battery_charge_uses_the_battery_device_class() -> None:
    meta = metadata.describe("soc")
    assert meta is not None
    assert meta.device_class is SensorDeviceClass.BATTERY
    assert meta.unit == "%"


def test_unknown_names_get_no_metadata() -> None:
    assert metadata.describe("something_we_never_defined") is None


def test_pretty_labels_are_readable() -> None:
    assert metadata.pretty("input_1_voltage") == "Input 1 voltage"
