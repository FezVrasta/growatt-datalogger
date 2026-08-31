# Migrating from another Growatt integration

You have years of solar production in Home Assistant and you do not want to start again.
You do not have to: everything can be carried over, and for most people the whole
migration is one service call per sensor.

## What "carrying it over" actually means

Home Assistant keys history and long-term statistics by **entity id**, not by entity.
Nothing in the database says which integration produced `sensor.solar_lifetime_energy` —
only that this id has been recorded since 2021 and how much energy it has counted.

So the migration is a rename. When an entity takes over an id, Home Assistant moves its
history and its long-term statistics along with it, and everything that referred to the
old id — the Energy Dashboard, automations, cards, templates — goes on referring to
something that exists. That is a documented Home Assistant behaviour, not a trick.

The `growatt_datalogger.adopt_history` action performs the rename, having first checked
the things that quietly break a migration. You can do the same by hand in the entity's
settings; the action exists because those checks are worth having.

There is one condition, and it is the whole reason this needs a procedure: **an entity id
has exactly one owner**. The old sensor must be gone before the new one can take its id —
and its history stays in the database when it goes, which is precisely what makes it
adoptable.

## Run both first

Do not delete anything until you can see this integration working. Whether you can run
the old one alongside it depends on where it got its data:

**From the Growatt cloud** (the built-in `growatt_server` integration). Turn on **Also
forward to the Growatt cloud** in this integration's options. Records continue to reach
Growatt, so the cloud integration — and ShinePhone — carry on as if nothing changed.

**From Grott.** Same option, but point it at Grott instead of at Growatt: set the Growatt
server to Grott's address and port. The datalogger uploads to Home Assistant, Home
Assistant forwards to Grott, and Grott forwards to Growatt. Every consumer stays fed
while you compare the two. If Grott runs on the same machine, the two cannot both listen
on 5279 — give one of them another port.

**Straight from the inverter over Modbus** (Growatt Local, or a Modbus RTU/TCP setup of
your own). Nothing to do — that path does not involve the datalogger at all, and the two
integrations do not interfere.

Give it a day. Compare the numbers, especially the lifetime energy counters, and check
that the sensors you actually depend on exist here too.

## Which sensor becomes which

This integration names a device `Growatt Inverter <serial>` and derives entity ids from
it, so a value called `output_energy_total` arrives as
`sensor.growatt_inverter_sml0examp2_output_energy_total`.

These are the ones worth being careful about, because they are what the Energy Dashboard
consumes:

| What it is | Here | Usually called |
|---|---|---|
| Lifetime PV production | `output_energy_total` | "Lifetime energy output", `pvenergytotal` |
| PV production today | `output_energy_today` | "Energy today", `pvenergytoday` |
| Imported from the grid | `energy_to_user_total`, `energy_to_user_today` | "Import from grid", `etouser_tot` |
| Exported to the grid | `energy_to_grid_total`, `energy_to_grid_today` | "Export to grid", `etogrid_tot` |
| Battery charged | `charge_energy_total`, `charge_energy_today` | "Lifetime battery charged" |
| Battery discharged | `discharge_energy_total`, `discharge_energy_today` | "Lifetime battery discharged" |
| Battery state of charge | `soc` | "State of charge" |

Everything else — per-string voltages and currents, per-phase output, temperatures — maps
by meaning, and the names are close enough to read off. Coming from **Growatt Local
Modbus** they are not merely close: this integration's register tables are generated from
that project's, so the value names are the same ones, and mapping is by inspection.

Only sensors that record long-term statistics are worth adopting: energy counters, power,
voltage, temperature — anything with a state class. Diagnostics like firmware version
have no history to carry.

## Doing it

**1. Write down the pairs.** For each sensor you care about, the old entity id and the
new one. Developer tools → Statistics lists every id that has recorded history, which is
the authoritative list of what there is to move.

**2. Delete the old integration.** Settings → Devices & services → the old entry → Delete.
For Grott, that means removing the MQTT sensors from your YAML and restarting, or
deleting the discovered device. Its statistics stay behind; they are what you are about
to adopt.

