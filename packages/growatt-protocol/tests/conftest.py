"""The library's tests run without Home Assistant, and without its test plugin."""

from __future__ import annotations

import sys
from pathlib import Path

# Allow running the suite straight from a checkout, before an editable install.
SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
