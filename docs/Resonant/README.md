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

`Script.md` is a V1 draft. Everything else is a first pass written against it. The Mage is
named **Kell**; `Script.md` still uses the `MAGE` speaker tag and wants a find-and-replace,
which reflows nothing since both are four characters.

## Deferred until after the slice

Held open deliberately. Each one is a world-building decision that the demo does not need and
that answering early would only constrain:

| Question | Where | Why it can wait |
|---|---|---|
| What the Nodes were *for*, and who the Client is | `World.md` | **Promoted to urgent.** The slice still withholds it, but the 60% escalation from "stop a mob boss" to "fight a god" only lands if the reveal was prepared, and preparation starts in Act I. |
| Is Van Subject 7? | `World.md` | Plays either way as written. Changes whether Van is *returning* to this place or arriving at it. |
| ~~Six Nodes, or four?~~ | `World.md` | **Settled: three**, with a tangent quest between each and a final line at ~60%. Seven zones lands near 150-170KB of 262KB; six nodes would have been eleven. |
| ~~Can Instability reach a fail state?~~ | `Combat.md`, `Stats.md` | **Settled: no fail state needed.** It is Act I's motivation -- Van wants it out of him -- so it drives the plot instead of punishing the player. Reduced only at Nodes until the second tangent teaches balancing. |
| Forge: a place or a portable tool? | `Gear.md` | A place gives Crystal Peaks a reason to be a hub; a tool suits ninety-second sessions. The slice has neither. |
| Do combos cost both turns? | `Combat.md` | One initiating and pulling the other in is more exciting and much harder to telegraph on this screen. |
| Does Precision earn its place? | `Stats.md` | It makes the timing window buyable. Simplest answer is to leave it out of the slice and decide later. |
| Who is the client? | `World.md`, `Characters.md` | The final exchange is the hook. It only holds if the player has nothing to guess with. |

**Not deferred:** the `flag_boss_dead` / `flag_boss_defeated` mismatch is a bug, the cold open
does not exist yet, the four tutorials are named but not designed, and Kell is never wrong
about anything. Those are slice-blocking.

## The rule these documents follow

Every number here is checked against what pebblnyx and the hardware actually do, and says so
when it is a guess instead. Design that ignores the device produces content that cannot ship
-- and content bugs on a watch do not crash, they present as nothing happening. See
[`../MEASUREMENTS.md`](../MEASUREMENTS.md).
