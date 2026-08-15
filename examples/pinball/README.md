# Pebble Pinbeasts

A pinball example, played off the wrist in two hands (see [`docs/PLATFORM.md`](../../docs/PLATFORM.md)),
loosely modelled on *Pokémon Pinball*'s creature-catching table design. Working title;
the directory stays `examples/pinball` regardless of what the game ends up called.

## Controls and orientation

`PNX_ORIENT_BUTTONS_BOTTOM` -- landscape, cluster along the bottom edge, both thumbs on
it. This is what `docs/PLATFORM.md` already recommends for pinball flippers, and it's why
DOWN/UP work as real held flippers rather than the tap-pulse hack an earlier portrait
version needed for BACK (see git history if curious -- BACK cannot give a raw/held button
state on Pebble hardware at all, confirmed on-device).

```
[DOWN]        [SELECT]        [UP]        [BACK]
left flipper   shoot/serve   right flipper  pause/menu (tap only)
```

Table is authored taller than the viewport and the camera scrolls to follow the ball
(`camera_y` in `main.c`) -- landscape is 228x200 versus portrait's 200x228, so scrolling
gets the vertical room back, closer to how the real Pokémon Pinball tables work (two
stacked screens, camera follows the ball, flippers off-screen while the ball is up top).

Rendering rotates table-space into the framebuffer (`fb_rect` in `main.c`) using the exact
point transform from `tools/pnx_assets.py`'s `rotate_point` for `ORIENT_BUTTONS_BOTTOM`
(`fx, fy = ay, w-1-ax`), extended to a rect.

**Left-handed mode** (mirrored cluster side) is out of scope -- the engine's orientation
enum has no mirrored landscape variant; adding one is a cross-cutting engine change.

## BACK

Can't be a flipper -- Pebble refuses a raw or held subscription on it, and even
subscribing raw alongside a single-click handler doesn't help (tried it; the raw handlers
just never fire). A tap is claimed via `window_single_click_subscribe`; a real hold
force-quits unconditionally at the firmware level (confirmed on-device, ~2s was enough),
no app code involved. Reserved for pause/menu later. `pnx_platform_set_screen_lock(true)`
is still called for the backlight, but no longer claims to affect BACK -- see
`platform/pnx_platform.h`'s corrected comment.

## Art

No shipped art yet. `Reference/` (gitignored) holds downloaded *Pokémon Pinball*
spritesheets kept locally as a layout/animation guide. Everything shipped will be
original art in that spirit, not traced from the reference. Until then, `main.c` draws
everything as `pnx_gfx_fill_rect` placeholders sized to the real collision geometry.

## v1 scope

- Gravity, two side rails, two flippers (`src/pnx/physics/pnx_physics.h`, new engine
  module, `PNX_USE_PHYSICS`, off by default framework-wide, forced on in this example's
  `wscript`).
- SELECT stands in for "shoot": serves a new ball. No real plunger or mash/capture
  minigame yet.
- Second table area, pause/menu, scoring/capture: not built yet.
- No `pnx_app` state stack -- follows `examples/empty`'s raw-loop shape.

## Also landed this session, engine-wide (not pinball-specific)

`src/pnx/collision/` -- AABB sprite-vs-tile movement resolution and sprite-vs-sprite
overlap, on top of the tile-flag lookups already in `assets/pnx_assets.h`
(`pnx_map_flags`/`pnx_map_solid`). `PNX_USE_COLLISION`, off by default, independent of
`PNX_USE_PHYSICS` -- this table has no tilemap; it's for tile-based games later.

## Editor registration

`assets.toml` exists (a `[project]` table, no content sections) purely so
`tools/pnx_project.py` recognises this folder as a project (`.pknproj` OR `assets.toml`;
this had neither before). No `.pknproj` written by hand -- the editor owns that file.

## Status

Confirmed on the real `emery` emulator (`pebble build` + `pebble install --emulator emery`
+ `pebble emu-button`/`pebble screenshot`/`pebble logs`), not just host-build: rotation
and scrolling place the ball, walls and both flippers correctly; DOWN and UP each drive
their flipper's swing independently; BACK tap no longer exits; BACK hold still
force-quits. Host build compiles clean under `-Wall -Wextra -Werror`. No automated test
yet. Gravity, bounce coefficients, table height, and camera framing are all unmeasured
guesses -- tune by playing it.
