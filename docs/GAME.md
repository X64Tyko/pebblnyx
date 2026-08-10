# The game

Working direction, deliberately high level. Mechanics will be tweaked; this exists so
the framework is built for the right shape of game rather than a generic one.

**An RPG crossing Chrono Trigger and Legend of Dragoon.**

From Chrono Trigger: visible field encounters with battles fought in place, an ATB
flow rather than strict turns, and combo techs between party members. From Legend of
Dragoon: timing-based attack execution — the Addition system — and a transformation
state with its own abilities.

---

## What each influence costs on this hardware

### Chrono Trigger's structure is a gift here

**Battles on the field map, not a separate scene.** No transition, no second tileset,
no battle backgrounds. Against a **256KB resource budget** that is a large saving, and
architecturally it means battle is a *mode* over the existing scene rather than a
parallel renderer. This is the single most valuable thing the reference brings.

**Party of three with combo techs** is trivially affordable. Entity counts are tiny
against ~35 ms of free CPU per frame; the interesting work is in the tech
combination tables, which are data, not computation.

**Time travel is the expensive part.** The same location across several eras multiplies
tile and map content, and content is what the 256KB cap actually binds. Mitigations to
decide early: share a base tileset across eras with a small per-era variant set, or
reduce the number of eras. This is a content-budget decision, not an engine one, and it
should be modelled before art is commissioned.

### Legend of Dragoon's Additions — RESOLVED, viable

Measured in the M0 spike (see [`MEASUREMENTS.md`](MEASUREMENTS.md)): tap spread is
**31 ms** with a systematic lag of **+27 ms**. A 2-frame window (±74 ms) hits 100%; a
1-frame window (±37 ms) hits 75%. **Additions work.**

Four rules fall out of the data, and they are design constraints rather than
implementation details:

1. **Touch, not buttons.** Half the lag (+27 vs +53 ms) at the same precision.
2. **Prefer anticipation.** A reaction cue measured ~352 ms of lag (replicated) and
   ~75 ms of spread — usable with a 3-frame window offset by 352 ms, but 2.4x less
   precise than an approaching indicator. Additions should use the anticipatory form,
   which is what Legend of Dragoon did; a reaction prompt stays available for discrete
   moments like a dodge, provided its cue leads the target by ~350 ms.
3. **Offset the window by the measured mean.** ~27 ms, subtracted when judging.
4. **Grade the result.** Input is timestamped at 1 ms even though the display is
   37 ms, so perfect / good / miss tiers are expressible and map onto Addition's
   escalating combos.

Suggested difficulty ladder: **±111 ms tutorial, ±74 ms accessible, ±37 ms standard**
(75% success is a good rate for a skill mechanic — frequent enough to feel earned,
failing often enough to matter).

The original concern, kept for context:

**This was expected to be the one mechanic the platform is worst at.** Additions need a timed input
during an attack animation, and the measured constraints are unfriendly:

- Frames are **37.33 ms** and cannot be shortened, so a timing window is quantised to
  ~37 ms steps. A 3-frame window is 111 ms.
- End-to-end input latency is **≥37 ms by construction, likely ~74 ms** touch-to-pixel,
  and has never been measured directly.

If real latency is ~74 ms and a window is 111 ms, most of the window is consumed by lag
and the mechanic will feel mushy or unfair. It may still work — Additions were forgiving
and rhythm-cued rather than reflex-cued — but **it must be spiked before it is designed
around.** Everything else in this game is turn-based or menu-driven and does not care
about latency; this one thing does.

Candidate mitigations if the spike goes badly: visual cue leading the window so the
player anticipates rather than reacts, generous windows with the difficulty in the
*sequence length* rather than the precision, or a held-input variant instead of a tap.

**Dragoon-style transformation** is a state machine with alternate ability tables and a
turn counter. Cheap, and mostly content.

## The screen is better suited than it first appears

200x228 portrait, versus the SNES's 256x224 landscape:

| | SNES JRPG | PT2 |
|---|---|---|
| Viewport in 16px tiles | 16 x 14 | **12.5 x 14.25** |

Only 78% the width, and *slightly taller*. A field map viewport is genuinely comparable
to what Chrono Trigger had. And portrait suits a vertically stacked battle layout —
enemies above, party below — better than landscape does.

Menus and inventory are where 200px of width will hurt. Those need designing for the
aspect ratio rather than ported from a landscape reference.

## What this implies for the framework

Additions to [`DESIGN.md`](DESIGN.md) that this direction justifies:

1. **A battle mode layered over the field scene**, sharing the tilemap renderer and
   entity pool — not a separate scene type. Follows directly from the CT structure.
2. **An ATB timer service** in `app`: per-combatant gauges advancing on the fixed 25 Hz
   tick. Trivial, but it belongs in the framework since any RPG built on this will want
   it.
3. **A timing-window input primitive** in `input`: open a window, report hit/miss with
   the offset in milliseconds. Shared by Additions and any other rhythm-adjacent
   mechanic, and it is where the latency compensation lives if compensation is needed.
4. **Menu and list widgets** in `gfx`, designed for a 200px-wide portrait screen. RPGs
   are menu-heavy and this is otherwise the thing every game reimplements badly.
5. **Save state will be a few KB** — party, inventory, flags, quest state, position. At
   106 ms per 4KB with a 297 ms save-on-blur window, that fits, but it confirms the
   incremental writer is needed rather than optional.

## Open creative questions

Not decisions to make now, but ones the framework should not accidentally foreclose:

- How many eras, and how much tileset sharing between them
- Whether Additions are tap-timed or held-timed. The spike measured **tap only**;
  held-and-release was not tested and may have different characteristics
- Party size on screen during battle, given portrait layout
- Whether encounters are truly on-map (CT) or on-map-triggering-an-overlay
- Music count: towns, field, battle, boss. Note-event music is cheap in bytes, so this
  is mostly authoring time rather than a budget constraint

## Roadmap status

**M0 is done and Additions are viable** — the numbers and the four design rules are in
the Additions section above. The framework's timing-window primitive can now be
specified concretely rather than defensively: judge in milliseconds against a
mean-corrected window, expose graded tiers, and drive it from touch.

Nothing else in this design is latency-sensitive, so the remaining risks are content
budget (eras multiplying tilesets) and the unmeasured items in
[`DESIGN.md`](DESIGN.md) section 11.