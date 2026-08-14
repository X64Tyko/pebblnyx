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

#include <stdint.h>
#include <stdbool.h>

// Mirrors FLAG_* in tools/pnx_assets.py. Keep the two in step.
#define PNX_TILE_SOLID 0x01
#define PNX_TILE_WARP  0x02

// Every blob carries this, so a stale .bin against a newer runtime is a clean error
// rather than garbage pixels. Bumped whenever a format changes.
#define PNX_BLOB_VERSION	  8
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

typedef struct
{
	uint8_t entries[PNX_PALETTE_ENTRIES];  // GColor8 values; [0] is transparent
} PnxPalette;

// Fills the table. Must be called before any atlas or sprite loads, since those carry
// palette indices rather than palette data.
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
typedef struct
{
	const uint8_t* pixels;		  // whole tiles, or the quadrant bank
	const uint8_t* tile_palette;  // tile_count, palette slot per tile
	const uint8_t* tile_flags;	  // tile_count, PNX_TILE_*
	const uint16_t* metatiles;	  // NULL when flat; else tile_count * 4 indices
	uint16_t tile_count;
	uint16_t subtile_count;
	uint8_t tile_px;
	uint8_t tile_bytes;	 // bytes per whole tile at 4bpp
	uint8_t sub_bytes;	 // bytes per quadrant; 0 when flat
} PnxAtlas;

static inline bool pnx_atlas_is_metatiled(const PnxAtlas* a)
{
	return a->metatiles != NULL;
}

typedef struct
{
	const uint8_t* pixels;		   // frame_count * frame_bytes, 4bpp
	const uint8_t* frame_palette;  // frame_count, palette slot per frame
	uint8_t w, h;
	uint8_t frame_count;
	uint16_t frame_bytes;  // w * h / 2
} PnxSprite;

typedef struct
{
	uint8_t x, y;	   // tile the warp triggers on
	uint8_t dest_map;  // index into the manifest's map order
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
	uint16_t asset;		  // the atlas's asset id
	uint16_t first_tile;  // where its slice of the map's tile id space begins
	uint16_t tile_count;
	uint8_t slot;  // atlas pool slot holding it, or PNX_MAP_NO_SLOT
} PnxMapAtlas;

// One resident WorldTile. `cells` points into the pool slot, not into the blob: the blob
// is never held whole.
typedef struct
{
	const uint8_t* cells;	   // cell_w * cell_h u16 entries
	const uint8_t* overrides;  // override_count * 3 bytes: x, y local to this WorldTile
	uint16_t override_count;
	uint8_t wx, wy;			 // which WorldTile of the grid this slot holds
	uint8_t cell_w, cell_h;	 // clipped at the map's edge, so no padding is stored
	bool live;
} PnxWorldTile;

// Flags come from the TILESET, not from a per-cell plane. A 32x24 map used to carry 768
// flag bytes restating what the tile already knew; at 30 maps that was 8.7% of the whole
// content budget. Cells that genuinely differ -- a door drawn on an ordinary scenery
// tile -- are listed as sparse overrides instead, inside the WorldTile they belong to.
//
// The flag table is the map's OWN, not the atlas's, and that is not duplication for its
// own sake: collision is asked about cells whose atlas may not be resident -- the player
// walking toward a wall the streamer has not reached -- so it cannot read flags out of an
// atlas that might be gone. A few hundred bytes buys collision that never depends on what
// the renderer happens to have loaded.
typedef struct
{
	const uint8_t* tile_flags;	// tile_total bytes, indexed by map-global tile id

	// Optional palette variant: tile_total bytes naming the palette slot to use instead of
	// the atlas's own, so one atlas serves several recoloured zones. NULL means use the
	// atlas's. 44 bytes for the cave tileset against ~5,600 for a second copy of it.
	const uint8_t* tile_palette;
	const PnxWarp* warps;
	const uint8_t* wt_mask;	 // wt_cols * wt_rows: which atlases each WorldTile needs

	// The atlas pool's slots are NOT a uniform stride. When there is a slot per atlas
	// nothing is ever evicted, so each slot is exactly its atlas's size -- which for one
	// large tileset beside one small one is the difference between fitting in RAM and not.
	// Only a map that really streams its atlases pays for slots that all hold the largest.
	const uint8_t* pool_offset;	 // (atlas_slots + 1) u32 offsets into pool_mem

	PnxMapAtlas atlas[PNX_MAP_MAX_ATLASES];
	PnxAtlas* pool;		  // atlas_slots views onto pool_mem
	uint8_t* pool_mem;	  // pool_bytes
	uint8_t* pool_owner;  // atlas_slots: which atlas index sits there, or NO_SLOT
	uint8_t* pool_pins;	  // atlas_slots: live WorldTiles depending on that slot

	PnxWorldTile* slots;  // slot_count of them
	uint8_t* slot_mem;	  // slot_count * slot_bytes
	uint8_t* wt_slot;	  // wt_cols * wt_rows: slot holding it, or NO_SLOT

	// WorldTile payloads are not in the map's resource. They live in BANK resources whose
	// asset ids run consecutively from `first_bank_asset`, because a ranged read costs by
	// how far in it starts -- see docs/MEASUREMENTS.md. A tile's home needs no lookup:
	// bank `index >> bank_shift`, offset `(index & mask) * slot_bytes`, since payloads are
	// padded to the slot stride. Which also makes a run of consecutive tiles one read.
	uint16_t first_bank_asset;
	uint8_t bank_shift;

	uint32_t resource;	  // the map's own resource: the resident preamble
	uint16_t tile_count;  // bound for tile_flags: the map's whole id space
	uint16_t slot_bytes;
	uint8_t w, h;
	uint8_t warp_count;
	uint8_t atlas_count;
	uint8_t atlas_slots;
	uint8_t slot_count;
	uint8_t wt_cols, wt_rows;
	uint8_t worldtile;	// cells per side
	uint8_t wt_shift;	// log2(worldtile): a cell finds its WorldTile by shifting
	uint8_t tile_px;

	// Every WorldTile and every atlas has a slot, so the map was loaded whole and can never
	// need another read. Every small map is in this case, which is most of them: the
	// streaming calls become one comparison and return.
	bool held_whole;
} PnxMap;

