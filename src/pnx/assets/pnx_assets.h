// Handle-based asset registry.
//
// Game code names assets by the handles that tools/pnx_assets.py generates, never by
// resource id, byte offset or sheet layout. Loading gives back a small view struct
// pointing into arena memory; there is no per-access I/O.
//
// **Residency, not streaming.** A resource read costs ~29 us per CALL plus ~33 MB/s of
// transfer, and there is no locality penalty -- a scattered read is as cheap as a
// sequential one. So one bulk read per asset, held for the scene's lifetime. Streaming
// a 16x16 tile at a time would cost ~6.7 ms/frame, 18% of the frame budget, to save
// memory that is not scarce. See docs/MEASUREMENTS.md.
//
// **The map is the exception, and the same numbers are why.** A map is read a WorldTile
// at a time -- a block of 16x16 cells -- because that unit amortises the ~29 us call over
// 256 cells and pays it when the player crosses a boundary rather than every frame: ~45 us,
// occasionally, against 6.7 ms every frame. The rule above is unchanged; only the unit is.
// It buys maps larger than RAM, which the u8 map dimensions could always describe and
// nothing could load. Its atlases stream on the same terms, pinned by the WorldTiles that
// name them.
//
// Assets live in an arena the caller supplies. There is no individual unload: a scene
// boundary resets the arena and reloads, which is the only load point that exists. A map's
// two pools are the one place a byte of arena is reused within a scene, and they are
// fixed-size and allocated once at map load -- so the arena still only ever grows.

#pragma once

#include "../pnx_config.h"

#if PNX_USE_ASSETS

#include "../core/pnx_arena.h"
#include "../core/pnx_orient.h"
#include "../platform/pnx_platform.h" // PNX_DISPLAY_BW, for PnxPalette's own shape

#include <stdint.h>
#include <stdbool.h>

// Mirrors tools/pnx_assets.py. `PNX_TILE_WARP` is the one legend-level flag left -- collision
// moved off the map and onto the tile (PNX_COLLISION_*, below), so it is no longer a bit a
// legend entry names.
#define PNX_TILE_WARP 0x01

// Collision MODE: a 2-bit field describing a TILE, not a cell. Lives in the revived
// PnxAtlas.tile_flags (one byte per art tile), which is why there is no per-cell exception
// list any more -- a fence's collision does not change depending on where it is placed.
// SCALED and COMPLEX name shape data (an inset rect, a 1bpp mask) that is not yet baked
// into a C-consumable side table -- the mode round-trips, the shape does not yet.
#define PNX_COLLISION_NONE	  0
#define PNX_COLLISION_SOLID	  1
#define PNX_COLLISION_SCALED  2
#define PNX_COLLISION_COMPLEX 3

// Every blob carries this, so a stale .bin against a newer runtime is a clean error
// rather than garbage pixels. Bumped whenever a format changes.
#define PNX_BLOB_VERSION 14 // v14: a map is 1..PNX_MAP_MAX_LAYERS streamed layers
							// (PnxMapLayer) instead of one implicit grid -- the "PM"
							// blob's header bytes 3..5 are now layer_count/
							// primary_layer/warp_count (were w/h/warp_count), and
							// its preamble carries one directory block per layer
							// (its own w/h/worldtile/bank range/parallax/wrap)
							// after the shared atlas table, ahead of the shared
							// palette/warp/cell-dictionary tables. Bank asset ids
							// are now per-layer (PNX_ASSET_BANK_<map>_<layer>_<i>).
							// v13: map preamble carries a cell dictionary
							// (PnxMap.cell_dict/dict_count/idx_width) -- a
							// WorldTile's cells are stored as 1- or 2-byte
							// indices into it rather than raw u16 entry words,
							// since every real map measured uses only a
							// handful of distinct entry values. v12: atlas
							// blobs carry baked SCALED rects / COMPLEX masks
							// (PnxAtlas.scaled_rects/complex_masks); WorldTile
							// payloads carry a sparse EXTENDED table
							// (PnxWorldTile.ext). v11: collision/warp moved
							// into the cell word; the old tile_flags[]-by-id
							// override bytes were dropped.
#define PNX_BLOB_HEADER_BYTES 8

// ------------------------------------------------------------------- palettes
//
// 4bpp throughout: two pixels per byte, high nibble first, indexing a 16-entry palette.
// **Index 0 is transparent in every palette**, following the SNES convention -- it costs
// one of sixteen slots and lets the blitter reject a pixel before it reads the palette.
//
// Palettes are shared across every asset in a project and live in one bounded table,
// sized by PNX_PALETTE_SLOTS. They are an asset of their own so a palette used by four
// atlases is stored once rather than four times.

#define PNX_PALETTE_ENTRIES		16
#define PNX_PALETTE_TRANSPARENT 0

// The pipeline always emits the palette table as asset 0, before anything that indexes
// it. Named here so the scene loader does not carry a bare literal, and asserted by the
// generated header so the two cannot drift.
#define PNX_ASSET_PALETTES_SLOT 0

// Colour only. A 1-bit build never indexes a palette at all: its atlas/sprite pixels are
// ~bw resources (pack_unit_2bpp, tools/pnx_assets.py) with ink/paper/dither already baked
// in at build time, and pnx_gfx_fill_rect/pnx_text.c classify their raw GColor8 `colour`
// argument straight through pnx_bw_is_ink, no palette involved either. So this struct
// carries no 1-bit-specific field any more (an earlier version did: a per-entry ink mask,
// for a 4bpp-indexed-plus-mask fallback path that let a build mix 4bpp and ~bw art. That
// path is gone -- a build is exclusively one format now -- and the mask went with it).
//
// pnx_palettes_load is still callable on every platform (see its own comment) so game
// code stays the same source everywhere, but on a 1-bit build it never touches flash: the
// palette table it would fill is not used by anything.
//
// Memory-mapped straight onto the blob (pnx_assets.c: `(PnxPalette*)data`), so this layout
// is the wire format: tools/pnx_assets.py's palette_bytes must produce exactly
// sizeof(PnxPalette) bytes per palette -- which is what pnx_palettes_load checks against,
// so the two cannot silently drift the way a hand-copied byte count could.
typedef struct
{
	uint8_t entries[PNX_PALETTE_ENTRIES]; // GColor8 values; [0] is transparent
} PnxPalette;

// Fills the table on a colour build. Must be called before any atlas or sprite loads on
// that build, since those carry palette indices rather than palette data -- but a no-op on
// a 1-bit one (see PnxPalette's own comment): it neither reads a resource nor requires
// calling before anything else there, kept callable only so init code is the same source
// on every platform.
bool pnx_palettes_load(uint16_t asset_id);

