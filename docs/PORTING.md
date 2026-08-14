# Porting to the rest of the Pebble family

Reference material for M9 (`docs/ROADMAP.md`), gathered before the work starts rather than
during it: the packaging mechanism a multi-platform build actually depends on, and what it
implies for both content and engine. Nothing here has been run on a second platform yet —
every example today is `targetPlatforms: ["emery"]` — so treat the specifics as read off
SDK 4.17 and the pipeline as it stands, not as verified.

## Author once: one game, one build, seven watches

The goal is not seven ports. It is one set of sprites, one set of game logic, one
`pebble build`, and a single `.pbw` that installs everywhere. What varies, and how each
difference is absorbed without the game knowing:

| Varies | Absorbed by | Art or logic change |
|---|---|---|
| Screen 144x168 to 260x260 | camera shows more or less world | none |
| Round corners | safe-area rect from per-row bounds | none |
| 64 colour vs 1-bit | **palette ink mask** (below) | none |
| Speaker or not | API stubs to a no-op | none |
| Touch or not | abstract actions, not buttons or taps | none |
| 24 KB app RAM (`aplite`) | per-platform compilation, maybe -- see below | open question |

**1-bit is a palette property, not an art asset.** This falls out of a decision already
made: art is 4bpp indexed, so shape and colour are already separate. A 1-bit platform needs
one extra field per palette -- a 16-bit mask saying which of the 16 indices are ink and which
are paper. Two bytes. Index 0 stays transparent as it already is, so nothing else changes:
tile and sprite blobs ship **byte-identical to all seven platforms**, and only the palette
resource and the span writer differ. The pipeline proposes a split by luminance and the
editor lets you flip individual entries against a live 1-bit preview, because thresholding
by luminance alone will vanish any figure that matches its ground.

Note what this avoids. Per-pixel thresholding needs separate 1-bit art; dithering survives
on backgrounds but shimmers on anything that moves and destroys readability at 16x16. Neither
is necessary when the decision can be made 16 entries at a time.

**Blocker to clear first: opt-outs must stub, not delete.** `PNX_USE_AUDIO=0` removes the
declarations, so game code calling `pnx_music_play` fails to compile rather than doing
nothing -- which forces `#if` into game logic and breaks author-once immediately. The same
fault already breaks `PNX_USE_DIAGNOSTICS=0` in two of the three examples. The rule the
framework needs: **a disabled subsystem keeps its entire API as inline no-ops returning a
safe value.** Cheap to do, and it is what makes `gabbro` having no speaker a non-event for
the game.

**Packaging.** One bundle via `targetPlatforms` plus the SDK's per-platform resources. The
pipeline has to budget per platform rather than once, since the appstore resource cap is
131,072 bytes on `aplite` against 262,144 everywhere else.

## `~` tagging: how "per-platform resources" actually works

The mechanism the section above waves at has a name, and it is worth writing down before
the 1-bit work starts, because it decides how much of that work is naming and how much is
engine. Read from `sdk-core/pebble/common/waftools/resources/find_resource_filename.py`,
not recalled.

**A resource is declared once, untagged, and the SDK picks the file per platform.**
`package.json` names `tiles.bin`; the build globs for `tiles*.bin`, reads `~`-separated tags
off each candidate, and takes the one matching the target platform most closely. So
`tiles.bin` beside `tiles~bw.bin` ships 4bpp to the colour watches and 1-bit to the others,
from one manifest entry, with no `#if` anywhere and one `pebble build`.

The resolution rules, all of which bite:

- **Specificity is a count**, not a priority order: the number of the candidate's tags that
  appear in the platform's tag set. Most matches wins.
- **Any unknown tag disqualifies a candidate outright.** `tiles~bw.bin` is not merely
  outranked on `emery`, it is invisible -- which is what makes the untagged file a genuine
  fallback rather than a tie-breaker.
- **A tie is a hard build failure**, naming the ambiguous files. This is the trap: `bw` and
  `144w` both apply to `diorite`, so shipping `tiles~bw.bin` *and* `tiles~144w.bin` fails
  the build rather than picking one. Combine into `tiles~bw~144w.bin` or stay on one axis.
