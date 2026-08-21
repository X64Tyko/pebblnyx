// A small named runtime table for HUD data binding.
//
// Game code sets a value each tick (speed, a timer, a dialog speaker's name); a HUD
// draw call (pnx_hud.h today, a bound HUD window in a later milestone) reads it back.
// IDs and the table's size are generated at build time from the manifest's own
// [[hud_var]] declarations (tools/pnx_assets.py) -- PNX_HUD_VAR_* constants and
// PNX_HUD_VAR_COUNT in the project's own assets_gen.h, never hand-numbered.
//
// This file does NOT own the storage. PNX_HUD_VAR_COUNT is a per-PROJECT generated
// constant this generic, shared engine file has no visibility into -- the same reason
// pnx_assets_init (pnx_assets.h) takes its resource table as a parameter rather than
// declaring its own array. A game declares its own storage, sized by its own
// PNX_HUD_VAR_COUNT, and hands the pointers to pnx_hud_vars_init once at boot.

#pragma once

#include "../pnx_config.h"

#if PNX_USE_HUD

#include <stdint.h>

// Bytes per text variable, NUL included -- covers a short label or dialog speaker name
// ("GRANDPA") without per-variable configurable length, which is more than this needs.
#define PNX_HUD_VAR_TEXT_LEN 16

// `ints`/`text` may be NULL if the project declared no [[hud_var]] of that type --
// there are then no valid ids for it, so every getter/setter below is simply
// unreachable for that type, not a null-deref waiting to happen. Zeroes/empties
// whatever storage IS provided, so a fresh boot never reads stale values from a
// previous run's memory.
void pnx_hud_vars_init(int32_t* ints, char (*text)[PNX_HUD_VAR_TEXT_LEN], uint8_t count);

void pnx_hud_var_set_i32(uint8_t id, int32_t value);
int32_t pnx_hud_var_get_i32(uint8_t id);

// `value` is copied and truncated to fit PNX_HUD_VAR_TEXT_LEN-1 characters plus a NUL
// -- never overruns regardless of the caller's own string length.
void pnx_hud_var_set_text(uint8_t id, const char* value);
// Never NULL, even for an out-of-range id or before init -- "" rather than a caller
// having to null-check before it can safely pnx_text_draw the result.
const char* pnx_hud_var_get_text(uint8_t id);

#endif // PNX_USE_HUD
