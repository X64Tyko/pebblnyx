// Host tests for pnx_layer.c: parallax/callback dispatch and sprite-layer filtering.
//
// The sprite-layer half needs real, scene-loaded PnxSprite data -- pnx_scene_sprite only
// resolves indices a real pnx_scene_load populated -- so this reuses the same
// examples/overworld fixture test_assets.c already loads real blobs from, rather than
// fabricating sprite bytes a mock loader would only prove agrees with itself.

#include "../src/pnx/core/pnx_arena.h"
#include "../src/pnx/gfx/pnx_layer.h"
#include "../src/pnx/platform/pnx_platform.h"
#include "../src/pnx/platform/pnx_platform_host.h"

#include <stdio.h>
#include <string.h>

extern int s_failures;
extern int s_checks;

#define L_CHECK(cond)                                                \
	do                                                               \
	{                                                                \
		s_checks++;                                                  \
		if (!(cond))                                                 \
		{                                                            \
			printf("  FAIL %s:%d  %s\n", __FILE__, __LINE__, #cond); \
			s_failures++;                                            \
		}                                                            \
	} while (0)

#define L_CHECK_EQ(a, b)                                                                     \
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
#include "../examples/overworld/src/c/assets_gen.h"

#define A_SCENES PNX_ASSET_SCENES_SCENES
#define A_COUNT	 PNX_ASSET_COUNT

enum
{
	SCENE_CAVE,
	SCENE_OUTDOOR
}; // pipeline sorts scene names alphabetically -- see test_assets.c's own comment

static uint32_t s_resources[A_COUNT];
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
			printf("  SKIP layer: %s not built -- run tools/pnx_assets.py\n", s_asset_paths[i]);
			return false;
		}
		fclose(f);
		pnx_host_register_resource(s_resources[i], s_asset_paths[i]);
	}
	return true;
}

static int ink_in(PnxTarget* t, int16_t x0, int16_t y0, int16_t x1, int16_t y1,
				  uint8_t background)
{
	int n = 0;
	for (int16_t y = y0; y <= y1; y++)
	{
		PnxRow row = pnx_target_row(t, y);
		if (!row.data)
			continue;
		for (int16_t x = x0; x <= x1; x++)
			if (x >= row.min_x && x <= row.max_x && row.data[x] != background)
				n++;
	}
	return n;
}

// --- callback dispatch: order, ctx, and parallax scaling, with no sprite involved at all.

typedef struct
{
	void* ctx;
	int32_t cam_x, cam_y;
} CallbackRecord;

static CallbackRecord s_records[4];
static int s_record_count;

static void record_draw(void* ctx, PnxTarget* t, const PnxCamera* cam)
{
	(void)t;
	if (s_record_count < 4)
	{
		s_records[s_record_count].ctx	= ctx;
		s_records[s_record_count].cam_x = cam->x;
		s_records[s_record_count].cam_y = cam->y;
	}
	s_record_count++;
}

static void test_callback_layers(PnxTarget* t)
{
	int marker = 0;
	PnxCamera cam;
	cam.x	   = 100;
	cam.y	   = 50;
	cam.view_w = 200;
	cam.view_h = 228;

	// clang-format off
	const PnxLayer layers[4] = {
		{ .kind = PNX_LAYER_CALLBACK, .parallax_pct_x = PNX_LAYER_PARALLAX_WORLD,  .parallax_pct_y = PNX_LAYER_PARALLAX_WORLD,  .as.draw = record_draw },
		{ .kind = PNX_LAYER_CALLBACK, .parallax_pct_x = 128,                      .parallax_pct_y = 128,                      .as.draw = record_draw },
		{ .kind = PNX_LAYER_CALLBACK, .parallax_pct_x = PNX_LAYER_PARALLAX_SCREEN, .parallax_pct_y = PNX_LAYER_PARALLAX_SCREEN, .as.draw = record_draw },
		// Independent axes: a horizon strip that scrolls with curve (X) but never
		// vertically (Y=SCREEN) is exactly the case a single shared rate couldn't
		// express -- this is the capability the X/Y split exists for.
		{ .kind = PNX_LAYER_CALLBACK, .parallax_pct_x = PNX_LAYER_PARALLAX_WORLD,  .parallax_pct_y = PNX_LAYER_PARALLAX_SCREEN, .as.draw = record_draw },
	};
	// clang-format on

	s_record_count = 0;
	pnx_layers_draw(&marker, layers, 4, NULL, 0, NULL, t, &cam);

	L_CHECK_EQ(s_record_count, 4); // array order, one call per layer, no extras

	// Every callback layer gets the SAME ctx, unchanged -- the pnx_app.h convention.
	L_CHECK(s_records[0].ctx == &marker);
	L_CHECK(s_records[1].ctx == &marker);
	L_CHECK(s_records[2].ctx == &marker);
	L_CHECK(s_records[3].ctx == &marker);

	// WORLD: the real camera, untouched -- an existing single-layer game's one layer costs
	// nothing extra to draw through this.
	L_CHECK_EQ(s_records[0].cam_x, 100);
	L_CHECK_EQ(s_records[0].cam_y, 50);

	// 128/255 of the camera offset, integer division.
	L_CHECK_EQ(s_records[1].cam_x, (100 * 128) / 255);
	L_CHECK_EQ(s_records[1].cam_y, (50 * 128) / 255);

	// SCREEN: fixed, regardless of where the real camera is.
	L_CHECK_EQ(s_records[2].cam_x, 0);
	L_CHECK_EQ(s_records[2].cam_y, 0);

	// Independent axes: X moves 1:1 with the camera, Y stays fixed to the screen.
	L_CHECK_EQ(s_records[3].cam_x, 100);
	L_CHECK_EQ(s_records[3].cam_y, 0);
}

