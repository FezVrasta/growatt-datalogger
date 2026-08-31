"""The package must import nothing but the standard library.

This is the property the whole package exists to offer: talking to a Growatt datalogger
should need nothing installed beyond Python itself. Checking it here makes it something
the build verifies rather than something the README claims, and it is also what keeps
Home Assistant concerns -- units, device classes, entity descriptions -- from leaking in.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parent.parent / "src" / "growatt_protocol"

#: Called out by name so a failure says what went wrong, rather than merely
#: "not in the standard library".
FORBIDDEN_ROOTS = {
    "homeassistant",
    "voluptuous",
    "aiohttp",
    "paho",
    "requests",
    "libscrc",
    "pymodbus",
}


def _module_roots(tree: ast.AST) -> set[str]:
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".", 1)[0] for alias in node.names)
        # level > 0 is a relative import, which is always internal.
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".", 1)[0])
    return roots


def _modules() -> list[Path]:
    files = sorted(PACKAGE.rglob("*.py"))
    assert files, f"no modules found under {PACKAGE}"
    return files


def _module_id(path: Path) -> str:
    return str(path.relative_to(PACKAGE))


@pytest.mark.parametrize("path", _modules(), ids=_module_id)
def test_module_imports_nothing_forbidden(path: Path) -> None:
    offending = _module_roots(ast.parse(path.read_text())) & FORBIDDEN_ROOTS
    assert not offending, f"{_module_id(path)} imports {sorted(offending)}"


@pytest.mark.parametrize("path", _modules(), ids=_module_id)
def test_module_imports_only_the_standard_library(path: Path) -> None:
    roots = _module_roots(ast.parse(path.read_text()))
    non_stdlib = roots - sys.stdlib_module_names - {"growatt_protocol"}
    assert not non_stdlib, f"{_module_id(path)} imports non-stdlib {sorted(non_stdlib)}"


def test_the_declared_dependencies_are_empty() -> None:
    """The promise in pyproject.toml, checked against the file itself."""
    import tomllib

    pyproject = PACKAGE.parent.parent / "pyproject.toml"
    project = tomllib.loads(pyproject.read_text())["project"]
    assert project["dependencies"] == []


def test_every_subpackage_was_scanned() -> None:
    """Guards against a rename quietly emptying the scan and passing vacuously."""
    scanned = {path.relative_to(PACKAGE).parts[0] for path in _modules()}
    assert {"registers", "testing"} <= scanned
