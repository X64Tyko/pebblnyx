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
// Assets live in an arena the caller supplies. There is no individual unload: a scene
// boundary resets the arena and reloads, which is the only load point that exists.

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
#define PNX_BLOB_VERSION 7
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

#define PNX_PALETTE_ENTRIES 16
#define PNX_PALETTE_TRANSPARENT 0

// The pipeline always emits the palette table as asset 0, before anything that indexes
// it. Named here so the scene loader does not carry a bare literal, and asserted by the
// generated header so the two cannot drift.
#define PNX_ASSET_PALETTES_SLOT 0

typedef struct {
  uint8_t entries[PNX_PALETTE_ENTRIES];   // GColor8 values; [0] is transparent
} PnxPalette;

// Fills the table. Must be called before any atlas or sprite loads, since those carry
// palette indices rather than palette data.
bool pnx_palettes_load(uint16_t asset_id);

// NULL if the slot is beyond what was loaded.
const PnxPalette *pnx_palette(uint8_t slot);
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
typedef struct {
  const uint8_t *pixels;        // whole tiles, or the quadrant bank
  const uint8_t *tile_palette;  // tile_count, palette slot per tile
  const uint8_t *tile_flags;    // tile_count, PNX_TILE_*
  const uint16_t *metatiles;    // NULL when flat; else tile_count * 4 indices
  uint16_t tile_count;
  uint16_t subtile_count;
  uint8_t tile_px;
  uint8_t tile_bytes;           // bytes per whole tile at 4bpp
  uint8_t sub_bytes;            // bytes per quadrant; 0 when flat
} PnxAtlas;

static inline bool pnx_atlas_is_metatiled(const PnxAtlas *a) {
  return a->metatiles != NULL;
}

typedef struct {
  const uint8_t *pixels;         // frame_count * frame_bytes, 4bpp
  const uint8_t *frame_palette;  // frame_count, palette slot per frame
  uint8_t w, h;
  uint8_t frame_count;
  uint16_t frame_bytes;          // w * h / 2
} PnxSprite;

typedef struct {
  uint8_t x, y;             // tile the warp triggers on
  uint8_t dest_map;         // index into the manifest's map order
  uint8_t dest_x, dest_y;
} PnxWarp;

// Flags come from the TILESET, not from a per-cell plane. A 32x24 map used to carry 768
// flag bytes restating what the tile already knew; at 30 maps that was 8.7% of the whole
// content budget. Cells that genuinely differ -- a door drawn on an ordinary scenery
// tile -- are listed as sparse overrides instead.
typedef struct {
  const uint8_t *tiles;       // w * h atlas indices
  const uint8_t *tile_flags;  // borrowed from the atlas; NOT owned

  // Optional palette variant: tile_count bytes naming the palette slot to use instead of the
  // atlas's own, so one atlas serves several recoloured zones. NULL means use the atlas's.
  // 44 bytes for the cave tileset against ~5,600 for a second copy of it.
  const uint8_t *tile_palette;
  const uint8_t *overrides;   // override_count * 3 bytes: x, y, flags
  const PnxWarp *warps;
  uint16_t override_count;
  uint16_t tile_count;        // bound for tile_flags
  uint8_t w, h;
  uint8_t warp_count;
} PnxMap;

