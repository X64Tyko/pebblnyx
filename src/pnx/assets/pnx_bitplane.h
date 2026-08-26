// Bitplane/Elias-gamma pixel decoding -- the codec behind PNX_COMPRESS_BITPLANE
// (pnx_config.h). Wired into pnx_sprite_cache.c (one frame at a time) and
// pnx_tile_cache.c (one atlas tile/subtile at a time), both decoding on a cache miss via
// pnx_bitplane_sprite_fetch/pnx_bitplane_atlas_fetch (pnx_assets.c). Real per-unit decode
// cost, measured on basalt hardware via examples/bitplanebench: tens of microseconds/unit.
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

#if PNX_COMPRESS_MODE == PNX_COMPRESS_BITPLANE

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

// Bytes a decoded unit of `n` pixels occupies once bp_pack (pnx_bitplane.c) packs it --
// callers size a cache slot or scratch buffer with this rather than re-deriving the /2 or
// /4 themselves, so PNX_DISPLAY_BW only has to be branched on in one place per format
// question. Matches bp_pack exactly; see pnx_bitplane_decode's own comment above for why
// the two output formats exist. A macro, not an inline function, so it stays a compile-
// time constant expression where a caller sizes a fixed (stack or struct) array with it.
#if PNX_DISPLAY_BW
#define PNX_BITPLANE_PACKED_BYTES(n) (((size_t)(n) + 3) / 4)
#else
#define PNX_BITPLANE_PACKED_BYTES(n) (((size_t)(n) + 1) / 2)
#endif

// Decodes one unit (a sprite frame or an atlas tile) into `dst`, packed to whichever
// format this build's PNX_DISPLAY_BW selects -- 2 pixels/byte 4bpp (high nibble first,
// pack_unit_4bpp/pnx_blit_4bpp's own layout) on a colour build, 4 pixels/byte 2bpp (high
// bits first, pack_unit_2bpp's own layout) on a 1-bit one. The bitplane/Elias-gamma
// stream itself already costs only ceil(log2(k)) bits/pixel for a unit's own real colour
// count `k` regardless of which output packing this decodes into -- a 2-colour BW tile
// costs the same ~1 bit/pixel it would on a colour build, this #if only changes how the
// reconstructed indices are packed back into bytes at the very end. `dst` must be
// `(n+1)/2` bytes on a colour build, `(n+3)/4` on a 1-bit one. `scratch` is the decoder's
// own working buffer -- bitplane reconstruction inherently produces one local index per
// pixel before the final real-palette remap, and that intermediate can't be packed
// multiple-to-a-byte in place (a single bitplane's bit for pixel i and pixel i+1 land in
// different bytes' worth of what would be a shared output byte) -- so it needs `n` bytes
// of its own, caller-owned rather than an internal fixed buffer, the same division of
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

#endif // PNX_COMPRESS_MODE == PNX_COMPRESS_BITPLANE
