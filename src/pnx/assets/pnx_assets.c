#include "pnx_assets.h"

#if PNX_USE_ASSETS

#include "../platform/pnx_platform.h"
#include "../core/pnx_diag.h"

#include <string.h>

#ifndef PNX_SCENE_MAX_ATLASES
#define PNX_SCENE_MAX_ATLASES 4
#endif
#ifndef PNX_SCENE_MAX_SPRITES
#define PNX_SCENE_MAX_SPRITES 8
#endif
// Two covers the shape E7 was written around -- a small HUD face and a larger dialogue
// one. Raising it costs sizeof(PnxFont) of bss per slot and nothing else.
#ifndef PNX_SCENE_MAX_FONTS
#define PNX_SCENE_MAX_FONTS 2
#endif

static PnxArena *s_persistent;
static const uint8_t *s_scene_table;
static uint8_t s_scene_count;

// 0 is a real orientation, so "nobody has said yet" needs a value of its own.
#define PNX_ORIENT_UNSET 0xFF
static uint8_t s_orientation = PNX_ORIENT_UNSET;

static PnxAtlas s_atlases[PNX_SCENE_MAX_ATLASES];
static PnxSprite s_sprites[PNX_SCENE_MAX_SPRITES];
static PnxFont s_fonts[PNX_SCENE_MAX_FONTS];
static PnxMap s_map;
static PnxDialog s_dialog;
static uint8_t s_atlas_count, s_sprite_count, s_font_count;
static bool s_have_map, s_have_dialog;

static PnxPalette *s_palettes;
static uint16_t s_palette_count;
// s_arena is where the NEXT load allocates; s_scene and s_persistent are the two it can
// point at. Kept as three so pnx_assets_persistent can return to the scene arena without
// the caller having to hand it back -- and so the temporary swap pnx_scenes_load already
// does has something to restore to by name rather than by saved value.
static PnxArena *s_arena;
static PnxArena *s_scene;
static const uint32_t *s_resources;
static uint16_t s_resource_count;
static uint32_t s_bytes_loaded;

bool pnx_assets_init(PnxArena *persistent, PnxArena *scene,
                     const uint32_t *resources, uint16_t count) {
  if (!persistent || !scene || !resources || count == 0) return false;
  s_persistent = persistent;
  s_scene = scene;
  s_arena = scene;
  s_resources = resources;
  s_resource_count = count;
  s_bytes_loaded = 0;
  s_palettes = NULL;
  s_palette_count = 0;
  // Cleared here rather than left standing, so a second init starts from no expectation
  // instead of inheriting one. Which is why the expectation is set AFTER init.
  s_orientation = PNX_ORIENT_UNSET;
  return true;
}

bool pnx_assets_expect_orientation(uint8_t orientation) {
  if (orientation >= PNX_ORIENT_COUNT) return false;
  s_orientation = orientation;
  return true;
}

uint8_t pnx_assets_orientation(void) { return s_orientation; }

bool pnx_assets_persistent(bool on) {
  const bool was = (s_arena == s_persistent);
  s_arena = on ? s_persistent : s_scene;
  return was;
}

// ---------------------------------------------------------------------- palettes

static const uint8_t *load_blob(uint16_t asset_id, const char *magic,
                                uint8_t *out_a, uint8_t *out_b, uint8_t *out_c,
                                size_t *out_payload);

bool pnx_palettes_load(uint16_t asset_id) {
  uint8_t count = 0;
  size_t payload = 0;
  const uint8_t *data = load_blob(asset_id, "PP", &count, NULL, NULL, &payload);
  if (!data) return false;

  if (count == 0 || payload != (size_t)count * PNX_PALETTE_ENTRIES) {
    pnx_log("palettes %u: %u palettes needs %u bytes, blob has %u", asset_id, count,
            (unsigned)(count * PNX_PALETTE_ENTRIES), (unsigned)payload);
    return false;
  }
  if (count > PNX_PALETTE_SLOTS) {
    // Loud, because the alternative is drawing in whatever colours happen to be there,
    // which presents as an art bug rather than a configuration one.
    pnx_log("palettes %u: project needs %u slots, PNX_PALETTE_SLOTS is %u -- raise it",
            asset_id, count, PNX_PALETTE_SLOTS);
    return false;
  }

  s_palettes = (PnxPalette *)(const void *)data;
  s_palette_count = count;
  return true;
}

const PnxPalette *pnx_palette(uint8_t slot) {
  return slot < s_palette_count ? &s_palettes[slot] : NULL;
}

uint16_t pnx_palette_count(void) { return s_palette_count; }

void pnx_decode_4bpp(const uint8_t *src, const PnxPalette *palette,
                     uint8_t *dst, uint16_t pixels) {
  if (!palette) return;
  for (uint16_t i = 0; i < pixels; i += 2) {
    const uint8_t packed = src[i >> 1];
    const uint8_t hi = packed >> 4, lo = packed & 0x0F;
    if (hi != PNX_PALETTE_TRANSPARENT) dst[i] = palette->entries[hi];
    if (lo != PNX_PALETTE_TRANSPARENT) dst[i + 1] = palette->entries[lo];
  }
}

uint32_t pnx_assets_bytes_loaded(void) { return s_bytes_loaded; }

