// LZSS decoding for compressed WorldTile banks (M12).
//
// Classic (12,4) token, chosen for the decoder it buys rather than the ratio: a 4096-byte
// window (12-bit offset) and a 3-18 byte match (4-bit length), no entropy stage. Real maps
// measured 6-8% of raw cell bytes with this against gzip's 4-12% -- close enough to gzip
// that the gap is not worth a Huffman stage's code and CPU on a Cortex-M, since decoding
// here is a copy/literal loop and nothing else.
//
// The encoder lives in tools/pnx_assets.py, build-time only, where speed and optimality
// never matter -- only the decoder's simplicity does, and that is what this file is.

#pragma once

#include "../pnx_config.h"

#if PNX_USE_MAP_COMPRESS

#include <stddef.h>
#include <stdint.h>

// Decodes an LZSS stream into `dst`, stopping once `dst_len` bytes have been produced or
// `src_len` bytes have been consumed, whichever comes first -- the caller already knows
// the exact decompressed size (a bank's uncompressed body is `slot_bytes` times however
// many WorldTiles it holds, both known before the read), so this is a bound, not a guess.
// Returns the number of bytes actually written; short of `dst_len` means the stream ended
// early, which pnx_assets.c treats as a truncated/corrupt bank the same way a short
// resource read already is.
size_t pnx_lzss_decode(const uint8_t* src, size_t src_len, uint8_t* dst, size_t dst_len);

#endif // PNX_USE_MAP_COMPRESS
