"""Growatt register meanings, per inverter family.

Like :mod:`..protocol`, this package imports nothing outside the standard library, so it
can be tested without Home Assistant. Presentation metadata -- units, device classes --
lives in :mod:`..metadata`, which is the layer that does depend on Home Assistant.
"""

from __future__ import annotations

from .base import (
    Confidence,
    DecodedValues,
    Profile,
    RegisterSpace,
    RegisterSpec,
    ValueKind,
    decode_registers,
)
from .profiles import (
    FALLBACK_PROFILE,
    LEGACY_315,
    MANUAL_ONLY,
    OFFGRID,
    PROFILES,
    PROTOCOL_II,
    PROTOCOL_II_3000,
    STORAGE_1000,
    STORAGE_3000,
    ProfileMatch,
    all_spec_names,
    resolve_profile,
    specs_by_name,
)

__all__ = [
    "FALLBACK_PROFILE",
    "LEGACY_315",
    "MANUAL_ONLY",
    "OFFGRID",
    "PROFILES",
    "PROTOCOL_II",
    "PROTOCOL_II_3000",
    "STORAGE_1000",
    "STORAGE_3000",
    "Confidence",
    "DecodedValues",
    "Profile",
    "ProfileMatch",
    "RegisterSpace",
    "RegisterSpec",
    "ValueKind",
    "all_spec_names",
    "decode_registers",
    "resolve_profile",
    "specs_by_name",
]
