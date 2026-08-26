// Compile-time module selection.
//
// A game copies this file, edits it, and puts it on the include path ahead of the
// framework's default. Every module is opt-in, and a disabled module must contribute
// ZERO bytes -- its whole translation unit is wrapped in the matching #if, so it
// compiles to an empty object.
//
// This exists because `.text + .data + .bss` is capped at 65,535 bytes for the
// framework AND the game together (see docs/MEASUREMENTS.md). A module that cannot be
// compiled out taxes every game that does not use it. Run tools/size_report.py after a
// build to see what each one actually costs.

#pragma once

// Bytes pnx_arena_init_max leaves unclaimed below the platform's reported free heap,
// for whatever the SDK itself allocates after the game's own arena is sized (timers,
// click handlers, dialogs). A starting point, not a guarantee -- MEASUREMENTS.md
// documents a real case (PNX_USE_DIAGNOSTICS on aplite) where post-init heap use was
// enough to matter; tighten or loosen this per project once you've profiled headroom
// to spare against a real device.
#ifndef PNX_ARENA_HEAP_RESERVE
#define PNX_ARENA_HEAP_RESERVE 2048
#endif

// Asset registry: packed blobs, handle-based lookup, bulk residency.
#ifndef PNX_USE_ASSETS
#define PNX_USE_ASSETS 1
#endif

#ifndef PNX_USE_TILEMAP
#define PNX_USE_TILEMAP 1
#endif

#ifndef PNX_USE_SPRITES
#define PNX_USE_SPRITES 1
#endif

#ifndef PNX_USE_TEXT
#define PNX_USE_TEXT 1
#endif

// 9-slice panels: pnx_gfx_draw_nine_slice, drawing a PnxNineSlice's corners once and
// tiling its edges/centre to fill an arbitrary box. The asset type itself (PnxNineSlice,
// pnx_nineslice_load) is unconditional in pnx_assets.c, same as PnxSprite -- this flag
// gates only the drawing module, so a game that never draws a bordered panel does not pay
// for it, the way PNX_USE_SPRITES already works relative to PnxSprite.
#ifndef PNX_USE_NINESLICE
#define PNX_USE_NINESLICE 1
#endif

// Layer compositing: draw-order groups, parallax, and a place for the HUD to live.
// Depends on PNX_USE_SPRITES for its PNX_LAYER_SPRITES kind (pnx_layer.h's own comment
// explains why that is not threaded through as a further #if). A game that hand-rolls its
// own fixed draw order and has no reason to generalise it -- resonant did, before this
// milestone -- can leave this off; nothing else in the framework requires it.
#ifndef PNX_USE_LAYERS
#define PNX_USE_LAYERS 1
#endif

// HUD widgets: bars, labelled rows, bordered panels -- pnx_hud_bar_draw/pnx_hud_row_draw/
// pnx_hud_panel_draw. Depends on PNX_USE_NINESLICE (the panel widget) and PNX_USE_TEXT
// (the row widget), the same soft-dependency posture pnx_layer.h documents for its own
// PNX_USE_SPRITES dependency.
#ifndef PNX_USE_HUD
#define PNX_USE_HUD 1
#endif

// Easing curves and time-based value interpolation (pnx_tween.h) -- no dependencies of
// its own, and nothing else in the framework depends on it yet, but it is cheap and
// broadly useful the way PNX_USE_HUD is, so it defaults on rather than off like
// PNX_USE_PHYSICS/PNX_USE_COLLISION. Header-only (like pnx_fx.h): a disabled game pays
// nothing for functions it never calls, this flag exists so "not using it" is a stated
// choice a project can see in its own config, not just dead code left unlinked.
#ifndef PNX_USE_TWEEN
#define PNX_USE_TWEEN 1
#endif

// LZSS decoding for compressed WorldTile banks (M12). Paired with `compress_maps = true`
// in the manifest's `[project]` table the same way `PNX_PACK_2BIT` is paired with
// `pack_2bit` -- a project turns this on when it wants smaller map resources at the cost
// of a real, if small, CPU decode per bank load, never silently. A map built compressed
// against a runtime with this off refuses to load rather than reading garbage cells.
//
// Independent of PNX_COMPRESS_MODE below: a WorldTile bank is an array of cell-dictionary
// INDICES, not pixels, and can need more than the 16-entry alphabet bitplane's format is
// built around (a dictionary can run past 256 entries, stored 2 bytes wide) -- so bitplane
// does not generalise here the way it does for sprite/atlas pixel data, and a map's own
// compression stays this one boolean, LZSS or nothing.
#ifndef PNX_USE_MAP_COMPRESS
#define PNX_USE_MAP_COMPRESS 0
#endif

