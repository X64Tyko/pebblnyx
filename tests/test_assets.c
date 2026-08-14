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

#define A_CHECK(cond)                                                \
	do                                                               \
	{                                                                \
		s_checks++;                                                  \
		if (!(cond))                                                 \
		{                                                            \
			printf("  FAIL %s:%d  %s\n", __FILE__, __LINE__, #cond); \
			s_failures++;                                            \
		}                                                            \
	} while (0)

#define A_CHECK_EQ(a, b)                                                                     \
	do                                                                                       \
	{                                                                                        \
		s_checks++;                                                                          \
		const long _a = (long)(a), _b = (long)(b);                                           \
		if (_a != _b)                                                                        \
		{                                                                                    \
			printf("  FAIL %s:%d  %s == %s  (%ld vs %ld)\n", __FILE__, __LINE__, #a, #b, _a, \
				   _b);                                                                      \
			s_failures++;                                                                    \
		}                                                                                    \
	} while (0)

#define ASSETS_DIR "../examples/overworld/resources/"

// Include the pipeline's own generated header rather than restating the asset order.
// Hardcoding it meant that adding one atlas to the example silently shifted every id and
// the scene tests failed for a reason that had nothing to do with scenes. The RESOURCE_ID
// half is guarded out on a host build, so this is just the enum.
#include "../examples/overworld/src/c/assets_gen.h"

#define A_PALETTES PNX_ASSET_PALETTES_PALETTES
#define A_TILES	   PNX_ASSET_ATLAS_TILES
#define A_CAVESET  PNX_ASSET_ATLAS_CAVESET
#define A_SHIP	   PNX_ASSET_ATLAS_SHIP
#define A_WATER	   PNX_ASSET_ATLAS_WATER
#define A_MAP_DECK PNX_ASSET_MAP_DECK
#define A_HERO	   PNX_ASSET_SPRITE_HERO
#define A_NPC	   PNX_ASSET_SPRITE_NPC
#define A_OUTDOOR  PNX_ASSET_MAP_OUTDOOR
#define A_CAVE	   PNX_ASSET_MAP_CAVE
#define A_DIALOG   PNX_ASSET_DIALOG_DIALOG
#define A_FONT_HUD PNX_ASSET_FONT_HUD
#define A_FONT_DLG PNX_ASSET_FONT_DIALOGUE
#define A_SCENES   PNX_ASSET_SCENES_SCENES
#define A_COUNT	   PNX_ASSET_COUNT

enum
{
	SCENE_CAVE,
	SCENE_OUTDOOR
}; // pipeline sorts scene names alphabetically

// The host platform keys resources by number, so any distinct ids will do.
static uint32_t s_resources[A_COUNT];

// Paths straight from the pipeline, in asset-id order. This used to be a hand-written
// table of designated initialisers, which meant an asset added to the example left a hole
// nothing registered -- and a map's WorldTile banks are assets, thirty-odd of them, so
// the hand-written version stopped being maintainable rather than merely stale.
static const char* s_asset_files[] = PNX_ASSET_FILE_TABLE;
static char s_asset_paths[A_COUNT][64];

static bool register_assets(void)
{
	for (int i = 0; i < A_COUNT; i++)
	{
		s_resources[i] = (uint32_t)(i + 1);
		snprintf(s_asset_paths[i], sizeof(s_asset_paths[i]), "%s%s", ASSETS_DIR,
				 s_asset_files[i]);
		FILE* f = fopen(s_asset_paths[i], "rb");
		if (!f)
		{
			printf("  SKIP assets: %s not built -- run tools/pnx_assets.py\n",
				   s_asset_paths[i]);
			return false;
		}
		fclose(f);
		pnx_host_register_resource(s_resources[i], s_asset_paths[i]);
	}
	return true;
}

void test_assets(void);

void test_assets(void)
{
	printf("assets\n");

	pnx_host_reset();
	if (!register_assets())
		return;

	PnxArena persistent, arena;
	A_CHECK(pnx_arena_init(&persistent, "persistent", 4 * 1024, 4));
	// Deliberately larger than any watch's: these tests load every map in the example one
	// after another without a scene reset between them, so the arena has to hold the sum of
	// things a scene would only ever hold one of. The scene checks at the end are where
	// realistic residency is asserted.
	A_CHECK(pnx_arena_init(&arena, "scene", 192 * 1024, 4));
	A_CHECK(pnx_assets_init(&persistent, &arena, s_resources, A_COUNT));

	// --- palettes must load before anything that indexes them
	PnxAtlas atlas;
	A_CHECK(!pnx_atlas_load(&atlas, A_TILES)); // refused: no palettes yet
	A_CHECK(pnx_palettes_load(A_PALETTES));
	A_CHECK(pnx_palette_count() > 0);
	A_CHECK(pnx_palette_count() <= PNX_PALETTE_SLOTS);

	// Index 0 is transparent in every palette -- the SNES convention the blitter relies
	// on to reject a pixel before reading the table.
	for (uint16_t i = 0; i < pnx_palette_count(); i++)
	{
		A_CHECK_EQ(pnx_palette((uint8_t)i)->entries[PNX_PALETTE_TRANSPARENT], 0);
	}
	A_CHECK(pnx_palette(200) == NULL);

	// --- atlas
	A_CHECK(pnx_atlas_load(&atlas, A_TILES));
	A_CHECK_EQ(atlas.tile_px, 16);
	A_CHECK_EQ(atlas.tile_count, TILES_TILE_COUNT);
	A_CHECK_EQ(atlas.tile_bytes, 16 * 16 / 2); // 4bpp: two pixels per byte

	A_CHECK(pnx_atlas_tile_palette(&atlas, 0) != NULL);

	if (pnx_atlas_is_metatiled(&atlas))
	{
		// A metatiled tile has no contiguous pixels, so the whole-tile accessor must refuse
		// rather than hand back the wrong bytes.
		A_CHECK(pnx_atlas_tile(&atlas, 0) == NULL);
		A_CHECK(atlas.subtile_count > 0);
		A_CHECK(atlas.subtile_count <= atlas.tile_count * 4); // dedup can only shrink
		A_CHECK_EQ(atlas.sub_bytes, atlas.tile_bytes / 4);

		// Every quadrant index must be in range, or the blitter reads past the bank. The
		// loader validates this, so reaching here means it held.
		for (uint32_t i = 0; i < (uint32_t)atlas.tile_count * 4; i++)
		{
			if (atlas.metatiles[i] >= atlas.subtile_count)
			{
				A_CHECK(false);
				break;
			}
		}
		s_checks++;

		// Distinct tiles must still differ somewhere in their quadrant indices.
		A_CHECK(memcmp(&atlas.metatiles[0], &atlas.metatiles[4], 4 * sizeof(uint16_t)) != 0);
	}
	else
	{
		const uint8_t* t0 = pnx_atlas_tile(&atlas, 0);
		const uint8_t* t1 = pnx_atlas_tile(&atlas, 1);
		A_CHECK(t0 != NULL && t1 != NULL);
		// A_CHECK logs and continues rather than stopping the test, so a failed null check
		// above does not stop the memcmp/subtraction below from running on a null pointer --
		// guarded explicitly rather than trusting the log line to have been read in time.
		if (t0 != NULL && t1 != NULL)
		{
			A_CHECK(memcmp(t0, t1, atlas.tile_bytes) != 0);
			A_CHECK_EQ(t1 - t0, atlas.tile_bytes);
		}

		uint8_t decoded[16 * 16];
		memset(decoded, 0xAA, sizeof(decoded));
		pnx_decode_4bpp(t0, pnx_atlas_tile_palette(&atlas, 0), decoded, 16 * 16);
		int opaque = 0;
		for (int i = 0; i < 16 * 16; i++)
			if (decoded[i] != 0xAA)
				opaque++;
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
	pnx_decode_4bpp(pnx_sprite_frame(&npc, 0), pnx_sprite_frame_palette(&npc, 0), npc_px,
					npc.w * npc.h);
	int transparent = 0;
	for (int i = 0; i < npc.w * npc.h; i++)
		if (npc_px[i] == 0xAA)
			transparent++;
	A_CHECK(transparent > 0);

	// --- palette-swapped variants share this exact bitmap
	//
	// Decoded through a variant palette, the SHAPE must be pixel-identical -- the same pixels
	// transparent -- while the colours differ. That pairing is the whole test: had the pipeline
	// packed the variant's palette in a different colour order, the shape would still come out
	// right and the colours would be scrambled, so asserting on colour alone would miss the one
	// failure that matters.
	uint8_t ice_px[16 * 24];
	memset(ice_px, 0xAA, sizeof(ice_px));
	pnx_decode_4bpp(pnx_sprite_frame(&npc, 0), pnx_palette(SPRITE_NPC_PALETTE_NPC_ICE), ice_px,
					npc.w * npc.h);

	int shape_same = 1, colour_diffs = 0;
	for (int i = 0; i < npc.w * npc.h; i++)
	{
		const int a_clear = (npc_px[i] == 0xAA), b_clear = (ice_px[i] == 0xAA);
		if (a_clear != b_clear)
			shape_same = 0;
		else if (!a_clear && npc_px[i] != ice_px[i])
			colour_diffs++;
	}
	A_CHECK(shape_same);	   // identical silhouette, so the bitmap really is shared
	A_CHECK(colour_diffs > 0); // and it is genuinely recoloured, not a duplicate

	// Index 0 must stay transparent in every palette, or shared pixels show holes.
	A_CHECK_EQ(pnx_palette(SPRITE_NPC_PALETTE_NPC_ICE)->entries[0],
			   pnx_sprite_frame_palette(&npc, 0)->entries[0]);

	// --- maps
	//
	// A map takes no atlas: it names and owns the tilesets it draws from, and nothing of it
	// is resident until the streamer has run. Every assertion below the stream call would
	// read as "not resident" without it, which is the shape of the one mistake this API can
	// still invite.
	PnxMap outdoor;
	A_CHECK(pnx_map_load(&outdoor, A_OUTDOOR));
	A_CHECK_EQ(outdoor.w, MAP_OUTDOOR_W);
	A_CHECK_EQ(outdoor.h, MAP_OUTDOOR_H);
	A_CHECK_EQ(outdoor.warp_count, 1);
	A_CHECK_EQ(outdoor.atlas_count, 1);
	A_CHECK_EQ(outdoor.tile_px, TILES_TILE_PX);

	// Small enough to be held whole, so pnx_map_load filled it and the streaming path never
	// runs -- which is how every map in this example behaves, and how they all behaved
	// before WorldTiles existed. A map too large to hold comes back empty instead; that
	// case is tested in test_stream.c, which has one.
	A_CHECK_EQ(pnx_map_resident(&outdoor), outdoor.wt_cols * outdoor.wt_rows);
	A_CHECK(pnx_map_tile(&outdoor, 0, 0) != PNX_MAP_NO_CELL);
	A_CHECK_EQ(pnx_map_stream_now(&outdoor, 0, 0, 200, 228), 0);

	// The border is wall, the interior start tile is not.
	A_CHECK(pnx_map_solid(&outdoor, 0, 0));
	A_CHECK(!pnx_map_solid(&outdoor, 15, 11));

	// Out of bounds reads as solid, so collision needs no separate edge test.
	A_CHECK(pnx_map_solid(&outdoor, -1, 5));
	A_CHECK(pnx_map_solid(&outdoor, 32, 5));
	A_CHECK(pnx_map_solid(&outdoor, 5, -1));
	A_CHECK(pnx_map_solid(&outdoor, 5, 24));

	// The warp the manifest declares must be found where it says, and nowhere else.
	const PnxWarp* w = pnx_map_warp_at(&outdoor, 15, 9);
	A_CHECK(w != NULL);
	if (w)
	{
		A_CHECK_EQ(w->dest_x, 12);
		A_CHECK_EQ(w->dest_y, 13);
	}
	A_CHECK(pnx_map_warp_at(&outdoor, 1, 1) == NULL);

	// The cave is drawn with a DIFFERENT tileset, and says so itself rather than being
	// handed one -- which is what makes pairing it with the wrong atlas impossible rather
	// than merely detected.
	PnxAtlas caveset;
	A_CHECK(pnx_atlas_load(&caveset, A_CAVESET));

	PnxMap cave;
	A_CHECK(pnx_map_load(&cave, A_CAVE));
	A_CHECK_EQ(cave.atlas_count, 1);
	A_CHECK_EQ(cave.atlas[0].asset, A_CAVESET);
	A_CHECK_EQ(pnx_map_stream_now(&cave, 0, 0, 200, 228), 0);

	// --- palette variant: one atlas, a recoloured zone
	//
	// The map carries a palette slot per atlas tile, so the pixel data is the atlas's and only the
	// colours differ. Asserting the table exists is not enough -- a table identical to the atlas's
	// own would pass that and save nothing, so the test requires it to actually differ.
	A_CHECK(cave.tile_palette != NULL);
	if (cave.tile_palette)
	{
		int differing = 0;
		for (uint16_t i = 0; i < caveset.tile_count; i++)
		{
			if (cave.tile_palette[i] != caveset.tile_palette[i])
				differing++;
		}
		A_CHECK(differing > 0);

		// Every slot must resolve, or a recoloured tile draws through a NULL palette.
		for (uint16_t i = 0; i < caveset.tile_count; i++)
		{
			if (pnx_palette(cave.tile_palette[i]) == NULL)
			{
				A_CHECK(false);
				break;
			}
		}
	}

	// The outdoor map declares no variant, so it must fall through to the atlas's own palettes.
	A_CHECK(outdoor.tile_palette == NULL);
	A_CHECK_EQ(cave.w, MAP_CAVE_W);
	A_CHECK_EQ(cave.h, MAP_CAVE_H);

	// The return warp must land on a walkable tile in the other map -- the pipeline
	// checks this, and this confirms the check describes the shipped bytes.
	const PnxWarp* back = pnx_map_warp_at(&cave, 12, 14);
	A_CHECK(back != NULL);
	if (back)
		A_CHECK(!pnx_map_solid(&outdoor, back->dest_x, back->dest_y));

	// --- dialog
	PnxDialog dialog;
	A_CHECK(pnx_dialog_load(&dialog, A_DIALOG));
	A_CHECK_EQ(dialog.entry_count, 2);

	// Entries are alphabetical: npc_greeting (3 pages) then npc_repeat (1).
	A_CHECK_EQ(pnx_dialog_page_count(&dialog, 0), 3);
	A_CHECK_EQ(pnx_dialog_page_count(&dialog, 1), 1);

	const char* page = pnx_dialog_page(&dialog, 0, 0);
	A_CHECK(page != NULL);
	if (page)
		A_CHECK(strcmp(page, "Careful in there.") == 0);

	const char* last = pnx_dialog_page(&dialog, 1, 0);
	A_CHECK(last != NULL);
	if (last)
		A_CHECK(strcmp(last, "Still here?") == 0);

	// Out-of-range asks return NULL rather than reading past the blob.
	A_CHECK(pnx_dialog_page(&dialog, 0, 99) == NULL);
	A_CHECK(pnx_dialog_page(&dialog, 99, 0) == NULL);
	A_CHECK_EQ(pnx_dialog_page_count(&dialog, 99), 0);

	// --- type and range safety
	PnxAtlas wrong;
	A_CHECK(!pnx_atlas_load(&wrong, A_OUTDOOR)); // a map is not an atlas
	PnxMap wrong_map;
	A_CHECK(!pnx_map_load(&wrong_map, A_TILES)); // nor the reverse
	A_CHECK(!pnx_atlas_load(&wrong, A_COUNT));	 // out of range handle
	A_CHECK(!pnx_map_load(&wrong_map, A_COUNT));

	// --- multiple atlases in one map
	//
	// The ship draws from `ship` and `water`, so its cells name a MAP-global tile id that
	// the atlas table partitions. What is worth pinning is that the two slices do not
	// overlap and that a cell resolves to the atlas it was authored against -- a wrong
	// resolve draws a real tile from the wrong tileset, which looks like art and not like
	// a bug.
	PnxMap ship;
	A_CHECK(pnx_map_load(&ship, A_MAP_DECK));
	A_CHECK_EQ(ship.atlas_count, 2);
	A_CHECK_EQ(ship.atlas[0].asset, A_SHIP);
	A_CHECK_EQ(ship.atlas[1].asset, A_WATER);
	A_CHECK_EQ(ship.atlas[0].first_tile, 0);
	A_CHECK_EQ(ship.atlas[1].first_tile, ship.atlas[0].tile_count);
	A_CHECK_EQ(ship.tile_count, ship.atlas[0].tile_count + ship.atlas[1].tile_count);
	A_CHECK_EQ(pnx_map_stream_now(&ship, 0, 0, 200, 228), 0);

	// The manifest's rows put water in the corner and deck in the middle, so the two must
	// resolve to different atlases.
	uint16_t local_sea = 0, local_deck = 0;
	const PnxMapAtlas* sea = pnx_map_tile_atlas(&ship, pnx_map_tile(&ship, 0, 0), &local_sea);
	const PnxMapAtlas* deck =
		pnx_map_tile_atlas(&ship, pnx_map_tile(&ship, 10, 3), &local_deck);
	A_CHECK(sea != NULL && deck != NULL);
	if (sea && deck)
	{
		A_CHECK_EQ(sea->asset, A_WATER);
		A_CHECK_EQ(deck->asset, A_SHIP);
		A_CHECK(local_sea < sea->tile_count);
		A_CHECK(local_deck < deck->tile_count);
	}

	// Both atlases are resident, because the WorldTile that uses them pins them. Collision
	// works across the join: water is solid, the deck is not.
	A_CHECK(pnx_map_atlas(&ship, pnx_map_tile(&ship, 0, 0), NULL) != NULL);
	A_CHECK(pnx_map_atlas(&ship, pnx_map_tile(&ship, 10, 3), NULL) != NULL);
	A_CHECK(pnx_map_solid(&ship, 0, 0));
	A_CHECK(!pnx_map_solid(&ship, 10, 3));

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

	// Zero, not one: a scene no longer loads its map's tileset, because the map owns and
	// streams it. A scene atlas would be a second resident copy, which the pipeline now
	// refuses -- so this is the runtime half of that rule.
	A_CHECK_EQ(pnx_scene_atlas_count(), 0);
	A_CHECK_EQ(pnx_scene_sprite_count(), 2);
	A_CHECK(pnx_scene_map() != NULL);
	A_CHECK(pnx_scene_dialog() != NULL);
	if (pnx_scene_map())
		A_CHECK_EQ(pnx_scene_map()->w, MAP_OUTDOOR_W);

	// A scene's map is usable the moment the scene loads. That holds because this map fits
	// its pool; a scene whose map does not needs pnx_map_stream_now before the first frame,
	// which is the sequence every scene entry has to follow either way.
	PnxMap* scene_map = pnx_scene_map();
	if (scene_map)
	{
		A_CHECK(pnx_map_resident(scene_map) > 0);
		A_CHECK_EQ(pnx_map_stream_now(scene_map, 0, 0, 200, 228), 0);
		A_CHECK(pnx_map_atlas(scene_map, pnx_map_tile(scene_map, 1, 1), NULL) != NULL);
	}

	// Fonts load as scene assets like anything else, and the metrics the runtime reads
	// must match what the pipeline wrote into the header.
	A_CHECK_EQ(pnx_scene_font_count(), 2);
	const PnxFont* hud = pnx_scene_font(0);
	A_CHECK(hud != NULL);
	if (hud)
	{
		A_CHECK_EQ(hud->line_height, FONT_HUD_LINE_HEIGHT);
		A_CHECK_EQ(hud->baseline, FONT_HUD_BASELINE);
		A_CHECK_EQ(hud->glyph_count, FONT_HUD_GLYPHS);
		A_CHECK_EQ(hud->depth, FONT_HUD_DEPTH);
		// `extra` put digits in the HUD face that no dialog page contains; the dialogue
		// face, derived from dialog alone, has no '7' and must fall back rather than
		// index past its glyph table.
		A_CHECK(pnx_font_glyph_index(hud, '7') != hud->fallback);
	}
	const PnxFont* dlg = pnx_scene_font(1);
	A_CHECK(dlg != NULL);
	if (dlg)
		A_CHECK_EQ(dlg->depth, FONT_DIALOGUE_DEPTH);
	A_CHECK(pnx_scene_font(2) == NULL);

	// Loading another scene must release the first entirely rather than accumulating.
	const size_t after_outdoor = arena.used;
	A_CHECK(pnx_scene_load(SCENE_CAVE));
	A_CHECK_EQ(pnx_scene_sprite_count(), 1); // cave declares no npc
	A_CHECK(pnx_scene_dialog() == NULL);	 // nor dialog
	A_CHECK_EQ(pnx_scene_font_count(), 1);	 // HUD only: no conversations here
	if (pnx_scene_map())
		A_CHECK_EQ(pnx_scene_map()->w, MAP_CAVE_W);
	A_CHECK(arena.used < after_outdoor); // smaller scene, less arena

	// Reloading the bigger scene must fit again, which it cannot if resets leak.
	A_CHECK(pnx_scene_load(SCENE_OUTDOOR));
	A_CHECK_EQ(arena.used, after_outdoor);

	A_CHECK(!pnx_scene_load(99));

	pnx_arena_destroy(&arena);
	pnx_arena_destroy(&persistent);
}
