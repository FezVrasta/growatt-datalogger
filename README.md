<img src="custom_components/growatt_datalogger/brand/icon.png" width="120" align="right" alt="">

# Growatt Datalogger

A Home Assistant integration that **is** the Growatt server. Your datalogger uploads
straight to Home Assistant; nothing has to leave your network.

- **No add-on, no Docker, no MQTT broker.** It is an integration, installed through HACS.
- **No compiled dependencies.** Standard-library Python throughout — the CI job that runs
  the protocol and register suites installs only `pytest`, `pytest-asyncio` and
  `hypothesis`, and a test walks the source to prove it stays that way.
- **No DNS hijacking, no root, no privileged networking.** Dataloggers have a server
  address field; you point it at Home Assistant on TCP 5279.

## Setup

1. Install through HACS as a custom repository, then restart Home Assistant.
2. **Settings → Devices & services → Add integration → Growatt Datalogger.** The only
   question is the port; leave it at 5279 unless something else on the machine uses it.
3. Point the datalogger at Home Assistant:
   - **ShineLan / ShineLan-X** — open the datalogger's IP in a browser, go to the network
     settings, turn **domain-name resolution off**, set the server IP to your Home
     Assistant machine and the port to 5279. Reboot the datalogger; without that it keeps
     talking to the old server and nothing arrives.
   - **ShineWiFi-X / ShineWiFi-S** — in ShinePhone, go to **Me → Datalogger
     configuration**, put the stick into hotspot mode, connect your phone to it, then in
     advanced settings turn off domain-name mode and set the server IP and port.
4. Wait for the next upload — a minute for most dataloggers, longer for some. Devices and
   sensors appear on their own; there is nothing else to configure.

To undo it, put the datalogger's server setting back to `server.growatt.com`.

> By default, pointing the datalogger here means it stops reporting to Growatt, so
> ShinePhone and ShineServer stop updating. If you want both, turn on **Also forward to
> the Growatt cloud** in the integration's options.

## What you get

One device per datalogger, with each inverter as a child device, and sensors for
everything the inverter reports: PV strings, grid, temperatures, and energy counters
classified so the **Energy Dashboard** works without templates.

Sensors deliberately **keep their last value overnight** rather than going unavailable.
Inverters stop reporting when the sun goes down; marking every entity unavailable would
put a gap in every history graph, daily. Freshness is exposed as data instead — a
**Last record** timestamp and a **Connected** binary sensor — so you can alert on
staleness if you want to, without everyone else losing their overnight readings.

There is also a **Sync time** button, a small number of write entities for settings whose
registers Growatt actually documents, and services for reading or writing any register.

## Options

| Option | Default | What it does |
|---|---|---|
| Expose unrecognised registers | off | Creates a disabled diagnostic sensor for every register this integration cannot name. Useful for adding support for a new model; noise otherwise. |
| Historical records | Fire an event | What to do with the backlog a datalogger replays after an outage. See below. |
| Also forward to the Growatt cloud | off | Keeps ShinePhone working. |

### Historical records

After an outage a datalogger replays what it buffered. Those records carry a *past*
timestamp, so writing them to live sensors would invent power spikes and corrupt long-term
statistics. They are never merged with live data. By default they fire a
`growatt_datalogger_buffered_record` event carrying the decoded values and their real
timestamp, so an automation can do something useful with them; the alternative is to
discard them.

### Forwarding to the cloud

With forwarding on, this sits in front of Growatt rather than replacing it: packets go
upstream first, then are decoded locally, and Growatt's replies come back untouched.

If Growatt becomes unreachable, Home Assistant takes over answering the datalogger
immediately, so you never lose data to a cloud outage. It keeps answering for the rest of
that connection rather than switching back mid-stream, which would risk a moment where
nobody acknowledges the device.

## Writing settings

Growatt's published protocol documents the inverter holding registers in the low bank and
the storage control block. Those registers get real entities. A great deal of what
circulates about *other* registers is community folklore — correct on one person's
firmware, wrong on another's — so anything in that category is created disabled and only
when you opt in. Every write entity shows where its register's meaning came from in its
attributes.

For anything else there are services:

```yaml
action: growatt_datalogger.read_register
data:
  device_id: <your inverter>
  register: 45
```

```yaml
action: growatt_datalogger.write_register
data:
  device_id: <your inverter>
  register: 3
  value: 80
  confirm: true      # required outside a small known-safe list
```

`confirm` is a deliberate speed bump. Writing the wrong holding register on a grid-tied
inverter can change how it behaves on the grid.

## Adding support for your inverter

If your values look wrong or your model is not decoded, a packet capture is what makes it
fixable:

```sh
python tools/capture.py --out session.jsonl
```

Point the datalogger at the machine running that instead of at Home Assistant. It
forwards to Growatt as normal while recording. **Serial numbers are replaced before
anything is written**, deterministically and at the same length, so the capture still
decodes and can be attached to an issue without publishing your hardware identifiers.

## How it decodes

Growatt telemetry records are self-describing. After the serials and timestamp comes a
count of register groups, and each group states the Modbus register range it carries:

```
datalogger serial | inverter serial | timestamp | group count | group…
group := start register (2B) | end register (2B) | one 16-bit word per register
```

So the decoder reads the register numbers off the wire and looks their meaning up by
number. It does not need a table of byte offsets per inverter model, which is what other
implementations use and why they need dozens of model-specific layout files plus a
heuristic to guess which one applies. It also means a malformed record fails loudly
instead of decoding into plausible-looking nonsense.

Register *meaning* still varies by family — register 13 is PV3 power under one protocol,
grid frequency under another, and battery charge power on an off-grid unit — but the group
range identifies the block for free, so a handful of profiles covers it. The one case that
cannot be inferred is the off-grid SPF series, whose 0-based block is indistinguishable
from Protocol II; that has to be selected by hand.

## Layout

```
custom_components/growatt_datalogger/
  protocol/     wire protocol — no Home Assistant, no third-party imports
  registers/    register meaning per inverter family — same constraint
  brand/        icon, served by Home Assistant 2026.3+
  *.py          the Home Assistant layer
tests/
  protocol/     ┐ run with only pytest, pytest-asyncio and hypothesis
  registers/    ┘
  ha/           needs pytest-homeassistant-custom-component
```

`tests/test_purity.py` walks the AST of everything under `protocol/` and `registers/` and
fails on any import outside the standard library. That is what makes the zero-dependency
claim something the build checks rather than something the README asserts.

## Brand assets

The icon ships inside the integration, at
`custom_components/growatt_datalogger/brand/`, which is how Home Assistant has served
custom-integration brand images since 2026.3. Regenerate it with
`python tools/make_icon.py`; see
[the brand notes](custom_components/growatt_datalogger/brand/README.md).

## Licence and provenance

MIT, except the register definitions, which are Apache-2.0 and derived from
[`Homeassistant-Growatt-Local-Modbus`](https://github.com/WouterTuinstra/Homeassistant-Growatt-Local-Modbus).

This project is **not** derived from `johanmeijer/grott`, which carries no licence. See
[PROVENANCE.md](PROVENANCE.md) for the per-component breakdown of sources and licences.

## Development

```sh
python3 -m venv .venv
.venv/bin/pip install pytest pytest-asyncio hypothesis ruff pytest-homeassistant-custom-component
.venv/bin/python -m pytest tests -q
.venv/bin/ruff check . && .venv/bin/ruff format --check .
```

To check the zero-dependency property the way CI does, in an environment without Home
Assistant:

```sh
.venv/bin/python -m pytest tests/protocol tests/registers tests/test_purity.py -q
```
