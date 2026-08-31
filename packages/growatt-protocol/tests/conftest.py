"""The library's tests run without Home Assistant, and without its test plugin.

They should also run when someone has both installed in one environment, which is the
normal case for anyone working on the integration too. The Home Assistant test plugin
auto-loads whenever it is importable and blocks real sockets, so the socket-based tests
here need it switched off explicitly.
"""

from __future__ import annotations

import sys
from collections.abc import Generator
from importlib.util import find_spec
from pathlib import Path

import pytest

# Allow running the suite straight from a checkout, before an editable install.
SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


if find_spec("pytest_homeassistant_custom_component") is not None:

    @pytest.fixture(autouse=True)
    def _allow_real_sockets(socket_enabled: None) -> Generator[None]:
        """This package *is* a socket server; its tests bind loopback and drive it.

        Faking the transport would leave the framing, the acknowledgement timing and the
        reassembly untested, which are the parts most likely to be wrong.
        """
        yield
