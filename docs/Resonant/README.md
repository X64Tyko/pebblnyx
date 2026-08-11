# Resonant

An RPG for the Pebble Time 2, built on pebblnyx. Chrono Trigger's on-map encounters and
party-combo techs crossed with Legend of Dragoon's timed-input Additions and a
transformation state.

**Premise.** Van, a mercenary, takes a contract to crack a sealed vault in a dead research
facility. The vault's Relic bonds to him instead, revealing that he is a Resonance Vessel --
the thing the facility was built to contain, and the thing his handler was hired to deliver.
Six Nodes remain scattered across the continent. Van needs them to stop destabilising; the
client wants them for something else.

## The documents

| File | What it settles |
|---|---|
| [`Script.md`](Script.md) | Vertical slice dialogue, event by event |
| [`Slice.md`](Slice.md) | The moment-to-moment flow between those events |
| [`Characters.md`](Characters.md) | Who they are, and how each one talks |
| [`World.md`](World.md) | Resonance, the Nodes, the factions, the timeline |
| [`Combat.md`](Combat.md) | Turn structure, Additions, Resonance state, party |
| [`Stats.md`](Stats.md) | The stat model and every formula |
| [`Gear.md`](Gear.md) | Equipment slots, the forge, upgrade paths |
| [`Items.md`](Items.md) | Consumables and the scrap economy |
| [`Flags.md`](Flags.md) | Canonical flag list -- the script disagrees with itself |
| [`Budget.md`](Budget.md) | What the slice costs against 256KB |

## Status

`Script.md` is a V1 draft. Everything else here is a first pass written against it, and
several entries are marked **TBD** where a decision is genuinely open rather than where I
had nothing to write. Names in particular: the Mage has no name in the script, and OPR's
meaning is unstated.

## The rule these documents follow

Every number here is checked against what pebblnyx and the hardware actually do, and says so
when it is a guess instead. Design that ignores the device produces content that cannot ship
-- and content bugs on a watch do not crash, they present as nothing happening. See
[`../MEASUREMENTS.md`](../MEASUREMENTS.md).
