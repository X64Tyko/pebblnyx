// Save: a versioned, chunked blob over persist keys.
//
// Two numbers from docs/MEASUREMENTS.md decide everything here: a persist WRITE costs
// ~7ms per call almost regardless of size, and each key caps at PNX_PERSIST_KEY_BYTES
// (256). So a save is packed into as few 256-byte keys as the payload needs (minimum key
// count -- the thing worth minimising is calls, not bytes), and writing several of them
// back to back inside one call would just move the ~7ms/chunk cost from "spread out" to
// "all at once", which is a multi-frame stall on anything bigger than one key.
//
// This module never decides WHEN to write. A game drives it two ways:
//
//   pnx_save_begin() + pnx_save_step() once per frame while pnx_save_pending() is true.
//   Spreads a save's chunk writes one per RENDERED frame, so a ~7ms write lands as
//   roughly one tick of latency instead of stalling every frame it takes to flush. This is
//   the interactive path -- a player opening a menu and choosing SAVE.
//
//   pnx_save_flush(), which blocks until every chunk is written. Not spread, and not
//   meant to be while frames are still expected: save-on-blur has ~297ms of will_focus
//   warning before the app is actually covered (docs/MEASUREMENTS.md) against a ~106ms
//   4KB save, so there is room to just finish it, and no frame budget left to protect --
//   the display is about to stop accepting them anyway.
//
// A SLOT is a fixed range of PNX_SAVE_CHUNKS_PER_SLOT persist keys. Two slots never share
// a key, so a game can keep an automatic "resume" save and a manual "checkpoint" save
// side by side without either overwriting the other -- see the header comment in
// resonant's main.c for how that distinction is actually used.

#pragma once

#include "../pnx_config.h"

#if PNX_USE_SAVE

#include "../platform/pnx_platform.h"

#include <stdint.h>
#include <stdbool.h>
#include <stddef.h>

typedef uint8_t PnxSaveSlot;

// Keys per slot, and each chunk after the first carries a full PNX_PERSIST_KEY_BYTES, so
// PNX_SAVE_CHUNKS_PER_SLOT * PNX_PERSIST_KEY_BYTES (4,096B) is the ceiling on one slot --
// which is MEASUREMENTS.md's "realistic 4KB save, 16 keys" case, because that measurement
// is what sized this constant, not the other way round.
#ifndef PNX_SAVE_CHUNKS_PER_SLOT
#define PNX_SAVE_CHUNKS_PER_SLOT 16
#endif

// The header rides inside chunk 0 rather than spending a key of its own -- see the "16
// keys" note above. 2B magic, 1B version, 1B chunk count, 2B payload length, 2B checksum.
#define PNX_SAVE_HEADER_BYTES	8
#define PNX_SAVE_CHUNK0_PAYLOAD (PNX_PERSIST_KEY_BYTES - PNX_SAVE_HEADER_BYTES)
#define PNX_SAVE_MAX_PAYLOAD \
	(PNX_SAVE_CHUNK0_PAYLOAD + (PNX_SAVE_CHUNKS_PER_SLOT - 1) * PNX_PERSIST_KEY_BYTES)

// Starts a save: computes the chunking for `bytes` of `data` at `version`, and writes
// chunk 0 (header plus the first slice of payload) immediately, so pnx_save_pending is
// answerable the instant this returns. `data` must stay valid and unchanged for as long as
// pnx_save_pending(slot) is true -- pnx_save_step reads from it again on every call rather
// than copying it up front, because a copy would need PNX_SAVE_MAX_PAYLOAD of arena for a
// save that is usually a fraction of that.
//
// Only one save may be in flight at a time; a second pnx_save_begin before the first
// finishes abandons it (logged) and starts the new one. False if `bytes` exceeds
// PNX_SAVE_MAX_PAYLOAD or the first chunk fails to write.
bool pnx_save_begin(PnxSaveSlot slot, const void* data, size_t bytes, uint8_t version);

// True while `slot` has chunks left from a pnx_save_begin that has not finished. A game
// calls pnx_save_step() once per frame while this holds.
bool pnx_save_pending(PnxSaveSlot slot);

// Writes exactly the next chunk of the in-flight save. False on a persist failure, which
// also clears pending -- the save is abandoned rather than retried, matching how
// everything else in the platform seam treats a device write failure as unrecoverable
// this frame. True otherwise, whether or not that chunk was the last one; check
// pnx_save_pending() to know if there is more to do.
bool pnx_save_step(PnxSaveSlot slot);

// One-shot and blocking: begins a save and writes every chunk before returning. For
// save-on-blur; see the header comment for why blocking is correct there and wrong in the
// frame loop. `data` only needs to stay valid for the duration of this call.
bool pnx_save_write(PnxSaveSlot slot, const void* data, size_t bytes, uint8_t version);

// Writes every remaining chunk of a save already started with pnx_save_begin. Rarely
// needed directly -- pnx_save_write covers the one-shot case -- but here for a game that
// started an incremental save and then hit the same "no frames left to spread it over"
// situation pnx_save_write exists for, after already calling pnx_save_begin.
bool pnx_save_flush(PnxSaveSlot slot);

// A slot written in one call, no spreading -- reads cost ~70us each on device, nothing
// like a write, so there is nothing here worth spreading across frames.
//
// Fails if the slot has never been written, is corrupt (a bad magic or checksum -- the
// checksum exists to catch a save that was torn by, say, the device dying mid-write), or
// its stored version is NEWER than `version`. That refusal is deliberate: an older build
// must never load a save format it does not understand and then overwrite it with
// something it silently got wrong. A save with an OLDER or equal version is accepted as-is
// -- this module does not migrate formats, a game that needs that reads the stored
// version back out and does it itself.
bool pnx_save_load(PnxSaveSlot slot, void* out, size_t max_bytes, uint8_t version,
				   size_t* out_bytes);

// The version stored in a slot, without decoding the payload. False (and *out_version
// untouched) if the slot does not hold a valid save.
bool pnx_save_peek_version(PnxSaveSlot slot, uint8_t* out_version);

bool pnx_save_exists(PnxSaveSlot slot);
bool pnx_save_delete(PnxSaveSlot slot);

#endif // PNX_USE_SAVE