// Reads a whole blob into the arena and checks its header.
//
// The size check is the one that earns its keep: it catches a resource that was
// truncated, half-written, or built by a different version of the pipeline. Without it
// those present as garbage pixels or a wild pointer, with nothing pointing at the cause.
// `dst` is where the blob lands: NULL to bump the scene arena, or a caller buffer of `cap`
// bytes. The second exists for the map's atlas pool, whose slots are reused as atlases are
// evicted and reloaded -- something a bump arena cannot express, and the only reason this
// takes a destination at all. Every check below is the same either way, which is the point:
// there is still exactly one door.
static const uint8_t *load_blob_into(uint16_t asset_id, const char *magic,
                                     uint8_t *dst, size_t cap,
                                     uint8_t *out_a, uint8_t *out_b, uint8_t *out_c,
                                     uint8_t *out_d, size_t *out_payload) {
  if ((!s_arena && !dst) || asset_id >= s_resource_count) {
    pnx_log("asset %u: out of range (have %u)", asset_id, s_resource_count);
    return NULL;
  }

  const uint32_t resource = s_resources[asset_id];
  size_t size = 0;
  if (!pnx_platform_resource_size(resource, &size) || size < PNX_BLOB_HEADER_BYTES) {
    pnx_log("asset %u: missing or too small (%u bytes)", asset_id, (unsigned)size);
    return NULL;
  }

  uint8_t *buf = dst;
  if (!buf) {
    buf = (uint8_t *)pnx_arena_alloc(s_arena, size, 4);
    if (!buf) {
      pnx_log("asset %u: arena full, needed %u, %u free", asset_id, (unsigned)size,
              (unsigned)pnx_arena_remaining(s_arena));
      return NULL;
    }
  } else if (size > cap) {
    pnx_log("asset %u: %u bytes will not fit a %u-byte slot", asset_id, (unsigned)size,
            (unsigned)cap);
    return NULL;
  }

  // One call, whole blob. Splitting this up would cost 29 us per extra call for nothing.
  const size_t got = pnx_platform_resource_read(resource, 0, buf, size);
  if (got != size) {
    pnx_log("asset %u: short read, %u of %u", asset_id, (unsigned)got, (unsigned)size);
    return NULL;
  }
  s_bytes_loaded += (uint32_t)size;

  if (buf[0] != (uint8_t)magic[0] || buf[1] != (uint8_t)magic[1]) {
    pnx_log("asset %u: wrong type, got %c%c want %s", asset_id, buf[0], buf[1], magic);
    return NULL;
  }
  if (buf[2] != PNX_BLOB_VERSION) {
    pnx_log("asset %u: format v%u, runtime expects v%u -- rebuild assets",
            asset_id, buf[2], PNX_BLOB_VERSION);
    return NULL;
  }

  // Orientation, checked here because this is the one door every blob comes through.
  //
  // Content is rotated at build time, so a portrait atlas in a landscape bundle is not a
  // mismatch the renderer could ever notice -- it is simply a picture lying on its side,
  // and a map whose walls are in the wrong places. That is a debugging session; this is
  // one comparison.
  //
  // Without an explicit expectation the first blob loaded sets it, which still catches
  // the case that actually happens: a rebuild in the other orientation that left one
  // stale resource behind. Call pnx_assets_expect_orientation(PNX_ORIENTATION) at
  // start-up -- the generated header defines it -- and even a uniformly stale bundle is
  // refused.
  if (s_orientation == PNX_ORIENT_UNSET) {
    s_orientation = buf[7];
  } else if (buf[7] != s_orientation) {
    pnx_log("asset %u: built for orientation %u, project is %u -- stale resource, "
            "rebuild assets", asset_id, buf[7], s_orientation);
    return NULL;
  }

  if (out_a) *out_a = buf[3];
  if (out_b) *out_b = buf[4];
  if (out_c) *out_c = buf[5];
  if (out_d) *out_d = buf[6];
  if (out_payload) *out_payload = size - PNX_BLOB_HEADER_BYTES;

  return buf + PNX_BLOB_HEADER_BYTES;
}

static const uint8_t *load_blob_4(uint16_t asset_id, const char *magic,
                                  uint8_t *a, uint8_t *b, uint8_t *c, uint8_t *d,
                                  size_t *payload) {
  return load_blob_into(asset_id, magic, NULL, 0, a, b, c, d, payload);
}

static const uint8_t *load_blob(uint16_t asset_id, const char *magic,
                                uint8_t *a, uint8_t *b, uint8_t *c,
                                size_t *payload) {
  return load_blob_4(asset_id, magic, a, b, c, NULL, payload);
}

const uint8_t *pnx_blob_load(uint16_t asset_id, const char *magic,
                             uint8_t *a, uint8_t *b, uint8_t *c, uint8_t *d,
                             size_t *payload) {
  return load_blob_4(asset_id, magic, a, b, c, d, payload);
}

// Both tables are padded to 4 so the pixel block starts aligned.
static size_t pad4(size_t n) { return (n + 3u) & ~(size_t)3u; }

static bool atlas_load_into(PnxAtlas *out, uint16_t asset_id, uint8_t *dst, size_t cap) {
  if (!s_palettes) {
    pnx_log("atlas %u: load palettes first -- atlases carry indices, not colours",
            asset_id);
    return false;
  }

  uint8_t tile_px = 0, count_lo = 0, layout = 0;
  size_t payload = 0;
  const uint8_t *data = load_blob_into(asset_id, "PA", dst, cap,
                                       &tile_px, &count_lo, &layout, NULL, &payload);
  if (!data) return false;

  const uint16_t tile_count = count_lo;
  const size_t tile_bytes = (size_t)tile_px * tile_px / 2;
  const size_t tables = pad4(tile_count) * 2;

  out->metatiles = NULL;
  out->subtile_count = 0;
  out->sub_bytes = 0;

  if (layout == 0) {
    const size_t expected = tables + tile_count * tile_bytes;
    if (tile_px == 0 || tile_count == 0 || payload != expected) {
      pnx_log("atlas %u: %u tiles of %upx needs %u bytes, blob has %u",
              asset_id, tile_count, tile_px, (unsigned)expected, (unsigned)payload);
      return false;
    }
    out->tile_palette = data;
    out->tile_flags = data + pad4(tile_count);
    out->pixels = data + tables;
  } else {
    // u16 subtile_count, u16 pad, palettes, flags, metatile table, quadrant bank.
    if (payload < 4) return false;
    const uint16_t subs = (uint16_t)(data[0] | (data[1] << 8));
    const size_t sub_bytes = tile_bytes / 4;
    const size_t table_bytes = (size_t)tile_count * 4 * 2;
    const size_t expected = 4 + tables + table_bytes + (size_t)subs * sub_bytes;

    if (tile_px == 0 || tile_count == 0 || subs == 0 || payload != expected) {
      pnx_log("atlas %u: %u tiles, %u subtiles needs %u bytes, blob has %u",
              asset_id, tile_count, subs, (unsigned)expected, (unsigned)payload);
      return false;
    }

    out->tile_palette = data + 4;
    out->tile_flags = data + 4 + pad4(tile_count);
    out->metatiles = (const uint16_t *)(const void *)(data + 4 + tables);
    out->pixels = data + 4 + tables + table_bytes;
    out->subtile_count = subs;
    out->sub_bytes = (uint8_t)sub_bytes;

    for (uint32_t i = 0; i < (uint32_t)tile_count * 4; i++) {
      if (out->metatiles[i] >= subs) {
        pnx_log("atlas %u: metatile quadrant %u indexes subtile %u of %u",
                asset_id, (unsigned)i, out->metatiles[i], subs);
        return false;
      }
    }
  }

  out->tile_px = tile_px;
  out->tile_bytes = (uint8_t)tile_bytes;
  out->tile_count = tile_count;

  for (uint16_t i = 0; i < tile_count; i++) {
    if (out->tile_palette[i] >= s_palette_count) {
      pnx_log("atlas %u: tile %u wants palette %u, only %u loaded",
              asset_id, i, out->tile_palette[i], s_palette_count);
      return false;
    }
  }
  return true;
}

bool pnx_atlas_load(PnxAtlas *out, uint16_t asset_id) {
  return atlas_load_into(out, asset_id, NULL, 0);
}

