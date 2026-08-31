# Derived from Homeassistant-Growatt-Local-Modbus, Apache License 2.0.
# See LICENSE-APACHE, NOTICE and PROVENANCE.md. Modifications are described in
# tools/import_registers.py, which generates this file.
#
# GENERATED FILE -- do not edit by hand. Regenerate with:
#     python tools/import_registers.py <path to a Growatt-Local checkout>
"""The older Growatt PV Inverter Modbus RS485 RTU Protocol (-S / MTL-S).

Shares register numbers with Protocol II but not their meanings: register 11 is
PV3 voltage there and total output power here."""

from __future__ import annotations

from ..base import RegisterSpec, ValueKind

INPUT_REGISTERS: tuple[RegisterSpec, ...] = (
    RegisterSpec(0, "status_code", ValueKind.RAW, scale=1),
    RegisterSpec(1, "input_power", length=2, scale=10.0, signed=True),
    RegisterSpec(3, "input_1_voltage", scale=10.0),
    RegisterSpec(4, "input_1_amperage", scale=10.0),
    RegisterSpec(5, "input_1_power", length=2, scale=10.0, signed=True),
    RegisterSpec(7, "input_2_voltage", scale=10.0),
    RegisterSpec(8, "input_2_amperage", scale=10.0),
    RegisterSpec(9, "input_2_power", length=2, scale=10.0, signed=True),
    RegisterSpec(11, "output_power", length=2, scale=10.0, signed=True),
    RegisterSpec(13, "grid_frequency", scale=100.0),
    RegisterSpec(14, "output_1_voltage", scale=10.0),
    RegisterSpec(15, "output_1_amperage", scale=10.0),
    RegisterSpec(16, "output_1_power", length=2, scale=10.0, signed=True),
    RegisterSpec(18, "output_2_voltage", scale=10.0),
    RegisterSpec(19, "output_2_amperage", scale=10.0),
    RegisterSpec(20, "output_2_power", length=2, scale=10.0, signed=True),
    RegisterSpec(22, "output_3_voltage", scale=10.0),
    RegisterSpec(23, "output_3_amperage", scale=10.0),
    RegisterSpec(24, "output_3_power", length=2, scale=10.0, signed=True),
    RegisterSpec(26, "output_energy_today", length=2, scale=10.0, signed=True),
    RegisterSpec(28, "output_energy_total", length=2, scale=10.0, signed=True),
    RegisterSpec(30, "operation_hours", length=2, scale=7200.0, signed=True),
    RegisterSpec(32, "inverter_temperature", scale=10.0),
    RegisterSpec(40, "fault_code", ValueKind.RAW, scale=1),
    RegisterSpec(41, "ipm_temperature", scale=10.0),
    RegisterSpec(42, "p_bus_voltage", scale=10.0),
    RegisterSpec(43, "n_bus_voltage", scale=10.0),
    RegisterSpec(47, "derating_mode", ValueKind.RAW, scale=1),
    RegisterSpec(48, "input_1_energy_today", length=2, scale=10.0, signed=True),
    RegisterSpec(50, "input_1_energy_total", length=2, scale=10.0, signed=True),
    RegisterSpec(52, "input_2_energy_today", length=2, scale=10.0, signed=True),
    RegisterSpec(54, "input_2_energy_total", length=2, scale=10.0, signed=True),
    RegisterSpec(56, "input_energy_total", length=2, scale=10.0, signed=True),
    RegisterSpec(58, "output_reactive_power", length=2, scale=10.0, signed=True),
    RegisterSpec(60, "output_reactive_energy_today", length=2, scale=10.0, signed=True),
    RegisterSpec(62, "output_reactive_energy_total", length=2, scale=10.0, signed=True),
    RegisterSpec(64, "warning_code", ValueKind.RAW, scale=1),
    RegisterSpec(65, "warning_value", ValueKind.RAW, scale=1),
    RegisterSpec(66, "real_output_power_percent", ValueKind.RAW, scale=1),
)

HOLDING_REGISTERS: tuple[RegisterSpec, ...] = (
    RegisterSpec(0, "inverter_enabled", ValueKind.RAW, scale=1),
    RegisterSpec(3, "output_power_limit", ValueKind.RAW, scale=1),
    RegisterSpec(9, "firmware", ValueKind.TEXT, length=6),
    RegisterSpec(23, "serial_number", ValueKind.TEXT, length=5),
    RegisterSpec(73, "modbus_version", scale=100.0),
)