// NULL if the slot is beyond what was loaded.
const PnxPalette* pnx_palette(uint8_t slot);
uint16_t pnx_palette_count(void);

// An atlas is stored one of two ways, chosen by the pipeline on measured size:
//
//   flat       -- pixels are tile_count whole tiles.
//   metatiled  -- pixels are subtile_count deduplicated quadrants, and `metatiles` holds
//                 four u16 indices per tile (top-left, top-right, bottom-left,
//                 bottom-right). Measured 1.72x smaller across five real tilesets, but
//                 only 1.19x on a small hand-picked region, which is why it is a
//                 measurement rather than a preference.
//
// A quadrant always uses the palette of the tile it belongs to, which the pipeline's
// dedup key guarantees, so palette lookup is unchanged between the two.
// Field order below is deliberate, not incidental: every pointer first, then every u16,
// then every u8, so the struct needs no padding between them (clang-tidy's own
// optin.performance.Padding check is what caught the 13 wasted bytes an earlier,
// declared-in-the-order-they-made-sense-to-explain layout carried). The comments keep
// the earlier grouping in prose -- pixel data, then SCALED/COMPLEX shape data, then
// sizes -- so reading order is unaffected even though declaration order now is not.
typedef struct
{
	const uint8_t* pixels;		 // whole tiles, or the quadrant bank
	const uint8_t* tile_palette; // tile_count, palette slot per tile
	const uint8_t* tile_flags;	 // tile_count, PNX_COLLISION_* mode (see the #define above)
	const uint16_t* metatiles;	 // NULL when flat; else tile_count * 4 indices

	// SCALED and COMPLEX shape data, sparse -- most tiles are NONE or plain SOLID, whose
	// whole shape IS the mode byte above, so only the tiles that need more than that pay
	// for it. Byte-packed rather than struct-mapped on purpose: a record starts wherever
	// the previous one ended, which nothing guarantees is 2-byte aligned, so this is
	// walked with pnx_atlas_tile_scaled_rect/pnx_atlas_tile_complex_mask rather than cast
	// to a struct pointer the way PnxWarp's all-u8 layout safely can be.
	const uint8_t* scaled_rects;  // scaled_count * 6B: u16 tile, u8 x, u8 y, u8 w, u8 h
	const uint8_t* complex_masks; // complex_count * (2 + mask_bytes)B: u16 tile, then a
								  // row-major MSB-first 1bpp mask, mask_bytes =
								  // (tile_px*tile_px+7)/8

	uint16_t scaled_count;
	uint16_t complex_count;
	uint16_t tile_count;
	uint16_t subtile_count;
	uint8_t tile_px;
	// Bytes per whole tile: w*h/2 (4bpp indexed) on a colour build, w*h/4 (~bw packed 2bpp,
	// pack_unit_2bpp in tools/pnx_assets.py) on a 1-bit one. A build is exclusively one
	// format or the other (PNX_DISPLAY_BW, pnx_platform.h) -- there is no per-blob
	// ambiguity to record here, unlike an earlier version of this field.
	uint8_t tile_bytes;
	uint8_t sub_bytes; // bytes per quadrant; 0 when flat
} PnxAtlas;

static inline bool pnx_atlas_is_metatiled(const PnxAtlas* a)
{
	return a->metatiles != NULL;
}

// Linear scans over `a->scaled_count`/`a->complex_count` entries -- both are sparse by
// construction (most tiles are NONE or SOLID, which cost nothing here), the same
// tradeoff pnx_map_warp_at already makes for a map's handful of warps.

// SCALED's inset rect for `tile`, tile-local pixels. False (leaving x/y/w/h untouched) if
// `tile` is not SCALED, which pnx_atlas_tile(a, tile)'s caller can also just check via
// `a->tile_flags[tile] == PNX_COLLISION_SCALED` first if it wants to skip the scan.
bool pnx_atlas_tile_scaled_rect(const PnxAtlas* a, uint16_t tile, uint8_t* x, uint8_t* y,
								uint8_t* w, uint8_t* h);

// COMPLEX's 1bpp mask for `tile`, or NULL if `tile` is not COMPLEX. `mask_bytes` is
// (a->tile_px * a->tile_px + 7) / 8; pnx_atlas_mask_pixel is the bit test against it.
const uint8_t* pnx_atlas_tile_complex_mask(const PnxAtlas* a, uint16_t tile);

// One bit out of a mask pnx_atlas_tile_complex_mask returned -- row-major, MSB first (see
// PnxAtlas.complex_masks' own comment). No bounds check: `px`/`py` are the caller's to
// keep inside `a->tile_px`, the same contract pnx_atlas_tile's index already carries.
static inline bool pnx_atlas_mask_pixel(const uint8_t* mask, uint8_t tile_px, uint8_t px,
										uint8_t py)
{
	const uint32_t i = (uint32_t)py * tile_px + px;
	return (mask[i / 8] & (0x80 >> (i % 8))) != 0;
}

typedef struct
{
	const uint8_t* pixels;		  // frame_count * frame_bytes
	const uint8_t* frame_palette; // frame_count, palette slot per frame
	uint8_t w, h;
	uint8_t frame_count;
	uint16_t frame_bytes; // w*h/2 colour, w*h/4 1-bit -- see PnxAtlas's own field
} PnxSprite;

// One packed panel image plus the four insets that carve it into nine regions: four
// corners drawn once each, four edges tiled along their own axis, a centre tiled in
// both -- a bordered HUD panel at any size without stretching a single pixel, since
// nothing in this engine scales art (see pnx_gfx_draw_nine_slice's own comment). Single
// frame, single palette: an animated panel is not a thing any game has asked for, and
// PnxSprite already exists for the day one does.
typedef struct
{
	const uint8_t* pixels; // w*h/2 colour, w*h/4 1-bit -- one packed image, not nine
	uint8_t w, h;
	uint8_t border_l, border_t, border_r, border_b; // pixel insets into `pixels`
} PnxNineSlice;

typedef struct
{
	uint8_t x, y;	  // tile the warp triggers on
	uint8_t dest_map; // index into the manifest's map order
	uint8_t dest_x, dest_y;
} PnxWarp;

// ------------------------------------------------------------------ maps and WorldTiles
//
// A map is stored as a grid of **WorldTiles** -- square blocks of map CELLS that are the
// unit of residency. Not to be confused with a metatile, which is the deduplicated 8x8
// quadrant inside an atlas: a metatile is art, a WorldTile is a piece of the world.
//
// Every map is sliced, including small ones. When the whole grid fits the pool, all of it
// loads at map load and nothing is ever evicted -- so a 32x24 map costs what it always did
// and there is no second code path for "small". Only the residency policy adapts.
//
// A map also draws from SEVERAL atlases. Its cells name a map-global tile id, and the
// atlas table below says where each atlas's slice of that id space begins -- which spends
// none of the four per-cell bits reserved for a future per-cell palette, and costs the
// draw loop one walk of a table with at most PNX_MAP_MAX_ATLASES entries.

