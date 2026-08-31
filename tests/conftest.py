"""Shared test configuration.

The protocol and register suites must run in an environment with no Home Assistant
installed -- that is the point of the minimal CI job -- so the Home Assistant test plugin
is loaded only when it is actually present, and the tests that need it are skipped
otherwise. pytest requires ``pytest_plugins`` to be declared here, in the top-level
conftest, which is why the conditional lives at this level rather than in ``tests/ha``.
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
    # The Home Assistant test plugin blocks real sockets, which is a sensible default
    # for integrations that talk to hardware over a mocked client. This one *is* a
    # socket server: its tests bind loopback on an OS-assigned port and drive it with a
    # real client, because faking the transport would leave the framing, the ACK timing
    # and the reassembly -- the parts most likely to be wrong -- untested.
    @pytest.fixture(autouse=True)
    def _allow_real_sockets(socket_enabled: None) -> Generator[None]:
        yield
