// Flash read benchmark v3: does cost scale with a resource's OWN size, or with how much
// OTHER content precedes it in the .pbpack?
//
// v2 (see MEASUREMENTS.md's "Flash / resource reads") read five resources of different
// total sizes and found cost scaling with size, flat across offset within a resource. But
// it declared those five in ascending order, and `pbpack.py`'s own `finalize()` assigns
// offsets by walking resources in DECLARATION order, each placed right after the last --
// so the biggest resource was also always the one with the most other content before it
// in the pack. Size and pack position were the same variable wearing two names; v2 could
// not tell them apart.
//
// This version can: five IDENTICAL 8KB probes (the real WorldTile bank size), each
// declared behind a different amount of padding -- 0, 50, 100, 150, 200 KB of other
// content before it. Same size throughout. If cost still grows across the five, it is
// pack POSITION driving it, not the probe's own size -- and that would also explain the
// original WorldTile finding cleanly: maps are declared, and so packed, after their
// atlases, so a map read always has more preceding content than an atlas read, regardless
// of the map's own size. If the five come back flat, v2's size-only model holds and this
// closes the question instead of reopening it.
//
// Getting the numbers off the watch: same discipline as the other pnx benchmarks. Waits
// on SELECT so `pebble logs` can be attached first; SELECT flushes to pass-through
// immediately; every point logs its own result; every log line stays under 96 chars.

#include "pnx/pnx.h"
#include "assets_gen.h"

#include <string.h>

#define PERSIST_BYTES 512
#define SCENE_BYTES	  (4 * 1024)

#define POINT_COUNT 5
// Bytes of other resource content declared -- and so packed -- before each probe. Not
// read directly; this is what the read cost is being plotted against.
static const uint32_t PRECEDING_KB[POINT_COUNT] = { 0, 50, 100, 150, 200 };

static uint32_t s_probe_ids[POINT_COUNT];

#define READ_LEN 256u // fixed and small: this run is not about length or in-resource
					  // offset, both already settled by v1/v2 -- every probe is read
					  // at its own offset 0
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
	char result[POINT_COUNT][56];
	char fit[64];
} App;

static uint32_t per_call_us(uint32_t total_ms, uint32_t calls)
{
	return calls ? (total_ms * 1000u) / calls : 0;
}

static void start_bench(App* a)
{
	a->phase	   = PHASE_WARMUP;
	a->phase_frame = 0;
	a->point	   = 0;
	memset(a->ms, 0, sizeof(a->ms));
	memset(a->calls, 0, sizeof(a->calls));
	memset(a->mismatches, 0, sizeof(a->mismatches));
	for (int i = 0; i < POINT_COUNT; i++)
		a->result[i][0] = '\0';

	pnx_diag_flush();
	pnx_log("flashbench: v3 run started -- %u probes, %uB each, %uB reads", POINT_COUNT, 8192,
			(unsigned)READ_LEN);
}

// A signed two-point estimate against PRECEDING_KB rather than size -- every probe is the
// same size, so a non-zero slope here is entirely a position effect.
static void fit_two_point(uint32_t us_first, uint32_t us_last, int32_t* out_fixed,
						  int32_t* out_slope)
{
	const int32_t dk = (int32_t)PRECEDING_KB[POINT_COUNT - 1] - (int32_t)PRECEDING_KB[0];
	const int32_t dc = (int32_t)us_last - (int32_t)us_first;
	*out_slope		 = dk ? dc / dk : 0;
	*out_fixed		 = (int32_t)us_first - (*out_slope) * (int32_t)PRECEDING_KB[0];
}

static void report_results(App* a)
{
	uint32_t us[POINT_COUNT];
	for (int p = 0; p < POINT_COUNT; p++)
	{
		us[p] = per_call_us(a->ms[p], a->calls[p]);
		pnx_format(a->result[p], sizeof(a->result[p]), "%uKB pre: %uus", PRECEDING_KB[p],
				   (unsigned)us[p]);
		pnx_log("flashbench: %uKB preceding -- %uus/call (%u reads, %u mismatches)",
				PRECEDING_KB[p], (unsigned)us[p], (unsigned)READS_PER_POINT,
				(unsigned)a->mismatches[p]);
	}

	int32_t fixed, slope;
	fit_two_point(us[0], us[POINT_COUNT - 1], &fixed, &slope);
	pnx_format(a->fit, sizeof(a->fit), "~%dus + %dus/KB preceding", (int)fixed, (int)slope);
	pnx_log("flashbench: FIT (0/%uKB preceding) ~%dus + %dus per KB preceding",
			PRECEDING_KB[POINT_COUNT - 1], (int)fixed, (int)slope);

	// A near-zero slope here means position does not matter and v2's size-only model
	// holds; a slope near v2's ~179us/KB means position was v2's real variable all along.
	const uint32_t ratio_x10 = us[0] ? (us[POINT_COUNT - 1] * 10u) / us[0] : 0;
	pnx_log("flashbench: 200KB/0KB preceding ratio %u.%ux (1.0x = position irrelevant)",
			(unsigned)(ratio_x10 / 10), (unsigned)(ratio_x10 % 10));
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

	pnx_log("flashbench: point %u/%u done -- %uKB preceding, %uus/call", (unsigned)a->point + 1,
			POINT_COUNT, PRECEDING_KB[a->point],
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
	App* a					  = (App*)ctx;
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
		const uint32_t t0 = pnx_platform_now_ms();
		const size_t got  = pnx_platform_resource_read(s_probe_ids[a->point], 0, a->buf, READ_LEN);
		const uint32_t t1 = pnx_platform_now_ms();
		a->ms[a->point] += (t1 - t0);
		a->calls[a->point]++;
		// Byte 0 of probe N is N & 0xFF (see gen_flashdata.py's salt) -- confirms this read
		// landed on the probe meant for this point, not a deduplicated stand-in for another.
		if (got != READ_LEN || a->buf[0] != (a->point & 0xFFu))
			a->mismatches[a->point]++;
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
				pnx_format(a->status, sizeof(a->status), "%uKB pre  %u/%u", PRECEDING_KB[a->point],
						   (unsigned)a->phase_frame + 1, READS_PER_POINT);
				break;
			case PHASE_DONE:
				pnx_format(a->status, sizeof(a->status), "done -- SELECT to rerun");
				break;
		}
		pnx_text_draw(target, &a->font, "pnx flashbench v3", 10, 20, 0xFF);
		pnx_text_draw(target, &a->font, a->status, 10, 40, 0xC7);

		if (a->phase == PHASE_DONE)
		{
			int16_t y = 62;
			for (int p = 0; p < POINT_COUNT; p++)
			{
				pnx_text_draw(target, &a->font, a->result[p], 10, y, 0xFF);
				y = (int16_t)(y + 16);
			}
			pnx_text_draw(target, &a->font, a->fit, 10, (int16_t)(y + 6), 0xF0);
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

	s_probe_ids[0] = RESOURCE_ID_PROBE_0;
	s_probe_ids[1] = RESOURCE_ID_PROBE_1;
	s_probe_ids[2] = RESOURCE_ID_PROBE_2;
	s_probe_ids[3] = RESOURCE_ID_PROBE_3;
	s_probe_ids[4] = RESOURCE_ID_PROBE_4;

	a.has_font = pnx_font_load(&a.font, PNX_ASSET_FONT_BENCH);
	if (!a.has_font)
		pnx_log("flashbench: font would not load -- nothing to draw");

	a.phase = PHASE_IDLE;

	pnx_platform_run(frame, &a);

	pnx_arena_destroy(&a.scene);
	pnx_arena_destroy(&a.persistent);
	return 0;
}