#define PNX_MAP_MAX_ATLASES 8
#define PNX_MAP_NO_SLOT		0xFF

// A map declares 1..PNX_MAP_MAX_LAYERS WorldTile-streamed planes (M13), composited in
// array order -- background, ground, overlay, whatever a project calls them. The
// framework does not prescribe what an index MEANS, only how many can exist. Sprite
// layers (`PNX_SPRITE_LAYER_COUNT`, `pnx_sprite.h`) are a separate 0-15 range; the two
// interleave for free through `pnx_layers_draw` (`pnx_layer.h`), which walks one ordered
// `PnxLayer[]` regardless of whether an entry is a map layer or a sprite layer.
#define PNX_MAP_MAX_LAYERS 4

// How many resource READS a single pnx_map_stream call will issue.
//
// Reads, not WorldTiles, and the difference is not pedantic: a run of consecutive
// WorldTiles is fetched in one read, so counting tiles charges a batched fetch four or
// five times over and throttles the streamer to a quarter of what it is paying for. That
// mistake was invisible while WorldTiles were large and few, and showed up the moment the
// pipeline started choosing smaller ones -- the backlog went from 3 to 12 on the same
// content at the same speed, for no extra I/O.
//
// Eight, against a measured ~1.8 ms per read once payloads are banked: ~14 ms of a
// 37.33 ms frame in the worst case, on top of ~8 ms of ordinary work. The cap bounds a
// pathological case rather than an expensive ordinary one -- filling a fresh window takes
// about that many runs, so this is roughly "catch up within one frame".
#ifndef PNX_MAP_STREAM_BUDGET
#define PNX_MAP_STREAM_BUDGET 8
#endif

// Longest run of WorldTiles one read may carry. Bounds a stack array in the loader, and
// nothing else: a caller wanting more loops. Independent of the budget above, which counts
// reads and says nothing about how many WorldTiles one of them holds.
#define PNX_MAP_MAX_RUN 32

typedef struct
{
	uint16_t asset;		 // the atlas's asset id
	uint16_t first_tile; // where its slice of the map's tile id space begins
	uint16_t tile_count;
	uint8_t slot; // atlas pool slot holding it, or PNX_MAP_NO_SLOT
} PnxMapAtlas;

// One resident WorldTile. `cells` points into the pool slot, not into the blob: the blob
// is never held whole. `ext` is EXTENDED's own sparse table -- x, y (local to this
// WorldTile) and the tag value, one triple per cell that set PNX_MAP_EXTENDED. Empty for
// the overwhelming majority of WorldTiles, which is the same "sparse by construction, so
// nobody pays who doesn't use it" tradeoff the old collision-override table made, now
// spent on something that genuinely needs a per-cell exception list -- an arbitrary tag
// has no tile-owned default to diverge FROM the way collision's mode byte did.
typedef struct
{
	const uint8_t* cells; // cell_w * cell_h u16 entries
	const uint8_t* ext;	  // ext_count * 3 bytes: x, y local to this WorldTile, value
	uint16_t ext_count;
	uint8_t wx, wy;			// which WorldTile of the grid this slot holds
	uint8_t cell_w, cell_h; // clipped at the map's edge, so no padding is stored
	bool live;
} PnxWorldTile;

// Collision/warp used to come from a map-owned flag plane (plus sparse per-WorldTile
// overrides for the cells that genuinely differed from their tile's default) precisely
// because a cell can be asked about -- the player walking toward a wall -- while its
// atlas is still off the streamer's window. That "flags for a cell whose art is not
// resident" case is now answered a different way: collision/warp are baked straight into
// the cell's own u16 (PNX_MAP_ROTATE and friends, below), and the cell plane is exactly as
// resident as it always was. So there is no flag plane here any more, sparse or otherwise
// -- collision reads out of PnxAtlas.tile_flags on the OWNING atlas instead, which is a
// once-per-atlas cost rather than a per-cell one. EXTENDED is the one thing that DOES
// still need a per-cell exception list, and it lives on PnxWorldTile above rather than
// here, for the same residency reason the old flag plane existed: a tag is asked about
// exactly when its cell (and so its WorldTile) is already resident, never before.
// One streamed plane (M13). Everything that used to be the map's only WorldTile grid now
// lives here, one per declared layer -- a layer behaves exactly as the map used to: sliced
// into WorldTiles, held whole when it fits its own pool, streamed against the camera when
// it does not. What is new is that there can be more than one, each with its own extent
// (`w`/`h`, which may be smaller than another layer's -- a screen-sized parallax layer or a
// partial overlay are both just a small `w`/`h`), its own WorldTile granularity, and its
// own run of bank assets. A map's atlas table and tile-id space are NOT here -- see
// PnxMap's own comment for why those stay shared.
typedef struct
{
	const uint8_t* wt_mask; // wt_cols * wt_rows: which atlases each WorldTile needs

	PnxWorldTile* slots; // slot_count of them
	uint8_t* slot_mem;	 // slot_count * slot_bytes
	uint8_t* wt_slot;	 // wt_cols * wt_rows: slot holding it, or NO_SLOT

	// WorldTile payloads are not in the map's resource. They live in BANK resources whose
	// asset ids run consecutively from `first_bank_asset`, because a ranged read costs by
	// how far in it starts -- see docs/MEASUREMENTS.md. A tile's home needs no lookup:
	// bank `index >> bank_shift`, offset `(index & mask) * slot_bytes`, since payloads are
	// padded to the slot stride. Which also makes a run of consecutive tiles one read.
	// Every layer owns its OWN run -- `PNX_ASSET_BANK_<map>_<layer>_<i>` in the generated
	// header -- because two layers rarely share a WorldTile size, and so never share a
	// bank stride.
	uint16_t first_bank_asset;
	uint8_t bank_shift;

	uint16_t slot_bytes;
	uint8_t w, h; // this layer's own extent; may be smaller than another layer's
	uint8_t slot_count;
	uint8_t wt_cols, wt_rows;
	uint8_t worldtile; // cells per side
	uint8_t wt_shift;  // log2(worldtile): a cell finds its WorldTile by shifting

	// Consumed by pnx_layer.c's pnx_layers_draw, the same PNX_LAYER_PARALLAX_WORLD/SCREEN
	// scale every other layer kind uses -- a map layer is not a special case to that
	// compositor, just another PNX_LAYER_CALLBACK.
	uint8_t parallax_pct;

	// A layer smaller than the camera's view (the common case for a parallax background)
	// repeats: sampling and streaming both wrap modulo this layer's own w/h instead of
	// clamping at its edge. A non-wrap layer keeps the ordinary clamp -- open sky past the
	// last WorldTile, not a repeated one.
	bool wrap;

	// Every WorldTile and every atlas has a slot, so this layer was loaded whole and can
	// never need another read. Every small layer is in this case, which is most of them:
	// the streaming calls become one comparison and return.
	bool held_whole;
} PnxMapLayer;