bool pnx_sprite_load(PnxSprite *out, uint16_t asset_id) {
  if (!s_palettes) {
    pnx_log("sprite %u: load palettes first", asset_id);
    return false;
  }

  uint8_t w = 0, h = 0, frames = 0;
  size_t payload = 0;
  const uint8_t *data = load_blob(asset_id, "PS", &w, &h, &frames, &payload);
  if (!data) return false;

  const size_t frame_bytes = (size_t)w * h / 2;
  const size_t expected = pad4(frames) + frames * frame_bytes;
  if (w == 0 || h == 0 || frames == 0 || payload != expected) {
    pnx_log("sprite %u: %u frames of %ux%u needs %u bytes, blob has %u",
            asset_id, frames, w, h, (unsigned)expected, (unsigned)payload);
    return false;
  }

  out->frame_palette = data;
  out->pixels = data + pad4(frames);
  out->w = w;
  out->h = h;
  out->frame_count = frames;
  out->frame_bytes = (uint16_t)frame_bytes;
  return true;
}

// ---------------------------------------------------------------------------- maps
//
// The map is the one asset that is not read whole. Its preamble -- the atlas table, the
// flag table, the warps and the WorldTile index -- goes into the scene arena and stays;
// its cells arrive a WorldTile at a time into a pool, and its atlases into a second pool
// beside them.
//
// This is a deliberate exception to the residency rule at the top of pnx_assets.h, and it
// is worth being exact about why. That rule rejects streaming a TILE at a time: ~29 us per
// read call, against 725 tiles on screen, is 6.7 ms of frame spent reading art that is
// already in RAM. A WorldTile is a different trade -- one call brings 256 cells, so the
// per-call cost is amortised 256-fold, and it is paid when the player crosses a boundary
// rather than every frame. The rule is unchanged; the unit is.

static uint32_t read_u32(const uint8_t *p) {
  return (uint32_t)p[0] | ((uint32_t)p[1] << 8) | ((uint32_t)p[2] << 16)
         | ((uint32_t)p[3] << 24);
}

static uint16_t read_u16(const uint8_t *p) {
  return (uint16_t)(p[0] | ((uint16_t)p[1] << 8));
}

// Defined below with the rest of the streamer; pnx_map_load calls it to fill a map small
// enough to be held whole before it returns.
static bool worldtile_load_run(PnxMap *m, uint32_t first, uint8_t slot, uint8_t count);

// log2 for the power-of-two WorldTile size, so a cell finds its WorldTile by shifting.
// The pipeline refuses anything that is not a power of two, so this cannot fail.
static uint8_t shift_of(uint8_t n) {
  uint8_t s = 0;
  while ((1u << s) < n) s++;
  return s;
}

