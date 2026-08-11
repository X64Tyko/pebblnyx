# Slice flow

`Script.md` has the events. This has what happens between them, which is most of the playing
time and currently none of the document.

## The session constraint

This is played on a wrist, in ninety-second windows, and it can be interrupted at any moment
by a notification -- which throttles rendering to ~0.4 fps until dismissed. Two rules follow:

- **Never more than ~45 seconds between save-safe points.** Save-on-blur is viable (297 ms of
  warning against a 106 ms save), so the game should always be in a state worth restoring.
- **Never punish an interruption.** No timers that run while the app is covered, no fights
  that fail because attention left. Combat is turn-based partly for this reason.

## Cold open -- currently missing entirely

Event 01 opens on "Always a complication," which is a reaction to something the player has not
seen. Before it, three beats are needed and none exist:

**Beat 0a -- The contract.** One screen, before any map. Not a cutscene: text over black, the
contract itself. Job, location, payment, one clause that sounds routine and is not.

> `RETRIEVAL CONTRACT -- SECTOR 4`
> `TARGET: VAULT CORE ("RELIC")`
> `PAY: 400 SCRAP ON DELIVERY`
> `HANDLER: OPR`
> `CLAUSE 9: NO INDEPENDENT ANALYSIS OF THE TARGET.`

Clause 9 is the whole plot, stated in the first ten seconds, and it will read as boilerplate.
That is the point.

**Beat 0b -- Arrival and movement tutorial.** Van outside the facility. Player learns to walk.
No enemies, no text beyond a prompt. ~20 seconds.

**Beat 0c -- The complication.** The drones are already inside, and already fighting something.
*Then* "Always a complication," and Event 01 proceeds. The line now has an antecedent.

## Beat map

Times are targets for a first-time player. Combat and dialogue are marked so the ratio is
visible -- currently the slice is dialogue-heavy and this is where that shows.

| # | Beat | Kind | Time | Teaches |
|---|---|---|---|---|
| 0a | The contract | text | 0:10 | premise |
| 0b | Arrival | walk | 0:20 | movement |
| 0c | Complication | script | 0:05 | -- |
| 01 | Drone encounter | **combat** | 1:30 | attack, Additions |
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
| 06B | Terminal log 2 | optional | 0:30 | Subject 7 |
| 07 | The Vault | **boss** | 4:00 | **Resonance transformation** |
| 07B | Realization | dialogue | 0:40 | -- |
| 08 | New contract | dialogue | 0:50 | the hook |

**Total: ~23 minutes**, of which ~10:30 is combat and ~6:00 is dialogue. That ratio is
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

## Difficulty and level targets

The slice must be winnable at **level 5** (avoiding every optional fight) and comfortable at
**level 8** (fighting most). Tune the boss to level 6. A player who explored should feel
rewarded, not required.

## What still needs writing

- The optional pickups: two are referenced above and neither exists in `Script.md`.
- Failure. Nothing says what happens when the party dies.
- The Mage's combat entry -- she joins in dialogue at 02 but her first fight is 04.
  **Open:** is she in the party for the walk between, and does she fight the avoidable
  encounters there?