typedef struct
{
	// Optional palette variant: tile_total bytes naming the palette slot to use instead of
	// the atlas's own, so one atlas serves several recoloured zones. NULL means use the
	// atlas's. 44 bytes for the cave tileset against ~5,600 for a second copy of it.
	const uint8_t* tile_palette;
	// Warps apply to the PRIMARY layer only -- the one gameplay actually happens on. A
	// background or overlay layer is art, not a place the player's own position is ever
	// tested against.
	const PnxWarp* warps;

	// The cell dictionary (M12): a WorldTile's cells are stored as INDICES into this table,
	// not raw entry words. Shared across every layer, not one per layer -- a map's tiles
	// are one shared id space (see below), so its distinct (tile id + flip/rotate/warp/
	// extended) combinations are one shared table too, the same reason the atlas table
	// itself is shared rather than duplicated. `cell_dict[index]` recovers the exact entry
	// word `pnx_map_entry` used to read directly; nothing downstream of that function needs
	// to know cells are stored indexed at all.
	const uint8_t* cell_dict; // dict_count * 2 bytes, read the same way a cell's own entry
							  // word is -- see pnx_map_entry's own comment
	uint16_t dict_count;
	uint8_t idx_width; // 1 or 2 bytes per stored cell; 2 only when dict_count > 256

	// ONE shared atlas table and tile-id space for the whole map (M13): any layer's cells
	// may reference any tile from any atlas the map declares -- there is no per-layer
	// atlas. The pool's slots are NOT a uniform stride: when there is a slot per atlas
	// nothing is ever evicted, so each slot is exactly its atlas's size. Only a map that
	// really streams its atlases pays for slots that all hold the largest. Two layers
	// sharing an atlas do not double-load it -- `pool_pins[slot]` already just counts live
	// WorldTiles depending on a slot, regardless of which layer they belong to.
	const uint8_t* pool_offset; // (atlas_slots + 1) u32 offsets into pool_mem

	PnxMapAtlas atlas[PNX_MAP_MAX_ATLASES];
	PnxAtlas* pool;		 // atlas_slots views onto pool_mem
	uint8_t* pool_mem;	 // pool_bytes
	uint8_t* pool_owner; // atlas_slots: which atlas index sits there, or NO_SLOT
	uint8_t* pool_pins;	 // atlas_slots: live WorldTiles depending on that slot

	PnxMapLayer layers[PNX_MAP_MAX_LAYERS];
	uint8_t layer_count;
	// Which of `layers` owns `warps` and is what pnx_map_solid/pnx_map_tile/pnx_map_flags/
	// pnx_map_extended/pnx_map_warp_at operate on by default -- unchanged call signatures
	// for the common, single-layer case. The `_layer` sibling of each takes an explicit
	// index for anything else.
	uint8_t primary_layer;

	uint32_t resource;	 // the map's own resource: the resident preamble
	uint16_t tile_count; // the map's whole tile id space, summed across every atlas slice
	uint8_t warp_count;
	uint8_t atlas_count;
	uint8_t atlas_slots;
	uint8_t tile_px;

#if PNX_USE_MAP_COMPRESS
	// Set when this map's banks are LZSS-compressed (`compress_maps` in the manifest's
	// [project] table) -- one flag for the whole map, not per layer: the manifest's own
	// toggle is project-wide, so no real map ever has some layers compressed and others
	// not. A compressed bank is an ATOMIC streaming unit: any one WorldTile needed from it
	// means reading and decoding the whole thing, not the precise partial-range reads an
	// uncompressed bank allows -- see worldtile_load_run's own comment (pnx_assets.c) for
	// why that is the right trade for what this buys.
	//
	// ONE scratch pair, shared and reused across every layer's banks, not one per layer:
	// streaming is sequential (one bank decodes, gets consumed, then the next), so nothing
	// is lost by sharing, and a second/third/fourth layer's own scratch would otherwise
	// duplicate RAM for no reason. Sized to the LARGEST bank across every layer
	// (`max over layers of (1 << bank_shift) * slot_bytes`) so it covers all of them: `src`
	// holds the compressed bytes just read (LZSS output can never exceed what it started
	// from, so this bound covers the compressed size too), `dst` holds the decoded bank
	// body a WorldTile's own bytes are then copied out of into its resident pool slot.
	bool compressed;
	uint8_t* lzss_src;
	uint8_t* lzss_dst;
#endif
} PnxMap;

typedef struct
{
	const uint8_t* text;	 // NUL-terminated pages, back to back
	const uint16_t* offsets; // one per page, into text
	const uint8_t* index;	 // entry_count * 4 bytes: u16 first_page, u16 page_count
	uint16_t entry_count;
} PnxDialog;

// ---------------------------------------------------------------------- fonts
//
// Glyphs are 1bpp or 2bpp, NOT the 4bpp everything else uses. Text has one colour, so
// the four bits an atlas spends naming a palette entry would be three bits of waste per
// pixel. 1bpp is ink or nothing; 2bpp adds two coverage levels the blitter blends
// against whatever is already on screen.
//
// Every glyph is trimmed to its inked box, and positioned by a bearing from the pen and
// from the BASELINE. Uniform cells would be simpler, but most glyphs occupy nowhere near
// the full line box -- trimming is a wash at 12px and roughly halves a 24px face.
//
// Drawing lives in gfx/pnx_text.h; this is only the storage and the lookup.

#define PNX_FONT_GLYPH_BYTES 8
#define PNX_FONT_NO_GLYPH	 0xFF // codepoint map entry for a character the font lacks