typedef struct
{
	const uint8_t* text;	  // NUL-terminated pages, back to back
	const uint16_t* offsets;  // one per page, into text
	const uint8_t* index;	  // entry_count * 4 bytes: u16 first_page, u16 page_count
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
#define PNX_FONT_NO_GLYPH	 0xFF  // codepoint map entry for a character the font lacks

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
	PNX_ADVANCE_X_POS,	// left to right: portrait, and every Latin face
	PNX_ADVANCE_Y_POS,	// top to bottom
	PNX_ADVANCE_Y_NEG,	// bottom to top
	PNX_ADVANCE_X_NEG,	// right to left; carried by the format, not yet emitted
	PNX_ADVANCE_COUNT
} PnxAdvanceAxis;

typedef struct
{
	const uint8_t* bitmaps;
	const uint8_t* glyphs;	// glyph_count * PNX_FONT_GLYPH_BYTES
	const uint8_t* map;		// one byte per codepoint in [first_cp, last_cp]
	uint16_t glyph_count;
	uint16_t bitmap_bytes;
	uint8_t depth;		  // 1 or 2
	uint8_t line_height;  // ascent + descent: what to advance between lines
	uint8_t baseline;	  // ascent: top of the line box to the baseline
	uint8_t space_advance;
	uint8_t first_cp, last_cp;
	uint8_t fallback;  // glyph drawn for a character the font does not carry
	uint8_t advance;   // PnxAdvanceAxis: which way the pen walks
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
	uint8_t advance;   // along the baseline, whichever way that runs
	int8_t bearing_x;  // pen to the START edge of the bitmap, along the baseline
	int8_t bearing_y;  // baseline to the TOP of the bitmap, positive upwards
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
	out->w = e[2];
	out->h = e[3];
	out->advance = e[4];
	out->bearing_x = (int8_t)e[5];
	out->bearing_y = (int8_t)e[6];
	out->bits = out->w ? f->bitmaps + (uint16_t)(e[0] | (e[1] << 8)) : NULL;
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
// Not const: a map streams, so its resident set changes as the camera moves. Handing back
// a const pointer would have meant a `_mut` twin for the streaming calls, which says the
// same thing less honestly.
PnxMap* pnx_scene_map(void);
const PnxDialog* pnx_scene_dialog(void);
const PnxFont* pnx_scene_font(uint8_t index);
uint8_t pnx_scene_atlas_count(void);
uint8_t pnx_scene_sprite_count(void);
uint8_t pnx_scene_font_count(void);

// Each returns false and leaves `out` untouched if the resource is missing, the blob is
// the wrong type or version, or its declared dimensions do not match its actual size --
// the last of which is what catches a truncated or half-written resource.
bool pnx_atlas_load(PnxAtlas* out, uint16_t asset_id);
bool pnx_sprite_load(PnxSprite* out, uint16_t asset_id);
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
// WorldTile of margin around it.
//
// `pnx_map_stream` spends at most PNX_MAP_STREAM_BUDGET reads and returns how many
// WorldTiles are still missing, so a caller can see it falling behind. Per frame.
//
// `pnx_map_stream_now` returns only once everything the rectangle needs is loaded. For a
// scene load or a warp, where there is no previous frame to show and a partial world would
// be visible as holes.
uint8_t pnx_map_stream(PnxMap* m, int32_t x, int32_t y, int32_t w, int32_t h);
uint8_t pnx_map_stream_now(PnxMap* m, int32_t x, int32_t y, int32_t w, int32_t h);

// WorldTiles resident right now, for diagnostics and for tests that assert on eviction.
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
void pnx_decode_4bpp(const uint8_t* src, const PnxPalette* palette, uint8_t* dst,
					 uint16_t pixels);

// Map cells are u16, not u8. Ten bits of tile index (1024, up from 255), two flip bits, and
// four reserved for a per-cell palette index into a future per-map palette table. Doubling
// the map costs ~1.2KB across the example maps, against 128 bytes for every tile a mirrored
// pair no longer needs its own copy of.
//
// The ten bits index the MAP's id space, not one atlas's, which is what lets a map draw
// from several tilesets without spending a bit per cell on saying which.
#define PNX_MAP_INDEX_MASK	  0x03FF
#define PNX_MAP_FLIP_X		  0x0400
#define PNX_MAP_FLIP_Y		  0x0800
#define PNX_MAP_PALETTE_SHIFT 12

// A cell that is not resident. Distinct from tile 0, which is a real tile.
#define PNX_MAP_NO_CELL 0xFFFF

// The WorldTile holding this cell, or NULL when it is not resident. `worldtile` is a power
// of two so this is a shift, which is the whole reason the pipeline insists on one.
static inline const PnxWorldTile* pnx_map_worldtile(const PnxMap* m, int32_t x, int32_t y)
{
	const uint32_t i = (uint32_t)(y >> m->wt_shift) * m->wt_cols + (uint32_t)(x >> m->wt_shift);
	const uint8_t slot = m->wt_slot[i];
	return slot == PNX_MAP_NO_SLOT ? NULL : &m->slots[slot];
}

// PNX_MAP_NO_CELL when the cell's WorldTile is not resident. Callers on the hot path have
// already been told which WorldTiles are live -- pnx_tilemap_draw walks them -- so this is
// for the ones that ask about a single arbitrary cell.
static inline uint16_t pnx_map_entry(const PnxMap* m, int32_t x, int32_t y)
{
	const PnxWorldTile* wt = pnx_map_worldtile(m, x, y);
	if (!wt)
		return PNX_MAP_NO_CELL;
	const uint32_t i =
		((uint32_t)(y & (m->worldtile - 1)) * wt->cell_w + (uint32_t)(x & (m->worldtile - 1))) *
		2u;
	return (uint16_t)(wt->cells[i] | ((uint16_t)wt->cells[i + 1] << 8));
}

static inline uint16_t pnx_map_tile(const PnxMap* m, int32_t x, int32_t y)
{
	const uint16_t e = pnx_map_entry(m, x, y);
	return e == PNX_MAP_NO_CELL ? PNX_MAP_NO_CELL : (e & PNX_MAP_INDEX_MASK);
}

// PNX_FLIP_X / PNX_FLIP_Y, ready to hand to pnx_blit_4bpp.
static inline uint8_t pnx_map_flip(const PnxMap* m, int32_t x, int32_t y)
{
	const uint16_t e = pnx_map_entry(m, x, y);
	if (e == PNX_MAP_NO_CELL)
		return 0;
	return (uint8_t)(((e & PNX_MAP_FLIP_X) ? 1u : 0u) | ((e & PNX_MAP_FLIP_Y) ? 2u : 0u));
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

static inline uint8_t pnx_map_flags(const PnxMap* m, int32_t x, int32_t y)
{
	const PnxWorldTile* wt = pnx_map_worldtile(m, x, y);
	if (!wt)
		return PNX_TILE_SOLID;	// see pnx_map_solid

	const uint8_t lx = (uint8_t)(x & (m->worldtile - 1));
	const uint8_t ly = (uint8_t)(y & (m->worldtile - 1));
	const uint32_t i = ((uint32_t)ly * wt->cell_w + lx) * 2u;
	const uint16_t tile =
		(uint16_t)(wt->cells[i] | ((uint16_t)wt->cells[i + 1] << 8)) & PNX_MAP_INDEX_MASK;
	const uint8_t flags = tile < m->tile_count ? m->tile_flags[tile] : 0;

	// Linear, because overrides are rare by construction: the pipeline picks each tile's
	// most common flags as the default, so only genuine exceptions land here -- and the
	// scan is over ONE WorldTile's exceptions, not the whole map's, which is what keeps it
	// bounded as maps grow.
	for (uint16_t k = 0; k < wt->override_count; k++)
	{
		const uint8_t* o = wt->overrides + (uint32_t)k * 3;
		if (o[0] == lx && o[1] == ly)
			return o[2];
	}
	return flags;
}

// Out-of-bounds counts as solid, so a map needs no border wall to contain the player
// and collision code needs no separate edge test.
//
// A cell whose WorldTile is not resident counts as solid too, and for the same reason: it
// stops the player at the edge of what is loaded rather than walking them into a void. The
// streamer's margin means this should never fire during ordinary play -- if it does, the
// view is moving faster than PNX_MAP_STREAM_BUDGET can keep up with.
static inline bool pnx_map_solid(const PnxMap* m, int32_t x, int32_t y)
{
	if (x < 0 || y < 0 || x >= m->w || y >= m->h)
		return true;
	return (pnx_map_flags(m, x, y) & PNX_TILE_SOLID) != 0;
}

// Returns NULL when there is no warp on that tile.
const PnxWarp* pnx_map_warp_at(const PnxMap* m, int32_t x, int32_t y);

// Page text for entry `entry`, page `page`, or NULL if either is out of range.
const char* pnx_dialog_page(const PnxDialog* d, uint16_t entry, uint16_t page);
uint16_t pnx_dialog_page_count(const PnxDialog* d, uint16_t entry);

#endif	// PNX_USE_ASSETS
