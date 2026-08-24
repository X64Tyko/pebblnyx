// Named-clip playback: resolves elapsed wall-clock time against an arbitrary array of
// small indices into a current "frame" of that array.
//
// Originally lived inside pnx_sprite.h as sprite-frame-clip playback only (PnxAnimState's
// `frames` held sprite frame indices). Nothing about pnx_anim_frame is actually about
// sprites, though -- it is a pure function of (frames, count, start_ms, fps, durations,
// loop, now_ms) that returns which INDEX is current. Pulled out unconditionally (no
// PNX_USE_SPRITES gate, same footing as pnx_fx.h) so tile animation can resolve which
// ATLAS TILE ID is current right now using the exact same mechanism, generated the same
// way (`[atlas.anim]`, mirroring `[sprite.anim]`) -- one playback primitive, two different
// meanings for what an "index" names. pnx_sprite.h includes this and keeps re-exporting
// PnxAnimState/pnx_anim_play/pnx_anim_frame, so no existing sprite call site changes.
//
// Wall-clock, not fixed-tick: an authored fps is a real-time rate the way a music
// sequencer's row timing is (pnx_music_update's own comment, audio/pnx_music.h -- "against
// the wall clock ... because frames are not evenly spaced"), not a simulation unit the way
// PNX_TICK_MS is. pnx_anim_frame is a pure function of elapsed time since pnx_anim_play, so
// a large gap (the app was covered) just resolves further into/past the clip -- no
// clamping needed the way a fixed-step accumulator would.

#pragma once

#include <stdint.h>
#include <stdbool.h>

// A generated `[sprite.anim]` or `[atlas.anim]` clip (tools/pnx_assets.py) resolves at
// build time to a `static const uint8_t NAME_CLIP_FRAMES[]` plus
// `_COUNT`/`_FPS`/`_LOOP`/`_DURATIONS` #defines -- no blob, no runtime lookup by name, the
// same handle convention every other generated asset id already follows. This is the
// playback state and the two calls a game threads those through.
typedef struct
{
	const uint8_t* frames; // NULL until the first pnx_anim_play
	uint8_t count;
	uint32_t start_ms; // pnx_platform_now_ms() when this clip started
} PnxAnimState;

// Switches `state` to a new clip -- but only if `frames` differs from what is already
// playing, so calling this every frame with "whatever should be playing right now" (the
// ordinary usage: re-deciding the current clip from game state each frame) does not
// restart the loop on every call.
void pnx_anim_play(PnxAnimState* state, const uint8_t* frames, uint8_t count, uint32_t now_ms);

// The resolved INDEX for `state` at `now_ms` -- not pixels; assign straight to
// PnxSpriteInstance.frame for a sprite clip, or use directly as an atlas tile id for a
// tile clip. `durations` is a generated `NAME_CLIP_DURATIONS` array (one entry per frame,
// in units of `fps`'s own tick -- `[1,2,1,2]` at fps=8 means frames 2 and 4 hold twice as
// long as 1 and 3) or NULL for equal timing (the generated header emits
// `#define NAME_CLIP_DURATIONS NULL` when a clip's `durations` were not authored, so a
// caller always passes `NAME_CLIP_DURATIONS` without needing to know which case it is).
// Wraps through the clip if `loop`, else holds the last frame once played through once.
uint8_t pnx_anim_frame(const PnxAnimState* state, uint8_t fps, const uint8_t* durations,
					   bool loop, uint32_t now_ms);