bool pnx_map_load(PnxMap *out, uint16_t asset_id) {
  if (!s_arena || asset_id >= s_resource_count) {
    pnx_log("map %u: out of range (have %u)", asset_id, s_resource_count);
    return false;
  }
  const uint32_t resource = s_resources[asset_id];

  // The header plus the 16-byte preamble is everything needed to size the resident block,
  // so the map costs two reads rather than one -- and never the whole blob, which for a
  // large map is the thing that does not fit.
  uint8_t head[PNX_BLOB_HEADER_BYTES + 16];
  if (pnx_platform_resource_read(resource, 0, head, sizeof(head)) != sizeof(head)) {
    pnx_log("map %u: too small to hold a header", asset_id);
    return false;
  }
  if (head[0] != 'P' || head[1] != 'M') {
    pnx_log("map %u: wrong type, got %c%c want PM", asset_id, head[0], head[1]);
    return false;
  }
  if (head[2] != PNX_BLOB_VERSION) {
    pnx_log("map %u: format v%u, runtime expects v%u -- rebuild assets",
            asset_id, head[2], PNX_BLOB_VERSION);
    return false;
  }
  if (s_orientation != PNX_ORIENT_UNSET && head[7] != s_orientation) {
    pnx_log("map %u: built for orientation %u, project is %u -- stale resource",
            asset_id, head[7], s_orientation);
    return false;
  }

  const uint8_t w = head[3], h = head[4], warps = head[5], worldtile = head[6];
  const uint8_t *pre = head + PNX_BLOB_HEADER_BYTES;
  const uint8_t atlas_count = pre[0], cols = pre[1], rows = pre[2], flags = pre[3];
  const uint16_t tile_total = read_u16(pre + 4);
  const uint8_t tile_px = pre[6];
  const uint16_t slot_bytes = read_u16(pre + 8);
  const uint8_t want_slots = pre[10], atlas_slots = pre[11];
  const uint32_t pool_bytes = read_u32(pre + 12);

  if (w == 0 || h == 0 || cols == 0 || rows == 0 || worldtile == 0 || tile_px == 0
      || (worldtile & (worldtile - 1)) || atlas_count == 0
      || atlas_count > PNX_MAP_MAX_ATLASES || atlas_slots == 0
      || atlas_slots > atlas_count || want_slots == 0) {
    pnx_log("map %u: %ux%u in %ux%u WorldTiles of %u, %u atlases in %u slots -- refused",
            asset_id, w, h, cols, rows, worldtile, atlas_count, atlas_slots);
    return false;
  }

  const uint16_t n = (uint16_t)cols * rows;
  const size_t resident = 16 + (size_t)atlas_count * 4
                          + ((size_t)atlas_slots + 1) * 4
                          + 4                        // first_bank_asset, pad
                          + pad4(tile_total)
                          + ((flags & 1) ? pad4(tile_total) : 0)
                          + pad4((size_t)warps * sizeof(PnxWarp))
                          + pad4(n);

  uint8_t *pre_mem = (uint8_t *)pnx_arena_alloc(s_arena, resident, 4);
  if (!pre_mem) {
    pnx_log("map %u: arena full, needed %u for the preamble, %u free",
            asset_id, (unsigned)resident, (unsigned)pnx_arena_remaining(s_arena));
    return false;
  }
  if (pnx_platform_resource_read(resource, PNX_BLOB_HEADER_BYTES, pre_mem, resident)
      != resident) {
    pnx_log("map %u: short read of the %u-byte preamble", asset_id, (unsigned)resident);
    return false;
  }
  s_bytes_loaded += (uint32_t)(sizeof(head) + resident);

  const uint8_t *at = pre_mem + 16;
  uint16_t base = 0;
  for (uint8_t i = 0; i < atlas_count; i++) {
    out->atlas[i].asset = read_u16(at);
    out->atlas[i].first_tile = read_u16(at + 2);
    out->atlas[i].slot = PNX_MAP_NO_SLOT;
    if (out->atlas[i].first_tile != base) {
      pnx_log("map %u: atlas %u starts at tile %u, expected %u -- the id space must be "
              "contiguous", asset_id, i, out->atlas[i].first_tile, base);
      return false;
    }
    // The last atlas's slice runs to the end of the id space; the others end where the
    // next begins. Derived rather than stored, so the two can never disagree.
    base = (i + 1 < atlas_count) ? read_u16(at + 6) : tile_total;
    if (base < out->atlas[i].first_tile || base > tile_total) {
      pnx_log("map %u: atlas %u claims tiles %u..%u of %u", asset_id, i,
              out->atlas[i].first_tile, base, tile_total);
      return false;
    }
    out->atlas[i].tile_count = (uint16_t)(base - out->atlas[i].first_tile);
    at += 4;
  }

  out->pool_offset = at;
  at += ((size_t)atlas_slots + 1) * 4;
  if (read_u32(out->pool_offset + (size_t)atlas_slots * 4) != pool_bytes) {
    pnx_log("map %u: atlas pool says %u bytes, its slot table ends at %u",
            asset_id, (unsigned)pool_bytes,
            (unsigned)read_u32(out->pool_offset + (size_t)atlas_slots * 4));
    return false;
  }

  out->first_bank_asset = read_u16(at);
  at += 4;

  // Every bank checked once, here, rather than on every read. Stamping is worthless
  // unless something looks, and looking per read would cost a seek to offset 0 in front
  // of each one -- on a platform where the seek IS the cost. A scene boundary can afford
  // bank_count reads of eight bytes; a frame cannot.
  const uint16_t bank_count = (uint16_t)(((n - 1) >> pre[7]) + 1);
  if (out->first_bank_asset == 0
      || (uint32_t)out->first_bank_asset + bank_count > s_resource_count) {
    pnx_log("map %u: %u banks from asset %u, past the %u the project has",
            asset_id, bank_count, out->first_bank_asset, s_resource_count);
    return false;
  }
  for (uint16_t b = 0; b < bank_count; b++) {
    uint8_t bh[PNX_BLOB_HEADER_BYTES];
    const uint32_t res = s_resources[out->first_bank_asset + b];
    if (pnx_platform_resource_read(res, 0, bh, sizeof(bh)) != sizeof(bh)
        || bh[0] != 'P' || bh[1] != 'K' || bh[2] != PNX_BLOB_VERSION
        || (s_orientation != PNX_ORIENT_UNSET && bh[7] != s_orientation)) {
      pnx_log("map %u: bank %u is not a v%u %s WorldTile bank -- rebuild assets",
              asset_id, b, PNX_BLOB_VERSION,
              s_orientation == PNX_ORIENT_UNSET ? "current" : "matching");
      return false;
    }
    s_bytes_loaded += sizeof(bh);
  }

  out->tile_flags = at;
  at += pad4(tile_total);
  out->tile_palette = NULL;
  if (flags & 1) {
    out->tile_palette = at;
    at += pad4(tile_total);
  }
  // PnxWarp is five u8 fields, so it has no padding and maps directly onto the packed
  // bytes the pipeline writes. _Static_assert below keeps that true.
  out->warps = (const PnxWarp *)(const void *)at;
  at += pad4((size_t)warps * sizeof(PnxWarp));
  out->wt_mask = at;

  // Pools. The slot count is the smaller of what the pipeline asked for and what the map
  // actually has -- a map of four WorldTiles never needs nine slots, and paying for them
  // would make small maps cost more resident than they did before any of this.
  const uint8_t slots = want_slots < n ? want_slots : (uint8_t)n;
  out->slots = PNX_ARENA_CALLOC_ARRAY(s_arena, PnxWorldTile, slots);
  out->slot_mem = (uint8_t *)pnx_arena_alloc(s_arena, (size_t)slots * slot_bytes, 4);
  out->wt_slot = (uint8_t *)pnx_arena_alloc(s_arena, n, 4);
  out->pool = PNX_ARENA_CALLOC_ARRAY(s_arena, PnxAtlas, atlas_slots);
  out->pool_mem = (uint8_t *)pnx_arena_alloc(s_arena, pool_bytes, 4);
  out->pool_owner = (uint8_t *)pnx_arena_alloc(s_arena, atlas_slots, 4);
  out->pool_pins = (uint8_t *)pnx_arena_alloc(s_arena, atlas_slots, 4);
  if (!out->slots || !out->slot_mem || !out->wt_slot || !out->pool || !out->pool_mem
      || !out->pool_owner || !out->pool_pins) {
    pnx_log("map %u: arena full, needed %u for %u WorldTile slots and %u atlas slots, "
            "%u free", asset_id,
            (unsigned)((size_t)slots * slot_bytes + pool_bytes),
            slots, atlas_slots, (unsigned)pnx_arena_remaining(s_arena));
    return false;
  }

  memset(out->wt_slot, PNX_MAP_NO_SLOT, n);
  memset(out->pool_owner, PNX_MAP_NO_SLOT, atlas_slots);
  memset(out->pool_pins, 0, atlas_slots);

  out->resource = resource;
  out->tile_count = tile_total;
  out->slot_bytes = slot_bytes;
  out->w = w;
  out->h = h;
  out->warp_count = warps;
  out->atlas_count = atlas_count;
  out->atlas_slots = atlas_slots;
  out->slot_count = slots;
  out->wt_cols = cols;
  out->wt_rows = rows;
  out->worldtile = worldtile;
  out->wt_shift = shift_of(worldtile);
  out->bank_shift = pre[7];
  // Carried by the map rather than read off a loaded atlas: the streamer works in world
  // pixels and has to size its window BEFORE any atlas is resident. The atlas load checks
  // the two agree.
  out->tile_px = tile_px;

  // **A map that fits is loaded whole, here, before this returns.** Every small map is in
  // this case, so they behave exactly as they did before WorldTiles existed: one load
  // point, everything resident, no streaming code ever running. Leaving them to fill
  // lazily would have been cheaper by a few reads and wrong in a way that matters -- a
  // map that never streams should never be seen to stream, and a game measuring flash
  // traffic would otherwise find a "resident" map still reading as the player walks.
  //
  // Only when the atlases fit too: with fewer atlas slots than atlases, holding every
  // WorldTile at once is not something the pool can express, and the streamer's window is
  // what keeps the two in step.
  out->held_whole = (slots == n && atlas_slots >= atlas_count);
  if (out->held_whole) {
    // One read per bank, not one per WorldTile. Slot i holds tile i here, so a bank's
    // tiles are consecutive at both ends and the run loader can take the whole thing --
    // which on a 144-WorldTile map is 18 reads instead of 144, and 18 seeks instead of
    // 144 into a resource where the seek is what costs.
    uint16_t per_bank = (uint16_t)(1u << out->bank_shift);
    if (per_bank > PNX_MAP_MAX_RUN) per_bank = PNX_MAP_MAX_RUN;
    for (uint16_t i = 0; i < n; i += per_bank) {
      const uint16_t run = (n - i) < per_bank ? (uint16_t)(n - i) : per_bank;
      if (!worldtile_load_run(out, i, (uint8_t)i, (uint8_t)run)) {
        pnx_log("map %u: WorldTiles %u..%u failed while loading the map whole",
                asset_id, i, i + run - 1);
        return false;
      }
    }
  }
  return true;
}

