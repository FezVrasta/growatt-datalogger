# Derived from Homeassistant-Growatt-Local-Modbus, Apache License 2.0.
# See LICENSE-APACHE, NOTICE and PROVENANCE.md. Modifications are described in
# tools/import_registers.py, which generates this file.
#
# GENERATED FILE -- do not edit by hand. Regenerate with:
#     python tools/import_registers.py <path to a Growatt-Local checkout>
"""Storage and hybrid registers (SPH / SPA / MIX).

These overlay a base inverter map rather than replacing it: a hybrid reports
the usual PV and grid telemetry plus a battery block."""

from __future__ import annotations

from ..base import RegisterSpec, ValueKind

INPUT_REGISTERS_1000: tuple[RegisterSpec, ...] = (
    RegisterSpec(1014, "soc", ValueKind.RAW, scale=1),
    RegisterSpec(1009, "discharge_power", length=2, scale=10.0, signed=True),
    RegisterSpec(1011, "charge_power", length=2, scale=10.0, signed=True),
    RegisterSpec(1044, "energy_to_user_today", length=2, scale=10.0, signed=True),
    RegisterSpec(1046, "energy_to_user_total", length=2, scale=10.0, signed=True),
    RegisterSpec(1048, "energy_to_grid_today", length=2, scale=10.0, signed=True),
    RegisterSpec(1050, "energy_to_grid_total", length=2, scale=10.0, signed=True),
    RegisterSpec(1052, "discharge_energy_today", length=2, scale=10.0, signed=True),
    RegisterSpec(1054, "discharge_energy_total", length=2, scale=10.0, signed=True),
    RegisterSpec(1056, "charge_energy_today", length=2, scale=10.0, signed=True),
    RegisterSpec(1058, "charge_energy_total", length=2, scale=10.0, signed=True),
    RegisterSpec(1021, "pac_to_user_total", length=2, scale=10.0, signed=True),
    RegisterSpec(1029, "pac_to_grid_total", length=2, scale=10.0, signed=True),
)

INPUT_REGISTERS_3000: tuple[RegisterSpec, ...] = (
    RegisterSpec(3171, "soc", ValueKind.RAW, scale=1),
    RegisterSpec(3178, "discharge_power", length=2, scale=10.0, signed=True),
    RegisterSpec(3180, "charge_power", length=2, scale=10.0, signed=True),
    RegisterSpec(3041, "power_to_user", length=2, scale=10.0, signed=True),
    RegisterSpec(3043, "power_to_grid", length=2, scale=10.0, signed=True),
    RegisterSpec(3045, "power_user_load", length=2, scale=10.0, signed=True),
    RegisterSpec(3067, "energy_to_user_today", length=2, scale=10.0, signed=True),
    RegisterSpec(3069, "energy_to_user_total", length=2, scale=10.0, signed=True),
    RegisterSpec(3071, "energy_to_grid_today", length=2, scale=10.0, signed=True),
    RegisterSpec(3073, "energy_to_grid_total", length=2, scale=10.0, signed=True),
    RegisterSpec(3125, "discharge_energy_today", length=2, scale=10.0, signed=True),
    RegisterSpec(3127, "discharge_energy_total", length=2, scale=10.0, signed=True),
    RegisterSpec(3129, "charge_energy_today", length=2, scale=10.0, signed=True),
    RegisterSpec(3131, "charge_energy_total", length=2, scale=10.0, signed=True),
)

HOLDING_REGISTERS: tuple[RegisterSpec, ...] = (
    RegisterSpec(0, "inverter_enabled", ValueKind.RAW, scale=1),
    RegisterSpec(3, "output_power_limit", ValueKind.RAW, scale=1),
    RegisterSpec(9, "firmware", ValueKind.TEXT, length=6),
    RegisterSpec(3001, "serial_number", ValueKind.TEXT, length=15),
    RegisterSpec(88, "modbus_version", scale=100.0),
    RegisterSpec(3049, "ac_charge_enabled", ValueKind.RAW, scale=1),
)
