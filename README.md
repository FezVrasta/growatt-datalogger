# Growatt Datalogger

A Home Assistant custom component that **is** the Growatt server. Your datalogger uploads
to Home Assistant directly; nothing leaves your network.

- **No add-on, no Docker, no MQTT broker.** It is an integration, installed through HACS.
- **No compiled dependencies.** The whole thing is standard-library Python — the CI job
  that runs the protocol suite installs only `pytest` and `hypothesis`.
- **No DNS hijacking, no root, no privileged networking.** Dataloggers have a server
  address field; you point it at Home Assistant on TCP 5279.

Status: **early development.** The protocol layer is implemented and tested; the Home
Assistant integration is not yet usable.

## How it decodes

Growatt telemetry records are self-describing. After the serials and timestamp, a record
carries a count of register groups, and each group states the Modbus register range it
contains:

```
datalogger serial | inverter serial | timestamp | group count | group… 
group := start register (2B) | end register (2B) | one 16-bit word per register
```

So the decoder reads the register numbers off the wire and looks their meaning up by
number. It does not need a table of byte offsets per inverter model, which is what other
implementations use and why they need dozens of model-specific layout files plus a
heuristic to guess which one applies. It also means a malformed record fails loudly
rather than decoding into plausible-looking nonsense.

Register *meaning* still varies by inverter family — register 13 is PV3 power on one
family and grid frequency on another — but the group range identifies the block for free,
so only a handful of profiles are needed rather than a layout per model.

## Layout

```
custom_components/growatt_datalogger/
  protocol/     wire protocol — no Home Assistant, no third-party imports (enforced by a test)
  registers/    register meaning per inverter profile
tests/
  protocol/     runs with only pytest + hypothesis
```

## Licence and provenance

MIT, except the register definitions, which are Apache-2.0 and derived from
[`Homeassistant-Growatt-Local-Modbus`](https://github.com/WouterTuinstra/Homeassistant-Growatt-Local-Modbus).

This project is **not** derived from `johanmeijer/grott`, which carries no licence. See
[PROVENANCE.md](PROVENANCE.md) for the full per-component breakdown.

## Development

```sh
python3 -m venv .venv
.venv/bin/pip install pytest hypothesis ruff
.venv/bin/python -m pytest tests/protocol -q
.venv/bin/ruff check . && .venv/bin/ruff format --check .
```
