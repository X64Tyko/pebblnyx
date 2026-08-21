// Pebblnyx umbrella header.
//
// A game includes this and nothing else from the framework. Most module headers are
// pulled in only when the corresponding PNX_USE_* is set, so a disabled module leaves no
// declarations behind and no way to accidentally call into code that was not compiled.
//
// audio/pnx_audio.h and audio/pnx_music.h are the deliberate exception, included
// unconditionally: PNX_USE_AUDIO now defaults from PBL_SPEAKER (pnx_config.h), so it is
// off by construction on gabbro/basalt/chalk/diorite/aplite, and a device-derived default
// is exactly the case docs/PORTING.md's "opt-outs must stub, not delete" rule is for --
// game code that calls pnx_music_play must keep compiling on a watch with no speaker, not
// fail with an undeclared identifier the game never asked to guard against. Both headers
// declare real APIs when the module is on and inline no-ops when it is off; only
// audio/pnx_synth.h stays gated here, because PNX_USE_SYNTH is a measurement gate, not a
// hardware capability, and has no stub branch of its own (see its own file for why).

#pragma once

#include "pnx_config.h"

#include "core/pnx_fx.h"
#include "core/pnx_arena.h"
#include "core/pnx_fmt.h"
#include "core/pnx_diag.h"
#include "platform/pnx_platform.h"

#if PNX_USE_TWEEN
#include "core/pnx_tween.h"
#endif

#if PNX_USE_ASSETS
#include "assets/pnx_assets.h"
#include "gfx/pnx_gfx.h"
#endif

#if PNX_USE_TILEMAP
#include "gfx/pnx_tilemap.h"
#endif

#if PNX_USE_SPRITES
#include "gfx/pnx_sprite.h"
#endif

#if PNX_USE_NINESLICE
#include "gfx/pnx_nineslice.h"
#endif

#if PNX_USE_LAYERS
#include "gfx/pnx_layer.h"
#endif

#if PNX_USE_HUD
#include "gfx/pnx_hud.h"
#include "gfx/pnx_hud_vars.h"
#include "gfx/pnx_hud_window.h"
#endif

#if PNX_USE_TEXT
#include "gfx/pnx_text.h"
#endif

#if PNX_USE_INPUT
#include "input/pnx_input.h"
#endif

#include "audio/pnx_audio.h"
#include "audio/pnx_music.h"

#if PNX_USE_SYNTH
#include "audio/pnx_synth.h"
#endif

#if PNX_USE_SAVE
#include "save/pnx_save.h"
#endif

#if PNX_USE_APP
#include "app/pnx_app.h"
#endif

#if PNX_USE_PHYSICS
#include "physics/pnx_physics.h"
#endif

#if PNX_USE_COLLISION
#include "collision/pnx_collision.h"
#endif

// Later milestones add their headers here behind their own #if guards:
//   #if PNX_USE_TILEMAP
//   #include "gfx/pnx_tilemap.h"
//   #endif
