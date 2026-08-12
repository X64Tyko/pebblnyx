// Pebblnyx umbrella header.
//
// A game includes this and nothing else from the framework. Module headers are pulled
// in only when the corresponding PNX_USE_* is set, so a disabled module leaves no
// declarations behind and no way to accidentally call into code that was not compiled.

#pragma once

#include "pnx_config.h"

#include "core/pnx_fx.h"
#include "core/pnx_arena.h"
#include "core/pnx_fmt.h"
#include "core/pnx_diag.h"
#include "platform/pnx_platform.h"

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

#if PNX_USE_TEXT
#include "gfx/pnx_text.h"
#endif

#if PNX_USE_AUDIO
#include "audio/pnx_audio.h"
#endif

#if PNX_USE_SEQUENCER
#include "audio/pnx_music.h"
#endif

// Later milestones add their headers here behind their own #if guards:
//   #if PNX_USE_TILEMAP
//   #include "gfx/pnx_tilemap.h"
//   #endif
