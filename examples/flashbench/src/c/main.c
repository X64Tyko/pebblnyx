// Flash read benchmark v2: does cost scale with OFFSET within a resource, with the
// resource's own TOTAL SIZE, or both?
//
// v1 (see MEASUREMENTS.md's "Flash / resource reads" for its numbers) read one 220KB
// resource at seven offsets and got a flat ~40ms regardless of offset or length. That does
// not match the ORIGINAL WorldTile streaming finding, which found reads costing 46-305ms
// depending on offset within a 75KB resource -- and that finding was read off session-log
// totals (elapsed time / call count), not bracketed per call the way this app does. The
// two results cannot both be a clean O(offset) law in the same resource, so something is
// different between them, and resource SIZE is the obvious candidate: WorldTile's map was
// 75KB, v1's resource was 220KB -- 2.9x bigger, all at once, rather than swept.
//
// This version separates the two variables directly: five resources at different total
// sizes (8KB, matching a WorldTile bank -- the post-fix streaming unit; 75KB, matching the
// pre-banking map resource the original finding used; 32/150/220KB filling the curve
// between and past them), each read at a near-origin offset (0) and a two-thirds-deep one
// -- deliberately the same fraction the original finding's own language used ("two thirds
// of the way through it"). Fixed 256B length throughout, so length is not a third variable.
//
// Reading the grid: for each size, deep/near > 1 means offset matters (the WorldTile
// story); across sizes at the SAME offset fraction, growth means resource size matters
// (the hypothesis v1 could not rule out). Both, neither, or one -- the grid answers it
// rather than reasoning about it.
//
// Getting the numbers off the watch: same discipline as the other pnx benchmarks. Waits
// on SELECT so `pebble logs` can be attached first; SELECT flushes to pass-through
// immediately; every point logs its own result; every log line stays under 96 chars.

#include "pnx/pnx.h"
#include "assets_gen.h"

#include <string.h>

#define PERSIST_BYTES 512
#define SCENE_BYTES	  (4 * 1024)

#define SIZE_COUNT 5
static const uint32_t RES_BYTES[SIZE_COUNT] = {
	8 * 1024u, 32 * 1024u, 75 * 1024u, 150 * 1024u, 220 * 1024u,
};
static const char* SIZE_LABEL[SIZE_COUNT] = { "8K", "32K", "75K", "150K", "220K" };

// Resolved at runtime, not compile time -- RESOURCE_ID_* are plain uint32_t constants
// from the SDK's own generated header, not something an array initializer can rely on
// being constant-foldable across every toolchain. See main()'s populate step.
static uint32_t s_resource_ids[SIZE_COUNT];

// Fixed throughout, matching PNX_PERSIST_KEY_BYTES scale, so length is never a variable
// this grid has to account for.
#define READ_LEN 256u

// near = offset 0. deep = 66% into the resource -- "two thirds of the way through it",
// the ORIGINAL WorldTile finding's own phrase, reused deliberately for a direct comparison.
#define OFFSET_KIND_COUNT 2
static const char* OFFSET_LABEL[OFFSET_KIND_COUNT] = { "near", "deep" };

#define POINT_COUNT		(SIZE_COUNT * OFFSET_KIND_COUNT)
#define READS_PER_POINT 30
#define WARMUP_FRAMES	20

typedef enum
{
	PHASE_IDLE,
	PHASE_WARMUP,
	PHASE_RUN,
	PHASE_DONE,
} Phase;

typedef struct
{
	PnxArena persistent, scene;
	PnxFont font;
	bool has_font;

	Phase phase;
	uint32_t phase_frame;
	uint8_t point;

	uint32_t ms[POINT_COUNT], calls[POINT_COUNT], mismatches[POINT_COUNT];

	uint8_t buf[READ_LEN];

	char status[64];
	char result[SIZE_COUNT][56];
	char summary1[64], summary2[64];
} App;

static uint8_t point_size(uint8_t p)
{
	return (uint8_t)(p / OFFSET_KIND_COUNT);
}
static uint8_t point_kind(uint8_t p)
{
	return (uint8_t)(p % OFFSET_KIND_COUNT);
}

static uint32_t point_offset(uint8_t p)
{
	const uint32_t size = RES_BYTES[point_size(p)];
	return (point_kind(p) == 0) ? 0 : (size * 66u / 100u);
}