// Which way the pen walks between glyphs.
//
// A landscape build stores its glyphs turned on their side, and a turned glyph still
// blits like any other rectangle -- but the next one is no longer to the right of it.
// This is the whole of what pre-rotation costs the engine: one field, and the arithmetic
// in pnx_text that reads it.
//
// It belongs to the FONT rather than to the project because that is where it can never be
// wrong: a blob that knows how to draw itself cannot be paired with the wrong constant.
// And it is the same field a vertical script needs -- Japanese set top-to-bottom is
// ADVANCE_Y_POS with glyphs that were never rotated -- which is why it is an axis rather
// than a bool called `landscape`.
typedef enum
{
	PNX_ADVANCE_X_POS, // left to right: portrait, and every Latin face
	PNX_ADVANCE_Y_POS, // top to bottom
	PNX_ADVANCE_Y_NEG, // bottom to top
	PNX_ADVANCE_X_NEG, // right to left; carried by the format, not yet emitted
	PNX_ADVANCE_COUNT
} PnxAdvanceAxis;

typedef struct
{
	const uint8_t* bitmaps;
	const uint8_t* glyphs; // glyph_count * PNX_FONT_GLYPH_BYTES
	const uint8_t* map;	   // one byte per codepoint in [first_cp, last_cp]
	uint16_t glyph_count;
	uint16_t bitmap_bytes;
	uint8_t depth;		 // 1 or 2
	uint8_t line_height; // ascent + descent: what to advance between lines
	uint8_t baseline;	 // ascent: top of the line box to the baseline
	uint8_t space_advance;
	uint8_t first_cp, last_cp;
	uint8_t fallback; // glyph drawn for a character the font does not carry
	uint8_t advance;  // PnxAdvanceAxis: which way the pen walks
} PnxFont;

// One glyph's metrics, unpacked from the index. `bits` is NULL when the glyph has no ink
// -- a space -- which the drawing loop treats as advance-only rather than as an error.
// `w` and `h` are the bitmap AS STORED, which a landscape build has already rotated, so
// the blitter needs no special case. The three metrics beside them stay typographic --
// along the baseline, and up from it -- because that is the frame line layout works in.
// pnx_text is the one place the two frames meet.
typedef struct
{
	const uint8_t* bits;
	uint8_t w, h;
	uint8_t advance;  // along the baseline, whichever way that runs
	int8_t bearing_x; // pen to the START edge of the bitmap, along the baseline
	int8_t bearing_y; // baseline to the TOP of the bitmap, positive upwards
} PnxGlyph;

bool pnx_font_load(PnxFont* out, uint16_t asset_id);

// Never fails: an unmapped character resolves to the font's fallback glyph. A visible
// substitute beats a silent gap, which reads as a layout bug rather than a missing
// character.
static inline uint8_t pnx_font_glyph_index(const PnxFont* f, char c)
{
	const uint8_t cp = (uint8_t)c;
	if (cp < f->first_cp || cp > f->last_cp)
		return f->fallback;
	const uint8_t g = f->map[cp - f->first_cp];
	return g == PNX_FONT_NO_GLYPH ? f->fallback : g;
}

// No bounds check on `index`: the loader has already verified every map entry and every
// bitmap offset, and this runs per character per frame.
static inline void pnx_font_glyph(const PnxFont* f, uint8_t index, PnxGlyph* out)
{
	const uint8_t* e = f->glyphs + (uint32_t)index * PNX_FONT_GLYPH_BYTES;
	out->w			 = e[2];
	out->h			 = e[3];
	out->advance	 = e[4];
	out->bearing_x	 = (int8_t)e[5];
	out->bearing_y	 = (int8_t)e[6];
	out->bits		 = out->w ? f->bitmaps + (uint16_t)(e[0] | (e[1] << 8)) : NULL;
}

// Bytes per bitmap row. Rows are byte-aligned rather than a continuous bit stream, so a
// row is indexed by multiply-and-add instead of tracked as a bit offset.
static inline uint8_t pnx_font_row_bytes(const PnxFont* f, uint8_t w)
{
	return (uint8_t)(((uint16_t)w * f->depth + 7u) / 8u);
}

// Two arenas, because they have different lifetimes. `persistent` holds the scene table
// and outlives everything; `scene` holds the assets a scene needs and is reset wholesale
// at every scene boundary. Keeping them separate is what lets a scene load free its
// predecessor without also freeing the table telling it what to load.
//
// `resources` maps each PnxAssetId to its platform resource id -- pass the generated
// PNX_ASSET_RESOURCE_TABLE.
bool pnx_assets_init(PnxArena* persistent, PnxArena* scene, const uint32_t* resources,
					 uint16_t count);

// Declares which orientation this build's resources must carry. Pass the generated
// header's PNX_ORIENTATION, AFTER pnx_assets_init -- init clears any expectation so that
// a second init does not inherit the first one's.
//
// Optional, and worth one line anyway. Without it the first blob loaded sets the
// expectation and every later blob is checked against it, which catches the mixed bundle;
// with it, a bundle that is uniformly stale is caught too. False if the value is not an
// orientation.
bool pnx_assets_expect_orientation(uint8_t orientation);

// What the loaded resources say they were built for, or 0xFF before anything is loaded.
uint8_t pnx_assets_orientation(void);

// ------------------------------------------------------- loading past a scene
//
// Routes subsequent loads to the PERSISTENT arena instead of the scene one. Returns the
// previous setting, so a caller can restore rather than assume.
//
//     const bool was = pnx_assets_persistent(true);
//     pnx_music_load(&song, PNX_ASSET_MUSIC_BATTLE);
//     pnx_assets_persistent(was);
//
// This exists because a scene is not the only lifetime a game has. Music is the clear
// case: it is not scene-declared, it plays across a scene boundary by design -- a battle
// theme starts as the field scene unloads -- and loaded into the scene arena it is freed
// out from under the sequencer by the very transition it is scoring. That does not fail
// loudly; the song plays on through whatever now occupies those bytes.
//
// It is a narrow hook rather than a third arena because the persistent arena already has
// exactly the right lifetime. What was missing was any way for a game to reach it, which
// pnx_scenes_load has needed internally since it was written.
//
// Keep what goes here small and bounded. The persistent arena is never reset, so anything
// loaded into it is loaded for the life of the app -- load once at boot, not per scene.
bool pnx_assets_persistent(bool on);

// ---------------------------------------------------------------------- scenes
//
// A scene is the only load point. Loading one resets the scene arena, then loads exactly
// the assets the manifest declared for it -- so an asset list is content, checked by the
// pipeline and budgeted by it, rather than a sequence of load calls in C that nothing can
// verify.

bool pnx_scenes_load(uint16_t asset_id);

// Resets the scene arena and loads the scene's declared assets. False leaves nothing
// usable loaded; the log says which asset failed.
bool pnx_scene_load(uint16_t scene_id);