_Static_assert(sizeof(PnxWarp) == 5,
               "PnxWarp must stay packed: it is cast directly onto blob bytes");

// ------------------------------------------------------------------ the streamer

// Finds `which` in the atlas pool, loading it if it is not there. Returns false only when
// every slot is pinned by a resident WorldTile -- which the pipeline's window check makes
// impossible for content it accepted, so it means the view moved further in one step than
// the pool was sized for.
static bool atlas_pin(PnxMap *m, uint8_t which) {
  PnxMapAtlas *a = &m->atlas[which];
  if (a->slot != PNX_MAP_NO_SLOT) return true;

  uint8_t slot = PNX_MAP_NO_SLOT;
  if (m->atlas_slots >= m->atlas_count) {
    // A slot per atlas: slot i belongs to atlas i and is exactly its size, so there is no
    // choice to make and nothing is ever evicted. Picking any other slot would be picking
    // one sized for a different atlas.
    slot = which;
  } else {
    for (uint8_t i = 0; i < m->atlas_slots; i++) {
      if (m->pool_owner[i] == PNX_MAP_NO_SLOT) { slot = i; break; }
    }
    if (slot == PNX_MAP_NO_SLOT) {
      // Any unpinned slot will do: an atlas nothing resident depends on is as evictable
      // as any other, and every slot is the same size in this case.
      for (uint8_t i = 0; i < m->atlas_slots; i++) {
        if (m->pool_pins[i] == 0) { slot = i; break; }
      }
    }
    if (slot == PNX_MAP_NO_SLOT) return false;
  }

  if (m->pool_owner[slot] != PNX_MAP_NO_SLOT) {
    m->atlas[m->pool_owner[slot]].slot = PNX_MAP_NO_SLOT;
  }
  m->pool_owner[slot] = PNX_MAP_NO_SLOT;

  const uint32_t from = read_u32(m->pool_offset + (size_t)slot * 4);
  const uint32_t to = read_u32(m->pool_offset + ((size_t)slot + 1) * 4);
  if (!atlas_load_into(&m->pool[slot], a->asset, m->pool_mem + from, to - from)) {
    return false;
  }
  if (m->pool[slot].tile_count != a->tile_count || m->pool[slot].tile_px != m->tile_px) {
    pnx_log("map atlas %u holds %u tiles of %upx, the map was built against %u of %upx "
            "-- rebuild assets", a->asset, m->pool[slot].tile_count,
            m->pool[slot].tile_px, a->tile_count, m->tile_px);
    return false;
  }
  m->pool_owner[slot] = which;
  a->slot = slot;
  return true;
}

// Pins and unpins are kept in step by going through these two, because the failure they
// prevent is silent: an atlas whose count never returns to zero is never evictable again,
// so the pool shrinks by one slot per mistake until nothing can load.
static void atlas_unpin_mask(PnxMap *m, uint8_t mask) {
  for (uint8_t k = 0; k < m->atlas_count; k++) {
    const uint8_t slot = m->atlas[k].slot;
    if ((mask >> k & 1) && slot != PNX_MAP_NO_SLOT && m->pool_pins[slot] > 0) {
      m->pool_pins[slot]--;
    }
  }
}

static void worldtile_release(PnxMap *m, uint8_t slot) {
  PnxWorldTile *wt = &m->slots[slot];
  if (!wt->live) return;
  const uint32_t i = (uint32_t)wt->wy * m->wt_cols + wt->wx;
  atlas_unpin_mask(m, m->wt_mask[i]);
  m->wt_slot[i] = PNX_MAP_NO_SLOT;
  wt->live = false;
}

// Pins the atlases one WorldTile needs, and reports which ones it pinned so a later
// failure can hand them back.
//
// Atlases first, always, and pinned as they land rather than at the end. A WorldTile whose
// art is not loaded would draw as holes, and pinning late would let the second atlas this
// WorldTile needs evict the first -- the two-atlas WorldTile in a one-slot pool, which the
// pipeline rejects but which a hand-built blob could still ask for.
//
// The mask lives beside the index rather than in the payload precisely so this ordering is
// possible without reading the payload to find out what it needs.
static bool worldtile_pin(PnxMap *m, uint32_t i, uint8_t *out_pinned) {
  const uint8_t mask = m->wt_mask[i];
  uint8_t pinned = 0;
  for (uint8_t k = 0; k < m->atlas_count; k++) {
    if (!(mask >> k & 1)) continue;
    if (!atlas_pin(m, k)) {
      pnx_log("map: WorldTile %u needs atlas %u, and every pool slot is in use",
              (unsigned)i, k);
      atlas_unpin_mask(m, pinned);
      return false;
    }
    m->pool_pins[m->atlas[k].slot]++;
    pinned |= (uint8_t)(1u << k);
  }
  *out_pinned = pinned;
  return true;
}

// Unpacks a payload that has already landed in its slot.
static bool worldtile_accept(PnxMap *m, uint32_t i, uint8_t slot) {
  const uint8_t *dst = m->slot_mem + (size_t)slot * m->slot_bytes;
  PnxWorldTile *wt = &m->slots[slot];

  wt->cell_w = dst[0];
  wt->cell_h = dst[1];
  wt->override_count = read_u16(dst + 2);
  wt->cells = dst + 4;
  wt->overrides = dst + 4 + (size_t)wt->cell_w * wt->cell_h * 2;

  const size_t need = 4 + (size_t)wt->cell_w * wt->cell_h * 2
                      + (size_t)wt->override_count * 3;
  if (wt->cell_w == 0 || wt->cell_h == 0 || wt->cell_w > m->worldtile
      || wt->cell_h > m->worldtile || need > m->slot_bytes) {
    pnx_log("map: WorldTile %u claims %ux%u cells and %u overrides, %u bytes of %u",
            (unsigned)i, wt->cell_w, wt->cell_h, wt->override_count,
            (unsigned)need, m->slot_bytes);
    return false;
  }

  wt->wx = (uint8_t)(i % m->wt_cols);
  wt->wy = (uint8_t)(i / m->wt_cols);
  wt->live = true;
  m->wt_slot[i] = slot;
  return true;
}

