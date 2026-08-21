# Need 4 Pebble

An OutRun-style pseudo-3D racer for Pebble Time 2 (emery). Landscape, two-handed,
shoulder-trigger controls.

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
one they already passed). No scoring hook yet (see "Open questions" --
`police_takedowns` in the scoring formula is still unweighted). `police_normal`/
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
to a stop mid-chase. Frozen like `paused` (`Game.game_over`), but BACK restarts
(`game_restart`) instead of resuming -- see "Controls".

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

Not built yet, in roughly the order they'd naturally get added:
- Checkpoints, race timer, time-added-on-checkpoint.
- Score, multiple maps/stage select, high score persistence (`pnx_save` exists and
  is host-tested per prior work, not yet wired here).
- Car recolor (`variants`, see "Personalization: car recolor").
- Synthwave art pass (current art is placeholder).
- Feature backlog items, pulled in whenever -- see that section.

## Open questions

- Steering-input choice (touch drag vs. something else) is a guess -- confirm once
  it's playable in-hand.
- Scoring formula (distance? time remaining? near-misses? perfect checkpoints?
  police takedowns? some weighted combination?) is unspecified.
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
