"""Home Assistant presentation metadata for decoded values.

This is the only place that knows about device classes and units, which is what keeps
:mod:`.registers` and :mod:`.protocol` free of Home Assistant.

Most of the ~110 value names follow strict conventions -- anything ending ``_voltage`` is
volts, anything ending ``_energy_total`` is a lifetime kWh counter -- so metadata is
derived from the name and only the genuine exceptions are written out. That way adding a
register to a table cannot silently produce a unitless sensor: a name matching no rule is
a test failure, not a shrug.

The energy classifications are what make the Energy Dashboard work, so they are the part
worth being careful about. ``total_increasing`` is correct for the daily counters too:
they reset at midnight and Home Assistant handles that, whereas ``total`` would need an
explicit last-reset.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace

from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.const import (
    PERCENTAGE,
    EntityCategory,
    UnitOfApparentPower,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfEnergy,
    UnitOfFrequency,
    UnitOfPower,
    UnitOfTemperature,
    UnitOfTime,
)

MEASUREMENT = SensorStateClass.MEASUREMENT
TOTAL_INCREASING = SensorStateClass.TOTAL_INCREASING


@dataclass(frozen=True, slots=True)
class ValueMeta:
    """How one decoded value should appear in Home Assistant."""

    device_class: SensorDeviceClass | None = None
    state_class: SensorStateClass | None = None
    unit: str | None = None
    icon: str | None = None
    precision: int | None = None
    category: EntityCategory | None = None
    enabled_default: bool = True


_VOLTAGE = ValueMeta(
    SensorDeviceClass.VOLTAGE, MEASUREMENT, UnitOfElectricPotential.VOLT, precision=1
)
_CURRENT = ValueMeta(
    SensorDeviceClass.CURRENT, MEASUREMENT, UnitOfElectricCurrent.AMPERE, precision=1
)
_POWER = ValueMeta(SensorDeviceClass.POWER, MEASUREMENT, UnitOfPower.WATT, precision=1)
_ENERGY = ValueMeta(
    SensorDeviceClass.ENERGY, TOTAL_INCREASING, UnitOfEnergy.KILO_WATT_HOUR, precision=1
)
_TEMPERATURE = ValueMeta(
    SensorDeviceClass.TEMPERATURE, MEASUREMENT, UnitOfTemperature.CELSIUS, precision=1
)
_FREQUENCY = ValueMeta(SensorDeviceClass.FREQUENCY, MEASUREMENT, UnitOfFrequency.HERTZ, precision=2)
_PERCENT = ValueMeta(None, MEASUREMENT, PERCENTAGE, precision=0)
_DIAGNOSTIC = ValueMeta(category=EntityCategory.DIAGNOSTIC, enabled_default=False)

#: Matched in order against the value name. First hit wins.
_PATTERNS: tuple[tuple[re.Pattern[str], ValueMeta], ...] = (
    (re.compile(r"_energy_(today|total)$"), _ENERGY),
    (re.compile(r"_voltage$"), _VOLTAGE),
    (re.compile(r"_amperage$"), _CURRENT),
    (re.compile(r"_temperature$"), _TEMPERATURE),
    (re.compile(r"_frequency$"), _FREQUENCY),
    (re.compile(r"_power$"), _POWER),
    (re.compile(r"_percent$"), _PERCENT),
    (re.compile(r"^(fault|warning|status)_code$"), _DIAGNOSTIC),
)

#: Names whose meaning the patterns cannot infer, or infer wrongly.
_OVERRIDES: dict[str, ValueMeta] = {
    # Totals and flows.
    "input_power": replace(_POWER, icon="mdi:solar-power"),
    "output_power": replace(_POWER, icon="mdi:transmission-tower-export"),
    "output_active_power": replace(_POWER, icon="mdi:transmission-tower-export"),
    "input_energy_total": replace(_ENERGY, icon="mdi:solar-power"),
    "output_energy_today": replace(_ENERGY, icon="mdi:solar-power"),
    "output_energy_total": replace(_ENERGY, icon="mdi:solar-power"),
    # Grid exchange. Named "pac_*" in the protocol; they are instantaneous power.
    "pac_to_user_total": replace(_POWER, icon="mdi:transmission-tower-import"),
    "pac_to_grid_total": replace(_POWER, icon="mdi:transmission-tower-export"),
    "power_to_user": replace(_POWER, icon="mdi:transmission-tower-import"),
    "power_to_grid": replace(_POWER, icon="mdi:transmission-tower-export"),
    "power_user_load": replace(_POWER, icon="mdi:home-lightning-bolt"),
    # Grid energy counters. The naming breaks the "_energy_today" convention, and these
    # are precisely the entities the Energy Dashboard wants for grid consumption and
    # return, so they are worth spelling out.
    "energy_to_user_today": replace(_ENERGY, icon="mdi:transmission-tower-import"),
    "energy_to_user_total": replace(_ENERGY, icon="mdi:transmission-tower-import"),
    "energy_to_grid_today": replace(_ENERGY, icon="mdi:transmission-tower-export"),
    "energy_to_grid_total": replace(_ENERGY, icon="mdi:transmission-tower-export"),
    # Battery.
    "soc": ValueMeta(
        SensorDeviceClass.BATTERY, MEASUREMENT, PERCENTAGE, "mdi:battery", precision=0
    ),
    "battery_power": replace(_POWER, icon="mdi:home-battery"),
    "charge_power": replace(_POWER, icon="mdi:battery-charging"),
    "discharge_power": replace(_POWER, icon="mdi:battery-arrow-down"),
    # Reactive quantities. Home Assistant has a device class for reactive power but
    # none for reactive energy, so the latter is a plain unit-bearing sensor.
    "output_reactive_power": ValueMeta(
        SensorDeviceClass.REACTIVE_POWER,
        MEASUREMENT,
        UnitOfApparentPower.VOLT_AMPERE,
        precision=1,
    ),
    "output_reactive_energy_today": ValueMeta(
        None, TOTAL_INCREASING, "kvarh", "mdi:flash", precision=1
    ),
    "output_reactive_energy_total": ValueMeta(
        None, TOTAL_INCREASING, "kvarh", "mdi:flash", precision=1
    ),
    # Reported in half-seconds and scaled to hours by the register table.
    "operation_hours": ValueMeta(
        SensorDeviceClass.DURATION,
        TOTAL_INCREASING,
        UnitOfTime.HOURS,
        "mdi:timer-outline",
        precision=1,
        category=EntityCategory.DIAGNOSTIC,
    ),
    "real_output_power_percent": replace(_PERCENT, icon="mdi:gauge"),
    "load_percent": replace(_PERCENT, icon="mdi:gauge"),
    # Bus voltages are internal measurements, useful for diagnosis rather than display.
    "p_bus_voltage": replace(_VOLTAGE, category=EntityCategory.DIAGNOSTIC, enabled_default=False),
    "n_bus_voltage": replace(_VOLTAGE, category=EntityCategory.DIAGNOSTIC, enabled_default=False),
    "bus_voltage": replace(_VOLTAGE, category=EntityCategory.DIAGNOSTIC, enabled_default=False),
    "boost_temperature": replace(_TEMPERATURE, enabled_default=False),
    "dc_dc_temperature": replace(_TEMPERATURE, enabled_default=False),
    # Identity and configuration, read from the announce record.
    "firmware": replace(_DIAGNOSTIC, icon="mdi:chip"),
    "serial_number": replace(_DIAGNOSTIC, icon="mdi:identifier"),
    "modbus_version": replace(_DIAGNOSTIC, icon="mdi:protocol"),
    "device_type_code": _DIAGNOSTIC,
    "inverter_enabled": replace(_DIAGNOSTIC, icon="mdi:power"),
    "ac_charge_enabled": replace(_DIAGNOSTIC, icon="mdi:battery-charging"),
    "output_power_limit": replace(_PERCENT, category=EntityCategory.DIAGNOSTIC),
    "constant_power": _DIAGNOSTIC,
    "derating_mode": _DIAGNOSTIC,
    "warning_value": _DIAGNOSTIC,
}

#: Values the integration produces itself rather than reading from a register.
_INTERNAL: dict[str, ValueMeta] = {
    "last_record": ValueMeta(SensorDeviceClass.TIMESTAMP, icon="mdi:clock-check-outline"),
    "records_received": ValueMeta(
        None, TOTAL_INCREASING, icon="mdi:counter", category=EntityCategory.DIAGNOSTIC
    ),
    "decode_errors": ValueMeta(
        None,
        TOTAL_INCREASING,
        icon="mdi:alert-circle-outline",
        category=EntityCategory.DIAGNOSTIC,
        enabled_default=False,
    ),
    "crc_mismatches": ValueMeta(
        None,
        TOTAL_INCREASING,
        icon="mdi:alert-circle-outline",
        category=EntityCategory.DIAGNOSTIC,
        enabled_default=False,
    ),
    "buffered_records": ValueMeta(
        None,
        TOTAL_INCREASING,
        icon="mdi:history",
        category=EntityCategory.DIAGNOSTIC,
        enabled_default=False,
    ),
    "profile": ValueMeta(
        icon="mdi:tag-outline", category=EntityCategory.DIAGNOSTIC, enabled_default=False
    ),
}

#: Metadata for a register the tables cannot name. Always a disabled diagnostic: it is
#: an unlabelled integer, so presenting it as a normal sensor would be a lie.
UNKNOWN_REGISTER_META = ValueMeta(
    icon="mdi:help-circle-outline",
    category=EntityCategory.DIAGNOSTIC,
    enabled_default=False,
)


def describe(name: str) -> ValueMeta | None:
    """Return metadata for ``name``, or ``None`` if no rule covers it."""
    if (meta := _INTERNAL.get(name)) is not None:
        return meta
    if (meta := _OVERRIDES.get(name)) is not None:
        return meta
    for pattern, meta in _PATTERNS:
        if pattern.search(name):
            return meta
    return None


def pretty(name: str) -> str:
    """A readable fallback label for a value with no translation."""
    return name.replace("_", " ").capitalize()