// Valid only after a successful pnx_scene_load. NULL when the scene declared none.
const PnxAtlas* pnx_scene_atlas(uint8_t index);
const PnxSprite* pnx_scene_sprite(uint8_t index);
const PnxNineSlice* pnx_scene_nine_slice(uint8_t index);
// Not const: a map streams, so its resident set changes as the camera moves. Handing back
// a const pointer would have meant a `_mut` twin for the streaming calls, which says the
// same thing less honestly.
PnxMap* pnx_scene_map(void);
const PnxDialog* pnx_scene_dialog(void);
const PnxFont* pnx_scene_font(uint8_t index);
uint8_t pnx_scene_atlas_count(void);
uint8_t pnx_scene_sprite_count(void);
uint8_t pnx_scene_font_count(void);
uint8_t pnx_scene_nine_slice_count(void);

// Each returns false and leaves `out` untouched if the resource is missing, the blob is
// the wrong type or version, or its declared dimensions do not match its actual size --
// the last of which is what catches a truncated or half-written resource.
bool pnx_atlas_load(PnxAtlas* out, uint16_t asset_id);
bool pnx_sprite_load(PnxSprite* out, uint16_t asset_id);
bool pnx_nineslice_load(PnxNineSlice* out, uint16_t asset_id);
bool pnx_dialog_load(PnxDialog* out, uint16_t asset_id);

// Takes no atlas: a map names the tilesets it was authored against and owns them, which
// is what lets it draw from several and stream them. Loading a map allocates its two pools
// from the scene arena -- the sizes come from the blob, so what a map costs resident is a
// number the pipeline printed at build time rather than one discovered on the watch.
//
// A map small enough to be held whole IS held whole when this returns -- every WorldTile
// and every atlas -- so small maps behave exactly as they did before WorldTiles existed
// and never run the streaming path at all. A map larger than its pool comes back with
// nothing resident; call pnx_map_stream_now once before the first frame, which is what a
// scene load and a warp both do.
bool pnx_map_load(PnxMap* out, uint16_t asset_id);

// Bring the WorldTiles covering a world-pixel rectangle into residency, plus one
// WorldTile of margin around it -- across EVERY layer the map declares (M13), each scaled
// by its own `parallax_pct` and wrapped modulo its own extent first if `wrap` is set.
//
// `pnx_map_stream` spends at most PNX_MAP_STREAM_BUDGET reads TOTAL across every layer,
// not per layer, and returns how many WorldTiles are still missing summed the same way, so
// a caller can see it falling behind. Per frame.
//
// `pnx_map_stream_now` returns only once everything every layer's rectangle needs is
// loaded. For a scene load or a warp, where there is no previous frame to show and a
// partial world would be visible as holes.
uint8_t pnx_map_stream(PnxMap* m, int32_t x, int32_t y, int32_t w, int32_t h);
uint8_t pnx_map_stream_now(PnxMap* m, int32_t x, int32_t y, int32_t w, int32_t h);

// WorldTiles resident right now, summed across every layer, for diagnostics and for tests
// that assert on eviction.
uint8_t pnx_map_resident(const PnxMap* m);

// Reads a whole blob into the scene arena and validates magic and version, handing back
// the four format-specific header bytes. Shared so modules outside assets/ -- audio, for
// instance -- do not each reimplement the header check.
const uint8_t* pnx_blob_load(uint16_t asset_id, const char* magic, uint8_t* a, uint8_t* b,
							 uint8_t* c, uint8_t* d, size_t* payload);

// Bytes read from flash since init, for budgeting scene loads.
uint32_t pnx_assets_bytes_loaded(void);

// ------------------------------------------------------------------- inline access
//
// Hot paths. No bounds checking on the tile accessors: they run per pixel per frame,
// and the pipeline already guarantees indices are in range.

// Whole-tile pixels. NULL on a metatiled atlas, where a tile has no contiguous
// representation -- use pnx_blit_metatile instead.
static inline const uint8_t* pnx_atlas_tile(const PnxAtlas* a, uint8_t index)
{
	return a->metatiles ? NULL : a->pixels + (uint32_t)index * a->tile_bytes;
}

static inline const PnxPalette* pnx_atlas_tile_palette(const PnxAtlas* a, uint8_t index)
{
	return pnx_palette(a->tile_palette[index]);
}

static inline const uint8_t* pnx_sprite_frame(const PnxSprite* s, uint8_t frame)
{
	return s->pixels + (uint32_t)frame * s->frame_bytes;
}

static inline const PnxPalette* pnx_sprite_frame_palette(const PnxSprite* s, uint8_t frame)
{
	return pnx_palette(s->frame_palette[frame]);
}

// Expands 4bpp source into 8bpp GColor8. Transparent pixels are left untouched in dst,
// so a caller can pre-fill a background. The real blitter (M3) does this inline with
// clipping; this exists for code that just wants pixels.
//
// Not on a 1-bit build: it hands back GColor8 bytes, and PnxPalette carries none there
// (see the struct's own comment) -- there is no colour for this to decode INTO. Code that
// wants ink/paper directly should use pnx_bw_is_ink against the palette's ink_mask
// instead (pnx_gfx.h), which is what the real blitter already does.
#if !PNX_DISPLAY_BW
void pnx_decode_4bpp(const uint8_t* src, const PnxPalette* palette, uint8_t* dst,
					 uint16_t pixels);
#endif

// Map cells are u16, not u8. Ten bits of tile index (1024, up from 255), two flip bits, a
// rotate bit, a warp bit, and a bit (EXTENDED) naming a per-cell tag in a sparse side
// table. Doubling the map costs ~1.2KB across the example maps, against 128 bytes for
// every tile a mirrored pair no longer needs its own copy of.
//
// The ten bits index the MAP's id space, not one atlas's, which is what lets a map draw
// from several tilesets without spending a bit per cell on saying which.
//
// ROTATE is a transpose, not an angle: rotate plus flip X/Y spans all eight symmetries of
// a square with three independent bits rather than a redundant 2-bit angle encoding. A
// 15x4 art tile in a 16x16 cell becomes 4x15 with ROTATE set. Render-side support (the
// blitter turning a tile on its side) is not built yet -- the bit round-trips through the
// pipeline and the map format, but nothing draws it rotated yet.
//
// WARP moved here from the old per-tile-id flag byte: whether a cell triggers a warp is
// now a property of the placement, like it always should have been, not of the tile
// (`fold_flag_into_entry`, tools/pnx_assets.py). pnx_map_warp_at does not consult this bit
// at runtime -- it scans the small PnxWarp table by (x, y) -- so this is mainly an
// authoring-time signal; a bit accessor is provided anyway for symmetry with flip/rotate.
//
// EXTENDED (bit 14) says a cell has an entry in its WorldTile's own sparse tag table
// (PnxWorldTile.ext, pnx_map_extended) -- an arbitrary u8 the GAME defines the meaning of
// (a door's state, a spawn id, whatever does not deserve its own bit). Authored per
// legend char / .pnxmap tile-table entry ("extended = N"), same as warp; unlike warp, a
// nonzero value cannot fold into the bit alone, so it rides in the table instead. Bit 15
// (0x8000) is still free.
#define PNX_MAP_INDEX_MASK 0x03FF
#define PNX_MAP_FLIP_X	   0x0400
#define PNX_MAP_FLIP_Y	   0x0800
#define PNX_MAP_ROTATE	   0x1000
#define PNX_MAP_WARP	   0x2000
#define PNX_MAP_EXTENDED   0x4000

