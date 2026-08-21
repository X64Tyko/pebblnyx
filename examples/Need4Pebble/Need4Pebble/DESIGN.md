# Need 4 Pebble

An OutRun-style pseudo-3D racer for Pebble, targeting all seven pebblnyx-supported
platforms (see "Multi-platform"). Landscape, two-handed, shoulder-trigger controls.

## Core loop

Timed race against the clock, not against opponents directly. The clock counts down;
crossing a checkpoint adds time. Reaching the finish before time runs out clears the
stage. There is no rank/position mechanic (no other racers to place against) -- the
challenge is the clock, the road, and the traffic in the way of holding a fast line.

- **Checkpoints** add time and mark progress along a stage.
- **Traffic** (touring cars, police cars, from the existing sprite tiers) must be
  dodged. Contact costs time and/or speed (crash animation already asset'd:
  impact flash -> spin -> tumble -> wreck).
- **Racing line matters**: apex a turn on the inside and top speed through it is
  higher than taking the outside. This is the one deliberate skill expression in an
  otherwise straightforward "avoid traffic, hit checkpoints" loop, and it's what
  should feel good to learn.
- **Score, not rank**: the goal across a run is a high score (distance, time
  remaining, near-misses, perfect checkpoints, police takedowns -- some
  combination; exact weighting TBD), not "1st place." Multiple maps, replayed for
  a better score each time -- an arcade attract-mode shape, not a campaign.

## Police: chase, not traffic

Built (`game.c`'s `police_tick`/`check_collision`/`check_busted`, `render.c`'s
`draw_police`). One pursuer at a time, spawning after an occasional cooldown
(`POLICE_FIRST_SPAWN_*`/`POLICE_RESPAWN_*`, `game.h`) rather than a per-tick
chance -- a fixed range has a predictable worst case, a per-tick roll doesn't.
Corrects its speed toward a desired following gap rather than simply matching the
player, which is also part of what lets it occasionally force a hit (same
`TRAFFIC_COLLIDE_Z`/`LANE_HALF` hitbox as a traffic collision) without a separate
ramming state. The player's actual tool for losing a pursuer is forcing IT to
crash -- steering it into traffic or off the road wrecks it the same two ways the
player's own car can wreck (`TRAFFIC_BEHIND_MARGIN` raised 60->220, game.h, so a
just-passed traffic car sticks around long enough behind the player to actually be
steerable into a trailing cop, and so a player who slows back down can catch up to
one they already passed). Now scored (see "Checkpoints, timer, and scoring" below):
`game.c`'s `police_crash` -- the single call site both self-crash paths in
`police_tick` (off-road, into traffic) route through -- awards `POLICE_TAKEDOWN_SCORE`
and posts the "ELUDED +2000" HUD banner; a ram that crashes the PLAYER (`check_collision`'s
police branch) does not, since the cop didn't crash there. `police_normal`/
`police_crash` (the sprite set the assets note below describes) are what
`draw_police` actually draws; `police_slope`/`police_title`/`police_avatar` are
staged but not yet used by anything.

**Lateral drift, not a lane-centre tail**: the cop favours one side of the
player or the other, never sitting dead centre behind them, and wanders
between sides over a chase rather than committing to one. `police_tick`
tracks `g->lane_x + offset_x` instead of the player's own lane directly.
`offset_x` eases (`POLICE_OFFSET_EASE_DIVISOR`) toward an `offset_target`
re-rolled every `POLICE_OFFSET_RETARGET_MIN/SPAN_TICKS` to a magnitude in
`[POLICE_OFFSET_MIN, POLICE_OFFSET_MAX]` on a randomly picked side -- never
near 0. This is also most of what makes the cop's own sprite readable as a
distinct car: dead centre, it renders mostly underneath the player's own car.