// Loads `count` consecutive WorldTiles starting at index `first` into `count` consecutive
// pool slots starting at `slot` -- in ONE resource read.
//
// This is the batching half of the fix. A ranged read costs by how far into the resource
// it starts, so the win is not fewer bytes but fewer seeks: a whole bank fetched at once
// pays one seek instead of eight. It works at all because payloads are padded to the slot
// stride, which makes a run of tiles contiguous at both ends -- contiguous in the bank,
// and contiguous in the pool.
//
// The caller guarantees the run stays inside one bank and inside the slot array.
static bool worldtile_load_run(PnxMap *m, uint32_t first, uint8_t slot, uint8_t count) {
  // Sized independently of the stream budget, which counts READS and says nothing about
  // how many WorldTiles one carries. A bank can hold up to 128 of them at the smallest
  // WorldTile size, and the caller loops rather than this growing a 128-byte frame.
  uint8_t pinned[PNX_MAP_MAX_RUN];
  if (count > PNX_MAP_MAX_RUN) count = PNX_MAP_MAX_RUN;

  for (uint8_t k = 0; k < count; k++) {
    if (worldtile_pin(m, first + k, &pinned[k])) continue;
    // Whatever was pinned for earlier tiles of this run has to go back, or those atlas
    // slots are held by WorldTiles that never became resident.
    while (k--) atlas_unpin_mask(m, pinned[k]);
    return false;
  }

  const uint16_t per_bank = (uint16_t)(1u << m->bank_shift);
  const uint16_t bank = (uint16_t)(first >> m->bank_shift);
  const size_t offset = PNX_BLOB_HEADER_BYTES
                        + (size_t)(first & (per_bank - 1)) * m->slot_bytes;
  const size_t len = (size_t)count * m->slot_bytes;
  uint8_t *dst = m->slot_mem + (size_t)slot * m->slot_bytes;

  const uint32_t resource = s_resources[m->first_bank_asset + bank];
  if (pnx_platform_resource_read(resource, offset, dst, len) != len) {
    pnx_log("map: bank %u short read of %u bytes at %u for WorldTiles %u..%u",
            bank, (unsigned)len, (unsigned)offset, (unsigned)first,
            (unsigned)(first + count - 1));
    for (uint8_t k = 0; k < count; k++) atlas_unpin_mask(m, pinned[k]);
    return false;
  }
  s_bytes_loaded += (uint32_t)len;

  for (uint8_t k = 0; k < count; k++) {
    if (worldtile_accept(m, first + k, (uint8_t)(slot + k))) continue;
    // Roll the whole run back: a half-accepted run would leave slots marked live with
    // pins that no longer describe them.
    for (uint8_t j = 0; j < k; j++) worldtile_release(m, (uint8_t)(slot + j));
    for (uint8_t j = k; j < count; j++) atlas_unpin_mask(m, pinned[j]);
    return false;
  }
  return true;
}


// Manhattan distance from the window's centre, which is what "furthest from where the
// player is" means on a grid. A WorldTile outside the window scores above everything
// inside it, so eviction always takes an off-screen one first.
static uint32_t wt_distance(const PnxWorldTile *wt, int32_t cx, int32_t cy) {
  const int32_t dx = (int32_t)wt->wx - cx;
  const int32_t dy = (int32_t)wt->wy - cy;
  return (uint32_t)((dx < 0 ? -dx : dx) + (dy < 0 ? -dy : dy));
}

// Floor division, not truncation, because a camera can sit at a negative x when the map
// is narrower than the screen -- and there truncation rounds toward zero and names the
// wrong WorldTile.
static int32_t floor_div(int32_t a, int32_t b) {
  return a >= 0 ? a / b : -(((-a) + b - 1) / b);
}

static uint8_t stream_window(PnxMap *m, int32_t x, int32_t y, int32_t w, int32_t h,
                             uint8_t budget) {
  // A map held whole has nothing to load and nothing it may evict -- the eviction pass
  // below would happily drop WorldTiles outside the window and read them back as the
  // camera returned, turning a map that fits into one that streams. Every small map
  // takes this path, so it is also where most of the per-frame cost of streaming goes
  // for games that never needed it.
  if (!m->slots || m->held_whole) return 0;

  // One WorldTile of margin on each side, matching worldtile_window() in the pipeline --
  // which is what sized the pool. The two have to agree or the streamer asks for more
  // slots than it was given and thrashes.
  const int32_t span = (int32_t)m->tile_px * m->worldtile;
  int32_t x0 = floor_div(x, span) - 1, y0 = floor_div(y, span) - 1;
  int32_t x1 = floor_div(x + w - 1, span) + 1, y1 = floor_div(y + h - 1, span) + 1;
  if (x0 < 0) x0 = 0;
  if (y0 < 0) y0 = 0;
  if (x1 >= m->wt_cols) x1 = m->wt_cols - 1;
  if (y1 >= m->wt_rows) y1 = m->wt_rows - 1;
  if (x1 < x0 || y1 < y0) return 0;

  const int32_t cx = (x0 + x1) / 2, cy = (y0 + y1) / 2;
  uint8_t missing = 0;

  // Drop everything outside the window BEFORE loading anything, rather than evicting one
  // slot at a time as the loads demand it.
  //
  // This is not tidiness. Evicting on demand cannot free an ATLAS: a warp asks for a
  // region drawn from a tileset nothing resident uses, the first new WorldTile tries to
  // pin it, and every atlas slot is still held by the fifteen WorldTiles that have not
  // been evicted yet -- because they are only evicted one per load, and each load is what
  // was blocked. The whole window fails and the screen stays empty. Walking never showed
  // it, since the window moves a tile at a time and the old WorldTiles drain gradually;
  // it took a jump to a distant part of the map, which is exactly what a warp is.
  //
  // Safe to do unconditionally: the window already carries the streamer's margin, so
  // anything outside it is not on screen and is not about to be.
  for (uint8_t i = 0; i < m->slot_count; i++) {
    const PnxWorldTile *wt = &m->slots[i];
    if (!wt->live) continue;
    if (wt->wx < x0 || wt->wx > x1 || wt->wy < y0 || wt->wy > y1) {
      worldtile_release(m, i);
    }
  }

  for (int32_t wy = y0; wy <= y1; wy++) {
    int32_t wx = x0;
    while (wx <= x1) {
      const uint32_t first = (uint32_t)wy * m->wt_cols + wx;
      if (m->wt_slot[first] != PNX_MAP_NO_SLOT) { wx++; continue; }

      // How far this run of missing WorldTiles goes. Consecutive along the row, which
      // makes them consecutive in the bank, and stopping at the bank boundary because one
      // read cannot cross two resources.
      const uint32_t per_bank = 1u << m->bank_shift;
      int32_t run = 1;
      while (wx + run <= x1
             && m->wt_slot[first + run] == PNX_MAP_NO_SLOT
             && ((first + run) >> m->bank_shift) == (first >> m->bank_shift)
             && (uint32_t)run < per_bank
             && run < PNX_MAP_MAX_RUN) {
        run++;
      }

      if (budget == 0) { missing += (uint8_t)run; wx += run; continue; }

      // Consecutive free slots for a consecutive run, because the read lands in one
      // stretch of pool memory. Shrinking rather than failing: a fragmented pool still
      // loads, just in more reads than it might have.
      uint8_t slot = PNX_MAP_NO_SLOT;
      while (run > 0 && slot == PNX_MAP_NO_SLOT) {
        for (uint8_t i = 0; i + run <= m->slot_count; i++) {
          uint8_t k = 0;
          while (k < run && !m->slots[i + k].live) k++;
          if (k == run) { slot = i; break; }
        }
        if (slot == PNX_MAP_NO_SLOT) run--;
      }

      if (slot == PNX_MAP_NO_SLOT) {
        // Nothing free at all. Evict the resident WorldTile furthest from the middle of
        // what is wanted -- the one the player is walking away from, by construction --
        // and take it alone.
        run = 1;
        uint32_t worst = 0;
        for (uint8_t i = 0; i < m->slot_count; i++) {
          const uint32_t d = wt_distance(&m->slots[i], cx, cy);
          if (slot == PNX_MAP_NO_SLOT || d > worst) { slot = i; worst = d; }
        }
        worldtile_release(m, slot);
      }

      if (!worldtile_load_run(m, first, slot, (uint8_t)run)) {
        missing++;
        wx++;
        continue;
      }
      budget--;                     // one READ, however many WorldTiles it carried
      wx += run;
    }
  }
  return missing;
}