// --- sprite layers: "grounded" and "fliers" are two ids into ONE shared instance array.

static void test_sprite_layers(PnxTarget* t)
{
	if (!register_assets())
		return;

	PnxArena arena;
	L_CHECK(pnx_arena_init(&arena, "layer-arena", 68 * 1024, 4));
	L_CHECK(pnx_assets_init(&arena, s_resources, A_COUNT));
	L_CHECK(pnx_scenes_load(A_SCENES));
	L_CHECK(pnx_scene_load(SCENE_OUTDOOR));
	L_CHECK_EQ(pnx_scene_sprite_count(), 2);

#define LAYER_GROUND 0
#define LAYER_FLIER	 1

	const PnxSpriteInstance instances[4] = {
		{ .x = 40, .y = 60, .sprite = 0, .frame = 0, .palette = PNX_SPRITE_PALETTE_DEFAULT, .flags = LAYER_GROUND << PNX_SPRITE_LAYER_SHIFT },
		{ .x = 100, .y = 60, .sprite = 1, .frame = 0, .palette = PNX_SPRITE_PALETTE_DEFAULT, .flags = LAYER_GROUND << PNX_SPRITE_LAYER_SHIFT },
		{ .x = 160, .y = 60, .sprite = 0, .frame = 0, .palette = PNX_SPRITE_PALETTE_DEFAULT, .flags = LAYER_FLIER << PNX_SPRITE_LAYER_SHIFT },
		{ .x = 100, .y = 150, .sprite = 1, .frame = 0, .palette = PNX_SPRITE_PALETTE_DEFAULT, .flags = (uint8_t)((LAYER_FLIER << PNX_SPRITE_LAYER_SHIFT) | PNX_SPRITE_HIDDEN) },
	};
	uint8_t order[4];

	PnxCamera cam;
	pnx_camera_init(&cam, 200, 228);

	// Ground box: [32,48)x[36,60), [92,108)x[36,60) -- both hero and npc are 16x24.
	// Flier box: [152,168)x[36,60). Hidden's box: [92,108)x[126,150).
	const PnxLayer ground_only[1] = {
		{ .kind = PNX_LAYER_SPRITES, .parallax_pct_x = PNX_LAYER_PARALLAX_WORLD, .parallax_pct_y = PNX_LAYER_PARALLAX_WORLD, .as.sprite_layer = LAYER_GROUND },
	};
	pnx_gfx_clear(t, 0x40);
	pnx_layers_draw(NULL, ground_only, 1, instances, 4, order, t, &cam);
	L_CHECK(ink_in(t, 32, 36, 47, 59, 0x40) > 0);	   // grounded hero drawn
	L_CHECK(ink_in(t, 92, 36, 107, 59, 0x40) > 0);	   // grounded npc drawn
	L_CHECK_EQ(ink_in(t, 152, 36, 167, 59, 0x40), 0);  // flier NOT drawn on this layer
	L_CHECK_EQ(ink_in(t, 92, 126, 107, 149, 0x40), 0); // hidden instance never drawn

	const PnxLayer flier_only[1] = {
		{ .kind = PNX_LAYER_SPRITES, .parallax_pct_x = PNX_LAYER_PARALLAX_WORLD, .parallax_pct_y = PNX_LAYER_PARALLAX_WORLD, .as.sprite_layer = LAYER_FLIER },
	};
	pnx_gfx_clear(t, 0x40);
	pnx_layers_draw(NULL, flier_only, 1, instances, 4, order, t, &cam);
	L_CHECK_EQ(ink_in(t, 32, 36, 47, 59, 0x40), 0); // ground layer NOT drawn on this pass
	L_CHECK_EQ(ink_in(t, 92, 36, 107, 59, 0x40), 0);
	L_CHECK(ink_in(t, 152, 36, 167, 59, 0x40) > 0);	   // flier drawn
	L_CHECK_EQ(ink_in(t, 92, 126, 107, 149, 0x40), 0); // hidden stays hidden on its own layer too

#undef LAYER_GROUND
#undef LAYER_FLIER
}

void test_layer(void)
{
	printf("layer\n");

	pnx_host_reset();
	PnxTarget* t = pnx_host_target();

	test_callback_layers(t);
	test_sprite_layers(t);
}