**Ramming forces a lane change**: a hit from the cop (`check_collision`'s
police branch, same `TRAFFIC_COLLIDE_Z`/`LANE_HALF` hitbox a traffic
collision uses, no separate ramming state) shoves the player's `lane_x` by
`POLICE_RAM_SHOVE` (game.h) to the side opposite wherever the cop was at the
moment of the hit, clamped to `PLAYER_LANE_MAX` -- a real knock into the next
lane, not just a stop-in-place crash. While the player is frozen through the
resulting crash, `police_tick` (which keeps running through the stun, same
as `traffic_tick`, so the chase doesn't just pause) pins its own `z` to
never exceed the player's stalled `distance` -- its ordinary gap-correction
(`POLICE_CATCHUP_BONUS`, a few units/tick) is far too gentle on its own to
stop existing momentum carrying it straight past a stopped player and out in
front for the whole stun window.

**Visibility**: two independent things make the pursuer readable while
actually driving. First, `render.c`'s `draw_police_lights` -- a flashing
red/blue light-bar wash across the near field whenever a pursuer is active
and not already crashed. `pnx_gfx` has no alpha blend for a filled rect (see
`COLOUR_MENU_BG`'s own comment), so "semi-transparent" is faked by leaving
rows unpainted between tinted ones, the dither idea the engine's own sprite
rendering already uses elsewhere for coverage; density tapers from dense at
the true near edge to sparse at the wash's own far edge
(`POLICE_LIGHT_BAND_ROWS`), fading into the road rather than stopping at a
hard line. Colour alternates on `Game.tick_count` (`POLICE_LIGHT_FLASH_TICKS`),
a dedicated tick counter decoupled from `distance` so the flash rate doesn't
depend on the player's own speed, but still freezes with everything else
while paused/busted.

Second, and the larger of the two: `police_normal` (12434 bytes, the same
size as the player's own `touring_normal`) was silently failing to load for
the entire span this feature existed. `SCENE_BYTES` (game.h) was 20KB;
`touring_normal`(12434) + `touring_crash`(~1.9KB) + `menu_font`(646B) alone
left only ~5.5KB free by the time `game_boot` reached it. `has_police` was
false on every boot, so `draw_police`'s own guard silently skipped the cop's
actual chase sprite on every draw call -- the lateral-drift and light-wash
work above were real improvements but couldn't have mattered on their own if
the sprite never loaded. `SCENE_BYTES` is now 32KB, covering the full actual
load list (~29.3KB) with headroom; heap was never the binding constraint
(~115KB free either way, per `pebble build`'s own memory report).

**Game over**: hitting 0 velocity with an active (not currently crashed) pursuer
within `POLICE_BUST_RANGE` world units is "BUSTED" (`game.h`/`game.c`'s
`check_busted`) -- whatever caused the stop, a forced collision or just braking
to a stop mid-chase. Frozen like `paused` (`Game.game_over_reason`, one of two
ways a run can now end -- see "Checkpoints, timer, and scoring"), but BACK
restarts (`game_restart`) instead of resuming -- see "Controls".

## Checkpoints, timer, and scoring

The core loop DESIGN.md always described but hadn't built: a countdown clock,
checkpoints that add to it, and a score built from more than one signal (distance,
near-misses, checkpoints, police takedowns). First cut -- see "Open questions" for
what's still an eyeballed guess rather than felt in-hand.

**The clock.** `Game.timer_ticks_left` (`game.h`) counts down from
`RACE_TIMER_START_TICKS` (45s); reaching 0 sets `Game.game_over_reason` to
`GAME_OVER_TIME_UP` (`game.c`'s `tick_clock`) -- a second way a run can end,
alongside `GAME_OVER_BUSTED`. Both freeze the sim exactly like `paused` and both
restart on BACK rather than resume (`main.c`); `render.c`'s `draw_game_over` reads
the reason to show "BUSTED" or "TIME'S UP". The clock runs through a crash/stun,
same as traffic and a chasing cop already do -- DESIGN.md's "contact costs time"
is this, for free, not a separate penalty.

**Checkpoints.** `Game.next_checkpoint_z` starts at `CHECKPOINT_SPACING` (2800 world
units -- 2 per lap on `TRACK`'s 5600-unit loop) and simply keeps incrementing by
that amount every time `game.c`'s `check_checkpoint` sees `distance` clear it, so a
looping test track just keeps handing out checkpoints lap after lap rather than
needing to wrap. Each one adds `CHECKPOINT_TIME_BONUS_TICKS` to the clock and
`CHECKPOINT_SCORE` (200) to the score. Spacing was originally 700 (8/lap) and the
time bonus a flat 12s -- reported directly as "too frequent, too valuable, and
award far too much time": at `MAX_SPEED` cruise, 700 units took ~1.1s to cover, so
a flat 12s bonus was an 11s windfall on almost every tick. `CHECKPOINT_TIME_BONUS_TICKS`
is no longer a flat number: `CHECKPOINT_PERFECT_TICKS` (`game.h`) is the ceiling-
divided tick count to cover `CHECKPOINT_SPACING` at `MAX_SPEED` (cruise -- "perfect
driving"), plus a small fixed buffer -- "one checkpoint should be 5-10s more than
needed to reach the next checkpoint with perfect driving" (the ask, verbatim), so
it recalculates correctly if spacing or top speed ever change again rather than
needing to be re-tuned by hand alongside them.

**Scoring**, `game.h`'s own comment has the full breakdown:
- Distance isn't accumulated into `Game.score` at all -- `Game.distance` is already
  a running total, so `render.c`'s `hud_total_score` just reads
  `distance / DISTANCE_SCORE_DIVISOR` directly at display time.
- Near misses: `check_collision`'s traffic loop, on a car that DIDN'T collide,
  checks the same Z window `TRAFFIC_COLLIDE_Z` uses against a wider lane band
  (`NEAR_MISS_LANE_MAX`) -- "close enough to have hit at a slightly different
  line." `Traffic.near_miss_scored` caps it at one `NEAR_MISS_SCORE` (150) per
  approach, cleared by `traffic_spawn` on the next recycle.
- Checkpoints: `CHECKPOINT_SCORE`, above.
- Police takedowns ("eluding police" per the ask): `POLICE_TAKEDOWN_SCORE` (2000)
  -- see "Police: chase, not traffic"'s own updated note.

Every award (`game.c`'s `award_score`) both adds to `Game.score` and posts a
short-lived banner (`Game.hud_event`, e.g. "CHECKPOINT +200") that `render.c`'s
`draw_hud_event` shows centred under the score/timer for `HUD_EVENT_TICKS` (~1.8s)
before disappearing -- ticks, not wall-clock, same as everything else timed here,
so it freezes with the sim rather than animating through a pause.

**HUD.** `render.c`'s `draw_hud` draws score (top-left) and the clock as `M:SS`
(top-right, not a bare seconds count -- two plain numbers in the same corner
would otherwise be easy to confuse), both `pnx_text_draw_outlined` (below) rather
than boxed -- an opaque backing rect was the first draft, dropped per direct
feedback ("I hate the black background on the sprites"). `draw_hud_event`'s
banner and `draw_pause_menu`/`draw_game_over`'s panel text draw the same way,
except the panel keeps its own solid box (a deliberate modal background, not
what the feedback was about) so its text stays on the plain `pnx_text_draw`.

**Outlined text, an engine addition.** `pnx_text_draw_outlined`
(`src/pnx/gfx/pnx_text.h`/`.c`, host-tested in `tests/test_text.c`) draws a
1px outline (4 cardinal offsets, not 8 -- barely thicker at this glyph size and
half the draw cost) before the fill colour, so a HUD string over open sky/road
stays legible without an opaque panel behind it. Engine-level, not project-
local -- the same "pull it up a level once a technique gets reused" call this
project has made before (`pnx_gfx_fill_rect_dither`, `pnx_tween`), and broadly
useful to any pebblnyx game with a HUD, not specific to a racer.

**Two typefaces, split by content, not by which HUD element it is.** "Numbers =
Monster Racing. Text = Racing Energy" (the ask, verbatim, after two earlier
attempts at this split didn't land): score/timer/speedometer-value are pure
numbers, `hud`/`menu` menu-panel labels are pure text, and `draw_hud_event`'s
banner ("CHECKPOINT +200") and `draw_game_over`'s score line ("SCORE 13170")
mix both in one string. The FIRST fix for the mixed case was a render-time
split -- draw the label in one font, measure it, draw the value in the other
flush after it -- which worked but was real machinery (a duplicated split/
measure/draw path) for what turned out to be a font-authoring problem, not a
rendering one: "just combine all the rendered glyphs into a single font."
`assets.toml`'s `menu` font now does exactly that -- `overlay_source`/
`overlay_charset` (a new `pnx_assets.py` capability, `pack_font`) rasterise
just the digits/`+` from Monster Racing while every other character still
comes from Racing Energy, all baked into ONE packed font asset at build time.
`render.c` went back to one plain draw call per string once this landed; no
game code needs to know two typefaces are involved. `hud` (score/timer) stays
pure Monster Racing -- its own charset is 100% digits already, nothing to
overlay. See `assets.toml`'s own comment on the `menu` font for the fuller
back-and-forth (Monster Racing needed `tracking` to stop letters touching at
14px; Racing Energy read fine for letters but its own digit glyphs turned out
broken; the combination keeps only each face's strong half).

**The speedometer is a vertical stack of horizontal bars, not a curved arc.**
Went through two designs before landing: first a straight bar-plus-"N MPH"-text
gauge, then a curved ring of small rotated rectangles swept clockwise from the
corner (both replaced after direct follow-up -- "no MPH text," then "I want it
to be counter clockwise," then a full redescription: "purely horizontal bars...
a synthwavey stack of slightly differently colored boxes... shift to a more
saturated color when activated. Imagine a cornucopia resting vertically, the
green tip on the bottom, wide opening on the top... remove chunks of horizontal
sections so it has a transparent gradient look"). `render.c`'s `draw_speedometer`
now draws `GAUGE_SEGMENTS` (6) plain axis-aligned `fb_rect` bars, right-aligned
to a shared edge and bottom-up stacked from the round-safe/car-safe pivot (below)
-- bar 0 narrowest/green at the bottom, bar 5 widest/red at the top, a real gap
between bars (not touching), no outline (the gauge's own "no black background"
equivalent). `gauge_segment_colour` is the bar's fixed, printed-scale hue (green
-> yellow -> red across the SEGMENT INDEX, the same two-stop `pnx_tween_gcolor8`
cross-fade the sky/sun ramps use); `gauge_segment_dim_colour` blends that toward
a near-black navy for bars not yet reached, rather than one shared grey for
every unlit bar, so even a dim bar keeps a hint of its own eventual hue. How
many bars are lit is the actual value slider -- a plain fraction of `MAX_SPEED`.
The speed VALUE itself sits above the widest bar, `menu_font` (small, mixed
per above) rather than `hud_font` -- no unit suffix. Being a stack of plain
axis-aligned rects rather than a rotated arc is also what made this trivial to
keep round-safe and identical on square displays: only the shared pivot point
needs the round-safe/car-safe check (below), since every bar's own footprint
is strictly further from the true corner than the pivot in both axes (bars
extend left of it, stack upward from it) -- "the same shape should exist on
the square displays but placed in the right corner" is true by construction,
the geometry never referenced platform shape at all.

**A real rasterisation bug caught along the way**: the curved-arc draft's
rotated-rectangle segments (since replaced) used forward-mapped local (t, r)
steps into world pixels, which doesn't guarantee one output pixel per one
integer input step in every direction -- some destination pixels were never
hit at all, visible as flecks of the ROAD's own magenta grid lines showing
through what should have been solid segments in an emulator screenshot. Fixed
(at the time) by scanning the world-space bounding box and testing each
candidate pixel against the rotated rect via inverse transform instead --
moot now that the design itself changed to plain axis-aligned bars, but worth
remembering the shape of for any future rotated-shape fill in this engine.

**Round-safe placement**: see "Multi-platform" below for `round_safe_margin`/
`round_safe_row_for_width`, the project-local math that places all of the
above correctly on a round display.

**A real bug this caught**: the `menu` font's charset (`assets.toml`) was
literally never given digits -- `extra = "ABCDEFGHIJKLMNOPQRSTUVWXYZ :"`, built for
"PAUSED"/"STEER: TILT"-style text only, before any HUD element ever needed a
number. A character outside a font's charset renders as `FONT_ABSENT` --
silently nothing, not a visible fallback glyph -- so the first draft of the score/
timer HUD (before the `hud` font existed) rendered as a couple of stray pixels
per number, confirmed via a rotated emulator screenshot before the cause was
clear. Fixed by adding digits and `+` to `menu`'s own `extra`, independent of
adding the new `hud` font for the numeric readouts themselves.

## Personalization: car recolor

The player's car should be recolorable. `tools/pnx_assets.py`'s `[[sprite]]`
`variants` mechanism is the fit: a variant shares the base sprite's frame
geometry/dimensions exactly (checked at build time -- `shape_signature` must
match) and costs one extra palette per recolor, not a second copy of every frame.
`hangon_bike`'s green/yellow recolors in `assets.toml` were done as flat baked-in
frames instead specifically because that source sheet never split them into
separate images (see that sprite's own manifest comment) -- `touring_normal`
doesn't have that constraint, so a proper `variants` entry (one recolored sheet,
or several) is the right approach here, not a repeat of the bike's workaround.
`touring_normal` already has exactly this working, for traffic rather than
player customisation -- see "Aesthetic"'s "Traffic recolour" for the real,
built example this section describes (one variant sheet, one palette slot,
`pnx_palette(SPRITE_..._PALETTE_...)` passed at draw time), still not built
for the player's own choice of colour.

## Controls

Landscape, held in two hands, cluster along the **top** edge
(`PNX_ORIENT_BUTTONS_TOP` -- shoulder triggers, per `docs/PLATFORM.md`'s "what it
makes possible"):

- **Right top button** (physical DOWN, `pnx_input_cluster(2)`): gas
- **Left top button** (physical UP, `pnx_input_cluster(0)`): brake
- **SELECT** (physical middle, `pnx_input_cluster(1)`): while paused, swaps the
  steering mode (Touch/Tilt); a no-op while driving.
- **Steering**: touch drag left/right (`pnx_input_drag_dx`) by default, with
  accelerometer tilt as a second option, toggled from the pause menu (SELECT).
  Thumbs rest on the screen while index fingers work the top buttons in the
  touch case; tilt frees the thumbs entirely, which may end up the more
  natural two-handed hold for this control scheme -- neither is confirmed as
  the better feel yet, both are worth having since the button cluster has no
  spare axis for steering once both ends are claimed by gas/brake. Tilt reads
  `PnxAccel.y` with `ACCEL_STEER_DEADZONE` (`game.h`), negated in
  `game.c`'s `steer_input` -- **confirmed on real hardware**: the axis was
  right but the raw sign had tilt-left steering right, backwards; fixed.
- **BACK**: pauses/resumes (single click only -- a long hold force-quits at
  the OS level unconditionally, per `docs/PLATFORM.md`, so there is no in-app
  quit to build). No longer calls `pnx_platform_quit()`. While `game_over`
  ("BUSTED" -- see "Police: chase, not traffic"), BACK restarts instead
  (`game_restart`) -- `main.c` gates which behaviour on `Game.game_over`.

Accelerometer support did not exist anywhere in the pebblnyx engine before
this -- added as a platform-layer capability (`PnxAccel`,
`pnx_platform_accel_read`/`pnx_platform_has_accel` in
`src/pnx/platform/pnx_platform.h`, implemented via the Pebble SDK's
`accel_service_peek` -- no subscription needed, which is what makes it fit
pebblnyx's poll-once-a-frame model). Deliberately unopinionated about which
axis means "steer": that depends on which landscape orientation a game holds
the watch in, which is a game choice the platform layer shouldn't bake in.

## Aesthetic

Neon synthwave. Sprite assets (`art/NES_Touring_Car_Sprite_Sheet.png`, the police
variant, and the Hang-On bike sheet) are still placeholder-grade NES/Genesis
rips -- out of scope for an art pass done entirely in code, since replacing them
needs new source art this project doesn't have. Everything drawn from flat
colour, though, is built: sky, horizon sun, and the ground grid all landed in one
pass (`render.c`'s `draw_sky`/`draw_sun`, and the grid lines `draw_road` adds
beyond the pavement edge).

**Palette-resolution dithering, not flat bands.** `pnx_gfx`'s colour build is 2
bits/channel -- 64 colours total -- so any gradient built purely from
`pnx_gfx_fill_rect` steps reads as visible banding, not a smooth ramp. Added
`pnx_gfx_fill_rect_dither` (engine-level, `src/pnx/gfx/pnx_gfx.c`/`.h`, host-tested
in `tests/test_gfx.c`): an ordered 1-pixel checkerboard between two colours,
keyed off absolute framebuffer `(x, y)` rather than rect-relative offsets so two
calls covering adjacent areas tile as one continuous pattern instead of each
restarting its own phase at its own corner. `draw_sky`/`draw_sun` dither the
single row where a colour ramp's index changes rather than cutting hard between
solid bands -- the same trick pixel art has always used to fake a bigger
palette, applied at engine level rather than composed from many small
`fill_rect` calls (which would have meant hundreds of extra draw calls a frame
for a true per-pixel checkerboard, a real cost at this frame budget -- see
`docs/MEASUREMENTS.md`'s PT2 frame-rate figures).

- **Sky**: a 6-step ramp from near-black indigo at the top through purple and
  magenta to an orange-pink glow at the horizon (`SKY_RAMP`, `render.c`).
  Continuous in `y / horizon_y`, not a fixed row count, so it holds up as
  `current_horizon_y` itself changes with the player's slope.
- **Horizon sun**: a filled circle (`pnx_isqrt` per row for the half-width, no
  libm), gradient-shaded core-to-rim with the same dithered-boundary
  treatment, sliced by a few gap bands through the lower half -- the
  sun-behind-venetian-blinds look every retrowave horizon uses. Both size
  (`SUN_RADIUS`) and screen position (`SUN_TOP_OFFSET`/`SUN_START_X_OFFSET`,
  anchored to the TOP of the screen) are fixed, independent of `horizon_y`.
  This took two corrections to land: radius originally scaled with
  `horizon_y` (shrinking the sun on a shortened uphill sky) and position was
  computed from the horizon line, both of which visibly resized/bobbed the
  sun with every hill and valley -- reported directly, twice ("the sun
  changes sizes based on the horizon's size", then "the sun is definitely
  still moving up and down based on hills and valleys" after only the size
  had been fixed). `horizon_y` is still read for the one thing it should
  affect -- the pixel-visibility bound (`y < horizon_y`) -- so a hill still
  occludes the sun the way it occludes the real horizon; it just no longer
  feeds back into the sun's own size or centre. Confirmed directly:
  screenshotted the sun at the same fixed screen position across a flat
  segment and a hill with a visibly different `horizon_y`.
- **Ground grid**: magenta lines beyond the road edge, at fixed world-space
  offsets from `ROAD_HALF_MAX`, scaled by `row.half_width` the same way
  `draw_traffic`'s own `screen_x` scales anything off the road centre -- so a
  line at a fixed world offset still converges toward the horizon correctly
  through curves and hills, not just on a flat straight. Drawn every other row
  only (cost: full coverage doesn't read differently at this resolution and
  would double the `fill_rect` calls in the hottest loop in the renderer).

**A slow day/night cycle, not a fixed look**: "that whole color gradient
should happen over the length of a drive and the sun should move right to
left and down as we progress. slowly." `sky_cycle_progress` (`render.c`) is a
0..1000..0 triangle wave over `SKY_CYCLE_LENGTH` (300000 world units -- 7-8
minutes each way even held at `MAX_SPEED` flat out, raised from an initial
80000 that was "much" too fast per direct follow-up -- a background mood
shift, not something to watch tick by) driven by `g->distance`. Both
`SKY_RAMP_SUNSET`/`SKY_RAMP_NIGHT` and `SUN_RAMP_SUNSET`/`SUN_RAMP_NIGHT` are
keyframe pairs; `pnx_tween_gcolor8` (the shared engine library, below) cross-
fades every entry between them each frame (plain per-channel interpolation in
this palette's own 2-bit/channel space, not dithering -- the two keyframes
are close enough, and the cycle long enough, that consecutive frames rarely
even land on a different integer channel value). The sun's own screen
position starts at `SUN_START_X_OFFSET`/`SUN_TOP_OFFSET` (top-right, partly
clipped by the top edge -- "farther off to the right top of the screen", then
"start higher still") and drifts left (`SUN_DRIFT_X`) and down (`SUN_DROP_Y`)
via `pnx_tween_i32` as progress advances, easing back over the second half of
the cycle rather than snapping to a start position -- a triangle wave, not a
sawtooth, specifically so driving forever never produces a jarring reset,
just a continuous breathe between sunset and night and back. `sky_cycle_progress`
itself (the triangle wave's own 0..1000 position, keyed off world distance)
stays project-specific code, not a `PnxTween` -- that primitive is a one-shot
run driven by elapsed real time, with no way to hand it a repeating,
distance-keyed cycle that never "finishes"; see that function's own comment.
Confirmed directly (temporarily shortened `SKY_CYCLE_LENGTH` for a fast test,
reverted after): sky cools from the warm sunset ramp to pale moonlit blues,
the sun visibly drifts left/down and turns into a dim moon-like disc at the
cycle's midpoint, then both ease back.

**Built on `pnx_tween` (`src/pnx/core/pnx_tween.h`), a shared engine library,
not project-local math.** The sky/sun work above hand-rolled this same
pattern -- a 0..1000 progress value, a per-channel colour lerp, a plain
linear interpolation for the sun's own drift -- three separate times before
it was pulled into a real, reusable, opt-in (`PNX_USE_TWEEN`, defaults on)
module with its own host tests. `render.c` no longer defines its own
`lerp_colour`; `pnx_tween_gcolor8` replaced it directly (identical values,
confirmed by an unchanged screenshot before/after the swap), and the sun's
own `cx`/`center_y` drift now goes through `pnx_tween_i32` instead of hand-
written `(DELTA * t1000) / 1000` arithmetic at each call site.

Confirmed on the `emery` emulator across a flat straight, a curve, a hill (sun
visibly smaller), traffic, the BUSTED overlay, and a full shortened test cycle
(sunset -> night -> sunset) -- no artifacts, and the police light-bar wash
layers correctly over the new grid at any point in the cycle.

**Road edge and lane markings, redone as a real road rather than a circuit.**
Reported directly: "the white and red edges are more like a racetrack and the
lane guides are too thick and need to be more like real road lane guides."
The original rumble strip (`COLOUR_RUMBLE_A`/`B`, alternating bold white/red
across the full `rumble_width` band) was OutRun's own circuit-kerb look, not a
road shoulder. `draw_road` now draws that same band as a muted, alternating
grey-tan paved shoulder (`COLOUR_SHOULDER_A`/`B`, the same close-shade motion
cue `COLOUR_GROUND_A`/`B` already use) with a separate, thin solid-white edge
line (`COLOUR_EDGE_LINE`, `EDGE_LINE_FRAC` = `half_width`/14) painted over the
road/shoulder boundary -- a fog line, not a kerb. Lane dividers went from
`half_width`/12 wide (a bold neon bar, roughly 8% of the road's own width) to
`half_width`/`LANE_DASH_FRAC` (30) -- proportionally closer to how thin a real
lane line reads against the road it's painted on. UI chrome and the road/
ground surface colours themselves were already reasonably synthwave-adjacent
from earlier work and were left as-is.

**Traffic recolour, no new art required.** Traffic reuses the player's own
`touring_normal` sprite handle at other tiers/angles (`draw_traffic`,
`game.h`'s own comment on why that's not a second sprite load) -- which meant
it rendered in the same green as the player's own car, hard to tell apart at
a glance. Reported directly: "recolor the traffic cars so they stand out on
the road." `assets.toml`'s `variants` mechanism is a palette swap, not a
second copy of every frame -- exactly the tool the "Personalization: car
recolor" section above already earmarked for this, so no new source art was
needed: `art/traffic.png` is `touring_normal`'s own base sheet with its one
body-green pixel value (85, 199, 83) programmatically remapped to orange
(255, 170, 0) and nothing else touched (outline, brown accents, transparency
untouched) -- a Python/PIL pixel remap, not hand-drawn art. The pipeline
emits `SPRITE_TOURING_NORMAL_PALETTE_TRAFFIC` as a palette slot;
`draw_traffic` passes `pnx_palette(SPRITE_TOURING_NORMAL_PALETTE_TRAFFIC)`
where it used to pass `NULL`, so the exact same frame data draws recoloured
without a second sprite load. Confirmed directly by the user in real play:
"I verified that the traffic vehicles pop against the purple and blue road."

## Feature backlog

A brainstorm pass turned up a large menu of polish/flash ideas, organized here by
what they need. Nothing in this section is scheduled -- it's a backlog to pull
from, not a plan. One correction against the brainstorm as given: pebblnyx sprites
have **no runtime scale** (`docs/MEASUREMENTS.md`/the engine's own design) --
depth is pre-rendered distance tiers (`touring_normal`'s 6 tiers), not a
`pnx_sprite_set_scale()`-style call, because that function doesn't exist. A
"turbo stretch" effect needs a different trick (e.g. swap to a taller/narrower
pre-baked tier, or a screen-space squash via `pnx_blit_4bpp`'s existing flip/
transpose machinery) rather than runtime scaling.

**Visual**
- Layered roadside scenery (foreground/midground/background at different scroll
  rates) -- real fit for `PNX_USE_LAYERS`' `parallax_pct` (`pnx_layer.h`), which
  already exists for exactly this.
- Turbo boost effect (colour shift, speed lines, faster scenery scroll) -- scale
  correction above applies to the "car stretches" part specifically.
- Roadside scenery -- trees and signs whipping past at the road's edge, two-frame
  animated for signs. Explicitly asked for and explicitly deferred (not
  brainstormed-only): needs real sprite art, which this project doesn't have: a
  procedural/vector alternative (flat-shape silhouettes, no art dependency) was
  offered and declined in favour of waiting for real art instead.
- Car shadow, scaling with road perspective (a small filled ellipse/rect sized
  off the same `RoadRow.half_width` the road already computes per row).

**Audio** (`pnx/audio/` -- `pnx_audio`, `pnx_music`, `pnx_synth` all exist; the
batch Speaker API is unusable here -- ~94ms/submission and no concurrent
playback -- so this has to go through the engine's own streamed software mixer,
not a naive per-effect play call)
- Chiptune soundtrack, tempo/key shift on checkpoint.
- Engine note that pitches with speed (variable playback rate on a loop).
- Continuous road-noise bed.
- Short SFX: pickup chime, crash crunch, checkpoint ding.

**Gameplay**
- Designed traffic patterns (a blocking wall, a precision gap, a weaving convoy)
  instead of pure-random spawns.
- Branching paths with a difficulty/reward tradeoff (easier+more time vs.
  narrower+more traffic+higher score).
- Perfect-checkpoint bonus (centered crossing).
- Near-miss bonus (close pass without contact).
- Drift (brake+steer into a turn for an exit speed boost).
- End-of-run grade (S/A/B/C/D) from the score, for replay incentive.

## What's built vs. not

Built (across this and the following session):
- Orientation set to `buttons_top` in `assets.toml`, asset pipeline re-run.
- Split into `track.c` (road geometry + curve data) / `game.c` (simulation) /
  `render.c` (drawing) / `main.c` (frame loop + boot), the same shape
  `examples/pinball` uses -- see each file's own top comment.
- Pseudo-3D road, 4 lanes (`LANES` in `track.h`) with dashed dividers between
  each, rumble strips, scrolling ground/road texture bands for a sense of
  speed.
- Curved road segments: a small procedural test loop (`TRACK` in `track.c`,
  not a real map yet) drives a near-to-far curve accumulation in `render.c`'s
  `draw_road`, the standard double-integration (curve -> rate -> offset) every
  segment-based pseudo-3D racer uses.
- Inside-vs-outside cornering: a curve pulls the car toward its outside each
  tick (`game_tick`'s centrifugal term) unless the player steers into it: the
  inside of the bend holds full speed, the outside costs it, same as the
  existing off-rumble-strip penalty.
- Player car sprite driven by touch-steer + gas/brake, clamped lane position,
  fractional (ACCEL_NUM/DEN) acceleration ramp (~3s to top speed, not instant).
- Illusion of speed: the player's car pulls back a little and swaps to a
  smaller pre-rendered tier as speed rises (`PLAYER_NEAR_ROW_MIN/MAX`,
  `PLAYER_TIER_MAX` in `game.h`) -- pebblnyx sprites have no runtime scale, so
  this is the same tier-swap trick traffic uses for depth, capped well short
  of traffic's full 0-5 range. Also leaves room near the true near edge for
  the chasing police car (`POLICE_BEHIND_ROW_MIN/MAX`, see "Police: chase,
  not traffic") to render without fully overlapping the player's own car.
- Traffic: `MAX_TRAFFIC` cars (`game.h`) crawling forward on their own,
  close to but still below the player's own top speed (`TRAFFIC_MIN/MAX_SPEED`
  = 17-22 vs `MAX_SPEED` 26 -- raised from an original 8-14 that made traffic
  close distance so fast it read as "zooming by"/"out of nowhere"), snapped to
  a lane centre (`lane_center()`, `track.h`), recycled ahead once the player
  passes one. Reuses the player's own `touring_normal` sprite handle at other
  tiers/angles (`render.c`'s `draw_traffic`) rather than a second sprite load.
  `TRAFFIC_SPAWN_AHEAD_MIN/SPAN` dropped from 600-1400 to 250-550 after a
  report that traffic seemed disabled entirely -- it wasn't; the accel ramp
  (~3s) makes the gap to a distant car GROW before the player is fast enough
  to start closing it, so the original spawn distance meant 15-20+ seconds
  of uninterrupted driving before the first car was ever reached. Confirmed
  by emulator log (not screenshot -- the visible window is narrow and easy
  to miss with one snapshot): `relz` (traffic Z minus player distance)
  descending cleanly through the visible range and going negative once
  passed, then the car recycling ahead correctly. The mechanism was never
  broken; a short or interrupted test drive (gas released and re-pressed)
  restarts the accel ramp each time and can make it look that way.
- Crashes: a collision (world-Z AND lane both close, `check_collision` in
  `game.c`) stops the player, plays `touring_crash`'s 4-frame clip
  tick-based (not `pnx_anim_play`, which is wall-clock and would keep
  running through a paused sim -- see `game.h`'s own comment), holds the
  wreck frame briefly, then returns control. The traffic car that caused it
  is recycled immediately so it can't re-trigger the same collision next
  tick. `touring_crash`/`police_crash`'s frame 0 (impact flash) was
  `[24, 432, 197, 24]` in `assets.toml` -- 197px wide against the other three
  frames' 33-52px, because the source sheet actually draws a much longer
  tumble sequence at that row (with no gap between adjacent poses) than the 4
  frames meant to be pulled from it. The old rect captured roughly the first
  five poses of that sequence overlapping in one image, which rendered as
  "multiple frames of the crash all at once" (reported directly). Narrowed to
  `[24, 432, 42, 24]` -- just the first pose, same scale as frames 1-3 -- in
  both sprites (the police sheet is laid out identically at this row, just
  recoloured).
- Pause menu: BACK toggles `Game.paused`, which freezes `game_tick` at its
  own top (traffic, crash countdown, everything). Draws a panel over the
  frozen last frame (`render.c`'s `draw_pause_menu`) showing the current
  steering mode and the two controls that matter (SELECT swaps mode, BACK
  resumes). First text in the project -- added a `menu` font
  (`art/fonts/LiberationSans-Regular.ttf`, already used by other pebblnyx
  examples, SIL OFL) since none existed before.
- Accelerometer tilt steering, toggled from the pause menu -- confirmed and
  correct on real hardware (see "Controls" above for the sign-fix note).
- On-screen containment: `PLAYER_LANE_MAX` (`game.h`, 85) hard-clamps the
  player's `lane_x`, strictly wider than `ROAD_HALF_MAX` (`track.h`, down
  from 130 to 75) so there's real off-road territory between the pavement
  edge and the wall, but narrow enough the car's own sprite can never draw
  past the screen edge -- it used to (130 was wider than LOGICAL_W/2).
- Off-road speed: capped at `OFFROAD_MAX_SPEED` (8), not braked toward 0.
  Originally also carried a continuous drain on top of the cap, which
  dragged the car to a dead stop rather than just limiting it -- removed
  per the user's own framing ("shouldn't stop the car, just limit top
  speed"); the ceiling alone is `pnx_min_i32`, no separate deceleration.
- Turn rate scales with speed (`TURN_RATE_SPEED_CAP`, `game.h`): none at a
  standstill, ramping linearly to full `STEER_RATE` by half of `MAX_SPEED`,
  clamped (not reduced) above that. Verified via emulator logs: lane_x
  stayed put at speed 0-1 under sustained tilt, then moved progressively
  faster as speed rose (0/1/5/8 speed -> 0/0/15/50 lane_x in successive
  ticks).
- True linear road perspective (`track.h`/`track.c`'s `road_row`): the road's
  half-width is linear in screen row, zero at the horizon row and
  `ROAD_HALF_MAX` at the near row -- replaced an earlier `1/depth` (hyperbolic)
  width formula, which put a curve bulge in the edges of even a flat straight
  road, reading as the road climbing on its own regardless of actual
  elevation. Also fixed the near rows visually "falling straight down" under
  the car, which was the clamp on that old formula's width saturating across
  many consecutive near rows instead of tapering.
- Dynamic view distance (`track.c`'s `current_horizon_y`): the effective
  horizon row shifts with the player's current slope -- cresting a hill
  shortens the visible span (fewer rows drawn before the road vanishes,
  the same way a real hill blocks the view over its far side), a valley
  lengthens it. All per-frame row sweeps (`draw_road`, `draw_traffic`,
  `road_curve_offset`, `road_elevation_offset`) take this as a parameter
  rather than assuming a fixed horizon.
- Road elevation / hills and valleys (`track.c`'s `track_elevation_at`,
  `road_elevation_offset`; `render.c`'s `draw_road`): same near-to-far
  double-integration `curve` already used, applied to each row's drawn
  screen Y instead of its X. A steep enough slope can make two rows want the
  same line (clamped to strictly decrease) or skip several lines between
  one world-row and the next -- unclamped, the first collapses distinct rows
  into static; unfilled, the second leaves gaps that keep showing a stale
  colour from whichever earlier frame last drew them, which (since elevation
  changes every tick) accumulates into dense, seemingly-random horizontal
  noise across frames even though any single frame's own row sweep is
  internally clean. Fixed by giving each row's fill an explicit `height`
  (rows actually claimed, not always 1) instead of a fixed single-pixel
  strip, so gaps get stretched shut instead of left undrawn.
- Police pursuit AI and the "BUSTED" game-over -- see "Police: chase, not
  traffic" above.
- Synthwave sky gradient, horizon sun, ground grid, the slow sunset/night
  drive-length cycle driving both, a real-road edge/lane redesign, and the
  traffic recolour, plus the engine-level `pnx_gfx_fill_rect_dither`
  primitive it's all built on -- see "Aesthetic" above.
- Checkpoints, race timer, time-added-on-checkpoint, and a first-cut scoring
  formula (distance/near-misses/checkpoints/police takedowns) with an
  outlined-text HUD and a curved segmented speedometer gauge -- see
  "Checkpoints, timer, and scoring" above.
- All seven pebblnyx-supported platforms, including round-safe HUD placement
  (`chalk`/`gabbro`) and a new engine-level `pnx_text_draw_outlined` primitive
  -- see "Multi-platform" above.

Not built yet, in roughly the order they'd naturally get added:
- Multiple maps/stage select, high score persistence (`pnx_save` exists and is
  host-tested per prior work, not yet wired here -- the score built this session
  lives only for the current run, same as `distance` always has).
- Car recolor (`variants`, see "Personalization: car recolor").
- Synthwave art pass (current art is placeholder).
- Feature backlog items, pulled in whenever -- see that section.

## Open questions

- Steering-input choice (touch drag vs. something else) is a guess -- confirm once
  it's playable in-hand.
- Scoring weights (`CHECKPOINT_SCORE` 200, `NEAR_MISS_SCORE` 150,
  `POLICE_TAKEDOWN_SCORE` 2000, `DISTANCE_SCORE_DIVISOR` 10 -- game.h) are a first
  cut, eyeballed the same way the curve-tuning constants below were -- not felt
  in-hand or balanced against an actual full run yet, though `CHECKPOINT_SCORE`
  and `CHECKPOINT_TIME_BONUS_TICKS` at least are no longer PURELY eyeballed --
  see `CHECKPOINT_PERFECT_TICKS`'s own comment for the derivation. Whether a
  checkpoint should additionally reward centred crossing ("perfect checkpoint,"
  feature backlog) is still open.
- **`SCENE_BYTES` (game.h) is hand-tuned and has already silently broken twice**
  (`police_normal` failing to load, then `traffic_car` on the very next asset
  added after that) -- direct feedback: "manually adjusting scene arena like
  this is tedious and a foot-gun for users." `pnx_assets.py` already computes a
  per-scene resident-byte report (`report_scene_budgets`) but only when a
  project declares `[scene.*]`, which this one doesn't, and even then it's a
  console report a human has to read and hand-copy, not a constant a game can
  just use. The easy fix (emit a generated `PNX_ASSET_..._BYTES` upper bound,
  sum of every declared sprite/font/palette, and size the arena off that) was
  deliberately NOT taken -- explicitly asked for something more creative than
  "reserve the conservative max and let it sit unused" first. Revisit; no
  design decided yet.
- **The running packaged `pebblnyx-editor` app silently reverted BOTH the engine
  patch and the resource build twice this session** -- it bundles its own,
  older copy of pebblnyx (engine C sources AND `tools/pnx_assets.py`), and
  re-stages/rebuilds from that bundle on its own schedule, independent of and
  overwriting whatever the live `~/CLionProjects/pebblnyx` checkout produces.
  First hit: `pnx_text_draw_outlined` (a live-checkout-only addition) vanished
  from `src/c/pnx/`, `pebble build` failing with an implicit-declaration error
  on every platform -- fixed by re-staging from the live checkout and calling
  `pnx_project.take_engine_ownership(folder, True)` (`.pknproj`'s
  `engine_owned`), pebblnyx's own existing mechanism for exactly this, so the
  editor stops touching `src/c/pnx/` for this project going forward. Second
  hit, same session: `resources/font_menu.bin` got silently rebuilt by the
  editor's own bundled (overlay-unaware) `pnx_assets.py`, which doesn't
  understand `overlay_source`/`overlay_charset` at all and just silently
  drops unknown manifest keys -- every digit in `menu` fell back to Racing
  Energy's own broken numerals again, all rendering as the SAME glyph
  (confirmed by parsing the packed `.bin` directly: identical offset/w/h for
  every digit 0-9, a `pack_font` dedup collapsing what should have been ten
  distinct glyphs into one). Reported directly as "all numbers except the
  timer and score on the HUD are busted" -- score/timer (`hud` font, no
  overlay, pure single-source Monster Racing) were never touched by this,
  which is exactly why they were the two things that still worked. Fixed by
  re-running the live checkout's `pnx_assets.py` again. **No engine_owned-
  equivalent lock exists for resources/the asset pipeline** -- unlike the C
  engine, there is nothing stopping the old editor from silently redoing this
  again the next time it touches this project. Until the packaged editor is
  rebuilt from a checkout that has the overlay feature, re-running
  `python3 <live-checkout>/tools/pnx_assets.py assets.toml --package
  package.json` is the recovery step if digits break again with this exact
  signature (every digit identical).
- The `hud` font (`assets.toml`'s "Monster Racing") is free for personal use only
  -- fine for local builds, a blocker before any appstore submission or other
  distribution unless replaced or licensed. See its own `[[font]]` comment.
- Pursuit tuning: one pursuer at a time, occasional cooldown-based spawn,
  proportional gap-correction chase speed (see "Police: chase, not traffic")
  are all eyeballed starting points, not felt in-hand or confirmed against
  real play sessions. Whether difficulty should scale pursuit frequency over
  a run is still open.
- `track.c`'s `TRACK` is a hand-tuned procedural loop for feel-testing curves,
  not a real authored course -- "multiple maps" needs an actual track format
  (and probably an editor tab) eventually, not more hardcoded segment arrays.
- Curve tuning constants (`CURVE_SCALE`, `CENTRIFUGAL_DIVISOR`,
  `CORNER_INSIDE_BONUS`, `CORNER_OUTSIDE_NUM`/`DEN` in `game.h`) are eyeballed
  from emulator screenshots, not felt in-hand on real hardware yet.
- `draw_pause_menu`/`draw_game_over`'s panel (`fb_rect(target, 24, 48, LOGICAL_W-48,
  104, ...)`) still uses fixed pixel margins tuned against `emery`'s 200x228 --
  not re-derived per platform the way the HUD now is (below). Checked against
  `chalk` (180x180): the panel's own bottom-left corner sits ~0.6% outside that
  platform's inscribed circle (dist²=8200 vs r²=8100) -- barely, probably a
  single clipped row, not confirmed visually. Also untested on `aplite`/
  `diorite`'s shorter 168px-tall display (168 - (48+104) = 16px bottom margin --
  cramped, not clipped, since those are rect). `round_safe_margin()` (`render.c`)
  exists now and could size this panel too; not done here since it wasn't part
  of what this session's checkpoints/scoring/HUD work actually touched.

## Multi-platform

Was `targetPlatforms: ["emery"]` only; now targets all seven pebblnyx supports
(`aplite`, `basalt`, `chalk`, `diorite`, `emery`, `flint`, `gabbro` --
`package.json`). Confirmed by building all seven, clean, via the project's own
`pebble build` (`pebble clean` first to force every platform to actually
recompile rather than reuse a cached emery-only tree) -- no errors, no resource-
budget overage on any of them. The asset pipeline's existing `pack_2bit`
mechanism (`assets.toml`'s own pipeline output: "emitting ~bw resource variants")
already emits 1-bit variants for `aplite`/`diorite` automatically; nothing in
this project had to change for colour depth. `aplite` is the tightest fit --
6,619 B free heap of a 24 KB budget -- but links and reports fine; not yet run
on the `aplite` emulator or real hardware, only confirmed via the linker's own
memory report.

**Round-safe HUD placement**, the one real gap: corner-anchored UI (score,
timer, speedometer) is correct on a rect display but wrong on a round one
(`chalk`/`gabbro`, both `PBL_ROUND`) -- a literal screen corner falls outside
a round bezel's visible circle. `docs/PORTING.md`, pebblnyx's own porting
reference, names this exact problem ("Round corners | safe-area rect from
per-row bounds") with no engine primitive for it yet anywhere in the framework
-- real work, not something to fake with a guessed margin. Built here instead,
project-local, two related functions in `render.c`, both integer-only (squared-
distance comparisons, no sqrt/trig -- `LOGICAL_W == VIEW_H` on every round
platform makes the visible area exactly the circle inscribed in that square,
so this is exact math, not an approximation):
- `round_safe_margin()`: the smallest inset `m` such that a box's own near
  corner, offset `(m, m)` from whichever screen corner it's anchored to,
  provably clears the circle. A box's far corner, extending inward by its own
  width/height, is always closer to centre than this near corner, so checking
  just the one point is sufficient regardless of box size -- what
  `draw_hud`'s score/timer and `draw_speedometer`'s gauge pivot are both
  anchored against.
- `round_safe_row_for_width()`: the smallest row at which the circle is at
  least a given width -- for CENTRED content (`draw_hud_event`'s banner),
  whose exposure is width, not a corner. Added after a first assumption --
  "centred content only gets safer moving down the screen" -- turned out true
  in DIRECTION but not automatically SUFFICIENT: a "CHECKPOINT +500" banner
  positioned right below the score/timer row still ran past the bezel on the
  right, confirmed directly on the `chalk` emulator (rotated screenshot), once
  its own text turned out wider than the row it was sitting at could clear.
  Fixed by deriving the row FROM the banner's own measured width instead of
  assuming any fixed offset was enough.

`round_safe_margin` also anchors `draw_speedometer`'s pivot (its bar-stack
design now, see "Checkpoints, timer, and scoring" above -- was a curved arc of
rotated segments when this was first verified, below, but the safety argument
carries over unchanged: every bar's own footprint is strictly further from the
true corner than the pivot, in both axes, so the pivot clearing the check is
sufficient regardless of what shape extends from it).

Confirmed on the `chalk` emulator (rotated screenshots, same pipeline used
throughout this project): score, timer, the gauge (the curved-arc version, at
the time), and the checkpoint banner all clear of the bezel with visible
margin, across a fresh boot, mid-drive, and max-speed sequence, before AND
after the banner fix above (the fix was caught by this same screenshot
process, not assumed correct on the first attempt). Re-confirmed `emery`
(rect) renders correctly throughout -- every `#ifdef PBL_ROUND` branch is
inert there, not just visually similar. Not re-screenshotted on `chalk` since
the gauge's own redesign to a bar stack -- the geometric argument above is
unchanged either way, but this is a real gap in DIRECT confirmation, not just
reasoning, worth closing before trusting it further.

**Not done**: `gabbro`'s own QEMU emulator has known missing-pixel rendering
issues (`pebblnyx/docs/EDITOR.md` flags this independently) that made it a
worse test target than `chalk` for visual confirmation -- round-safety was
verified geometrically (the same circle math applies to both, `LOGICAL_W`/
`VIEW_H` just differ) and on `chalk` directly, not screenshotted on `gabbro`
itself. Neither round platform, nor `aplite`/`diorite`/`basalt`/`flint`, has
been run on real hardware.

**Fixed bug, worth remembering the shape of:** the outside-of-a-curve penalty
was originally a flat `-2`/tick, every tick, unconditionally, whenever the
car was on the wrong side of ANY curve -- which is any curve at all unless
the player is actively counter-steering, i.e. the default case for anyone who
hasn't already learned the mechanic. Since gas only adds `ACCEL_NUM/DEN`
(~+0.33/tick) on average, a flat per-tick penalty of 1 or more always wins
that race eventually, down to a dead stop gas alone could never recover from
-- reported verbatim as "the game stops once we reach the first curve."
Fixed by making the penalty fractional via the same accumulator trick as
`ACCEL_NUM/DEN`, tuned strictly BELOW accel's rate (`CORNER_OUTSIDE_NUM/DEN`
= 1/6 vs accel's 1/3) so full gas always nets positive speed even on the
outside of every curve. The general lesson for any future speed-modifying
mechanic here (drift, weather, damage, whatever): a flat per-tick penalty
applied under a plausible-to-sustain condition is a stall waiting to be
found, not just a tuning number -- check it against ACCEL's own rate, not
just against how it feels in isolation.
- Time-to-first-traffic from a cold start is still ~10-15s even after the
  `TRAFFIC_SPAWN_AHEAD_MIN/SPAN` cut (see "Traffic" above) -- the ~3s accel
  ramp alone adds a few hundred units of gap before the player is even
  faster than traffic, which a shorter spawn distance can't fully undo
  without either speeding up the ramp (undoes the "goes incredibly fast"
  fix) or slowing traffic further (undoes the "zooms by" fix). Acceptable
  for now; worth another look once there's a full race (start-of-run
  pacing) to feel rather than a synthetic test.
