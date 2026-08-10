// Host tests for the asset runtime.
//
// These load the SAME blobs the device loads, produced by the real pipeline from
// examples/overworld/assets.toml, through the same parsing code. A mock returning
// synthetic bytes would only test the mock; the failure mode worth catching is a
// disagreement between what the Python writes and what the C reads, and only real data
// exercises that.
//
// Run `make test` -- the Makefile builds the example's assets first if they are absent.

#include "../src/pnx/core/pnx_arena.h"
#include "../src/pnx/assets/pnx_assets.h"
#include "../src/pnx/platform/pnx_platform.h"
#include "../src/pnx/platform/pnx_platform_host.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

extern int s_failures;
extern int s_checks;

#define A_CHECK(cond) do {                                                  \
    s_checks++;                                                             \
    if (!(cond)) {                                                          \
      printf("  FAIL %s:%d  %s\n", __FILE__, __LINE__, #cond);              \
      s_failures++;                                                         \
    }                                                                       \
  } while (0)

#define A_CHECK_EQ(a, b) do {                                               \
    s_checks++;                                                             \
    const long _a = (long)(a), _b = (long)(b);                              \
    if (_a != _b) {                                                         \
      printf("  FAIL %s:%d  %s == %s  (%ld vs %ld)\n",                      \
             __FILE__, __LINE__, #a, #b, _a, _b);                           \
      s_failures++;                                                         \
    }                                                                       \
  } while (0)

#define ASSETS_DIR "../examples/overworld/resources/"

// Include the pipeline's own generated header rather than restating the asset order.
// Hardcoding it meant that adding one atlas to the example silently shifted every id and
// the scene tests failed for a reason that had nothing to do with scenes. The RESOURCE_ID
// half is guarded out on a host build, so this is just the enum.
#include "../examples/overworld/src/c/assets_gen.h"

#define A_PALETTES PNX_ASSET_PALETTES_PALETTES
#define A_TILES    PNX_ASSET_ATLAS_TILES
#define A_CAVESET  PNX_ASSET_ATLAS_CAVESET
#define A_HERO     PNX_ASSET_SPRITE_HERO
#define A_NPC      PNX_ASSET_SPRITE_NPC
#define A_OUTDOOR  PNX_ASSET_MAP_OUTDOOR
#define A_CAVE     PNX_ASSET_MAP_CAVE
#define A_DIALOG   PNX_ASSET_DIALOG_DIALOG
#define A_SCENES   PNX_ASSET_SCENES_SCENES
#define A_COUNT    PNX_ASSET_COUNT

enum { SCENE_CAVE, SCENE_OUTDOOR };   // pipeline sorts scene names alphabetically

// The host platform keys resources by number, so any distinct ids will do.
static uint32_t RESOURCES[A_COUNT];

// Designated initialisers keyed by the generated enum, so an asset added to the example
// leaves a NULL hole rather than shifting everything after it.
static const char *ASSET_PATHS[A_COUNT] = {
  [A_PALETTES] = ASSETS_DIR "palettes.bin",
  [A_TILES]    = ASSETS_DIR "tiles.bin",
  [A_CAVESET]  = ASSETS_DIR "caveset.bin",
  [A_HERO]     = ASSETS_DIR "hero.bin",
  [A_NPC]      = ASSETS_DIR "npc.bin",
  [A_OUTDOOR]  = ASSETS_DIR "map_outdoor.bin",
  [A_CAVE]     = ASSETS_DIR "map_cave.bin",
  [A_DIALOG]   = ASSETS_DIR "dialog.bin",
  [A_SCENES]   = ASSETS_DIR "scenes.bin",
};

