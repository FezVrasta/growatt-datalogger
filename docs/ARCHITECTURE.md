# Architecture and development

The user-facing documentation is in the [README](../README.md). This is the technical
side: how records are decoded, why the repository is split the way it is, and how to work
on it.

## Records are self-describing

This is the idea the whole project rests on, and it is what makes it small.

A Growatt telemetry record does not just carry values — it states which Modbus registers
those values are:

```
datalogger serial | inverter serial | timestamp | group count | group…
group := start register (2B) | end register (2B) | one 16-bit word per register
```

So the decoder reads register numbers off the wire and looks their meaning up by number.
It never needs a table of byte offsets per inverter model, which is what other
implementations use, and why they need dozens of model-specific layout files plus a
heuristic to guess which one applies to a given packet.

It also means a malformed record fails loudly. The parser checks that every group's word
count matches the range it declared and that the groups consume the payload exactly, so a
record we have misunderstood raises instead of decoding into plausible-looking nonsense.

## Profiles

Register *meaning* still varies by inverter family. Register 13 is PV3 power under
Protocol II, grid frequency on the legacy map, and battery charge power on an off-grid
unit. But the group range identifies the block for free:

| Reported range | Profile |
|---|---|
| ends at 44 | `legacy_315` |
| ends at 124 | `protocol_ii` |
| starts at 1000 | `storage_1000` |
| starts at 3000 | `protocol_ii_3000` |
| starts at 3000 **and** 3125 | `storage_3000` |

Two cases deserve care. A plain string inverter and a hybrid both report 3000–3124, and
several storage registers fall inside that range, so "contains a storage register" does
not discriminate — only a hybrid sends a 3125+ group. And an off-grid SPF reports a
0-based block whose meanings conflict with everything else, with nothing in the record to
distinguish it; that profile is reachable only by explicit override, never inferred.

Only telemetry votes on the profile. An announce carries holding registers over ranges
that are not comparable — a string inverter's announce includes a 3125+ group that looks
exactly like a hybrid's battery block.

## Two register spaces

A device is fed by two kinds of record from two different address spaces. Telemetry
(`0x04`) carries **input** registers; an announce (`0x03`) carries **holding** registers.
Holding 3001 is the serial number while input 3001 is total PV power, so a lookup keyed
on the number alone conflates them.

This also means neither kind of record can be published as a wholesale replacement of a
device's data: each would wipe the other, and since a datalogger announces on every
reconnect, telemetry would survive only seconds at a time.

The separation has to survive into the value names as well. A register with no spec is
published raw, and naming those by number alone puts holding 1100 and input 1100 under one
name where each record overwrites the other — a value that silently means two different
things depending on which record landed last. So unnamed input registers are `register_N`
and unnamed holding registers are `holding_N`.

## What a datalogger expects back

Getting this wrong produces silence rather than errors, which is why it is worth writing
down.

- **Acknowledge before decoding.** The retransmit timer is short; a slow or throwing
  decoder must never delay an acknowledgement.
- **Echo pings byte for byte.**
- **Set the clock after an announce.** A device waits for this, and without it announces,
  waits, gives up and reconnects forever, never sending a single telemetry record.
- **Never acknowledge a command response.** Those are replies to us.
- **Acknowledge unknown record types anyway.** Silence makes a device retransmit and
  eventually drop the connection, which is worse than a possibly-unnecessary reply.
- **Treat the checksum as advisory.** Some dataloggers fail it on every record while
  emitting perfectly decodable payloads; refusing those loses all data from that device.
  Frame length is the real validity gate.

A read reply (`0x05`) echoes the range it was asked for — start *and* end — before any
values. Reading the word straight after the start register gives you the end register,
which on a single-register read looks convincingly like a real value: ask for register 3
and you get back 3.

## Repository layout

```
packages/growatt-protocol/        the protocol, published to PyPI
  src/growatt_protocol/
    *.py                          framing, obfuscation, checksums, records, commands, server
    registers/                    register meaning per inverter family
    testing/                      fake datalogger and fake cloud
    redaction.py                  serial replacement for shareable captures
  tests/                          runs with only pytest, pytest-asyncio and hypothesis

custom_components/growatt_datalogger/   the Home Assistant layer, and nothing else
tests/ha/                         needs pytest-homeassistant-custom-component
tools/                            capture proxy, icon generator, register importer
```

