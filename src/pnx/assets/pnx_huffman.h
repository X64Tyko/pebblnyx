// Bitplane's sibling under PNX_COMPRESS_HUFFMAN (pnx_config.h) -- same independent
// per-unit, decode-on-cache-miss shape as pnx_bitplane.c, but run lengths are coded
// against ONE canonical Huffman table shared by every sprite and atlas in the project
// (loaded once via pnx_huffman_table_load, pnx_assets.c) instead of each unit coding its
// own Elias-gamma runs with no table at all.
//
// Bitplanes are CONCATENATED into one bpp*n-bit sequence before RLE (one start bit, runs
// may cross plane boundaries) rather than RLE'd independently per plane the way
// pnx_bitplane.c does -- this format's run-length alphabet is pooled project-wide, and
// pooling independent per-plane runs measured worse than pooling the concatenated
// sequence's runs (tools/bpeg2_benchmark.py, docs/GAME-COMPARISON.md).
//
// Format (tools/bpeg2_prototype.py's encode_unit_with_table/decode_unit_with_table,
// validated there against real content before this port):
//   header byte 0x00                        -- k==1 fill: next byte's high nibble is the
//                                               one colour's real palette index, no run
//                                               coding, no offset table, n pixels of it.
//   header byte 0b1kkkk (0x80 set)           -- ESC_RAW: offset table (ceil(k/2) bytes,
//                                               hi/lo real-palette-index pairs) then
//                                               ceil(n/2) bytes of packed LOCAL indices
//                                               (2/byte, high nibble first), remapped
//                                               through the offset table at decode.
//   header byte 0b0kkkk                      -- coded: offset table, then 1 start bit,
//                                               then one shared-table Huffman code per
//                                               run (RLE emission order, not sorted) over
//                                               the concatenated bpp*n-bit sequence.
// `kkkk` is (real colour count - 1); bpp is derived from k the same way pnx_bitplane.c
// does (ceil(log2(k))).

#pragma once

#include "../pnx_config.h"

#if PNX_COMPRESS_MODE == PNX_COMPRESS_HUFFMAN

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

// Bounds build_shared_table's own max_code_len (tools/bpeg2_prototype.py always calls it
// with 16) -- first_code/first_symbol/count are fixed [1..16] arrays, so loading a table
// never needs a dynamic allocation for anything but the flat `symbols` array below.
#define PNX_HUFFMAN_MAX_CODE_LEN 16

// One project-wide table, loaded once (pnx_huffman_table_load, pnx_assets.c) and passed
// to every pnx_huffman_decode call thereafter. Canonical-Huffman decode structure, not a
// verbatim copy of the wire format: the wire transmits each entry's own code explicitly
// (Specs.md's own stated reason -- "simpler decoder, one less thing it has to get right"
// for a REFERENCE decoder), but a real per-frame decoder here reconstructs first_code/
// first_symbol from code LENGTHS alone (guaranteed to reproduce the same canonical codes
// the encoder assigned) so a run decodes in O(code length) instead of a linear scan
// across every table entry per bit -- see pnx_huffman_table_load's own comment.
typedef struct
{
	uint16_t first_code[PNX_HUFFMAN_MAX_CODE_LEN + 1];	 // [0] unused, lengths are 1..16
	uint16_t first_symbol[PNX_HUFFMAN_MAX_CODE_LEN + 1]; // offset into `symbols`
	uint16_t count[PNX_HUFFMAN_MAX_CODE_LEN + 1];
	const uint16_t* symbols; // `cols` run-length values, arena-owned, resident
	uint16_t cols;
	uint8_t run_bits;
} PnxHuffmanTable;

// Bytes a decoded unit of `n` pixels occupies once hf_pack (pnx_huffman.c) packs it --
// mirrors PNX_BITPLANE_PACKED_BYTES exactly (same two output layouts, same reasoning).
#if PNX_DISPLAY_BW
#define PNX_HUFFMAN_PACKED_BYTES(n) (((size_t)(n) + 3) / 4)
#else
#define PNX_HUFFMAN_PACKED_BYTES(n) (((size_t)(n) + 1) / 2)
#endif

// Parses a global run-length table (tools/bpeg2_prototype.py's encode_table) from raw
// bytes into `table`, ready for pnx_huffman_decode. Reconstructs the canonical decode
// structure (first_code/first_symbol/count) from code LENGTHS alone -- the wire also
// carries each entry's own code value (Specs.md's stated reason: a simpler reference
// decoder that never has to reconstruct canonically), but this parse only needs to
// consume those bits to stay aligned, not use their value; canonical construction from
// lengths is guaranteed to reproduce exactly what the encoder assigned.
//
// `symbols_buf` (>= `symbols_buf_cap` entries) is caller-owned working memory the table
// keeps a pointer into for its whole resident lifetime -- an arena allocation sized to
// the table's own declared `cols` in pnx_assets.c, a plain stack/static array in a host
// test. Returns false, `table` left unusable, if `symbols_buf_cap` is smaller than the
// table's declared cols, cols exceeds PNX_HUFFMAN_TABLE_MAX_COLS, or the data is
// malformed/truncated.
#define PNX_HUFFMAN_TABLE_MAX_COLS 256

bool pnx_huffman_table_parse(PnxHuffmanTable* table, const uint8_t* data, size_t len,
							 uint16_t* symbols_buf, uint16_t symbols_buf_cap);

// Reads just the table's own declared column count (its first 12 bits) without parsing
// anything else -- lets a caller size its `symbols_buf` allocation to exactly this
// table's real cols instead of the PNX_HUFFMAN_TABLE_MAX_COLS worst case (pnx_assets.c's
// pnx_huffman_table_load does this, arena-allocating rather than reserving a static
// worst-case array that every project would pay for whether or not it's ever this big).
// Returns false (out_cols untouched) if `len` is too short to even hold the field.
bool pnx_huffman_table_peek_cols(const uint8_t* data, size_t len, uint16_t* out_cols);

// NULL until pnx_huffman_table_load (pnx_assets.c) succeeds -- pnx_sprite_cache.c/
// pnx_tile_cache.c's own use, to hand pnx_huffman_decode the table it needs on a cache
// miss. Declared here (not just pnx_assets.h) since the cache modules only include this
// header, not the whole of pnx_assets.h.
const PnxHuffmanTable* pnx_huffman_table(void);

// Decodes one unit against an already-loaded global `table` -- pnx_bitplane_decode's own
// sibling: same dst/scratch sizing contract, same "return false, dst undefined" posture
// on a malformed or truncated stream. `n` is not stored in the blob, same reasoning as
// pnx_bitplane_decode (caller already knows it from the unit's own frame/tile metadata).
bool pnx_huffman_decode(const PnxHuffmanTable* table, const uint8_t* src, size_t src_len,
						uint8_t* dst, uint8_t* scratch, uint16_t n);

#endif // PNX_COMPRESS_MODE == PNX_COMPRESS_HUFFMAN
