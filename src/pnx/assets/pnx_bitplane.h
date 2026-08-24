// Bitplane/Elias-gamma pixel decoding -- experimental, not yet wired into
// pnx_sprite_load/atlas_load_into. Standalone so it can be measured (correctness and
// decode cost) before any production load path depends on it.
//
// Where this differs from LZSS (pnx_lzss.c): LZSS compresses a whole pixel region as one
// sequential stream with backreferences, so decoding one sprite frame or atlas tile in
// isolation is impossible without decoding everything before it. This format compresses
// each unit (one sprite frame, or one atlas tile) completely independently -- no
// backreferences ever cross a unit boundary -- specifically so a single tile can be
// decoded straight from a small ROM read with no whole-sheet RAM copy. It loses on raw
// size against LZSS on most measured content (LZSS exploits cross-tile repetition this
// can't see) and wins on GB-derived/flat-shaded art specifically -- see
// docs/GAME-COMPARISON.md's session notes. Not a general replacement for compress_sprites
// / compress_atlases as they stand today.
//
// Per unit: bitplane-separate the local (frequency-sorted, <=16 entries) palette index
// into ceil(log2(k)) 1-bit planes, RLE each plane independently with Elias-gamma-coded
// run lengths (unbounded, no fixed-width cap the way compress_sprites/compress_atlases'
// LZSS token format needs) -- mirrors the mechanism the original Game Boy Pokemon sprite
// format uses, which a from-source measurement this session found beats generic LZSS by
// ~68% on the same pixels specifically because of this structure (plus mechanisms not
// reproduced here: a literal/zero-run hybrid and a delta pre-transform, both measured
// this session to not pay for themselves at this block scale once the palette is
// frequency-sorted -- see docs/GAME-COMPARISON.md).

#pragma once

#include "../pnx_config.h"

#if PNX_USE_BITPLANE_COMPRESS

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

// Decodes one unit (a sprite frame or an atlas tile) into `dst`, packed 4bpp -- 2
// pixels/byte, high nibble first, the SAME layout pack_unit_4bpp/pnx_blit_4bpp already
// use, so a decoded tile is immediately blittable or cacheable at half the bytes a
// one-byte-per-pixel output would cost. `dst` must be `(n+1)/2` bytes. `scratch` is the
// decoder's own working buffer -- bitplane reconstruction inherently produces one local
// index per pixel before the final real-palette remap, and that intermediate can't be
// packed 2-to-a-byte in place (a single bitplane's bit for pixel i and pixel i+1 land in
// different nibbles of what would be a shared output byte) -- so it needs `n` bytes of
// its own, caller-owned rather than an internal fixed buffer, the same division of
// labour PnxMap's own lzss_src/lzss_dst scratch already has for compressed atlas pool
// decode (pnx_assets.c's atlas_load_into).
//
// `n` is not stored in the blob -- the same way pnx already knows a frame's w*h or a
// tile's tile_px*tile_px from its own metadata before ever reading pixels, this format
// doesn't pay to repeat that per unit. Returns false if `src` is malformed (declares
// more distinct colours than fit in the encoded bit width, or the bitstream runs out
// before `n` pixels are produced) -- `dst` is undefined on failure, same posture as
// pnx_lzss_decode.
bool pnx_bitplane_decode(const uint8_t* src, size_t src_len, uint8_t* dst, uint8_t* scratch,
						 uint16_t n);

#endif // PNX_USE_BITPLANE_COMPRESS