The split is not cosmetic. Everything that knows about the wire is useful to anyone
talking to a Growatt datalogger, Home Assistant or not, and keeping it in its own package
is what lets its dependency list be empty and stay that way.
`packages/growatt-protocol/tests/test_purity.py` walks the AST of every module, fails on
any import outside the standard library, and asserts the declared dependency list is
empty. The integration depends on the package the ordinary way, through `manifest.json`.

## Home Assistant specifics

Devices are discovered, never configured: dataloggers announce themselves, so setup asks
only for a port. Entities are created as values arrive, through a dispatcher signal scoped
by config-entry id — a globally named signal would cross-talk between entries.

Devices and their field sets are persisted, and entities restore their last value, so a
restart after sunset does not blank every dashboard until sunrise.

Entities deliberately never go unavailable from stale data; freshness is exposed as data
instead. `unique_id` excludes the profile name, so correcting a device's profile does not
orphan every entity and recreate it with a `_2` suffix.

Write entities take their value from the announce, which already carries those holding
registers — from its raw words, not from the profile's named values, because a profile
names four holding registers and none of the SPH/SPA storage block. Reading the word is
what makes that free refresh reach every window and SOC limit rather than two entities.
A register no announce reports is asked for directly, but only once a record has proved
the device is connected — reading at entity-add time happens during setup, before any
datalogger has connected, and as a one-shot would never be retried.

That direct read is a one-shot, so the announce is the only thing here that refreshes: a
write entity with no announce behind it shows what its register held at startup until the
integration is reloaded. Where both exist, the newer wins — a read-back taken seconds ago
beats an announce from the last reconnect, or a freshly written switch would snap back to
its old value until the datalogger next connects.

Those reads are batched. Which registers a device wants is known from its profile before
any of them is asked for, so they go out as a handful of range reads rather than one
command each — 27 registers in four commands on a storage inverter, not 27 behind a lock
that spaces commands out. A range a device cannot answer in full comes back as an echo
with no values, which would lose every register in it, so such a range is retried one at a
time: the batch is an optimisation and must never return less than asking singly would.

One entity is usually one register, with two exceptions. A charge or discharge window is
three registers — start, stop, enable — that firmware validates as a unit, so all three go
out as one `0x10` range with the sibling values read back from the inverter first. And a
rejected write costs one extra read before it is reported, so the message can distinguish
a register the model does not have from one it has and would not take this value for.
Which registers belong to which family matters here: the SPH/SPA storage block at
1000–1118 does not exist on a 3000-block hybrid, and offering it there produced entities
whose every write came back "no such register". So every writable register names the
profiles it applies to, with no default — scope used to default to "every family", which
is fail-open, and an off-grid SPF was being offered Protocol II registers on the strength
of it despite that profile declaring no holding table at all. Two tests hold the line:
every named profile must exist, and where a profile's read table and the writable table
share a key they must agree on the address.

## Development

```sh
python3 -m venv .venv
.venv/bin/pip install -e packages/growatt-protocol
.venv/bin/pip install pytest pytest-asyncio hypothesis ruff pytest-homeassistant-custom-component
.venv/bin/python -m pytest tests -q          # the integration
.venv/bin/ruff check . && .venv/bin/ruff format --check .
```

The library stands on its own, and its suite is meant to run in an environment with
neither Home Assistant nor anything else installed:

```sh
cd packages/growatt-protocol && python -m pytest -q
```

Regenerate the icon with `python tools/make_icon.py`; see
[the brand notes](../custom_components/growatt_datalogger/brand/README.md).

## Releasing

`manifest.json` pins an exact version of `growatt-protocol` and Home Assistant installs
exactly that, so a release is two steps, in order:

1. Bump `version` in `packages/growatt-protocol/pyproject.toml` and push a
   `growatt-protocol-v*` tag. The publish workflow builds and uploads to PyPI through
   Trusted Publishing, so there is no token to store.
2. Bump the pin in `custom_components/growatt_datalogger/manifest.json` to match.

CI fails if the two disagree. Shipping a manifest that pins a version older than the code
was written against would silently give every user the wrong library.
