"""Shared configuration for the Home Assistant integration tests.

The protocol and register layers live in the ``growatt-protocol`` package and are tested
there, without Home Assistant installed. What is left here needs Home Assistant, so it is
skipped when the test plugin is absent rather than failing to import.
"""

from __future__ import annotations

import sys
from collections.abc import Generator
from importlib.util import find_spec
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

HAS_HOMEASSISTANT = find_spec("pytest_homeassistant_custom_component") is not None

pytest_plugins: list[str] = ["pytest_homeassistant_custom_component"] if HAS_HOMEASSISTANT else []

collect_ignore_glob: list[str] = [] if HAS_HOMEASSISTANT else ["ha/*"]


if HAS_HOMEASSISTANT:
    # The Home Assistant test plugin blocks real sockets, which is the right default for
    # an integration that talks to hardware through a mocked client. This one runs a
    # socket server: its tests bind loopback on an OS-assigned port and drive it with a
    # real client, because faking the transport would leave the framing, the ACK timing
    # and the reassembly untested.
    @pytest.fixture(autouse=True)
    def _allow_real_sockets(socket_enabled: None) -> Generator[None]:
        yield
