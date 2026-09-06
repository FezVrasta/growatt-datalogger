"""Fakes for driving the protocol without hardware.

Shipped with the library rather than kept in its test suite, because anyone building on
this needs the same things: a client that speaks the real wire format, frame builders,
and a stand-in for the Growatt cloud. They are also what the Home Assistant integration's
own tests use.

The fake datalogger can misbehave the way real ones do -- fragmenting a record across
many small writes, or coalescing several into one -- which is what catches an
implementation that assumes one read equals one record.
"""

from __future__ import annotations

from .datalogger import FakeDatalogger
from .frames import (
    build_command_response,
    build_data_record,
    build_frame,
    build_group,
    build_range_write_response,
    build_read_response,
    build_register_body,
    build_write_response,
)
from .inverter import FakeInverter, request_register, request_values
from .upstream import FakeUpstream

__all__ = [
    "FakeDatalogger",
    "FakeInverter",
    "FakeUpstream",
    "build_command_response",
    "build_data_record",
    "build_frame",
    "build_group",
    "build_range_write_response",
    "build_read_response",
    "build_register_body",
    "build_write_response",
    "request_register",
    "request_values",
]
