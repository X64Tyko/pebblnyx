# Combat

Chrono Trigger's on-map encounters and party-combo techs, Legend of Dragoon's timed-input
Additions, and an ATB gauge. Every timing figure below is measured on device; see
[`../MEASUREMENTS.md`](../MEASUREMENTS.md).

## ATB, and the constraint that shapes it

Gauges fill continuously; a character acts when full. **Wait** by default -- gauges freeze while
a menu is open or an Addition is running. Active is a settings toggle and costs almost nothing to
support.

**Gauges must advance on the sim tick, never on `time_ms()`.** A notification throttles the app
to ~0.4fps, so wall-clock timing would hand enemies free turns while the player reads a message.
The fixed-timestep accumulator is already clamped for exactly this reason, which makes a covered
battle pause rather than fast-forward. This is not a preference -- wall-clock ATB is unshippable
on a watch.

Active mode plus a twelve-enemy horde is punishing by construction. That is the player's choice
to make, but the default should never be that.

## What the hardware allows

| | mean lag | spread | hit rate at +-74ms |
|---|---|---|---|
| Anticipation (converging marker), touch | +27 ms | 31 ms | **100%** |
| Anticipation, **buttons** | +53 ms | 33 ms | **97%** |
| Reaction (respond to a cue) | +352 ms | 75 ms | 85% at 2f, 92% at 3f |

Three consequences that settle design rather than inform it:

1. **Additions work.** A converging marker with a 2-frame window is hit essentially every time.
2. **Nothing may be touch-only.** Only `emery` and `gabbro` have touch. Buttons at 97% are fine;
   the window is the same width, offset **+53 ms instead of +27 ms**, per platform.
3. **Grading is honest.** Input is timestamped at 1 ms even though the display runs at 26.8fps,
   so perfect / good / miss is real rather than cosmetic.

**Reaction prompts must lead their target by ~350 ms**, so a dodge cue has to be an explicit
discrete prompt and never something read off an attack animation. Prefer anticipation everywhere.

## Layout

200x228. Party of three scattered across corners -- right-top, right-bottom, bottom-left -- with
enemies through the centre and upper-left. Not a column: a corner is unambiguously top or bottom,
which is what makes the box popping from the opposite side meaningful.

| | |
|---|---|
| Character sprite | 20x48, with an ATB gauge beneath |
| Enemy sprite | 32x32 typical, varies per design |
| Dialog / menu box | **64px**, slides in from the top or bottom |
| Enemies on screen | up to **12** |

**The box is only on screen when it is needed.** It slides in from whichever edge is away from the
acting character, and slides back off the moment a target is being chosen, so the field is never
obstructed while the player is reading it. A 64px slide over six frames is 224 ms -- deliberate,
not sluggish.

Players do not move in combat; enemies do. That fixes party positions, which makes the whole HUD
a compile-time layout with no runtime positioning, and leaves the depth sort with only enemies to
order.

## Additions

An attack is a short sequence of timed inputs. Each character's is a different *kind*, so the
party does not feel like one mechanic wearing three hats.

**Van -- converging ring, 2-5 steps.** A shaded annulus contracts toward the centre; a thin ring
at the band's midpoint is *perfect*, and contracting past it fails. The centre reads `tap` or
`sel` depending on input method, which is also the cheapest possible answer to the touch/no-touch
split. Van's damage scales with skill and he carries the AOE options.

**GORD -- the same ring, shorter chains.** Single-target burst, tanky, fewer steps. He gets a real
Addition rather than none: making the tank the character with nothing to do would be a mistake.

**Kell -- a timed input sequence**, in the shape of Auron's overdrive. A 4-6 symbol sequence drawn
from the three battle buttons, executed before a time limit; longer sequences at higher mastery.
Read-then-execute, so input *precision* does not gate it and only total time does -- which means it
ports to every platform unchanged. **Symbols, not directional swipes**: swipes would not survive
the four platforms without touch.

**A missed step ends the chain and banks the damage already landed.** Zero-on-miss is too harsh at
this window size, and it is the difference between a skill system and a punishment.

Draw the ring, do not store it. A contracting annulus is two circle spans per row -- about 200
bytes of code, **zero resource bytes**, and it scales to `gabbro`'s 260x260 for free, where
pre-rendered rings would be 2KB+ and locked to one screen size.