// Project-wide sprite/atlas pixel compression -- exactly one of these, never a
// combination. A sprite or atlas built under one mode refuses to load against a runtime
// compiled for another, rather than reading garbage pixels.
//
//   PNX_COMPRESS_NONE     raw packed pixels, resident for the sprite/atlas's whole
//                         lifetime -- no decode cost, no ROM traffic beyond the one load.
//   PNX_COMPRESS_LZSS     whole pixel region is one LZSS stream, decoded into a fresh
//                         arena buffer at load (pnx_lzss.c). Cheapest to decode a
//                         CONTIGUOUS region, but a single frame or tile cannot be decoded
//                         in isolation -- backreferences cross unit boundaries freely.
//   PNX_COMPRESS_BITPLANE default. Each unit (one sprite frame, one atlas tile/subtile)
//                         is bitplane/Elias-gamma-coded independently (pnx_bitplane.c) and
//                         stays in ROM; pnx_atlas_load/pnx_sprite_load bring in only the
//                         small resident metadata (frame/tile tables, the per-unit offset
//                         table) and pnx_tile_cache.c/pnx_sprite_cache.c decode one unit
//                         into a small RAM cache on demand, evicting the least-recently-
//                         used entry under pressure. See pnx_tile_cache.h's own comment
//                         for why decoded pixels are cached rather than compressed bytes.
//
// Bitplane is the default because it is the only mode that keeps art out of RAM until a
// frame or tile is actually drawn -- LZSS and NONE both hold a sprite/atlas's whole pixel
// region resident for as long as it is loaded. A project sets `compress = "lzss"` or
// `compress = "none"` in the manifest's `[project]` table to opt out.
#define PNX_COMPRESS_NONE	  0
#define PNX_COMPRESS_LZSS	  1
#define PNX_COMPRESS_BITPLANE 2

#ifndef PNX_COMPRESS_MODE
#define PNX_COMPRESS_MODE PNX_COMPRESS_BITPLANE
#endif

// Decoded-tile LRU cache (pnx_tile_cache.c) -- the piece that makes PNX_COMPRESS_BITPLANE
// viable against a renderer with no dirty-tracking (pnx_tilemap_draw_layer redraws every
// visible tile every frame, unconditionally -- confirmed this session). Caching only the
// COMPRESSED bytes would mean paying full decode cost on every blit of every frame
// regardless of hits, measured at ~40ms/screen on real emery hardware -- the whole frame
// budget gone on tile decode alone. Caching the DECODED pixels instead means a tile pays
// decode once per cache miss (new tile scrolling into view), not once per blit -- a
// stationary or slow-scrolling camera hits the cache almost every frame.
//
// Arena-backed (pnx_tile_cache_init(arena, target_entries, max_tile_px)), not a
// compile-time slot count: real content spans multiple tile sizes (Need4Pebble alone has
// 16px and 32px tiles) and available RAM varies per platform. Slots are fixed-size,
// sized to `max_tile_px` -- see pnx_tile_cache.h's own comment for why a fixed slot
// (rather than a variable-size pool) is what makes single-entry LRU eviction possible at
// all here.

// Frames a tile may sit undrawn before it self-releases (pnx_tile_cache_tick, called once
// per game tick) -- independent of the evict-oldest-on-miss path, which can free an entry
// sooner under pressure regardless of this threshold. ~90 ticks is ~3.6s at this engine's
// own 25 ticks/sec fixed step -- long enough that a slow-panning camera doesn't thrash
// tiles it's about to re-need, short enough that a scene change (pnx_tile_cache_reset
// already handles that explicitly) isn't the only way stale entries ever clear.
#ifndef PNX_TILE_CACHE_MAX_AGE
#define PNX_TILE_CACHE_MAX_AGE 90
#endif

// Decoded-sprite-FRAME LRU cache (pnx_sprite_cache.c) -- pnx_tile_cache's sibling for
// bitplane sprite frames, same fixed-slot design (see pnx_tile_cache.h's own comment).
// PNX_SPRITE_CACHE_MAX_UNIT_PX bounds the largest frame pixel count a project's sprites
// reach; pnx_sprite_load refuses a sprite whose largest reachable frame exceeds it.
#ifndef PNX_SPRITE_CACHE_MAX_UNIT_PX
#define PNX_SPRITE_CACHE_MAX_UNIT_PX 1200
#endif

// Ticks a cached frame may sit undrawn before it self-releases -- same reasoning as
// PNX_TILE_CACHE_MAX_AGE, independent knob since a game may want sprite frames and tiles
// to age out on different schedules.
#ifndef PNX_SPRITE_CACHE_MAX_AGE
#define PNX_SPRITE_CACHE_MAX_AGE 90
#endif

