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
| ~~What the Nodes are *for*~~ | `World.md` | **Settled.** The world believes they weaponise then kill a Resonant (half true, hence credible). The Client believes four make a god and he will be it (right, then wrong). Truth: his body is the sacrifice and the Pirearch Runigran is what arrives. |
| Does anyone alive state the sacrifice truth? | `World.md` | Leaning no -- the player infers it. Moireage knows the god part and not the sacrifice; nobody else has an unforged history to reason from. |
| Runigran or Runigan? | -- | Spelled both ways so far. Docs use **Runigran**; say which and it is one find-and-replace. |
| ~~Does transformation stay auto-triggered in the final fight?~~ | `Combat.md` | **Settled: neither.** Soul Bond at 100% means Destabilisation reads NONE, so the last phase is sustained with no gauge at all. The mechanic it would have gated on stops existing. |
| ~~Who has the realisation if OPRA died?~~ | `World.md` | **Settled: the player chooses who takes Moireage's Core.** Same outcome, but the double bearer dies after the fight and relays the voice. Play Node 2 well and nobody dies; otherwise choose which of them will. |
| Is Van eligible for the sacrifice? | `World.md` | If yes, the epilogue must support the protagonist's death. If no, say why the choice is Kell or GORD. |
| Does GORD's loss cost the same? | `World.md` | He is the emotionally cheap pick and would calculate that he is correct, which drains the choice of weight. |
| Is Van Subject 7? | `World.md` | Plays either way as written. Changes whether Van is *returning* to this place or arriving at it. |
| ~~How many Nodes?~~ | `World.md` | **Settled: four, one held by the Client**, and the number erased from history so everyone says three. The party reaches ~60% holding three; the final act is taking his. |
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