static uint32_t per_call_us(uint32_t total_ms, uint32_t calls)
{
	return calls ? (total_ms * 1000u) / calls : 0;
}

static void start_bench(App* a)
{
	a->phase = PHASE_WARMUP;
	a->phase_frame = 0;
	a->point = 0;
	memset(a->ms, 0, sizeof(a->ms));
	memset(a->calls, 0, sizeof(a->calls));
	memset(a->mismatches, 0, sizeof(a->mismatches));
	for (int i = 0; i < SIZE_COUNT; i++)
		a->result[i][0] = '\0';

	pnx_diag_flush();
	pnx_log("flashbench: run started -- %u sizes x %u offsets, %uB reads", (unsigned)SIZE_COUNT,
			(unsigned)OFFSET_KIND_COUNT, (unsigned)READ_LEN);
}

static void report_results(App* a)
{
	for (int s = 0; s < SIZE_COUNT; s++)
	{
		const uint32_t near_us = per_call_us(a->ms[s * 2], a->calls[s * 2]);
		const uint32_t deep_us = per_call_us(a->ms[s * 2 + 1], a->calls[s * 2 + 1]);
		pnx_format(a->result[s], sizeof(a->result[s]), "%s: near=%u deep=%u", SIZE_LABEL[s],
				   (unsigned)near_us, (unsigned)deep_us);
		pnx_log("flashbench: %s -- near %uus/call, deep %uus/call (%u reads each)",
				SIZE_LABEL[s], (unsigned)near_us, (unsigned)deep_us, (unsigned)READS_PER_POINT);
	}

	// Does offset matter, within a size? Ratio deep/near, x10 fixed point, per size.
	for (int s = 0; s < SIZE_COUNT; s++)
	{
		const uint32_t near_us = per_call_us(a->ms[s * 2], a->calls[s * 2]);
		const uint32_t deep_us = per_call_us(a->ms[s * 2 + 1], a->calls[s * 2 + 1]);
		const uint32_t ratio_x10 = near_us ? (deep_us * 10u) / near_us : 0;
		pnx_log("flashbench: %s deep/near ratio %u.%ux", SIZE_LABEL[s],
				(unsigned)(ratio_x10 / 10), (unsigned)(ratio_x10 % 10));
	}

	// Does resource SIZE matter, at the same offset fraction? Smallest vs largest, both
	// offset kinds.
	const uint32_t near_small = per_call_us(a->ms[0], a->calls[0]);
	const uint32_t near_large =
		per_call_us(a->ms[(SIZE_COUNT - 1) * 2], a->calls[(SIZE_COUNT - 1) * 2]);
	const uint32_t deep_small = per_call_us(a->ms[1], a->calls[1]);
	const uint32_t deep_large =
		per_call_us(a->ms[(SIZE_COUNT - 1) * 2 + 1], a->calls[(SIZE_COUNT - 1) * 2 + 1]);
	pnx_format(a->summary1, sizeof(a->summary1), "near: 8K=%u 220K=%u", (unsigned)near_small,
			   (unsigned)near_large);
	pnx_format(a->summary2, sizeof(a->summary2), "deep: 8K=%u 220K=%u", (unsigned)deep_small,
			   (unsigned)deep_large);
	pnx_log("flashbench: size effect -- near 8K=%uus 220K=%uus, deep 8K=%uus 220K=%uus",
			(unsigned)near_small, (unsigned)near_large, (unsigned)deep_small,
			(unsigned)deep_large);
}

static void advance(App* a)
{
	a->phase_frame++;
	const uint32_t length = (a->phase == PHASE_WARMUP) ? WARMUP_FRAMES : READS_PER_POINT;
	if (a->phase_frame < length)
		return;
	a->phase_frame = 0;

	if (a->phase == PHASE_WARMUP)
	{
		a->phase = PHASE_RUN;
		return;
	}
	if (a->phase != PHASE_RUN)
		return;

	pnx_log("flashbench: point %u/%u done -- %s %s, %uus/call", (unsigned)a->point + 1,
			POINT_COUNT, SIZE_LABEL[point_size(a->point)], OFFSET_LABEL[point_kind(a->point)],
			(unsigned)per_call_us(a->ms[a->point], a->calls[a->point]));

	a->point++;
	if (a->point >= POINT_COUNT)
	{
		a->phase = PHASE_DONE;
		report_results(a);
	}
}