// Defaults from the HARDWARE, not from a blanket "on": PBL_SPEAKER is a compiler define
// the SDK hands exactly the platforms that have one (emery, flint -- see docs/PORTING.md,
// "The .pbw is seven apps in a zip"), so a build for gabbro/basalt/chalk/diorite/aplite
// sees no PBL_SPEAKER and this correctly defaults to off with no manifest or #if of the
// game's own. The host build has no PBL_* defines at all and keeps the old default of on,
// since a host test wants audio available unless a test specifically turns it off. Still
// overridable either way: PNX_DEFINES=PNX_USE_AUDIO=0 forces it off on a speakered
// platform, and =1 forces it on for someone who wants to find out what happens.
#ifndef PNX_USE_AUDIO
#if defined(PNX_PLATFORM_HOST) || defined(PBL_SPEAKER)
#define PNX_USE_AUDIO 1
#else
#define PNX_USE_AUDIO 0
#endif
#endif

// The sequencer is the music half of audio. Follows PNX_USE_AUDIO's derived default so a
// speakerless platform does not pay for a sequencer with nothing to feed, but stays a
// separate knob: a game with sound effects but no music wants PNX_USE_AUDIO=1 and this 0,
// which is still available by setting it explicitly.
#ifndef PNX_USE_SEQUENCER
#define PNX_USE_SEQUENCER PNX_USE_AUDIO
#endif

// Button edges, hold times, and the orientation-aware cluster mapping. A game that reads
// pnx_platform_poll_event itself and does its own bookkeeping can turn this off; one in
// landscape almost certainly should not, since it is the only thing that knows the
// buttons did not rotate with the content.
// Subtractive synth voices -- three detuned oscillators, a resonant filter with its own
// envelope, an LFO and sends into a global reverb and chorus.
//
// OFF by default, and deliberately: it is a measurement spike until the device says what
// it costs, and audio is the one category MEASUREMENTS.md names as having no measured
// cost and the most room to blow the budget. Nothing pays for it until the numbers do.
#ifndef PNX_USE_SYNTH
#define PNX_USE_SYNTH 0
#endif

#ifndef PNX_USE_INPUT
#define PNX_USE_INPUT 1
#endif

#ifndef PNX_USE_SAVE
#define PNX_USE_SAVE 1
#endif

// The app-state stack: push/pop/replace, and the lifecycle a game would otherwise hand-roll
// -- a clamped fixed-timestep accumulator, and OS focus events turned into suspend/resume so
// a covered app stops ticking instead of fast-forwarding on return. A game that wants to
// drive pnx_platform_run itself can leave this off; resonant does not.
#ifndef PNX_USE_APP
#define PNX_USE_APP 1
#endif

#ifndef PNX_USE_DIALOG
#define PNX_USE_DIALOG 1
#endif

// Circle-vs-segment ball physics for pinball-shaped games: gravity, static table
// geometry, and flippers that pivot between two baked poses. See physics/pnx_physics.h.
//
// OFF by default for the same reason PNX_USE_SYNTH is: unmeasured on device. It costs a
// pinball table nothing to turn on, but every other game nothing to leave off.
#ifndef PNX_USE_PHYSICS
#define PNX_USE_PHYSICS 0
#endif

// AABB collision: sprite-vs-tile movement resolution and sprite-vs-sprite overlap. Sits
// on top of pnx_map_solid/pnx_map_flags (assets/pnx_assets.h), which already exist and
// cost nothing extra -- this module is the sweep/resolve logic a game would otherwise
// hand-roll on top of them, not a second source of truth about which tiles are solid.
//
// A separate knob from PNX_USE_PHYSICS on purpose: an RPG wants tile collision with no
// pinball ball in sight, and a pinball table's ball never asks a tilemap anything, so
// forcing one on with the other would tax whichever game does not want both.
//
// OFF by default, same reasoning as PNX_USE_PHYSICS: unmeasured on device.
#ifndef PNX_USE_COLLISION
#define PNX_USE_COLLISION 0
#endif

// Timing-window judging for Additions and similar. Cheap, but pointless in a game
// with no timed input. See docs/GAME.md for the measured parameters.
#ifndef PNX_USE_TIMING
#define PNX_USE_TIMING 1
#endif