typedef struct {
  const uint8_t *text;      // NUL-terminated pages, back to back
  const uint16_t *offsets;  // one per page, into text
  const uint8_t *index;     // entry_count * 4 bytes: u16 first_page, u16 page_count
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
#define PNX_FONT_NO_GLYPH 0xFF   // codepoint map entry for a character the font lacks

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
typedef enum {
  PNX_ADVANCE_X_POS = 0,   // left to right: portrait, and every Latin face
  PNX_ADVANCE_Y_POS = 1,   // top to bottom
  PNX_ADVANCE_Y_NEG = 2,   // bottom to top
  PNX_ADVANCE_X_NEG = 3,   // right to left; carried by the format, not yet emitted
  PNX_ADVANCE_COUNT
} PnxAdvanceAxis;

typedef struct {
  const uint8_t *bitmaps;
  const uint8_t *glyphs;     // glyph_count * PNX_FONT_GLYPH_BYTES
  const uint8_t *map;        // one byte per codepoint in [first_cp, last_cp]
  uint16_t glyph_count;
  uint16_t bitmap_bytes;
  uint8_t depth;             // 1 or 2
  uint8_t line_height;       // ascent + descent: what to advance between lines
  uint8_t baseline;          // ascent: top of the line box to the baseline
  uint8_t space_advance;
  uint8_t first_cp, last_cp;
  uint8_t fallback;          // glyph drawn for a character the font does not carry
  uint8_t advance;           // PnxAdvanceAxis: which way the pen walks
} PnxFont;

// One glyph's metrics, unpacked from the index. `bits` is NULL when the glyph has no ink
// -- a space -- which the drawing loop treats as advance-only rather than as an error.
// `w` and `h` are the bitmap AS STORED, which a landscape build has already rotated, so
// the blitter needs no special case. The three metrics beside them stay typographic --
// along the baseline, and up from it -- because that is the frame line layout works in.
// pnx_text is the one place the two frames meet.
typedef struct {
  const uint8_t *bits;
  uint8_t w, h;
  uint8_t advance;           // along the baseline, whichever way that runs
  int8_t bearing_x;          // pen to the START edge of the bitmap, along the baseline
  int8_t bearing_y;          // baseline to the TOP of the bitmap, positive upwards
} PnxGlyph;

bool pnx_font_load(PnxFont *out, uint16_t asset_id);

// Never fails: an unmapped character resolves to the font's fallback glyph. A visible
// substitute beats a silent gap, which reads as a layout bug rather than a missing
// character.
static inline uint8_t pnx_font_glyph_index(const PnxFont *f, char c) {
  const uint8_t cp = (uint8_t)c;
  if (cp < f->first_cp || cp > f->last_cp) return f->fallback;
  const uint8_t g = f->map[cp - f->first_cp];
  return g == PNX_FONT_NO_GLYPH ? f->fallback : g;
}

// No bounds check on `index`: the loader has already verified every map entry and every
// bitmap offset, and this runs per character per frame.
static inline void pnx_font_glyph(const PnxFont *f, uint8_t index, PnxGlyph *out) {
  const uint8_t *e = f->glyphs + (uint32_t)index * PNX_FONT_GLYPH_BYTES;
  out->w = e[2];
  out->h = e[3];
  out->advance = e[4];
  out->bearing_x = (int8_t)e[5];
  out->bearing_y = (int8_t)e[6];
  out->bits = out->w ? f->bitmaps + (uint16_t)(e[0] | (e[1] << 8)) : NULL;
}

// Bytes per bitmap row. Rows are byte-aligned rather than a continuous bit stream, so a
// row is indexed by multiply-and-add instead of tracked as a bit offset.
static inline uint8_t pnx_font_row_bytes(const PnxFont *f, uint8_t w) {
  return (uint8_t)(((uint16_t)w * f->depth + 7u) / 8u);
}

// Two arenas, because they have different lifetimes. `persistent` holds the scene table
// and outlives everything; `scene` holds the assets a scene needs and is reset wholesale
// at every scene boundary. Keeping them separate is what lets a scene load free its
// predecessor without also freeing the table telling it what to load.
//
// `resources` maps each PnxAssetId to its platform resource id -- pass the generated
// PNX_ASSET_RESOURCE_TABLE.
bool pnx_assets_init(PnxArena *persistent, PnxArena *scene,
                     const uint32_t *resources, uint16_t count);

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
const PnxAtlas *pnx_scene_atlas(uint8_t index);
const PnxSprite *pnx_scene_sprite(uint8_t index);
const PnxMap *pnx_scene_map(void);
const PnxDialog *pnx_scene_dialog(void);
const PnxFont *pnx_scene_font(uint8_t index);
uint8_t pnx_scene_atlas_count(void);
uint8_t pnx_scene_sprite_count(void);
uint8_t pnx_scene_font_count(void);

// Each returns false and leaves `out` untouched if the resource is missing, the blob is
// the wrong type or version, or its declared dimensions do not match its actual size --
// the last of which is what catches a truncated or half-written resource.
bool pnx_atlas_load(PnxAtlas *out, uint16_t asset_id);
bool pnx_sprite_load(PnxSprite *out, uint16_t asset_id);
bool pnx_dialog_load(PnxDialog *out, uint16_t asset_id);

// Takes the atlas because a map's collision flags live on its tileset. The atlas must
// outlive the map, which it does when both sit in the same scene arena.
//
// The map records which atlas it was authored against, and this refuses a mismatch
// rather than drawing one tileset's map in another's tiles -- a failure that looks like
// corrupted art rather than a pairing mistake.
bool pnx_map_load(PnxMap *out, uint16_t asset_id, const PnxAtlas *atlas);

// The asset id of the atlas a map needs, readable without loading the map. The scene
// loader uses it to pick the right one among several.
bool pnx_map_atlas_asset(uint16_t asset_id, uint8_t *out_atlas_asset);

// Reads a whole blob into the scene arena and validates magic and version, handing back
// the four format-specific header bytes. Shared so modules outside assets/ -- audio, for
// instance -- do not each reimplement the header check.
const uint8_t *pnx_blob_load(uint16_t asset_id, const char *magic,
                             uint8_t *a, uint8_t *b, uint8_t *c, uint8_t *d,
                             size_t *payload);

// Bytes read from flash since init, for budgeting scene loads.
uint32_t pnx_assets_bytes_loaded(void);

// ------------------------------------------------------------------- inline access
//
// Hot paths. No bounds checking on the tile accessors: they run per pixel per frame,
// and the pipeline already guarantees indices are in range.

// Whole-tile pixels. NULL on a metatiled atlas, where a tile has no contiguous
// representation -- use pnx_blit_metatile instead.
static inline const uint8_t *pnx_atlas_tile(const PnxAtlas *a, uint8_t index) {
  return a->metatiles ? NULL : a->pixels + (uint32_t)index * a->tile_bytes;
}

static inline const PnxPalette *pnx_atlas_tile_palette(const PnxAtlas *a, uint8_t index) {
  return pnx_palette(a->tile_palette[index]);
}

static inline const uint8_t *pnx_sprite_frame(const PnxSprite *s, uint8_t frame) {
  return s->pixels + (uint32_t)frame * s->frame_bytes;
}

static inline const PnxPalette *pnx_sprite_frame_palette(const PnxSprite *s,
                                                         uint8_t frame) {
  return pnx_palette(s->frame_palette[frame]);
}

// Expands 4bpp source into 8bpp GColor8. Transparent pixels are left untouched in dst,
// so a caller can pre-fill a background. The real blitter (M3) does this inline with
// clipping; this exists for code that just wants pixels.
void pnx_decode_4bpp(const uint8_t *src, const PnxPalette *palette,
                     uint8_t *dst, uint16_t pixels);

// Map cells are u16, not u8. Ten bits of tile index (1024, up from 255), two flip bits, and
// four reserved for a per-cell palette index into a future per-map palette table. Doubling
// the map costs ~1.2KB across the example maps, against 128 bytes for every tile a mirrored
// pair no longer needs its own copy of.
#define PNX_MAP_INDEX_MASK 0x03FF
#define PNX_MAP_FLIP_X     0x0400
#define PNX_MAP_FLIP_Y     0x0800
#define PNX_MAP_PALETTE_SHIFT 12

static inline uint16_t pnx_map_entry(const PnxMap *m, int32_t x, int32_t y) {
  const uint32_t i = ((uint32_t)y * m->w + (uint32_t)x) * 2u;
  return (uint16_t)(m->tiles[i] | ((uint16_t)m->tiles[i + 1] << 8));
}

static inline uint16_t pnx_map_tile(const PnxMap *m, int32_t x, int32_t y) {
  return pnx_map_entry(m, x, y) & PNX_MAP_INDEX_MASK;
}

// PNX_FLIP_X / PNX_FLIP_Y, ready to hand to pnx_blit_4bpp.
static inline uint8_t pnx_map_flip(const PnxMap *m, int32_t x, int32_t y) {
  const uint16_t e = pnx_map_entry(m, x, y);
  return (uint8_t)(((e & PNX_MAP_FLIP_X) ? 1u : 0u) | ((e & PNX_MAP_FLIP_Y) ? 2u : 0u));
}

static inline uint8_t pnx_map_flags(const PnxMap *m, int32_t x, int32_t y) {
  const uint16_t tile = pnx_map_tile(m, x, y);
  uint8_t flags = tile < m->tile_count ? m->tile_flags[tile] : 0;

  // Linear, because overrides are rare by construction: the pipeline picks each tile's
  // most common flags as the default, so only genuine exceptions land here. A map with
  // enough overrides for this to matter has a badly chosen tileset.
  for (uint16_t i = 0; i < m->override_count; i++) {
    const uint8_t *o = m->overrides + (uint32_t)i * 3;
    if (o[0] == x && o[1] == y) return o[2];
  }
  return flags;
}

// Out-of-bounds counts as solid, so a map needs no border wall to contain the player
// and collision code needs no separate edge test.
static inline bool pnx_map_solid(const PnxMap *m, int32_t x, int32_t y) {
  if (x < 0 || y < 0 || x >= m->w || y >= m->h) return true;
  return (pnx_map_flags(m, x, y) & PNX_TILE_SOLID) != 0;
}

// Returns NULL when there is no warp on that tile.
const PnxWarp *pnx_map_warp_at(const PnxMap *m, int32_t x, int32_t y);

// Page text for entry `entry`, page `page`, or NULL if either is out of range.
const char *pnx_dialog_page(const PnxDialog *d, uint16_t entry, uint16_t page);
uint16_t pnx_dialog_page_count(const PnxDialog *d, uint16_t entry);

#endif  // PNX_USE_ASSETS
