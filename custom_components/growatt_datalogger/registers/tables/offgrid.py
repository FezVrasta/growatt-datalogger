# Derived from Homeassistant-Growatt-Local-Modbus, Apache License 2.0.
# See LICENSE-APACHE, NOTICE and PROVENANCE.md. Modifications are described in
# tools/import_registers.py, which generates this file.
#
# GENERATED FILE -- do not edit by hand. Regenerate with:
#     python tools/import_registers.py <path to a Growatt-Local checkout>
"""Off-grid SPF series.

A 0-based block whose meanings conflict with every other family -- register 13
is battery charge power here and PV3 power under Protocol II -- and which the
record itself gives no way to distinguish. Selecting this profile requires
out-of-band knowledge of the device."""

from __future__ import annotations

from ..base import RegisterSpec, ValueKind

INPUT_REGISTERS: tuple[RegisterSpec, ...] = (
    RegisterSpec(0, "status_code", ValueKind.RAW, scale=1),
    RegisterSpec(1, "input_1_voltage", scale=10.0),
    RegisterSpec(2, "input_2_voltage", scale=10.0),
    RegisterSpec(3, "input_1_power", length=2, scale=10.0, signed=True),
    RegisterSpec(5, "input_2_power", length=2, scale=10.0, signed=True),
    RegisterSpec(7, "input_1_amperage", scale=10.0),
    RegisterSpec(8, "input_2_amperage", scale=10.0),
    RegisterSpec(9, "output_active_power", length=2, scale=10.0, signed=True),
    RegisterSpec(13, "charge_power", length=2, scale=10.0, signed=True),
    RegisterSpec(17, "battery_voltage", scale=100.0),
    RegisterSpec(18, "soc", ValueKind.RAW, scale=1),
    RegisterSpec(19, "bus_voltage", scale=10.0),
    RegisterSpec(20, "grid_voltage", scale=10.0),
    RegisterSpec(21, "grid_frequency", scale=100.0),
    RegisterSpec(22, "output_1_voltage", scale=10.0),
    RegisterSpec(23, "output_frequency", scale=100.0),
    RegisterSpec(24, "output_dc_voltage", scale=10.0),
    RegisterSpec(25, "inverter_temperature", scale=10.0),
    RegisterSpec(26, "dc_dc_temperature", scale=10.0),
    RegisterSpec(27, "load_percent", scale=10.0),
    RegisterSpec(28, "battery_port_voltage", scale=10.0),
    RegisterSpec(29, "battery_bus_voltage", scale=10.0),
    RegisterSpec(30, "operation_hours", length=2, scale=7200.0, signed=True),
    RegisterSpec(34, "output_1_amperage", scale=10.0),
    RegisterSpec(42, "fault_code", ValueKind.RAW, scale=1),
    RegisterSpec(43, "warning_code", ValueKind.RAW, scale=1),
    RegisterSpec(47, "constant_power", ValueKind.RAW, scale=1),
    RegisterSpec(48, "input_1_energy_today", length=2, scale=10.0, signed=True),
    RegisterSpec(50, "input_1_energy_total", length=2, scale=10.0, signed=True),
    RegisterSpec(52, "input_2_energy_today", length=2, scale=10.0, signed=True),
    RegisterSpec(54, "input_2_energy_total", length=2, scale=10.0, signed=True),
    RegisterSpec(56, "charge_energy_today", length=2, scale=10.0, signed=True),
    RegisterSpec(58, "charge_energy_total", length=2, scale=10.0, signed=True),
    RegisterSpec(60, "discharge_energy_today", length=2, scale=10.0, signed=True),
    RegisterSpec(62, "discharge_energy_total", length=2, scale=10.0, signed=True),
    RegisterSpec(64, "ac_discharge_energy_today", length=2, scale=10.0, signed=True),
    RegisterSpec(66, "ac_discharge_energy_total", length=2, scale=10.0, signed=True),
    RegisterSpec(68, "ac_charge_amperage", scale=10.0),
    RegisterSpec(69, "discharge_power", length=2, scale=10.0, signed=True),
    RegisterSpec(73, "battery_discharge_amperage", scale=10.0),
    RegisterSpec(77, "battery_power", length=2, scale=10.0, signed=True),
    RegisterSpec(85, "ac_load_energy_today", length=2, scale=10.0, signed=True),
    RegisterSpec(87, "ac_load_energy_total", length=2, scale=10.0, signed=True),
)