- **The generic name may not contain `~`** -- `bld.fatal`, immediately.
- No match at all falls back to the untagged file, so adding a tagged variant can never
  break a platform that has none.

Those four are not read off the source and assumed: the resolution was re-run over the tag
tables for all seven platforms. `tiles.bin` + `tiles~bw.bin` resolves to the tagged file on
`flint`, `diorite` and `aplite` and to the untagged one everywhere else, and adding
`tiles~144w.bin` to that pair fails the build on exactly the three `bw` platforms.

The media entry itself does not change: this project already declares resources as
`{"type": "raw", "name": "PALETTES", "file": "palettes.bin"}`, and that stays untagged
whatever variants sit beside it. `targetPlatforms` is `["emery"]` in every example today and
is the other half of the packaging change.

The tags each platform answers to, read from `pebble_platforms[...]["TAGS"]`:

| Platform | Colour | Shape | Size | Other |
|---|---|---|---|---|
| `emery` | `color` | `rect` | `200w` `228h` | `speaker` `touch` `mic` `strap` `strappower` `health` `compass` |
| `gabbro` | `color` | `round` | `260w` `260h` | `touch` `mic` `health` `compass` |
| `flint` | `bw` | `rect` | `144w` `168h` | `speaker` `mic` `health` `compass` |
| `basalt` | `color` | `rect` | `144w` `168h` | `mic` `strap` `strappower` `health` `compass` |
| `chalk` | `color` | `round` | `180w` `180h` | `mic` `strap` `strappower` `health` `compass` |
| `diorite` | `bw` | `rect` | `144w` `168h` | `mic` `strap` `health` |
| `aplite` | `bw` | `rect` | `144w` `168h` | `compass` |

Each platform also answers to its own name as a tag. **This table is the source for the
capability columns in `ROADMAP.md`'s M9 matrix**: `gabbro` carrying no `speaker` tag is
where "the flagship round watch cannot use M4" comes from, and `touch` appearing only on
`emery` and `gabbro` is the same fact for input.

**What it changes for this pipeline.** Three things, in the order they matter:

1. **It is the answer to per-platform atlas carves.** A 144x168 screen wanting a different
   carve than 260x260 is `world~144w.bin` beside `world~260w.bin`, not a second build.
2. **It makes a 1-bit pixel plane affordable.** The current plan ships blobs byte-identical
   to all seven platforms and absorbs 1-bit in the palette, which is right while resources
   fit. If `aplite`'s 131,072 cap ever binds, `~bw` is the escape hatch that does not cost
   author-once: measured on `examples/overworld`, packing pixels 4bpp -> 1bpp saves ~54 KB
   of its 78 KB, against ~300 bytes for dropping palette data.
3. **The pipeline has to emit the tags itself.** Tagging is resolved by the SDK's waf over
   files on disk, so `pnx_assets` would write `tiles~bw.bin` and keep the `package.json`
   media entry untagged. That is a real change to blob naming and to the size report, which
   would need to total per platform rather than once -- not merely a file-naming convention.

**Unverified.** Whether `flint` and `gabbro` accept these tags on real hardware, and whether
the SDK's own `bw` output path expects 1-bit resources in a particular format. Both are read
from SDK 4.17 on this machine and neither has been run on a watch.

## The `.pbw` is seven apps in a zip, not one app that adapts

This is the other half of `~` tagging and the more consequential half, because it applies to
the **engine** rather than to the content. `targetPlatforms` does not select which platforms
one binary claims to support -- it selects how many times the C is compiled.

Read from `examples/audiotest/build/audiotest.pbw`, which is a zip containing:

```
appinfo.json
emery/pebble-app.bin          <- compiled for emery
emery/app_resources.pbpack    <- resources resolved for emery
emery/manifest.json
```

Add `flint` to `targetPlatforms` and there is a second directory beside it with its own
binary and its own pack. `~` tags choose what goes in the pack; **the preprocessor chooses
what goes in the binary**, and the SDK hands each compile its own defines:

