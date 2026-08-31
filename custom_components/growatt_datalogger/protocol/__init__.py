"""Growatt datalogger wire protocol.

This package is deliberately free of Home Assistant imports and of any third-party
dependency, so it can be tested and audited on its own. A test enforces that.
"""

from __future__ import annotations

from .crc import append_crc, check_crc, modbus_crc
from .crypt import (
    KEY,
    OBFUSCATED_PROTOCOLS,
    SUPPORTED_PROTOCOLS,
    deobfuscate,
    obfuscate,
    xor_payload,
)
from .errors import (
    CommandTimeout,
    FrameError,
    GrowattProtocolError,
    RecordError,
)
from .framing import DEFAULT_MAX_FRAME, Framer, frame_length
from .records import (
    COMMAND_RESPONSE_FUNCTIONS,
    METER_FUNCTIONS,
    REGISTER_RECORD_FUNCTIONS,
    Frame,
    Function,
    RecordPayload,
    RegisterGroup,
    build_ack,
    build_ping_echo,
    parse_register_record,
)

__all__ = [
    "COMMAND_RESPONSE_FUNCTIONS",
    "DEFAULT_MAX_FRAME",
    "KEY",
    "METER_FUNCTIONS",
    "OBFUSCATED_PROTOCOLS",
    "REGISTER_RECORD_FUNCTIONS",
    "SUPPORTED_PROTOCOLS",
    "CommandTimeout",
    "Frame",
    "FrameError",
    "Framer",
    "Function",
    "GrowattProtocolError",
    "RecordError",
    "RecordPayload",
    "RegisterGroup",
    "append_crc",
    "build_ack",
    "build_ping_echo",
    "check_crc",
    "deobfuscate",
    "frame_length",
    "modbus_crc",
    "obfuscate",
    "parse_register_record",
    "xor_payload",
]