// A cell that is not resident. Distinct from tile 0, which is a real tile.
#define PNX_MAP_NO_CELL 0xFFFF

// Folds `x`/`y` into a wrap layer's own [0, w) / [0, h) range in place; a no-op on a
// non-wrap layer. Shared by every accessor below so "wrap" has exactly one meaning: every
// one of them agrees on which cell a coordinate outside the layer's own extent names.
static inline void pnx_map_layer_wrap_xy(const PnxMapLayer* l, int32_t* x, int32_t* y)
{
	if (!l->wrap)
		return;
	*x = *x % l->w;
	if (*x < 0)
		*x += l->w;
	*y = *y % l->h;
	if (*y < 0)
		*y += l->h;
}

// The WorldTile holding this cell on a given layer, or NULL when it is not resident (or
// `x`/`y` is outside that layer's own extent, and the layer does not wrap). `worldtile` is
// a power of two so this is a shift, which is the whole reason the pipeline insists on one.
//
// A wrap layer repeats instead of clamping: `x`/`y` are floor-modulo'd into
// [0, w) / [0, h) first (`pnx_map_layer_wrap_xy`), so a screen-sized parallax layer tiles
// seamlessly past its own edge instead of running out into nothing.
static inline const PnxWorldTile* pnx_map_worldtile_layer(const PnxMap* m, uint8_t layer,
														  int32_t x, int32_t y)
{
	if (layer >= m->layer_count)
		return NULL;
	const PnxMapLayer* l = &m->layers[layer];
	pnx_map_layer_wrap_xy(l, &x, &y);
	if (x < 0 || y < 0 || x >= l->w || y >= l->h)
		return NULL;
	const uint32_t i   = (uint32_t)(y >> l->wt_shift) * l->wt_cols + (uint32_t)(x >> l->wt_shift);
	const uint8_t slot = l->wt_slot[i];
	return slot == PNX_MAP_NO_SLOT ? NULL : &l->slots[slot];
}

static inline const PnxWorldTile* pnx_map_worldtile(const PnxMap* m, int32_t x, int32_t y)
{
	return pnx_map_worldtile_layer(m, m->primary_layer, x, y);
}

// PNX_MAP_NO_CELL when the cell's WorldTile is not resident. Callers on the hot path have
// already been told which WorldTiles are live -- pnx_tilemap_draw walks them -- so this is
// for the ones that ask about a single arbitrary cell.
static inline uint16_t pnx_map_entry_layer(const PnxMap* m, uint8_t layer, int32_t x, int32_t y)
{
	if (layer >= m->layer_count)
		return PNX_MAP_NO_CELL;
	const PnxMapLayer* l = &m->layers[layer];
	pnx_map_layer_wrap_xy(l, &x, &y);
	const PnxWorldTile* wt = pnx_map_worldtile_layer(m, layer, x, y);
	if (!wt)
		return PNX_MAP_NO_CELL;
	const uint32_t i = (uint32_t)(y & (l->worldtile - 1)) * wt->cell_w +
		(uint32_t)(x & (l->worldtile - 1));
	const uint16_t index = (m->idx_width == 1)
		? wt->cells[i]
		: (uint16_t)(wt->cells[i * 2] | ((uint16_t)wt->cells[i * 2 + 1] << 8));
	const uint8_t* d	 = m->cell_dict + (size_t)index * 2;
	return (uint16_t)(d[0] | ((uint16_t)d[1] << 8));
}

static inline uint16_t pnx_map_entry(const PnxMap* m, int32_t x, int32_t y)
{
	return pnx_map_entry_layer(m, m->primary_layer, x, y);
}

static inline uint16_t pnx_map_tile_layer(const PnxMap* m, uint8_t layer, int32_t x, int32_t y)
{
	const uint16_t e = pnx_map_entry_layer(m, layer, x, y);
	return e == PNX_MAP_NO_CELL ? PNX_MAP_NO_CELL : (e & PNX_MAP_INDEX_MASK);
}

static inline uint16_t pnx_map_tile(const PnxMap* m, int32_t x, int32_t y)
{
	return pnx_map_tile_layer(m, m->primary_layer, x, y);
}

// PNX_FLIP_X / PNX_FLIP_Y, ready to hand to pnx_blit_4bpp.
static inline uint8_t pnx_map_flip_layer(const PnxMap* m, uint8_t layer, int32_t x, int32_t y)
{
	const uint16_t e = pnx_map_entry_layer(m, layer, x, y);
	if (e == PNX_MAP_NO_CELL)
		return 0;
	return (uint8_t)(((e & PNX_MAP_FLIP_X) ? 1u : 0u) | ((e & PNX_MAP_FLIP_Y) ? 2u : 0u));
}

static inline uint8_t pnx_map_flip(const PnxMap* m, int32_t x, int32_t y)
{
	return pnx_map_flip_layer(m, m->primary_layer, x, y);
}

// Which of the map's atlases a tile id belongs to, and its index within that atlas.
// Linear over at most PNX_MAP_MAX_ATLASES entries; a table of 1024 bytes mapping every id
// would be O(1) and cost more RAM than the walk saves at this scale.
static inline const PnxMapAtlas* pnx_map_tile_atlas(const PnxMap* m, uint16_t tile,
													uint16_t* out_index)
{
	for (uint8_t i = 0; i < m->atlas_count; i++)
	{
		const PnxMapAtlas* a = &m->atlas[i];
		if (tile >= a->first_tile && tile < a->first_tile + a->tile_count)
		{
			if (out_index)
				*out_index = (uint16_t)(tile - a->first_tile);
			return a;
		}
	}
	return NULL;
}

// The loaded atlas behind a tile id, or NULL when its slot has been evicted. A resident
// WorldTile always pins the atlases it needs, so a cell whose WorldTile is live cannot
// return NULL here.
static inline const PnxAtlas* pnx_map_atlas(const PnxMap* m, uint16_t tile, uint16_t* out_index)
{
	const PnxMapAtlas* a = pnx_map_tile_atlas(m, tile, out_index);
	return (a && a->slot != PNX_MAP_NO_SLOT) ? &m->pool[a->slot] : NULL;
}

