#!/usr/bin/env python3
"""Regenerate the register tables from a Homeassistant-Growatt-Local-Modbus checkout.

The register definitions in ``custom_components/growatt_datalogger/registers/tables/``
are derived from that project, which is Apache-2.0 licensed and which in turn cites
Growatt's published Modbus RTU protocol documents. This script performs the conversion
so the derivation is reproducible and reviewable in a diff, rather than being a one-off
manual transcription nobody can check.

Usage::

    git clone https://github.com/WouterTuinstra/Homeassistant-Growatt-Local-Modbus /tmp/gl
    python tools/import_registers.py /tmp/gl

The upstream model is ``GrowattDeviceRegisters(name, register, value_type, length,
scale, function)`` and its decoder (``API/utils.py::process_registers``) treats:

* ``int``   -- raw, unscaled
* ``float`` with ``length == 2`` -- signed 32-bit, divided by ``scale``
* ``float`` with ``length == 1`` -- unsigned 16-bit, divided by ``scale``
* ``str``   -- two ASCII characters per register
* ``custom_function`` -- a Python callable

Conversions and deliberate divergences are listed in ``_TRANSFORM_NOTES`` and reflected
in PROVENANCE.md, as Apache-2.0 section 4(b) requires.
"""

from __future__ import annotations

import argparse
import importlib.util
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = REPO_ROOT / "custom_components" / "growatt_datalogger" / "registers" / "tables"

_TRANSFORM_NOTES = """\
Divergences from the upstream definitions, applied by tools/import_registers.py:

* ``custom_function`` entries are dropped. They exist to turn a device-type code into a
  human-readable string during Modbus discovery; that is a presentation concern here and
  is handled in the Home Assistant layer.
* Names are normalised to identifiers: "serial number" becomes "serial_number".
* ``int`` entries carry ``scale=1`` explicitly rather than an inherited default of 10
  that the upstream decoder happens to ignore for that type. The stored value is
  unchanged; the table now states what it means.
* 32-bit runs are marked ``signed=True`` and 16-bit values ``signed=False``, matching the
  upstream decoder's behaviour but as data rather than as a rule inferred from length.
* Two entries in ``INPUT_REGISTERS_120_TL_XH`` -- input_4_energy_today at register 71 and
  input_4_energy_total at register 73 -- are dropped. That table is otherwise entirely in
  the 3000 block, so 0-block register numbers there are an upstream transcription slip.
  The correct values are not guessed: 3067/3069 would follow the input_1..3 pattern but
  collide with energy_to_user_today/total in the storage overlay, so the registers are
  left unnamed and will surface in diagnostics if a device reports them.
"""

# (output module, docstring, [(constant name, upstream module, upstream attribute)])
_TABLES: list[tuple[str, str, list[tuple[str, str, str]]]] = [
    (
        "protocol_ii",
        "Growatt Inverter Modbus RTU Protocol II.\n\n"
        "Two distinct layouts, not one rebased on the other: the 0-block carries eight\n"
        "MPPT inputs and the 3000-block four, so the fields after the PV inputs sit at\n"
        "different offsets in each.",
        [
            ("INPUT_REGISTERS", "inverter_120", "INPUT_REGISTERS_120"),
            ("INPUT_REGISTERS_3000", "inverter_120", "INPUT_REGISTERS_120_TL_XH"),
            ("HOLDING_REGISTERS", "inverter_120", "HOLDING_REGISTERS_120"),
        ],
    ),
    (
        "legacy_315",
        "The older Growatt PV Inverter Modbus RS485 RTU Protocol (-S / MTL-S).\n\n"
        "Shares register numbers with Protocol II but not their meanings: register 11 is\n"
        "PV3 voltage there and total output power here.",
        [
            ("INPUT_REGISTERS", "inverter_315", "INPUT_REGISTERS_315"),
            ("HOLDING_REGISTERS", "inverter_315", "HOLDING_REGISTERS_315"),
        ],
    ),
    (
        "storage",
        "Storage and hybrid registers (SPH / SPA / MIX).\n\n"
        "These overlay a base inverter map rather than replacing it: a hybrid reports\n"
        "the usual PV and grid telemetry plus a battery block.",
        [
            ("INPUT_REGISTERS_1000", "storage_120", "STORAGE_INPUT_REGISTERS_120"),
            ("INPUT_REGISTERS_3000", "storage_120", "STORAGE_INPUT_REGISTERS_120_TL_XH"),
            ("HOLDING_REGISTERS", "storage_120", "STORAGE_HOLDING_REGISTERS_120"),
        ],
    ),
    (
        "offgrid",
        "Off-grid SPF series.\n\n"
        "A 0-based block whose meanings conflict with every other family -- register 13\n"
        "is battery charge power here and PV3 power under Protocol II -- and which the\n"
        "record itself gives no way to distinguish. Selecting this profile requires\n"
        "out-of-band knowledge of the device.",
        [("INPUT_REGISTERS", "offgrid", "INPUT_REGISTERS_OFFGRID")],
    ),
]