static void frame(void* ctx, uint32_t elapsed_ms, PnxTarget* target)
{
	App* a = (App*)ctx;
	const uint32_t work_start = pnx_platform_now_ms();

	PnxEvent ev;
	while (pnx_platform_poll_event(&ev))
	{
		if (ev.type == PNX_EVENT_BUTTON_DOWN && ev.button == PNX_BUTTON_SELECT &&
			(a->phase == PHASE_IDLE || a->phase == PHASE_DONE))
		{
			start_bench(a);
		}
	}

	pnx_gfx_clear(target, 0xC0);

	if (a->phase == PHASE_RUN)
	{
		const uint8_t size_idx = point_size(a->point);
		const uint32_t offset = point_offset(a->point);
		const uint32_t t0 = pnx_platform_now_ms();
		const size_t got =
			pnx_platform_resource_read(s_resource_ids[size_idx], offset, a->buf, READ_LEN);
		const uint32_t t1 = pnx_platform_now_ms();
		a->ms[a->point] += (t1 - t0);
		a->calls[a->point]++;
		if (got != READ_LEN || a->buf[0] != (uint8_t)(offset & 0xFF))
		{
			a->mismatches[a->point]++;
		}
	}

	if (a->has_font)
	{
		switch (a->phase)
		{
			case PHASE_IDLE:
				pnx_format(a->status, sizeof(a->status), "SELECT to start (attach logs first)");
				break;
			case PHASE_WARMUP:
				pnx_format(a->status, sizeof(a->status), "starting...");
				break;
			case PHASE_RUN:
				pnx_format(a->status, sizeof(a->status), "%s %s  %u/%u",
						   SIZE_LABEL[point_size(a->point)], OFFSET_LABEL[point_kind(a->point)],
						   (unsigned)a->phase_frame + 1, READS_PER_POINT);
				break;
			case PHASE_DONE:
				pnx_format(a->status, sizeof(a->status), "done -- SELECT to rerun");
				break;
		}
		pnx_text_draw(target, &a->font, "pnx flashbench v2", 10, 20, 0xFF);
		pnx_text_draw(target, &a->font, a->status, 10, 40, 0xC7);

		if (a->phase == PHASE_DONE)
		{
			int16_t y = 62;
			for (int i = 0; i < SIZE_COUNT; i++)
			{
				pnx_text_draw(target, &a->font, a->result[i], 10, y, 0xFF);
				y = (int16_t)(y + 16);
			}
			pnx_text_draw(target, &a->font, a->summary1, 10, (int16_t)(y + 6), 0xF0);
			pnx_text_draw(target, &a->font, a->summary2, 10, (int16_t)(y + 22), 0xF0);
		}
	}

	advance(a);
	pnx_diag_frame(elapsed_ms, pnx_platform_now_ms() - work_start);
}

int main(void)
{
	static App a;
	memset(&a, 0, sizeof(a));

	if (!pnx_arena_init(&a.persistent, "persistent", PERSIST_BYTES, 4) ||
		!pnx_arena_init(&a.scene, "scene", SCENE_BYTES, 4))
	{
		pnx_platform_log("arena init failed");
		return 1;
	}

	static const uint32_t RESOURCES[] = PNX_ASSET_RESOURCE_TABLE;
	pnx_assets_init(&a.persistent, &a.scene, RESOURCES, PNX_ASSET_COUNT);

	s_resource_ids[0] = RESOURCE_ID_FLASHDATA_8K;
	s_resource_ids[1] = RESOURCE_ID_FLASHDATA_32K;
	s_resource_ids[2] = RESOURCE_ID_FLASHDATA_75K;
	s_resource_ids[3] = RESOURCE_ID_FLASHDATA_150K;
	s_resource_ids[4] = RESOURCE_ID_FLASHDATA_220K;

	a.has_font = pnx_font_load(&a.font, PNX_ASSET_FONT_BENCH);
	if (!a.has_font)
		pnx_log("flashbench: font would not load -- nothing to draw");

	a.phase = PHASE_IDLE;

	pnx_platform_run(frame, &a);

	pnx_arena_destroy(&a.scene);
	pnx_arena_destroy(&a.persistent);
	return 0;
}