## Stun

Stunning a target **stops its ATB gauge from building**. Early on that is simply free damage, which
makes it worth learning in the first hour without anything explaining why it matters later.

It matters later because it is the crux of the game's one real branch.

**The OPRA fight has two outcomes.** She is carrying a synthetic Node. If the party can stun her
regularly enough to destroy the crystal, she survives it and becomes an **optional fourth party
member**. If they cannot, she is destroyed along with it.

Three requirements for that to be a choice rather than a coin toss, all of them the designer's job
rather than the player's:

- **The crystal must be visibly a target.** Its own HP bar, not ability-gated. A branch the player
  cannot see is not a branch.
- **Kell must say so, mid-fight.** One line naming the crystal, once the fight has been running long
  enough that the player is looking for a lever.
- **Regular stunning, not flawless.** The bar is "used the mechanic competently", not "played
  perfectly". Losing an optional character to a mechanic nobody told you about is the worst version of
  this beat.

**Cost worth stating:** an optional fourth character is a sprite set with variants, an Addition type,
and a branch-aware line in every subsequent scene. The bytes are trivial; the authoring and the
testing of two paths are not. That is the real price of the branch and it should be paid on purpose.

## Techs and combos

Single-character techs cost Focus; combos fire when two characters both have Focus and compatible
techs. Chain Lightning unlocks at Event 04 and is the tutorial for the system.

**Deferred (post-slice):** whether a combo costs both characters a turn, or one initiates and pulls
the other in. The second is more exciting and much harder to telegraph on this screen.

## Targeting

**Tap cycles deeper.** Tapping an enemy selects the nearest; tapping again selects the next one
behind it, and it wraps. So an ambiguous tap in a crowded field is resolved by tapping again rather
than by demanding precision. The hit test is against sprite bounds, not opaque pixels -- cheaper,
and cycling makes the imprecision harmless.

**Up/Down is always available**, not a fallback bolted on for the button platforms. It scrolls the
same candidate list, so targeting is fully playable without touch and the two methods share one
code path.

Picking which character acts when two gauges fill uses the same interaction: tap the character, or
scroll with Up/Down.

## Information

**Damage numbers always.** Every hit shoots a coloured number, so the interface never looks
incomplete. Up to twelve at once from an AOE, which wants a pool of sixteen.

**HP bars are ability-gated.** Equipping the right ability shows a bar above an enemy briefly when
it is hit. Gating *information* is a real tactical purchase -- but the gated version is **additive**:
the ungated state still shows damage, so a player without the ability reads a complete interface
rather than a broken one.

Ally HP always shows above the head when targeting. Enemy HP above the head is ability-gated the
same way. Party HP, Focus and their maxima appear when the menu opens.

**Colour cannot be the only channel.** Three of seven platforms are 1-bit, so damage *type* needs a
glyph prefix, position or size as well as a hue.

## Encounters

Placed, never random -- enemies are visible on the map and can be avoided. Chrono Trigger's rule,
and it suits a watch: a forced fight after a notification steals the one minute the player had.

A battle starts from a **trigger area** flagged in the map. Player and enemy start positions are
also tile flags, and *the game* randomises which flagged positions get used along with the enemy
roster. The camera is fixed for the duration, which follows from players not moving.

**The framework does not know what a battle is.** `pnx_map_flags` already returns a byte the game
interprets however it likes, and six of its eight bits are unused. So "battle trigger", "player
start" and "enemy start" are Resonant's meanings for three bits -- no engine work, no format
change.

What is missing is general: the manifest's flag names are a closed set of two (`solid`, `warp`).
**Any game needs to name its own flags**, so that becomes a manifest-declared table and the
pipeline validates against what the game declared. That is a framework feature because every game
wants it, not because this one does.

**Validation belongs in the pipeline** once flags are declarable: a trigger with fewer player
starts than the party, fewer enemy starts than the roster's maximum, a start inside a solid tile,
or a start outside the fixed camera view. Same class of fault the warp checks already catch --
content that does not crash, it just silently does nothing.

## Enemies

Palette swaps are the largest content multiplier available and the pipeline now collapses them
automatically -- see [`Budget.md`](Budget.md). Four silhouettes across three palettes reads as
twelve enemy types for the price of four.