_DROPPED = {("INPUT_REGISTERS_3000", 71), ("INPUT_REGISTERS_3000", 73)}


def _load_upstream(checkout: Path) -> Any:
    """Import the upstream device_type package from a checkout."""
    device_type = checkout / "custom_components" / "growatt_local" / "API" / "device_type"
    if not device_type.is_dir():
        raise SystemExit(f"not a Growatt-Local checkout: {checkout}")

    package_root = device_type.parent.parent.parent  # .../custom_components
    sys.path.insert(0, str(package_root.parent))
    sys.path.insert(0, str(device_type))
    return device_type


def _import_module(device_type: Path, name: str) -> Any:
    """Import one upstream module, satisfying its relative import of ``base``."""
    if "base" not in sys.modules:
        spec = importlib.util.spec_from_file_location("base", device_type / "base.py")
        assert spec and spec.loader
        base = importlib.util.module_from_spec(spec)
        sys.modules["base"] = base
        spec.loader.exec_module(base)

    source = (
        (device_type / f"{name}.py").read_text().replace("from .base import", "from base import")
    )
    spec = importlib.util.spec_from_loader(name, loader=None)
    assert spec
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    exec(compile(source, str(device_type / f"{name}.py"), "exec"), module.__dict__)
    return module


def _normalise(name: str) -> str:
    return name.strip().lower().replace(" ", "_")


def _render(entry: Any, constant: str) -> str | None:
    type_name = getattr(entry.value_type, "__name__", str(entry.value_type))

    if type_name == "custom_function":
        return None
    if (constant, entry.register) in _DROPPED:
        return None

    name = _normalise(entry.name)
    length = entry.length

    if type_name == "str":
        return f"    RegisterSpec({entry.register}, {name!r}, ValueKind.TEXT, length={length}),"
    if type_name == "int":
        return f"    RegisterSpec({entry.register}, {name!r}, ValueKind.RAW, scale=1),"

    signed = length == 2
    parts = [f"    RegisterSpec({entry.register}, {name!r}"]
    if length != 1:
        parts.append(f"length={length}")
    parts.append(f"scale={float(entry.scale)}")
    if signed:
        parts.append("signed=True")
    return ", ".join(parts) + "),"


def _render_module(module_name: str, doc: str, tables: list[tuple[str, Any]]) -> str:
    lines = [
        "# Derived from Homeassistant-Growatt-Local-Modbus, Apache License 2.0.",
        "# See LICENSE-APACHE, NOTICE and PROVENANCE.md. Modifications are described in",
        "# tools/import_registers.py, which generates this file.",
        "#",
        "# GENERATED FILE -- do not edit by hand. Regenerate with:",
        "#     python tools/import_registers.py <path to a Growatt-Local checkout>",
        '"""' + doc + '"""',
        "",
        "from __future__ import annotations",
        "",
        "from ..base import RegisterSpec, ValueKind",
        "",
    ]

    for constant, entries in tables:
        rendered = [line for entry in entries if (line := _render(entry, constant))]
        lines.append(f"{constant}: tuple[RegisterSpec, ...] = (")
        lines.extend(rendered)
        lines.append(")")
        lines.append("")

    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("checkout", type=Path, help="path to a Growatt-Local checkout")
    args = parser.parse_args()

    device_type = _load_upstream(args.checkout)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    for module_name, doc, table_specs in _TABLES:
        tables = []
        for constant, upstream_module, attribute in table_specs:
            module = _import_module(device_type, upstream_module)
            tables.append((constant, getattr(module, attribute)))

        target = OUTPUT_DIR / f"{module_name}.py"
        target.write_text(_render_module(module_name, doc, tables))
        print(f"wrote {target.relative_to(REPO_ROOT)}")

    (OUTPUT_DIR / "__init__.py").write_text(
        '"""Generated register tables. See tools/import_registers.py."""\n'
    )

    try:
        subprocess.run(["ruff", "format", "-q", str(OUTPUT_DIR)], check=False)
    except FileNotFoundError:
        print("ruff not found; generated files are unformatted", file=sys.stderr)

    print("\n" + _TRANSFORM_NOTES)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