// The SDK text hook: `pnx_platform_text_draw`, drawing through the system font.
//
// It is an ESCAPE HATCH and not the text path. gfx/pnx_text.h is, and has been since E7 --
// a glyph blit happens during the frame like any other blit, while this costs ~4.3 ms
// (12% of a frame) and can only run AFTER the framebuffer is released, so nothing can ever
// be drawn over its output.
//
// Set this to 0 and the hook does not exist. That turns "we use the glyph blitter" from a
// convention somebody has to keep into a link error the moment anyone does not -- which is
// the only version of that promise worth having, because SDK text does not look broken, it
// just quietly costs a frame and refuses to be overdrawn.
//
// Default 1 because `examples/synthspike` still draws its readout this way and predates
// the font pipeline. A game with fonts should set it to 0.
#ifndef PNX_USE_SDK_TEXT
#define PNX_USE_SDK_TEXT 1
#endif

// On-screen performance overlay and deferred logging. Should be 0 in a release build:
// a text draw costs ~4.3ms, 12% of a frame.
#ifndef PNX_USE_DIAGNOSTICS
#define PNX_USE_DIAGNOSTICS 1
#endif

// Deferred log ring. LINES * LINE_LEN bytes of bss, and at the default 24 x 96 that is
// 2,304 bytes -- the largest single static allocation in an empty app, roughly a third
// of its footprint. Worth shrinking in anything that logs little, and it costs nothing
// at all with PNX_USE_DIAGNOSTICS=0.
#ifndef PNX_LOG_LINES
#define PNX_LOG_LINES 24
#endif

#ifndef PNX_LOG_LINE_LEN
#define PNX_LOG_LINE_LEN 96
#endif

// Engages pnx_platform_set_screen_lock(true) at startup -- backlight held on, without
// game code ever having to call it (that flag is backlight-only; see
// platform/pnx_platform.h's own comment for why it does NOT also affect BACK). A build
// knob, not a player-facing feature: a watch worn on the wrist wants the ordinary
// timeout, but a QEMU emulator with no wrist and no ambient light to read the screen by
// often does not, and being unable to see whether a game is running or merely dark reads
// as a hang either way. Off by default; the editor's Device panel is what turns this on,
// via PNX_DEFINES=PNX_FORCE_SCREEN_LOCK=1 (see Emulator.start, tools/pnx_editor.py).
#ifndef PNX_FORCE_SCREEN_LOCK
#define PNX_FORCE_SCREEN_LOCK 0
#endif

// Palette table. Slots are claimed by atlas loads and by explicit variant loads, and
// all are released together when a scene resets the arena. Bounded deliberately: a
// framework cannot assume how much content a project has, and an unbounded table would
// fail as a wrong-colours art bug rather than as an error. Costs SLOTS * 16 bytes of
// arena. The pipeline reports palette count per scene against this number.
#ifndef PNX_PALETTE_SLOTS
#define PNX_PALETTE_SLOTS 32
#endif

// ---------------------------------------------------------------------- tuning

// Simulation tick. 25Hz sits just under the 26.8fps display ceiling, so the sim never
// starves waiting for a frame that cannot arrive.
#ifndef PNX_TICK_MS
#define PNX_TICK_MS 40
#endif

// Maximum ticks consumed in one frame. NOT defensive padding: while covered by a modal
// the app is throttled to ~0.4fps, so a frame can arrive carrying several seconds of
// elapsed time. Without this clamp the sim fast-forwards on every notification.
//
// Belt and braces with pnx_app's focus handling (M6): that stops ticks outright between
// FOCUS_LOST and FOCUS_GAINED, so this clamp firing at all now means a frame ran long or
// the platform coalesced two, not "we were covered" -- see pnx_app.h.
#ifndef PNX_MAX_CATCHUP_TICKS
#define PNX_MAX_CATCHUP_TICKS 4
#endif

// The render period on device, locked rather than requested as fast as the display will
// take it. The display gates at ~37.33ms (26.8fps) no matter what is asked for -- see
// docs/MEASUREMENTS.md -- and asking for exactly that leaves nothing to absorb a frame
// that runs a little long: it just arrives late, one for one. Asking for 40ms (25fps)
// instead banks the ~2.67ms between the two as slack every single frame, which is what
// keeps a minor per-frame dip from becoming a dropped frame. It also lines the render
// cadence up with PNX_TICK_MS, so a rendered frame carries almost exactly one sim tick
// instead of the old ~1-frame-in-14 that carried none.
#ifndef PNX_FRAME_MS
#define PNX_FRAME_MS 40
#endif

// How far a touch has to travel, in screen pixels, before pnx_input treats it as a drag
// rather than a tap -- see input/pnx_input.h's touch section. 10px is what
// resonant/src/c/field.c's own DRAG_DEAD used before that logic moved into the framework;
// kept as the default rather than picked fresh.
#ifndef PNX_INPUT_DRAG_DEAD
#define PNX_INPUT_DRAG_DEAD 10
#endif
