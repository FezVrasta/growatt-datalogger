# growatt-protocol

The protocol a Growatt datalogger speaks to its server over TCP 5279, as a standalone
Python library. Framing, obfuscation, checksums, record decoding, register semantics, and
the commands for reading and writing inverter registers.

**No dependencies.** Standard library only, no compiled extension, so it installs
anywhere Python runs. A test walks the source and fails on any import outside the
standard library, so that stays true.

```sh
pip install growatt-protocol
```

## Records are self-describing

The useful thing about this protocol, and the reason this library is small: a telemetry
record states the Modbus register ranges it carries.

```
datalogger serial | inverter serial | timestamp | group count | group…
group := start register (2B) | end register (2B) | one 16-bit word per register
```

So decoding reads register numbers off the wire rather than indexing a per-model table of
byte offsets. That is what lets a handful of register profiles replace the dozens of
model-specific layout files other implementations need, and it means a malformed record
fails loudly instead of decoding into plausible-looking nonsense.

## Decoding a record

```python
from growatt_protocol import Frame, Framer, parse_register_record
from growatt_protocol.registers import decode_registers, resolve_profile

framer = Framer()  # TCP splits and coalesces; reassembly is required
for raw in framer.feed(chunk):
    payload = parse_register_record(Frame(raw))
    match = resolve_profile([(g.start, g.end) for g in payload.groups])
    decoded = decode_registers(match.profile, payload.registers)

    print(payload.inverter_serial, decoded.values["output_power"], "W")
```

Register *meaning* varies by inverter family — register 13 is PV3 power under one
protocol, grid frequency under another, battery charge power on an off-grid unit — but
the group range identifies the block, so `resolve_profile` picks the right one from the
record itself. The exception is the off-grid SPF series, whose 0-based block is
indistinguishable from Protocol II; that one has to be passed as an `override`.

## Running a server

```python
import asyncio
from growatt_protocol import GrowattServer, ServerConfig


def on_record(record):
    print(record.payload.inverter_serial, record.payload.registers)


async def main():
    server = GrowattServer(ServerConfig(port=5279), on_record=on_record)
    await server.start()
    await asyncio.Event().wait()


asyncio.run(main())
```

The server answers what a datalogger expects: acknowledgements before decoding, ping
echoes, and — importantly — a clock update after the device announces itself. Without
that last one a datalogger announces, waits, gives up and reconnects forever, never
sending a single telemetry record.

## Testing without hardware

```python
from growatt_protocol.testing import FakeDatalogger

device = FakeDatalogger(chunk_size=1)  # one byte per write, to prove reassembly
await device.connect("127.0.0.1", server.port)
await device.send_data()
```

`FakeUpstream` stands in for the Growatt cloud, and can be killed mid-session to exercise
relay fallback.

## License

MIT, except the register definitions under `registers/`, which are Apache-2.0 and derived
from
[`Homeassistant-Growatt-Local-Modbus`](https://github.com/WouterTuinstra/Homeassistant-Growatt-Local-Modbus).
See `NOTICE`. This project is **not** derived from `johanmeijer/grott`, which carries no
license; full provenance is recorded in the
[repository](https://github.com/FezVrasta/growatt-datalogger/blob/main/PROVENANCE.md).
