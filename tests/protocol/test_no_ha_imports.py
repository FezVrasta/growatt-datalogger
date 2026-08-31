"""The protocol package must stay free of Home Assistant and third-party imports.

This is what keeps the protocol layer auditable and runnable on its own, and it is what
makes "pure Python, no special dependencies" a property the build checks rather than a
claim in a README.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

PROTOCOL_DIR = (
    Path(__file__).resolve().parents[2] / "custom_components" / "growatt_datalogger" / "protocol"
)

FORBIDDEN_ROOTS = {"homeassistant", "voluptuous", "aiohttp", "paho", "requests", "libscrc"}


def _module_roots(tree: ast.AST) -> set[str]:
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".", 1)[0] for alias in node.names)
        # level > 0 is a relative import, which is always internal.
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".", 1)[0])
    return roots


def _protocol_files() -> list[Path]:
    files = sorted(PROTOCOL_DIR.glob("*.py"))
    assert files, f"no protocol modules found under {PROTOCOL_DIR}"
    return files


@pytest.mark.parametrize("path", _protocol_files(), ids=lambda p: p.name)
def test_module_imports_nothing_forbidden(path: Path) -> None:
    roots = _module_roots(ast.parse(path.read_text()))
    offending = roots & FORBIDDEN_ROOTS
    assert not offending, f"{path.name} imports {sorted(offending)}"


@pytest.mark.parametrize("path", _protocol_files(), ids=lambda p: p.name)
def test_module_imports_only_the_standard_library(path: Path) -> None:
    roots = _module_roots(ast.parse(path.read_text()))
    non_stdlib = roots - sys.stdlib_module_names
    assert not non_stdlib, f"{path.name} imports non-stdlib modules {sorted(non_stdlib)}"
