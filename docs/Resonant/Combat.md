# Combat

Chrono Trigger's structure -- on-map encounters, no random battles, techs that combine
between party members -- with Legend of Dragoon's timed-input Additions and a transformation
state. Both halves are viable here, and the input measurements say precisely how.

## What the hardware allows

From [`../MEASUREMENTS.md`](../MEASUREMENTS.md), measured on device:

| | mean lag | spread | hit rate at +-74ms |
|---|---|---|---|
| Anticipation (sweeping marker), touch | +27 ms | 31 ms | **100%** |
| Anticipation, **buttons** | +53 ms | 33 ms | **97%** |
| Reaction (flash then respond) | +352 ms | 75 ms | 85% at 2f, 92% at 3f |

Three consequences, and they settle the design rather than inform it:

1. **Additions work.** A sweeping marker with a 2-frame window is hit essentially every time.
   This is the anticipation case, and it is the good one.
2. **Additions must be button-playable, not touch-only.** Touch is better -- half the lag --
   but only `emery` and `gabbro` have it. A touch-only combo system breaks author-once
   (see M9) and locks out four of seven watches. Buttons at 97% are fine; the window is the
   same width, just **offset by +53 ms instead of +27 ms**, per platform.
3. **Grading is honest.** Input is timestamped in the handler at 1 ms resolution even though
   the display runs at 26.8 fps, so perfect / good / miss is real rather than cosmetic.

**Reaction prompts must lead their target by ~350 ms.** A dodge cue is therefore possible but
must be an explicit discrete prompt, not something read off an attack animation. Prefer
anticipation everywhere it fits.

## Turn structure

Turn-based, party of up to three, initiative by Speed. On the player's turn:

```
Attack  -> Addition sequence (timed inputs, 2-5 steps)
Tech    -> costs Focus; solo or combo if partners have Focus and are ready
Item    -> no timing, always resolves
Guard   -> halves incoming, refunds Focus
```

**Additions.** An attack is a short sequence of timed hits. Each step shows a marker
converging on a target zone; hitting the window continues the chain, missing it ends the
attack early at reduced damage. Sequences are 2 steps at the start and grow to 5 with weapon
mastery. This is the moment-to-moment skill expression and it is the reason combat is not
menu-only.

- Window: +-2 frames (74 ms), offset per input method.
- Perfect (inner half-window): +25% damage on that step.
- A missed step ends the chain but never wastes the turn entirely -- damage already banked
  still lands. Punishing a miss with zero is too harsh at this window size.

**Techs and combos.** Chrono Trigger's model: single-character techs, and combos that fire
when two characters both have Focus and compatible techs. The script already names one --
Chain Lightning, unlocked at Event 04 -- and the mid-boss teaches Van-tanks / Mage-supports.

**Deferred (post-slice):** whether combos need both characters to spend a turn, or one initiates and the other
is pulled in. The second is more exciting and much harder to telegraph on this screen.

## Resonance (the transformation)

Van's Vessel state. Fills as he takes and deals damage; when full, he can spend it to
transform for a limited number of turns -- higher stats, an altered Addition set, access to
Resonance-only techs.

Narratively this is the thing killing him, so **it should cost something.** Proposal: entering
Resonance raises Instability (see `Stats.md`), which persists between fights and is only
reduced at Nodes. The player is spending story-health for combat power. That makes the
"34% stability" line mechanical rather than flavour, and gives the six Nodes a gameplay
reason to be collected beyond plot.

**Deferred (post-slice), and it matters:** if Instability never actually threatens the player, the mechanic is
a lie. Decide whether it can reach a fail state, and what that state is.

## Enemies

Palette swaps are cheap and the engine supports a per-entity palette override, so a
reskinned drone costs **8 bytes of palette** rather than a new atlas entry. Use this
aggressively -- it is the single largest content multiplier available. Vary behaviour and
palette; reuse silhouettes.

| Class | Role | First seen |
|---|---|---|
| Baseline drone | tutorial fodder | 01 |
| Drone, ranged variant | forces movement | 04 |
| Swarm drone | many, weak, tests AoE | 04 |
| Anchor Unit (Class-3) | mid-boss, grapple threat | 04B |
| Security construct | the GRD encounter | 05C |
| Containment core (Class-7) | boss | 07 |

## Encounters are placed, never random

Enemies are visible on the map and can be avoided. This is Chrono Trigger's rule and it suits
a watch far better than random battles: sessions are short and interruptible, and an
unavoidable fight after a notification steals the one minute the player had.
