# Slice flow

`Script.md` has the events. This has what happens between them, which is most of the playing
time and currently none of the document.

## The session constraint

**Design for interruptibility, not for brevity.** Those are different things and conflating them was
a mistake in the first draft of this document.

The device can be taken off and played with two hands, which is how it is actually comfortable --
see [`../PLATFORM.md`](../PLATFORM.md). So sessions are not inherently ninety seconds long and the
slice does not need to be a series of ninety-second chunks. What remains true is that a notification
can arrive at any moment and throttles rendering to ~0.4 fps until dismissed.

- **Never punish an interruption.** No timers that run while the app is covered, no fight that fails
  because attention left. ATB gauges advance on the sim tick for exactly this reason.
- **Always be in a state worth restoring.** Save-on-blur is viable -- ~297 ms of warning against a
  106 ms write -- so a notification never costs progress.
- **Do not pad for short attention.** A 26-minute slice is fine, and probably short.

## Cold open

**Beat 0a -- The contract, as a conversation.** Text over black, but not a data dump: Van and OPRA
talking. The player meets her as a handler doing a handler's job, which is the entire point -- when
she turns up at the vault it should land as *"god damn it"* rather than *"who is this?"*. A betrayal by
a stranger is a plot event; a betrayal by the voice that briefed you is a story one.

Clause 9 arrives in dialogue rather than on a form, so it reads as her being brusque instead of as
foreshadowing. ~20 seconds.

**Beat 0b -- Arrival and movement.** Van outside the facility. Walking, one prompt, no enemies. ~20s.

**Beat 0c -- One fleeing drone.** The combat tutorial: a single drone running away. It cannot hurt him,
so the player learns movement, attack and the Addition ring with no pressure and no way to lose.

Ordering matters here and it was wrong before. The tutorial has to come **before** the rescue, so the
player enters the Mage encounter competent and gets to feel like they saved someone -- rather than
blundering through a rescue while still working out which button attacks.

**Beat 0d -- Guaranteed level, and a new ability.** Van levels from that first kill, on rails, and
learns an ability immediately.

## Escort, not rescue

The Mage encounter is restructured. Kell is working the ward on the **ruins door** and cannot fight
while casting, so Van holds the line against arriving drones -- and the ability he just learned is
exactly what makes it manageable. Teaching a thing and then immediately needing it is worth more than
any tooltip.

**Kell must not be killable.** An escort that can fail in the first two minutes, on a character the
player does not control, is the worst possible introduction. Drones reaching her **interrupt the cast
and extend the fight** instead. Pressure without punishment.

The **sanctum door** later repeats the motif with the difference doing the work: same barrier, but a
mini-boss stands in front of it, so Kell fights *first* and only starts casting once it is down. The
player has already learned what the barrier means, so the variation reads immediately.

## Failure

Death returns the party to the **last save point**. Points sit near anything that matters -- before a
boss, at a zone entrance, after a long stretch of story.

**That needs two saves, not one.** Save-on-blur exists for the lifecycle (~297 ms of warning against a
106 ms write, and persist costs ~7 ms a call regardless of size), and it records *where you were* so a
notification never loses progress. The checkpoint records *where you retry*. They are different data
and both are needed -- collapsing them would either make blur a checkpoint, letting a player suspend
mid-boss and retry from there, or make death lose everything since the last point including the
inventory.

## Beat map

Times are targets for a first-time player. Combat and dialogue are marked so the ratio is
visible -- currently the slice is dialogue-heavy and this is where that shows.