uint8_t pnx_map_stream(PnxMap *m, int32_t x, int32_t y, int32_t w, int32_t h) {
  if (!m) return 0;
  return stream_window(m, x, y, w, h, PNX_MAP_STREAM_BUDGET);
}

uint8_t pnx_map_stream_now(PnxMap *m, int32_t x, int32_t y, int32_t w, int32_t h) {
  if (!m) return 0;

  // The budget is the whole pool: a scene load or a warp has no previous frame to show, so
  // there is nothing to protect by spreading the reads over several. Sixteen WorldTiles
  // and four atlases is ~1.5 ms against a 37.33 ms frame.
  return stream_window(m, x, y, w, h, m->slot_count);
}

uint8_t pnx_map_resident(const PnxMap *m) {
  uint8_t n = 0;
  for (uint8_t i = 0; m && i < m->slot_count; i++) n += m->slots[i].live ? 1 : 0;
  return n;
}

bool pnx_dialog_load(PnxDialog *out, uint16_t asset_id) {
  uint8_t entries = 0;
  size_t payload = 0;
  const uint8_t *data = load_blob(asset_id, "PD", &entries, NULL, NULL, &payload);
  if (!data) return false;

  const size_t index_bytes = (size_t)entries * 4;
  if (entries == 0 || payload < index_bytes) {
    pnx_log("dialog %u: %u entries needs %u index bytes, blob has %u",
            asset_id, entries, (unsigned)index_bytes, (unsigned)payload);
    return false;
  }

  // Total page count is the last entry's first + count, which the pipeline guarantees
  // is the end of the page list.
  uint16_t total_pages = 0;
  for (uint16_t i = 0; i < entries; i++) {
    const uint8_t *e = data + (size_t)i * 4;
    const uint16_t first = (uint16_t)(e[0] | (e[1] << 8));
    const uint16_t count = (uint16_t)(e[2] | (e[3] << 8));
    if (first + count > total_pages) total_pages = (uint16_t)(first + count);
  }

  if (payload < index_bytes + (size_t)total_pages * 2) {
    pnx_log("dialog %u: %u pages of offsets do not fit in %u bytes",
            asset_id, total_pages, (unsigned)payload);
    return false;
  }

  out->index = data;
  out->offsets = (const uint16_t *)(const void *)(data + index_bytes);
  out->text = data + index_bytes + (size_t)total_pages * 2;
  out->entry_count = entries;
  return true;
}

bool pnx_font_load(PnxFont *out, uint16_t asset_id) {
  uint8_t depth = 0, line_height = 0, baseline = 0, advance = 0;
  size_t payload = 0;
  const uint8_t *data = load_blob_4(asset_id, "PF", &depth, &line_height, &baseline,
                                    &advance, &payload);
  if (!data) return false;

  if (advance >= PNX_ADVANCE_COUNT) {
    pnx_log("font %u: advance axis %u, expected 0-%u", asset_id, advance,
            PNX_ADVANCE_COUNT - 1);
    return false;
  }

  // u16 glyph_count, u16 bitmap_bytes, u8 first_cp, last_cp, fallback, space_advance.
  if (payload < 8) {
    pnx_log("font %u: payload %u is shorter than its own header", asset_id,
            (unsigned)payload);
    return false;
  }

  const uint16_t glyphs = (uint16_t)(data[0] | (data[1] << 8));
  const uint16_t bitmap_bytes = (uint16_t)(data[2] | (data[3] << 8));
  const uint8_t first_cp = data[4], last_cp = data[5];
  const uint8_t fallback = data[6], space_advance = data[7];

  if (depth != 1 && depth != 2) {
    pnx_log("font %u: depth %u, expected 1 or 2", asset_id, depth);
    return false;
  }
  if (glyphs == 0 || line_height == 0 || first_cp > last_cp) {
    pnx_log("font %u: %u glyphs, %upx line, cp %u..%u -- not a usable font",
            asset_id, glyphs, line_height, first_cp, last_cp);
    return false;
  }
  if (fallback >= glyphs) {
    pnx_log("font %u: fallback glyph %u of %u", asset_id, fallback, glyphs);
    return false;
  }

  const size_t index_bytes = (size_t)glyphs * PNX_FONT_GLYPH_BYTES;
  const size_t map_bytes = (size_t)(last_cp - first_cp) + 1u;
  const size_t expected = 8 + index_bytes + map_bytes + bitmap_bytes;
  if (payload != expected) {
    pnx_log("font %u: %u glyphs, %u cp, %u bitmap bytes needs %u, blob has %u",
            asset_id, glyphs, (unsigned)map_bytes, bitmap_bytes,
            (unsigned)expected, (unsigned)payload);
    return false;
  }

  out->glyphs = data + 8;
  out->map = data + 8 + index_bytes;
  out->bitmaps = data + 8 + index_bytes + map_bytes;
  out->glyph_count = glyphs;
  out->bitmap_bytes = bitmap_bytes;
  out->depth = depth;
  out->line_height = line_height;
  out->baseline = baseline;
  out->space_advance = space_advance;
  out->first_cp = first_cp;
  out->last_cp = last_cp;
  out->fallback = fallback;
  out->advance = advance;

  // Both tables are validated once, here, so the blitter can index them per pixel with
  // no checks at all -- the same bargain pnx_atlas_load makes with palette slots. An
  // out-of-range offset would otherwise read arbitrary arena memory as glyph pixels.
  for (uint16_t i = 0; i < glyphs; i++) {
    const uint8_t *e = out->glyphs + (size_t)i * PNX_FONT_GLYPH_BYTES;
    const uint16_t off = (uint16_t)(e[0] | (e[1] << 8));
    const uint8_t w = e[2], h = e[3];
    if (w == 0) continue;

    const size_t need = (size_t)off + (size_t)h * pnx_font_row_bytes(out, w);
    if (need > bitmap_bytes) {
      pnx_log("font %u: glyph %u (%ux%u at %u) runs %u past its %u bitmap bytes",
              asset_id, i, w, h, off, (unsigned)(need - bitmap_bytes), bitmap_bytes);
      return false;
    }
  }

  for (size_t i = 0; i < map_bytes; i++) {
    if (out->map[i] != PNX_FONT_NO_GLYPH && out->map[i] >= glyphs) {
      pnx_log("font %u: codepoint %u maps to glyph %u of %u", asset_id,
              (unsigned)(first_cp + i), out->map[i], glyphs);
      return false;
    }
  }
  return true;
}