// PNX_ROTATE, ready to be composed with whatever pnx_map_flip returns -- kept a separate
// accessor rather than folded into flip's return value so existing callers passing that
// straight to pnx_blit_4bpp are untouched. Nothing draws a rotated tile yet (see
// PNX_MAP_ROTATE's own comment); this just makes the bit reachable for when something does.
static inline bool pnx_map_rotate_layer(const PnxMap* m, uint8_t layer, int32_t x, int32_t y)
{
	const uint16_t e = pnx_map_entry_layer(m, layer, x, y);
	return e != PNX_MAP_NO_CELL && (e & PNX_MAP_ROTATE) != 0;
}

static inline bool pnx_map_rotate(const PnxMap* m, int32_t x, int32_t y)
{
	return pnx_map_rotate_layer(m, m->primary_layer, x, y);
}

// PNX_TILE_WARP if the cell carries the warp bit, else 0. Reads the cell's OWN bit
// (PNX_MAP_WARP) directly now, rather than a tile-owned table with per-cell overrides --
// collision/warp both moved out of that table (see PnxMap's own comment). A cell whose
// WorldTile is not resident answers 0 rather than guessing, same as pnx_map_flip.
static inline uint8_t pnx_map_flags_layer(const PnxMap* m, uint8_t layer, int32_t x, int32_t y)
{
	const uint16_t e = pnx_map_entry_layer(m, layer, x, y);
	if (e == PNX_MAP_NO_CELL)
		return 0;
	return (e & PNX_MAP_WARP) ? PNX_TILE_WARP : 0;
}

static inline uint8_t pnx_map_flags(const PnxMap* m, int32_t x, int32_t y)
{
	return pnx_map_flags_layer(m, m->primary_layer, x, y);
}

// Out-of-bounds counts as solid, so a map needs no border wall to contain the player
// and collision code needs no separate edge test.
//
// A cell whose WorldTile is not resident counts as solid too, and for the same reason: it
// stops the player at the edge of what is loaded rather than walking them into a void. The
// streamer's margin means this should never fire during ordinary play -- if it does, the
// view is moving faster than PNX_MAP_STREAM_BUDGET can keep up with.
//
// Collision is a property of the tile now (PnxAtlas.tile_flags, PNX_COLLISION_*), not of
// the map. SOLID is a plain per-cell block; SCALED and COMPLEX name shape data (an inset
// rect, a 1bpp mask -- pnx_atlas_tile_scaled_rect/pnx_atlas_tile_complex_mask) that this
// coarse whole-cell test does not evaluate, on purpose: it stays a whole-cell probe for
// grid-style movement, and reading the real shape is what pnx_physics_collide_aabb and a
// COMPLEX pixel test (pnx_physics_collide_point) are FOR. So both count as solid here,
// which is the opposite of how the pipeline's own build-time reachability check treats
// them (tools/pnx_assets.py: solid_cells_for) -- and deliberately so: a flood fill errs
// toward not rejecting a reachable map, a live player errs toward not clipping through a
// wall that is only partly open. A caller that wants the real shape uses those finer
// primitives against the tile's own rect/mask, not this.
static inline bool pnx_map_solid_layer(const PnxMap* m, uint8_t layer, int32_t x, int32_t y)
{
	if (layer >= m->layer_count)
		return true;
	const PnxMapLayer* l = &m->layers[layer];
	int32_t wx = x, wy = y;
	pnx_map_layer_wrap_xy(l, &wx, &wy);
	if (wx < 0 || wy < 0 || wx >= l->w || wy >= l->h)
		return true;

	const uint16_t tile = pnx_map_tile_layer(m, layer, x, y);
	if (tile == PNX_MAP_NO_CELL)
		return true;

	uint16_t local;
	const PnxAtlas* a = pnx_map_atlas(m, tile, &local);
	// A resident WorldTile always pins the atlases it needs (see pnx_map_atlas's own
	// comment), so this is defensive rather than a case that should ever be reached --
	// "unknown" resolves to solid for the same reason a non-resident WorldTile does above.
	if (!a)
		return true;
	return a->tile_flags[local] != PNX_COLLISION_NONE;
}

static inline bool pnx_map_solid(const PnxMap* m, int32_t x, int32_t y)
{
	return pnx_map_solid_layer(m, m->primary_layer, x, y);
}

// The cell's EXTENDED tag, if PNX_MAP_EXTENDED is set on it. Returns false (leaving
// `out_value` untouched) for a cell with no tag, a non-resident cell, or -- defensively,
// should the format and the table ever disagree -- a set bit whose WorldTile table has no
// matching entry. Linear over `wt->ext_count`, which is sparse by construction (most
// WorldTiles tag no cells at all): the same tradeoff pnx_map_warp_at already makes for a
// map's handful of warps.
static inline bool pnx_map_extended_layer(const PnxMap* m, uint8_t layer, int32_t x, int32_t y,
										  uint8_t* out_value)
{
	const uint16_t e = pnx_map_entry_layer(m, layer, x, y);
	if (e == PNX_MAP_NO_CELL || !(e & PNX_MAP_EXTENDED))
		return false;

	const PnxMapLayer* l = &m->layers[layer];
	pnx_map_layer_wrap_xy(l, &x, &y);
	const PnxWorldTile* wt = pnx_map_worldtile_layer(m, layer, x, y);
	const uint8_t lx	   = (uint8_t)(x & (l->worldtile - 1));
	const uint8_t ly	   = (uint8_t)(y & (l->worldtile - 1));
	for (uint16_t k = 0; k < wt->ext_count; k++)
	{
		const uint8_t* e2 = wt->ext + (uint32_t)k * 3;
		if (e2[0] == lx && e2[1] == ly)
		{
			if (out_value)
				*out_value = e2[2];
			return true;
		}
	}
	return false;
}

static inline bool pnx_map_extended(const PnxMap* m, int32_t x, int32_t y, uint8_t* out_value)
{
	return pnx_map_extended_layer(m, m->primary_layer, x, y, out_value);
}

// Returns NULL when there is no warp on that tile. Warps apply to the primary layer only.
const PnxWarp* pnx_map_warp_at(const PnxMap* m, int32_t x, int32_t y);

// Page text for entry `entry`, page `page`, or NULL if either is out of range.
const char* pnx_dialog_page(const PnxDialog* d, uint16_t entry, uint16_t page);
uint16_t pnx_dialog_page_count(const PnxDialog* d, uint16_t entry);

#endif // PNX_USE_ASSETS