| # | Beat | Kind | Time | Teaches |
|---|---|---|---|---|
| 0a | The contract, with OPRA | text | 0:20 | premise, and OPRA |
| 0b | Arrival | walk | 0:20 | movement |
| 0c | One fleeing drone | **combat** | 1:00 | attack, the ring, no pressure |
| 0d | Level up, new ability | script | 0:15 | progression exists |
| 01 | Hold the line for Kell | **combat** | 2:00 | the new ability, escort pressure |
| -- | Approach the ward | walk, 1 optional pickup | 0:40 | exploration is rewarded |
| 02 | Meet the Mage | dialogue | 1:00 | party, doors gate progress |
| -- | Ruins, first branch | walk, 2 encounters avoidable | 1:30 | encounters can be dodged |
| 03 | Ruined statue | dialogue, optional | 0:30 | lore is opt-in |
| 04 | Courtyard swarm | **combat** | 2:00 | AoE, Chain Lightning, Focus |
| 04B | Vault Guardian | **boss** | 3:00 | roles, items, Guard |
| 04C | Aftermath | dialogue | 0:30 | -- |
| 05 | The Breach | script, scene change | 0:15 | act break |
| -- | Sanctum approach | walk | 0:40 | new tileset reads as danger |
| 05B | Gear check | dialogue, optional | 0:40 | **equipment screen** |
| 06 | Terminal log 1 | optional | 0:20 | lore |
| 05C | Guard's awakening | dialogue | 1:00 | third party member |
| -- | Corridor, GRD in party | walk, 2 encounters | 1:30 | party switching |
| -- | Commissary | optional shop | 0:40 | scrap has a use |
| 06B | Terminal log 2 | optional | 0:30 | Subject 7 |
| 07 | The Vault | **boss** | 4:00 | **Resonance transformation** |
| 07B | Realization | dialogue | 0:40 | -- |
| 08 | New contract | dialogue | 0:50 | the hook |

**Total: ~26 minutes**, and still on the short side, of which ~10:30 is combat and ~6:00 is dialogue. That ratio is
defensible for a story slice but it is the thing to watch: cut dialogue before cutting
exploration, because exploration is what makes the dialogue feel earned.

## Tutorials

Four are named in `Script.md` post-scripts with no content. Each needs a design, and the
constraint is the same throughout: **four buttons, no room for a tutorial box over the
action.** Teach by constraining the situation, not by explaining it.

**T1 -- Attack and Additions (Event 01).** One drone, cannot kill Van. The first Addition is
2 steps and the window is displayed generously wide, narrowing to normal over the fight.
Nothing is explained in words except a single prompt on the first marker. A player who
mistimes every input still wins, slowly -- and learns the mechanic from the damage difference.

**T2 -- Focus and techs (Event 04).** The swarm cannot be cleared with single-target attacks
in reasonable time. The Mage enters with full Focus and exactly one tech. The situation
teaches the lesson; the script's existing line ("thin them out") is the only prompt needed.

**T3 -- Equipment (Event 05B).** The workbench is the first place gear can be changed. The
Type-1 plating dropped by the mid-boss is already in the inventory, unequipped -- so the
screen opens with an obvious improvement waiting. Teaching by having the right answer visible
is faster than describing the interface.

**T4 -- Resonance (Event 07).** Forced. The transformation triggers as a scripted beat mid-boss
rather than as a choice, so the player experiences it before having to manage it. Instability
is *shown* rising here and not explained -- GRD's "VESSEL STABILITY: 34%" in 07B is the
explanation, arriving after the player has felt it.

**Party switching** is named in 05C's post-script but is not really a tutorial -- it is a
menu. Introduce it in the corridor after 05C with a fight that a two-member party handles
badly and a three-member party handles easily.

## The optional grind

The Sanctum commissary is reachable after `05C` and sells a full gear set for 360 scrap
against the ~45 a normal run yields. This is Chrono Trigger's fair: a player who wants to
farm drones for fifteen minutes gets something clearly above the curve, and a player who does
not never learns it was there.

It has to stay genuinely optional in both directions. **The slice must be winnable with zero
purchases**, and the boss must not become trivial for a player who bought everything -- gear
should make that fight *faster*, not skippable. Tune the boss to level 6 and let the ceiling
be speed.

Placing it after `05C` is deliberate: the third party member makes farming efficient enough to
be tempting rather than tedious.

## Difficulty and level targets

The slice must be winnable at **level 5** (avoiding every optional fight) and comfortable at
**level 8** (fighting most). Tune the boss to level 6. A player who explored should feel
rewarded, not required.

## What still needs writing

- The optional pickups: two are referenced above and neither exists in `Script.md`.
- The Mage's combat entry -- she joins in dialogue at 02 but her first fight is 04.
  **Open:** is she in the party for the walk between, and does she fight the avoidable
  encounters there?