// ---------------------------------------------------------------------- accessors

const PnxWarp *pnx_map_warp_at(const PnxMap *m, int32_t x, int32_t y) {
  // Linear: maps have a handful of warps, so an index would cost more memory than the
  // scan costs time, and this only runs on a tile boundary crossing.
  for (uint8_t i = 0; i < m->warp_count; i++) {
    if (m->warps[i].x == x && m->warps[i].y == y) return &m->warps[i];
  }
  return NULL;
}

uint16_t pnx_dialog_page_count(const PnxDialog *d, uint16_t entry) {
  if (entry >= d->entry_count) return 0;
  const uint8_t *e = d->index + (size_t)entry * 4;
  return (uint16_t)(e[2] | (e[3] << 8));
}

const char *pnx_dialog_page(const PnxDialog *d, uint16_t entry, uint16_t page) {
  if (entry >= d->entry_count) return NULL;
  const uint8_t *e = d->index + (size_t)entry * 4;
  const uint16_t first = (uint16_t)(e[0] | (e[1] << 8));
  const uint16_t count = (uint16_t)(e[2] | (e[3] << 8));
  if (page >= count) return NULL;
  return (const char *)(d->text + d->offsets[first + page]);
}

// ------------------------------------------------------------------------ scenes

bool pnx_scenes_load(uint16_t asset_id) {
  if (!s_persistent) return false;

  // Read into the PERSISTENT arena: the table has to survive the scene resets it
  // drives, which is the whole reason the two arenas are separate.
  PnxArena *saved = s_arena;
  s_arena = s_persistent;

  uint8_t count = 0;
  size_t payload = 0;
  const uint8_t *data = load_blob(asset_id, "PC", &count, NULL, NULL, &payload);

  s_arena = saved;
  if (!data) return false;

  if (count == 0 || payload < (size_t)count * 4) {
    pnx_log("scenes %u: %u scenes needs %u index bytes, blob has %u",
            asset_id, count, (unsigned)(count * 4), (unsigned)payload);
    return false;
  }

  s_scene_table = data;
  s_scene_count = count;
  return true;
}

bool pnx_scene_load(uint16_t scene_id) {
  if (!s_scene_table || scene_id >= s_scene_count) {
    pnx_log("scene %u: out of range (have %u)", scene_id, s_scene_count);
    return false;
  }

  const uint8_t *entry = s_scene_table + (size_t)scene_id * 4;
  const uint16_t first = (uint16_t)(entry[0] | (entry[1] << 8));
  const uint8_t count = entry[2];
  const uint16_t *ids = (const uint16_t *)(const void *)
                        (s_scene_table + (size_t)s_scene_count * 4);

  // Everything the previous scene held goes at once. There is no partial free anywhere
  // in the framework, and a scene boundary is the only point that needs one.
  pnx_arena_reset(s_arena);
  s_atlas_count = s_sprite_count = s_font_count = 0;
  s_have_map = s_have_dialog = false;
  s_palettes = NULL;
  s_palette_count = 0;

  // Palettes first: atlases and sprites carry indices into them, and refuse to load
  // before the table exists.
  if (!pnx_palettes_load(PNX_ASSET_PALETTES_SLOT)) return false;

  for (uint8_t i = 0; i < count; i++) {
    const uint16_t asset = ids[first + i];
    size_t size = 0;
    if (!pnx_platform_resource_size(s_resources[asset], &size) || size < 3) {
      pnx_log("scene %u: asset %u unreadable", scene_id, asset);
      return false;
    }

    // Dispatch on the blob's own magic rather than on a type recorded in the scene
    // table, so the two can never disagree.
    uint8_t magic[3] = {0};
    pnx_platform_resource_read(s_resources[asset], 0, magic, 3);

    bool ok = false;
    if (magic[0] == 'P' && magic[1] == 'A') {
      // A scene atlas is no longer paired with anything -- the map owns and streams the
      // tilesets it draws with -- so the asset id it came from is not kept. It is here for
      // whatever else a scene wants a resident tileset for.
      ok = s_atlas_count < PNX_SCENE_MAX_ATLASES
           && pnx_atlas_load(&s_atlases[s_atlas_count], asset);
      if (ok) s_atlas_count++;
    } else if (magic[0] == 'P' && magic[1] == 'S') {
      ok = s_sprite_count < PNX_SCENE_MAX_SPRITES
           && pnx_sprite_load(&s_sprites[s_sprite_count], asset);
      if (ok) s_sprite_count++;
    } else if (magic[0] == 'P' && magic[1] == 'M') {
      // A map names and owns its own tilesets, so there is nothing to pair here any more.
      // The scene's job ends at loading it; the map's pools do the rest.
      ok = pnx_map_load(&s_map, asset);
      s_have_map = ok;
    } else if (magic[0] == 'P' && magic[1] == 'D') {
      ok = pnx_dialog_load(&s_dialog, asset);
      s_have_dialog = ok;
    } else if (magic[0] == 'P' && magic[1] == 'F') {
      ok = s_font_count < PNX_SCENE_MAX_FONTS
           && pnx_font_load(&s_fonts[s_font_count], asset);
      if (ok) s_font_count++;
    }

    if (!ok) {
      pnx_log("scene %u: asset %u (%c%c) failed to load", scene_id, asset,
              magic[0], magic[1]);
      return false;
    }
  }

  pnx_log("scene %u: %u assets, %u atlases, %u sprites, %u fonts, arena %u/%u",
          scene_id, count, s_atlas_count, s_sprite_count, s_font_count,
          (unsigned)s_arena->used, (unsigned)s_arena->capacity);
  return true;
}

const PnxAtlas *pnx_scene_atlas(uint8_t index) {
  return index < s_atlas_count ? &s_atlases[index] : NULL;
}
const PnxSprite *pnx_scene_sprite(uint8_t index) {
  return index < s_sprite_count ? &s_sprites[index] : NULL;
}
const PnxFont *pnx_scene_font(uint8_t index) {
  return index < s_font_count ? &s_fonts[index] : NULL;
}
PnxMap *pnx_scene_map(void) { return s_have_map ? &s_map : NULL; }
const PnxDialog *pnx_scene_dialog(void) { return s_have_dialog ? &s_dialog : NULL; }
uint8_t pnx_scene_atlas_count(void) { return s_atlas_count; }
uint8_t pnx_scene_sprite_count(void) { return s_sprite_count; }
uint8_t pnx_scene_font_count(void) { return s_font_count; }

#endif  // PNX_USE_ASSETS
