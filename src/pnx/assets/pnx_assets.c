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

static PnxAtlas s_atlases[PNX_SCENE_MAX_ATLASES];
static uint16_t s_atlas_asset[PNX_SCENE_MAX_ATLASES];
static PnxSprite s_sprites[PNX_SCENE_MAX_SPRITES];
static PnxFont s_fonts[PNX_SCENE_MAX_FONTS];
static PnxMap s_map;
static PnxDialog s_dialog;
static uint8_t s_atlas_count, s_sprite_count, s_font_count;
static bool s_have_map, s_have_dialog;

static PnxPalette *s_palettes;
static uint16_t s_palette_count;
static PnxArena *s_arena;
static const uint32_t *s_resources;
static uint16_t s_resource_count;
static uint32_t s_bytes_loaded;

bool pnx_assets_init(PnxArena *persistent, PnxArena *scene,
                     const uint32_t *resources, uint16_t count) {
  if (!persistent || !scene || !resources || count == 0) return false;
  s_persistent = persistent;
  s_arena = scene;
  s_resources = resources;
  s_resource_count = count;
  s_bytes_loaded = 0;
  s_palettes = NULL;
  s_palette_count = 0;
  return true;
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
static const uint8_t *load_blob_4(uint16_t asset_id, const char *magic,
                                  uint8_t *out_a, uint8_t *out_b, uint8_t *out_c,
                                  uint8_t *out_d, size_t *out_payload) {
  if (!s_arena || asset_id >= s_resource_count) {
    pnx_log("asset %u: out of range (have %u)", asset_id, s_resource_count);
    return NULL;
  }

  const uint32_t resource = s_resources[asset_id];
  size_t size = 0;
  if (!pnx_platform_resource_size(resource, &size) || size < PNX_BLOB_HEADER_BYTES) {
    pnx_log("asset %u: missing or too small (%u bytes)", asset_id, (unsigned)size);
    return NULL;
  }

  uint8_t *buf = (uint8_t *)pnx_arena_alloc(s_arena, size, 4);
  if (!buf) {
    pnx_log("asset %u: arena full, needed %u, %u free", asset_id, (unsigned)size,
            (unsigned)pnx_arena_remaining(s_arena));
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

  if (out_a) *out_a = buf[3];
  if (out_b) *out_b = buf[4];
  if (out_c) *out_c = buf[5];
  if (out_d) *out_d = buf[6];
  if (out_payload) *out_payload = size - PNX_BLOB_HEADER_BYTES;

  return buf + PNX_BLOB_HEADER_BYTES;
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

bool pnx_atlas_load(PnxAtlas *out, uint16_t asset_id) {
  if (!s_palettes) {
    pnx_log("atlas %u: load palettes first -- atlases carry indices, not colours",
            asset_id);
    return false;
  }

  uint8_t tile_px = 0, count_lo = 0, layout = 0;
  size_t payload = 0;
  const uint8_t *data = load_blob(asset_id, "PA", &tile_px, &count_lo, &layout, &payload);
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

bool pnx_map_atlas_asset(uint16_t asset_id, uint8_t *out_atlas_asset) {
  if (!s_resources || asset_id >= s_resource_count) return false;

  // Header only: eight bytes rather than the whole map, so a scene can pair assets
  // before committing arena to any of them.
  uint8_t head[PNX_BLOB_HEADER_BYTES];
  if (pnx_platform_resource_read(s_resources[asset_id], 0, head, sizeof(head))
      != sizeof(head)) {
    return false;
  }
  if (head[0] != 'P' || head[1] != 'M') return false;
  if (out_atlas_asset) *out_atlas_asset = head[6];
  return true;
}

bool pnx_map_load(PnxMap *out, uint16_t asset_id, const PnxAtlas *atlas) {
  if (!atlas || !atlas->tile_flags) {
    pnx_log("map %u: needs a loaded atlas for its tile flags", asset_id);
    return false;
  }

  uint8_t w = 0, h = 0, warps = 0;
  size_t payload = 0;
  const uint8_t *data = load_blob(asset_id, "PM", &w, &h, &warps, &payload);
  if (!data) return false;

  // u16 override_count, u8 has_palette, u8 pad, [palette table], tiles, overrides, warps.
  if (payload < 4) return false;
  const uint16_t overrides = (uint16_t)(data[0] | (data[1] << 8));
  const size_t pal_bytes = data[2] ? atlas->tile_count : 0;

  // Two bytes per cell: u16 entries carrying tile index, flips and a reserved palette
  // field. The blob version guards a stale .bin against this reader.
  const size_t cells = (size_t)w * h * 2u;
  const size_t expected = 4 + pal_bytes + cells + (size_t)overrides * 3
                          + (size_t)warps * sizeof(PnxWarp);
  if (w == 0 || h == 0 || payload != expected) {
    pnx_log("map %u: %ux%u, %u overrides, %u warps needs %u bytes, blob has %u",
            asset_id, w, h, overrides, warps, (unsigned)expected, (unsigned)payload);
    return false;
  }

  out->tile_palette = pal_bytes ? data + 4 : NULL;
  out->tiles = data + 4 + pal_bytes;
  out->overrides = data + 4 + pal_bytes + cells;
  out->override_count = overrides;
  // PnxWarp is five u8 fields, so it has no padding and maps directly onto the packed
  // bytes the pipeline writes. _Static_assert below keeps that true.
  out->warps = (const PnxWarp *)(data + 4 + pal_bytes + cells + (size_t)overrides * 3);
  out->tile_flags = atlas->tile_flags;
  out->tile_count = atlas->tile_count;
  out->w = w;
  out->h = h;
  out->warp_count = warps;
  return true;
}

_Static_assert(sizeof(PnxWarp) == 5,
               "PnxWarp must stay packed: it is cast directly onto blob bytes");

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
  uint8_t depth = 0, line_height = 0, baseline = 0;
  size_t payload = 0;
  const uint8_t *data = load_blob(asset_id, "PF", &depth, &line_height, &baseline,
                                  &payload);
  if (!data) return false;

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
      ok = s_atlas_count < PNX_SCENE_MAX_ATLASES
           && pnx_atlas_load(&s_atlases[s_atlas_count], asset);
      if (ok) {
        s_atlas_asset[s_atlas_count] = asset;
        s_atlas_count++;
      }
    } else if (magic[0] == 'P' && magic[1] == 'S') {
      ok = s_sprite_count < PNX_SCENE_MAX_SPRITES
           && pnx_sprite_load(&s_sprites[s_sprite_count], asset);
      if (ok) s_sprite_count++;
    } else if (magic[0] == 'P' && magic[1] == 'M') {
      // A map names its own tileset, so several atlases can coexist in one scene.
      uint8_t wanted = 0;
      const PnxAtlas *use = NULL;
      if (pnx_map_atlas_asset(asset, &wanted)) {
        for (uint8_t k = 0; k < s_atlas_count; k++) {
          if (s_atlas_asset[k] == wanted) { use = &s_atlases[k]; break; }
        }
      }
      if (!use) {
        pnx_log("scene %u: map %u needs atlas asset %u, which the scene does not load",
                scene_id, asset, wanted);
        return false;
      }
      ok = pnx_map_load(&s_map, asset, use);
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
const PnxMap *pnx_scene_map(void) { return s_have_map ? &s_map : NULL; }
const PnxDialog *pnx_scene_dialog(void) { return s_have_dialog ? &s_dialog : NULL; }
uint8_t pnx_scene_atlas_count(void) { return s_atlas_count; }
uint8_t pnx_scene_sprite_count(void) { return s_sprite_count; }
uint8_t pnx_scene_font_count(void) { return s_font_count; }

#endif  // PNX_USE_ASSETS
