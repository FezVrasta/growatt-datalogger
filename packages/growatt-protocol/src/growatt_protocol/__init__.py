"""The Growatt datalogger upload protocol, and what its registers mean.

A standalone, dependency-free implementation of the protocol a Growatt datalogger speaks
to its server over TCP 5279: framing, obfuscation, checksums, record decoding, and the
commands for reading and writing inverter registers. Nothing here imports Home Assistant
or anything outside the standard library, and a test enforces both.

Records are self-describing -- each one states the Modbus register ranges it carries --
so decoding reads register numbers off the wire rather than indexing a per-model table of
byte offsets::

    from growatt_protocol import Frame, Framer, parse_register_record
    from growatt_protocol.registers import decode_registers, resolve_profile

    framer = Framer()
    for raw in framer.feed(data):
        payload = parse_register_record(Frame(raw))
        match = resolve_profile([(g.start, g.end) for g in payload.groups])
        values = decode_registers(match.profile, payload.registers).values

For a whole server, :class:`GrowattServer` accepts connections and hands decoded records
to a callback. :mod:`growatt_protocol.testing` provides a fake datalogger to drive it
without hardware.
"""

from __future__ import annotations

from . import registers, testing
from .commands import (
    REGISTER_TIME,
    Command,
    CommandResponse,
    parse_command_response,
    read_datalogger,
    read_inverter,
    set_time,
    write_datalogger,
    write_inverter,
    write_inverter_range,
)
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
from .relay import RelayConfig, RelayConnection
from .server import DEFAULT_PORT, GrowattServer, ServerConfig, ServerStats
from .session import Record, Session, SessionStats

__version__ = "0.1.0"

__all__ = [
    "COMMAND_RESPONSE_FUNCTIONS",
    "DEFAULT_MAX_FRAME",
    "DEFAULT_PORT",
    "KEY",
    "METER_FUNCTIONS",
    "OBFUSCATED_PROTOCOLS",
    "REGISTER_RECORD_FUNCTIONS",
    "REGISTER_TIME",
    "SUPPORTED_PROTOCOLS",
    "Command",
    "CommandResponse",
    "CommandTimeout",
    "Frame",
    "FrameError",
    "Framer",
    "Function",
    "GrowattProtocolError",
    "GrowattServer",
    "Record",
    "RecordError",
    "RecordPayload",
    "RegisterGroup",
    "RelayConfig",
    "RelayConnection",
    "ServerConfig",
    "ServerStats",
    "Session",
    "SessionStats",
    "__version__",
    "append_crc",
    "build_ack",
    "build_ping_echo",
    "check_crc",
    "deobfuscate",
    "frame_length",
    "modbus_crc",
    "obfuscate",
    "parse_command_response",
    "parse_register_record",
    "read_datalogger",
    "read_inverter",
    "registers",
    "set_time",
    "testing",
    "write_datalogger",
    "write_inverter",
    "write_inverter_range",
    "xor_payload",
]