| Class | Role | First seen |
|---|---|---|
| Baseline drone | tutorial fodder | 01 |
| Drone, ranged variant | forces target switching | 04 |
| Swarm drone | many, weak, tests AOE | 04 |
| Anchor Unit (Class-3) | mid-boss, grapple threat | 04B |
| Security construct | the GORD encounter | 05C |
| Containment core (Class-7) | boss | 07 |

## Resonance and Destabilisation

Built like FFIX's Trance, not like a resource the player spends.

**Destabilisation fills as the holder takes and deals damage, and triggers automatically on their
turn when it maxes.** The player does not choose to transform; it happens. That single decision
removes the risk that would otherwise sink the mechanic -- if entering it were a choice with a cost,
Act I Van would never take it and the player would arrive at the endgame never having learned the
thing the endgame is built on.

It also makes farming a legitimate strategy rather than an exploit. A hard boss is easier if you walk
in at 90%, so grinding the corridor outside is the player *reading the system correctly*. FFIX
players did exactly this and it is worth protecting, not patching.

**Node balance is the progression axis**, earned from Nodes and from the second tangent quest:

| Balance | Fill rate | Duration | Power |
|---|---|---|---|
| Low (Act I) | fast | short | weak |
| High (late) | slow | long | **large** |

Low balance is a twitchy, frequent, feeble transformation -- which is exactly how it should feel while
Van still fears it. High balance is rare, sustained and decisive. The numbers move in opposite
directions on purpose: the mechanic gets *less* frequent and *more* meaningful, so the late game does
not degenerate into permanent transformation.

**Van hates it.** He experiences it as becoming a mindless violent thing, and the auto-trigger means
he cannot decline. That is the character's problem, not a stat penalty -- there is no meter punishing
the player for something the plot requires. See [`World.md`](World.md) for how the arc resolves it.

After node 2 the shards stabilise the node enough to pass between party members, so the holder is a
choice. Whoever holds it is whoever transforms.

## What this needs built, and where it belongs

Gameplay does not get to decide how the framework works. `DESIGN.md` draws the line: a game author
writes content, scenes and per-tick logic, and the framework owns everything else. So each item
below is placed by whether it is general, not by whether Resonant wants it -- and anything the
framework takes on must be opt-in and cost zero bytes when unused.

**Framework**, each justified without reference to this game:

| | Why it is general |
|---|---|
| **Glyph blitting** | Every game needs text, and the SDK's text calls cannot run inside the framebuffer capture window, which is where we draw. ~74 glyphs at 6x12 4bpp is ~2,664 B; proportional spacing adds a 74-byte width table and buys ~15% more per page. |
| **Circle and annulus spans** | An ordinary drawing primitive. Behind its own compile-time flag, since a tile game may never draw one. |
| **Manifest-declared tile flag names** | The flag byte is already generic and read generically; only the *names* are hardcoded to `solid` and `warp`. Every game wants its own. Pipeline work, not engine. |

**Resonant**, in game code:

- **Flying enemies: two calls to `pnx_sprites_draw_sorted`, not one.** Grounded entities in one
  array, fliers in a second, drawn after. Each layer sorts correctly within itself and fliers read
  as being above the field. No framework change and no hand-rolled sort -- the same function, twice,
  and `order` is scratch so one buffer serves both.

  The trade: a flier occludes a grounded enemy even when the grounded one is nearer. That is wrong
  in principle and mostly invisible in practice, because a flier hovering high enough sits above
  the grounded sprites' tops anyway. It also buys something real -- fliers are never buried in a
  twelve-enemy horde, which matters when they have to be tappable.

  Tap-cycling then walks fliers first, then grounded, which is consistent with "cycles deeper into
  the screen" since fliers are the visually nearest layer.
- The Addition ring: what it means, its timing windows, its grading.
- Battle triggers: which flag bits mean what, roster randomisation, camera placement per trigger.
- The damage number pool -- sixteen entries, for a twelve-target AOE -- and its lifetimes.
- ATB gauges, Focus, Instability, and every rule above about them.

Frame cost is not a concern: twelve enemies plus three party members is ~15,000 sprite pixels,
about **1,350 µs** on top of the measured 5,100 µs full-screen baseline. Roughly 18% of the frame.