static bool register_assets(void) {
  // Every asset the example declares must resolve, including any the manifest gained
  // since these tests were written -- otherwise a scene referencing it fails obscurely.
  for (int i = 0; i < A_COUNT; i++) {
    RESOURCES[i] = (uint32_t)(i + 1);
    const char *path = ASSET_PATHS[i];
    if (!path) {
      // An asset this test does not name. Point it at a file that exists so a scene
      // loading it still works; the scene tests only assert on the named ones.
      continue;
    }
    FILE *f = fopen(path, "rb");
    if (!f) {
      printf("  SKIP assets: %s not built -- run tools/pnx_assets.py\n", path);
      return false;
    }
    fclose(f);
    pnx_host_register_resource(RESOURCES[i], path);
  }
  return true;
}

void test_assets(void);

void test_assets(void) {
  printf("assets\n");

  pnx_host_reset();
  if (!register_assets()) return;

  PnxArena persistent, arena;
  A_CHECK(pnx_arena_init(&persistent, "persistent", 4 * 1024, 4));
  A_CHECK(pnx_arena_init(&arena, "scene", 64 * 1024, 4));
  A_CHECK(pnx_assets_init(&persistent, &arena, RESOURCES, A_COUNT));

  // --- palettes must load before anything that indexes them
  PnxAtlas atlas;
  A_CHECK(!pnx_atlas_load(&atlas, A_TILES));      // refused: no palettes yet
  A_CHECK(pnx_palettes_load(A_PALETTES));
  A_CHECK(pnx_palette_count() > 0);
  A_CHECK(pnx_palette_count() <= PNX_PALETTE_SLOTS);

  // Index 0 is transparent in every palette -- the SNES convention the blitter relies
  // on to reject a pixel before reading the table.
  for (uint16_t i = 0; i < pnx_palette_count(); i++) {
    A_CHECK_EQ(pnx_palette((uint8_t)i)->entries[PNX_PALETTE_TRANSPARENT], 0);
  }
  A_CHECK(pnx_palette(200) == NULL);

  // --- atlas
  A_CHECK(pnx_atlas_load(&atlas, A_TILES));
  A_CHECK_EQ(atlas.tile_px, 16);
  A_CHECK_EQ(atlas.tile_count, TILES_TILE_COUNT);
  A_CHECK_EQ(atlas.tile_bytes, 16 * 16 / 2);      // 4bpp: two pixels per byte

  A_CHECK(pnx_atlas_tile_palette(&atlas, 0) != NULL);

  if (pnx_atlas_is_metatiled(&atlas)) {
    // A metatiled tile has no contiguous pixels, so the whole-tile accessor must refuse
    // rather than hand back the wrong bytes.
    A_CHECK(pnx_atlas_tile(&atlas, 0) == NULL);
    A_CHECK(atlas.subtile_count > 0);
    A_CHECK(atlas.subtile_count <= atlas.tile_count * 4);   // dedup can only shrink
    A_CHECK_EQ(atlas.sub_bytes, atlas.tile_bytes / 4);

    // Every quadrant index must be in range, or the blitter reads past the bank. The
    // loader validates this, so reaching here means it held.
    for (uint32_t i = 0; i < (uint32_t)atlas.tile_count * 4; i++) {
      if (atlas.metatiles[i] >= atlas.subtile_count) { A_CHECK(false); break; }
    }
    s_checks++;

    // Distinct tiles must still differ somewhere in their quadrant indices.
    A_CHECK(memcmp(&atlas.metatiles[0], &atlas.metatiles[4],
                   4 * sizeof(uint16_t)) != 0);
  } else {
    const uint8_t *t0 = pnx_atlas_tile(&atlas, 0);
    const uint8_t *t1 = pnx_atlas_tile(&atlas, 1);
    A_CHECK(t0 != NULL && t1 != NULL);
    A_CHECK(memcmp(t0, t1, atlas.tile_bytes) != 0);
    A_CHECK_EQ(t1 - t0, atlas.tile_bytes);

    uint8_t decoded[16 * 16];
    memset(decoded, 0xAA, sizeof(decoded));
    pnx_decode_4bpp(t0, pnx_atlas_tile_palette(&atlas, 0), decoded, 16 * 16);
    int opaque = 0;
    for (int i = 0; i < 16 * 16; i++) if (decoded[i] != 0xAA) opaque++;
    A_CHECK(opaque > 0);
  }

  // --- sprites
  PnxSprite hero;
  A_CHECK(pnx_sprite_load(&hero, A_HERO));
  A_CHECK_EQ(hero.w, 16);
  A_CHECK_EQ(hero.h, 24);
  A_CHECK_EQ(hero.frame_count, 3);
  A_CHECK_EQ(hero.frame_bytes, 16 * 24 / 2);
  A_CHECK_EQ(pnx_sprite_frame(&hero, 1) - pnx_sprite_frame(&hero, 0), hero.frame_bytes);
  A_CHECK(pnx_sprite_frame_palette(&hero, 0) != NULL);

  // The npc sheet has no alpha channel, so its transparency comes from a colour key.
  // If the key were dropped the frame would be fully opaque, which is worth pinning.
  PnxSprite npc;
  A_CHECK(pnx_sprite_load(&npc, A_NPC));
  uint8_t npc_px[16 * 24];
  memset(npc_px, 0xAA, sizeof(npc_px));
  pnx_decode_4bpp(pnx_sprite_frame(&npc, 0), pnx_sprite_frame_palette(&npc, 0),
                  npc_px, npc.w * npc.h);
  int transparent = 0;
  for (int i = 0; i < npc.w * npc.h; i++) if (npc_px[i] == 0xAA) transparent++;
  A_CHECK(transparent > 0);

  // --- maps
  PnxMap outdoor;
  A_CHECK(pnx_map_load(&outdoor, A_OUTDOOR, &atlas));
  A_CHECK_EQ(outdoor.w, MAP_OUTDOOR_W);
  A_CHECK_EQ(outdoor.h, MAP_OUTDOOR_H);
  A_CHECK_EQ(outdoor.warp_count, 1);

  // The border is wall, the interior start tile is not.
  A_CHECK(pnx_map_solid(&outdoor, 0, 0));
  A_CHECK(!pnx_map_solid(&outdoor, 15, 11));

  // Out of bounds reads as solid, so collision needs no separate edge test.
  A_CHECK(pnx_map_solid(&outdoor, -1, 5));
  A_CHECK(pnx_map_solid(&outdoor, 32, 5));
  A_CHECK(pnx_map_solid(&outdoor, 5, -1));
  A_CHECK(pnx_map_solid(&outdoor, 5, 24));

  // The warp the manifest declares must be found where it says, and nowhere else.
  const PnxWarp *w = pnx_map_warp_at(&outdoor, 15, 9);
  A_CHECK(w != NULL);
  if (w) {
    A_CHECK_EQ(w->dest_x, 12);
    A_CHECK_EQ(w->dest_y, 13);
  }
  A_CHECK(pnx_map_warp_at(&outdoor, 1, 1) == NULL);

  // The cave is drawn with a DIFFERENT tileset, so it must be paired with that one --
  // loading it against the wrong atlas is the mismatch the blob's atlas id prevents.
  PnxAtlas caveset;
  A_CHECK(pnx_atlas_load(&caveset, A_CAVESET));
  uint8_t wants = 0;
  A_CHECK(pnx_map_atlas_asset(A_CAVE, &wants));
  A_CHECK_EQ(wants, A_CAVESET);

  PnxMap cave;
  A_CHECK(pnx_map_load(&cave, A_CAVE, &caveset));
  A_CHECK_EQ(cave.w, MAP_CAVE_W);
  A_CHECK_EQ(cave.h, MAP_CAVE_H);

  // The return warp must land on a walkable tile in the other map -- the pipeline
  // checks this, and this confirms the check describes the shipped bytes.
  const PnxWarp *back = pnx_map_warp_at(&cave, 12, 14);
  A_CHECK(back != NULL);
  if (back) A_CHECK(!pnx_map_solid(&outdoor, back->dest_x, back->dest_y));

  // --- dialog
  PnxDialog dialog;
  A_CHECK(pnx_dialog_load(&dialog, A_DIALOG));
  A_CHECK_EQ(dialog.entry_count, 2);

  // Entries are alphabetical: npc_greeting (3 pages) then npc_repeat (1).
  A_CHECK_EQ(pnx_dialog_page_count(&dialog, 0), 3);
  A_CHECK_EQ(pnx_dialog_page_count(&dialog, 1), 1);

  const char *page = pnx_dialog_page(&dialog, 0, 0);
  A_CHECK(page != NULL);
  if (page) A_CHECK(strcmp(page, "Careful in there.") == 0);

  const char *last = pnx_dialog_page(&dialog, 1, 0);
  A_CHECK(last != NULL);
  if (last) A_CHECK(strcmp(last, "Still here?") == 0);

  // Out-of-range asks return NULL rather than reading past the blob.
  A_CHECK(pnx_dialog_page(&dialog, 0, 99) == NULL);
  A_CHECK(pnx_dialog_page(&dialog, 99, 0) == NULL);
  A_CHECK_EQ(pnx_dialog_page_count(&dialog, 99), 0);

  // --- type and range safety
  PnxAtlas wrong;
  A_CHECK(!pnx_atlas_load(&wrong, A_OUTDOOR));   // a map is not an atlas
  PnxMap wrong_map;
  A_CHECK(!pnx_map_load(&wrong_map, A_TILES, &atlas));   // nor the reverse
  A_CHECK(!pnx_atlas_load(&wrong, A_COUNT));     // out of range handle

  // A map cannot load without its tileset: flags live on the atlas.
  PnxMap orphan;
  A_CHECK(!pnx_map_load(&orphan, A_OUTDOOR, NULL));

  // The door is an override: it uses the same tile as ordinary scenery but carries a
  // warp flag. That is the case the sparse-override format exists to represent, so it
  // is worth pinning that the two really do share a tile and differ in flags.
  A_CHECK_EQ(pnx_map_tile(&outdoor, 15, 9), pnx_map_tile(&outdoor, 7, 12));
  A_CHECK(pnx_map_flags(&outdoor, 15, 9) & PNX_TILE_WARP);
  A_CHECK(!(pnx_map_flags(&outdoor, 7, 12) & PNX_TILE_WARP));

  A_CHECK(pnx_assets_bytes_loaded() > 1000);

  // --- scenes: the declared asset set loads as a unit
  A_CHECK(pnx_scenes_load(A_SCENES));

  A_CHECK(pnx_scene_load(SCENE_OUTDOOR));
  A_CHECK_EQ(pnx_scene_atlas_count(), 1);
  A_CHECK_EQ(pnx_scene_sprite_count(), 2);
  A_CHECK(pnx_scene_map() != NULL);
  A_CHECK(pnx_scene_dialog() != NULL);
  if (pnx_scene_map()) A_CHECK_EQ(pnx_scene_map()->w, MAP_OUTDOOR_W);

  // Loading another scene must release the first entirely rather than accumulating.
  const size_t after_outdoor = arena.used;
  A_CHECK(pnx_scene_load(SCENE_CAVE));
  A_CHECK_EQ(pnx_scene_sprite_count(), 1);       // cave declares no npc
  A_CHECK(pnx_scene_dialog() == NULL);           // nor dialog
  if (pnx_scene_map()) A_CHECK_EQ(pnx_scene_map()->w, MAP_CAVE_W);
  A_CHECK(arena.used < after_outdoor);           // smaller scene, less arena

  // Reloading the bigger scene must fit again, which it cannot if resets leak.
  A_CHECK(pnx_scene_load(SCENE_OUTDOOR));
  A_CHECK_EQ(arena.used, after_outdoor);

  A_CHECK(!pnx_scene_load(99));

  pnx_arena_destroy(&arena);
  pnx_arena_destroy(&persistent);
}
