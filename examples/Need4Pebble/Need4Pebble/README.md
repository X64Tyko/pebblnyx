# Need 4 Pebble

An OutRun-style pseudo-3D racer for Pebble Time 2 (`emery`). Landscape, two-handed,
shoulder-trigger controls. Full design rationale and status lives in
[`DESIGN.md`](DESIGN.md) -- this is the short version.

## Controls

`PNX_ORIENT_BUTTONS_TOP` -- cluster along the top edge, both index fingers on it:

| | |
|---|---|
| **Right top button** (physical DOWN) | gas |
| **Left top button** (physical UP) | brake |
| **Steering** | touch-drag by default; accelerometer tilt is a toggle (SELECT, while paused) |
| **BACK** | pause/resume -- restarts instead, while BUSTED (see below) |

## The road

Pseudo-3D via horizontal scanline strips, same family as the genre's usual trick, with
two things worth calling out:

- **True linear perspective, not `1/depth`.** Road half-width is linear in screen row,
  zero at the horizon and max at the near row -- a straight road's edges are dead-straight
  lines converging at the horizon, matching an actual horizon line. The hyperbolic
  `1/depth` version this replaced put a curve bulge in even a flat straight road's edges.
- **Hills and valleys** shift each row's drawn screen position (the same near-to-far
  accumulation curves already use, applied to Y instead of X), and the view distance
  itself changes with the player's own slope -- cresting a hill shows fewer rows ahead,
  a valley shows more. A steep slope can make one screen row's fill need to stretch
  across several lines to avoid leaving gaps that show stale pixels from an earlier
  frame; `draw_road` sizes each row's fill to the height it actually claims rather than
  a fixed 1px strip.

## Police pursuit

One pursuer at a time, spawning after an occasional cooldown. Chases from beside the
player rather than dead behind (drifting between favouring the left/right side over a
chase), can force a hit that shoves the player sideways into the next lane, and can be
lost by steering it into traffic or off the road -- it crashes the same two ways the
player's own car can. Speed hitting 0 with an active pursuer nearby is "BUSTED" --
game over, BACK restarts. Full mechanics and tuning constants in `DESIGN.md`'s "Police:
chase, not traffic".

## Art

Placeholder NES/Genesis sprite rips (`art/`), not the target look -- a neon synthwave
pass (magenta/cyan horizon gradient, grid-line horizon) is still pending. Good enough to
prove out the road, traffic, and chase feel.

## Building

```
pebble build
pebble install --emulator emery
```

Editing `assets.toml` does **not** get picked up by `pebble build` alone -- it only
recompiles what's already packed. Re-run the asset pipeline first:

```
python3 ../../../tools/pnx_assets.py assets.toml --out resources \
        --header src/c/assets_gen.h --orientation buttons_top
```

## Status

Host build passes clean (`cd ../../../tests && make`: 552 checks, 0 failures). Confirmed
on the `emery` emulator: road/curve/hill rendering, traffic, crashes, pause menu, police
chase/ram/BUSTED/restart, the light-bar overlay. Accelerometer tilt steering is also
confirmed on real hardware (axis and sign). Curve, chase, and pacing tuning constants are
still eyeballed from emulator play, not felt in-hand on a real run -- see `DESIGN.md`'s
"Open questions" for the specific list.
