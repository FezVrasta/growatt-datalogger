"""The protocol and register packages must import nothing but the standard library.

This is what keeps those layers auditable and runnable on their own, and it is what makes
"pure Python, no special dependencies" a property the build checks rather than a claim in
a README. It is also the boundary that keeps Home Assistant concerns -- units, device
classes, entity descriptions -- out of the decoding layers.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parent.parent / "custom_components" / "growatt_datalogger"

#: Packages whose presence would defeat the point, called out by name so a failure says
#: what went wrong rather than just "not in the standard library".
FORBIDDEN_ROOTS = {
    "homeassistant",
    "voluptuous",
    "aiohttp",
    "paho",
    "requests",
    "libscrc",
    "pymodbus",
}

PURE_SUBPACKAGES = ("protocol", "registers")


def _module_roots(tree: ast.AST) -> set[str]:
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".", 1)[0] for alias in node.names)
        # level > 0 is a relative import, which is always internal.
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".", 1)[0])
    return roots


def _pure_modules() -> list[Path]:
    files = sorted(path for name in PURE_SUBPACKAGES for path in (PACKAGE / name).rglob("*.py"))
    assert files, f"no modules found under {PACKAGE}"
    return files


def _module_id(path: Path) -> str:
    return str(path.relative_to(PACKAGE))


@pytest.mark.parametrize("path", _pure_modules(), ids=_module_id)
def test_module_imports_nothing_forbidden(path: Path) -> None:
    roots = _module_roots(ast.parse(path.read_text()))
    offending = roots & FORBIDDEN_ROOTS
    assert not offending, f"{_module_id(path)} imports {sorted(offending)}"


@pytest.mark.parametrize("path", _pure_modules(), ids=_module_id)
def test_module_imports_only_the_standard_library(path: Path) -> None:
    roots = _module_roots(ast.parse(path.read_text()))
    non_stdlib = roots - sys.stdlib_module_names
    assert not non_stdlib, f"{_module_id(path)} imports non-stdlib {sorted(non_stdlib)}"


def test_both_pure_subpackages_were_actually_scanned() -> None:
    """Guards against a rename silently emptying the scan and passing vacuously."""
    scanned = {path.relative_to(PACKAGE).parts[0] for path in _pure_modules()}
    assert scanned == set(PURE_SUBPACKAGES)
