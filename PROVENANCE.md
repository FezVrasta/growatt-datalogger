# Provenance

This file records, per component, where the knowledge and code came from and under which licence.
It exists so that the origin of every part of this project is verifiable from the repository
itself rather than from memory.

## Summary

This project is an independent implementation of the Growatt datalogger upload protocol (TCP port
5279) and of the Growatt Modbus register semantics. It is built from:

1. Permissively licensed prior art (BSD-3-Clause, MIT, Apache-2.0),
2. Growatt's own published Modbus RTU protocol specifications, and
3. Packet captures taken by the author from their own hardware.

**It is not derived from [`johanmeijer/grott`](https://github.com/johanmeijer/grott).** That project
carries no licence file, which under default copyright means all rights are reserved. Its author has
publicly declined to add one
([grott#512](https://github.com/johanmeijer/grott/issues/512)). No grott code, data table,
identifier name, or file layout is reproduced here.

## Sources

| Component | Derived from | Licence | Notes |
|---|---|---|---|
| `protocol/crc.py` | CRC-16/MODBUS is a published algorithm (reflected polynomial `0xA001`, init `0xFFFF`). Implemented from the algorithm definition. | n/a — algorithm | Growatt appends the checksum big-endian, unlike Modbus RTU serial framing. Verified against the standard `"123456789"` → `0x4B37` vector. |
| `protocol/crypt.py` | Protocol description in [nwf's Growatt protocol notes](https://www.ietfng.org/nwf/misc/growatt-protocol.html) (prose) and [`aaronjbrown/PyGrowatt`](https://github.com/aaronjbrown/PyGrowatt) | BSD-3-Clause | XOR with the repeating ASCII key `Growatt`, applied from byte offset 8 onward. |
| `protocol/framing.py` | Frame-length rule from the header description in the same sources; reassembly logic is original. | BSD-3-Clause (concept) | |
| `protocol/records.py` | Payload structure (serials, timestamp, register-group count, `(start, end)` group headers) from nwf's notes, [`aaronjbrown/PyGrowatt`](https://github.com/aaronjbrown/PyGrowatt) and [`akupila/wireshark-growatt`](https://github.com/akupila/wireshark-growatt); confirmed against the author's own captures. | BSD-3-Clause / MIT | |
| `registers/*` | [`WouterTuinstra/Homeassistant-Growatt-Local-Modbus`](https://github.com/WouterTuinstra/Homeassistant-Growatt-Local-Modbus), which itself cites the Growatt specifications below. | **Apache-2.0** | See "Apache-2.0 notice" below. |
| Register semantics | Growatt *Inverter Modbus RTU Protocol II* (V1.20 / V1.24), *PV Inverter Modbus RS485 RTU Protocol* (v3.x), *OffGrid SPF5000 Modbus RS485 RTU Protocol* | Vendor specification | Register numbers, scales and units are manufacturer-published facts. |
| Test fixtures | Packet captures taken by the author from their own ShineLan-X datalogger, pseudonymised by `tools/capture.py`. | This project | |

## Apache-2.0 notice

Parts of `custom_components/growatt_datalogger/registers/` are derived from
`WouterTuinstra/Homeassistant-Growatt-Local-Modbus`, licensed under the Apache License, Version 2.0.
A copy is provided in [`LICENSE-APACHE`](LICENSE-APACHE). That project contains no `NOTICE` file.

Changes made to the derived material, as required by Apache-2.0 §4(b):

- Restructured from `pymodbus`-oriented register definitions into a transport-independent
  `RegisterSpec` table keyed by `(profile, register)`.
- Added a `confidence` field distinguishing registers documented in the vendor specification from
  community-reported ones.
- Added profile resolution driven by the register-group ranges present in an upload record, which
  has no counterpart in the original (it polls over Modbus rather than receiving uploads).
- Home Assistant entity metadata rewritten for this integration's entity model.

Individual derived files carry an Apache-2.0 header identifying them as such.

## Hygiene

- This repository was developed without any grott checkout open.
- Identifier names follow the Growatt specification and the Apache-2.0 `ATTR_*` vocabulary, not
  grott's field names.
- Nothing under `protocol/` imports Home Assistant; that is enforced by a test, so the protocol
  layer can be audited and run standalone.