Disabling is not enough. A disabled entity still owns its id.

**3. Adopt, one sensor at a time:**

```yaml
action: growatt_datalogger.adopt_history
data:
  target_entity: sensor.growatt_inverter_sml0examp2_output_energy_total
  source_entity_id: sensor.growatt_lifetime_energy_output
```

The Growatt entity is now `sensor.growatt_lifetime_energy_output`, with the full series
behind it. Its friendly name does not change, so rename it in the entity's settings if
the old label is what you want to see.

For a batch, a script beats clicking:

```yaml
sequence:
  - repeat:
      for_each:
        - target: sensor.growatt_inverter_sml0examp2_output_energy_total
          source: sensor.growatt_lifetime_energy_output
        - target: sensor.growatt_inverter_sml0examp2_output_energy_today
          source: sensor.growatt_energy_today
        - target: sensor.growatt_inverter_sml0examp2_energy_to_user_total
          source: sensor.growatt_import_from_grid
      sequence:
        - action: growatt_datalogger.adopt_history
          data:
            target_entity: "{{ repeat.item.target }}"
            source_entity_id: "{{ repeat.item.source }}"
```

A pair that fails stops the script there, leaving the pairs after it undone. Everything
before it has already happened, so drop those lines before running the rest — a target
that has already been renamed no longer answers to the id in the script.

**4. Check the Energy Dashboard.** If you adopted the ids it was already configured with,
there is nothing to change and nothing to re-select. Look at the last few days: the graph
should be continuous across the switch.

## What the action refuses, and why

**"… still exists."** Something still owns that id — the old integration was disabled
rather than deleted, or a YAML entity is still defined. History cannot move to an id that
is taken.

**"No long-term statistics exist for …"** There is nothing recorded under that id.
Nearly always a typo; check it against Developer tools → Statistics. If you only want the
id and know there is no history, rename the entity in its settings instead.

**"… are not convertible."** The old series was recorded in units this sensor's readings
cannot be converted into — kWh against W, say, which usually means the pair is wrong.
This one matters: Home Assistant does not treat that as an error at compile time. It logs
a warning and silently stops producing long-term statistics for the entity, which you
would notice weeks later as an Energy Dashboard that stopped filling in. Wh against kWh
is fine — convertible units are converted, and the series keeps the unit it started with.

## Afterwards

**A log line about a statistic that could not be renamed.** Expected, and harmless:

```
Cannot rename statistic_id `sensor.growatt_inverter_…` to `sensor.growatt_lifetime_…`
because the new statistic_id is already in use
```

That is Home Assistant declining to overwrite the long history with the few days the new
sensor recorded under its own id. Declining is the right outcome. A matching line about
migrating *history* — the short-term state history rather than the statistics — means the
same thing, and is equally harmless.

**The series left behind.** Those few days remain in the database under the Growatt
entity's original id, where nothing can reach them any more. Developer tools → Statistics
will offer to delete them, or pass `discard_target_statistics: true` and the action does
it for you. They cover exactly the period the old sensor was recording in parallel, so
this normally loses nothing.

**A step in the graph.** Total counters continue from where the old series left off, so a
step means the two sensors were not counting the same thing — a lifetime total adopted
onto a daily counter, or a sensor reading a different inverter.

Renaming the entity back is not a clean undo: with nothing standing in the way, the
adopted series simply follows it back. Repair the series instead. Developer tools →
Statistics can adjust a single hour's sum, which is enough to flatten a spike or close a
gap when the two really are the same quantity and merely disagree — an inverter replaced
under the same roof, a counter reset — and can delete a series outright when the pair was
wrong to begin with.

## If you would rather keep the new entity ids

Then there is nothing to adopt. Point the Energy Dashboard at the new sensors and leave
the old statistics where they are: they stay browsable in the history, but the dashboard
will show nothing for the new entities before the day you switched, because for that
period they did not exist.

Adopting the ids is the only way to get one continuous series, which is why it is what
this guide recommends.