| Platform | Defines beyond the platform name |
|---|---|
| `emery` | `PBL_COLOR` `PBL_RECT` `PBL_TOUCH` `PBL_SPEAKER` `PBL_MICROPHONE` `PBL_SMARTSTRAP` `PBL_SMARTSTRAP_POWER` `PBL_HEALTH` `PBL_COMPASS` `PBL_RGB_BACKLIGHT` `PBL_DISPLAY_WIDTH=200` `PBL_DISPLAY_HEIGHT=228` |
| `gabbro` | `PBL_COLOR` `PBL_ROUND` `PBL_TOUCH` `PBL_MICROPHONE` `PBL_HEALTH` `PBL_COMPASS` `PBL_DISPLAY_WIDTH=260` `PBL_DISPLAY_HEIGHT=260` |
| `flint` | `PBL_BW` `PBL_RECT` `PBL_SPEAKER` `PBL_MICROPHONE` `PBL_HEALTH` `PBL_COMPASS` `PBL_DISPLAY_WIDTH=144` `PBL_DISPLAY_HEIGHT=168` |
| `basalt` | `PBL_COLOR` `PBL_RECT` `PBL_MICROPHONE` `PBL_SMARTSTRAP` `PBL_SMARTSTRAP_POWER` `PBL_HEALTH` `PBL_COMPASS` `PBL_SDK_FROZEN` `144x168` |
| `chalk` | `PBL_COLOR` `PBL_ROUND` `PBL_MICROPHONE` `PBL_SMARTSTRAP` `PBL_SMARTSTRAP_POWER` `PBL_HEALTH` `PBL_COMPASS` `PBL_SDK_FROZEN` `180x180` |
| `diorite` | `PBL_BW` `PBL_RECT` `PBL_MICROPHONE` `PBL_SMARTSTRAP` `PBL_HEALTH` `PBL_SDK_FROZEN` `144x168` |
| `aplite` | `PBL_BW` `PBL_RECT` `PBL_COMPASS` `PBL_SDK_FROZEN` `144x168` |

**What this changes, in the order it matters:**

1. **The 1-bit path and the 4bpp path never coexist.** It is still a second path, but it is
   `#if PBL_BW` and the colour watches never carry a byte of it. The cost is source
   complexity, not size on any device.
2. **`aplite` being out of scope deserves re-measuring before it is believed.** The ~13.4 KB
   static figure was measured on `emery` with audio, touch, colour blitting and diagnostics
   all compiled in. On `aplite` every one of those is absent from the defines. The 24 KB
   conclusion below rests on a number that does not describe the binary `aplite` would
   actually get, and nobody has built one.
3. **Capability gating stops being the game's problem.** `PNX_USE_AUDIO` is a hand-set
   opt-out today; `PBL_SPEAKER` is present on exactly `emery` and `flint`, so the engine can
   gate itself and be right by construction. Same for `PBL_TOUCH` and the input layer.
4. **`PBL_DISPLAY_WIDTH`/`HEIGHT` are compile-time constants, not runtime values.**
   Resolution independence can keep statically sized buffers -- a screen-sized array stays a
   fixed-size array, sized differently per binary, with no allocation added.

**And it raises the stakes on the blocker above.** "Opt-outs must stub, not delete" reads
like a tidiness problem. It is not: once the engine really does compile subsystems out per
platform, the stub rule is the only thing keeping one game source compiling for seven
targets. It should land before any `#if PBL_*` goes into the engine, not after.

**Unverified, and worth an experiment before M9 is planned in detail.** The cheap test is to
add `diorite` or `flint` to one example, build, and read the size report the wscript already
prints per platform (`for platform in ctx.env.TARGET_PLATFORMS`). That answers the `aplite`
question with a number instead of an estimate, for a one-line manifest change plus whatever
the compile turns up.

**What author-once cannot absorb.** `aplite`'s 24 KB was called a hard exclusion rather than
a tuning problem. **That is an open question, not a conclusion** -- it was reasoned from an
`emery` build carrying audio, touch and colour blitting, none of which `aplite` compiles at
all. Settle it with a build, not an argument. What does not change is screen size: it is
only free for a game that can show more or less world -- a fixed play area would have to
letterbox or scale, and integer scaling is the only kind that stays crisp. An RPG absorbs
this; a puzzle grid would not.
