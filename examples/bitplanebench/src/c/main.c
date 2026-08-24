// bitplane/Elias-gamma decode cost, on the watch. This session's compression
// investigation (docs/GAME-COMPARISON.md) built pnx_bitplane.c -- bitplane-separated,
// frequency-sorted-palette, Elias-gamma-RLE pixel decoding, standalone and not wired
// into pnx_sprite_load/atlas_load_into -- and measured real byte savings against it, but
// left the one number that actually decides whether it's worth building for real: how
// long decoding one unit costs against a plain 4bpp unpack and against pnx_lzss_decode,
// on real hardware rather than an x86 host.
//
// Thirteen real units: hero/npc sprite frames and "tiles" atlas tiles pulled from
// overworld's own built resources, two blocks from Need4Pebble's traffic_car (the
// worst-case dithered content this investigation found), and three synthetic edge cases
// (solid colour, checkerboard, all 16 colours present) -- encoded offline by
// tools' bpeg_encode.py mirror (not part of the pipeline; this format isn't wired into
// it), shipped as raw resources the same way lzssbench's map banks are.
//
// Same bracket-timing shape as lzssbench, for the same reason: pnx_platform_now_ms()
// has 1 ms resolution and a single decode of a few hundred bytes is nowhere near that,
// so DECODE_BATCH calls are timed as one bracket, never a single call divided by one.
//
// HOW IT REPORTS: same posture as every other pnx bench -- screen first (logs attach
// after init runs), full sweep to the log on SELECT.

#include "pnx/pnx.h"
#include "pnx/assets/pnx_bitplane.h"
#include "pnx/assets/pnx_lzss.h"
#include "assets_gen.h"

#include <string.h>

#define SRC_BUF_BYTES 256u
#define DST_BUF_BYTES 512u
// raw_all.bin/lzss_all.bin's PACKED (2 px/byte) size, known at build time -- kept
// distinct from ALL_PIXELS on purpose: an earlier cut of this bench used the packed
// byte count as a PIXEL count in the raw-unpack loop, silently comparing half the real
// pixel content against LZSS's (correctly full) 3664-pixel decode. Real device numbers
// exposed it (the mismatch masqueraded as "raw unpack looks 2x cheaper than it is").
#define ALL_PACKED_BYTES 1832u
#define ALL_PIXELS		 (ALL_PACKED_BYTES * 2u)

#define DECODE_BATCH		   200u
#define DECODE_FRAMES_PER_UNIT 10u

#define UNIT_COUNT 13

typedef struct
{
	uint32_t resource_id;
	uint16_t n; // pixel count -- NOT stored in the blob, same as pnx_bitplane_decode's
				// own contract; this bench has to know it the same way a real caller
				// would from the unit's own metadata (frame_meta / tile_px).
	const char* name;
} UnitSpec;

typedef struct
{
	uint32_t compressed_bytes;
	uint32_t decode_ms;
	uint32_t decode_calls;
	uint32_t checksum; // XOR of every decoded byte -- keeps the loop live, not optimised out
} UnitResult;

typedef enum
{
	PHASE_IDLE,
	PHASE_PRIME_UNIT,
	PHASE_DECODE_UNIT,
	PHASE_PRIME_RAW,
	PHASE_DECODE_RAW,
	PHASE_PRIME_LZSS,
	PHASE_DECODE_LZSS,
	PHASE_DONE,
} Phase;

typedef struct
{
	PnxArena arena;
	PnxFont font;
	bool has_font;

	Phase phase;
	uint8_t unit;
	uint32_t frame_in_step;

	uint8_t src_buf[SRC_BUF_BYTES];
	uint8_t dst_buf[DST_BUF_BYTES];
	uint8_t scratch_buf[DST_BUF_BYTES]; // pnx_bitplane_decode's own working buffer, now
										// that its output is packed 4bpp (half DST_BUF_BYTES
										// would do, but this reuses the same generous bound)

	UnitResult results[UNIT_COUNT];
	uint32_t raw_ms, raw_calls, raw_checksum;
	uint32_t lzss_ms, lzss_calls, lzss_checksum;

	char status[64];
	char line[5][48];
} App;

static UnitSpec s_units[UNIT_COUNT];

static uint32_t us_per_call(uint32_t ms, uint32_t calls)
{
	return calls ? (ms * 1000u) / calls : 0u;
}

static void start_bench(App* a)
{
	memset(a->results, 0, sizeof(a->results));
	a->raw_ms = a->raw_calls = a->raw_checksum = 0;
	a->lzss_ms = a->lzss_calls = a->lzss_checksum = 0;
	a->phase									  = PHASE_PRIME_UNIT;
	a->unit										  = 0;
	a->frame_in_step							  = 0;

	pnx_diag_flush();
	pnx_log("bitplane: run started -- %u units + raw + lzss", UNIT_COUNT);
}

// One unit's worth of read + decode, spread across PHASE_PRIME_UNIT (one frame) and
// PHASE_DECODE_UNIT (DECODE_FRAMES_PER_UNIT frames). Returns true once fully measured.
static bool step_unit(App* a)
{
	UnitResult* r = &a->results[a->unit];
	uint32_t rid  = s_units[a->unit].resource_id;

	if (a->phase == PHASE_PRIME_UNIT)
	{
		size_t sz = 0;
		pnx_platform_resource_size(rid, &sz);
		if (sz > SRC_BUF_BYTES)
			sz = SRC_BUF_BYTES; // cannot happen against real content; a bound, not a guess
		pnx_platform_resource_read(rid, 0, a->src_buf, sz);
		r->compressed_bytes = (uint32_t)sz;

		a->phase		 = PHASE_DECODE_UNIT;
		a->frame_in_step = 0;
		return false;
	}

	const uint16_t packed_len = (uint16_t)((s_units[a->unit].n + 1) / 2);
	const uint32_t t0		  = pnx_platform_now_ms();
	for (uint32_t i = 0; i < DECODE_BATCH; i++)
	{
		memset(a->dst_buf, 0, packed_len);
		const bool ok = pnx_bitplane_decode(a->src_buf, r->compressed_bytes, a->dst_buf,
											a->scratch_buf, s_units[a->unit].n);
		if (ok)
			for (uint16_t j = 0; j < packed_len; j++)
				r->checksum ^= a->dst_buf[j];
	}
	const uint32_t t1 = pnx_platform_now_ms();
	r->decode_ms += (t1 - t0);
	r->decode_calls += DECODE_BATCH;

	a->frame_in_step++;
	if (a->frame_in_step < DECODE_FRAMES_PER_UNIT)
		return false;

	pnx_log("bp %-9s n=%3u %uB %uus*%u cs=%02x", s_units[a->unit].name,
			(unsigned)s_units[a->unit].n, (unsigned)r->compressed_bytes,
			(unsigned)us_per_call(r->decode_ms, r->decode_calls), (unsigned)r->decode_calls,
			(unsigned)(r->checksum & 0xFF));

	a->phase = PHASE_PRIME_UNIT;
	return true;
}

static void frame(void* ctx, uint32_t elapsed_ms, PnxTarget* target)
{
	App* a					  = (App*)ctx;
	const uint32_t work_start = pnx_platform_now_ms();
	(void)elapsed_ms;

	PnxEvent ev;
	while (pnx_platform_poll_event(&ev))
	{
		if (ev.type == PNX_EVENT_BUTTON_DOWN && ev.button == PNX_BUTTON_SELECT &&
			(a->phase == PHASE_IDLE || a->phase == PHASE_DONE))
			start_bench(a);
	}

	pnx_gfx_clear(target, 0xC0);

	if (a->phase == PHASE_PRIME_UNIT || a->phase == PHASE_DECODE_UNIT)
	{
		if (step_unit(a))
		{
			a->unit++;
			if (a->unit >= UNIT_COUNT)
			{
				a->phase		 = PHASE_PRIME_RAW;
				a->frame_in_step = 0;
			}
		}
	}
	else if (a->phase == PHASE_PRIME_RAW)
	{
		a->phase		 = PHASE_DECODE_RAW;
		a->frame_in_step = 0;
	}
	else if (a->phase == PHASE_DECODE_RAW)
	{
		static uint8_t packed[ALL_PACKED_BYTES];
		static uint8_t unpacked[ALL_PIXELS];
		pnx_platform_resource_read(RESOURCE_ID_RAW_ALL, 0, packed, sizeof(packed));

		const uint32_t t0 = pnx_platform_now_ms();
		for (uint32_t i = 0; i < DECODE_BATCH; i++)
		{
			for (uint32_t j = 0; j < ALL_PIXELS; j++)
				unpacked[j] = (j & 1) == 0 ? (packed[j / 2] >> 4) : (packed[j / 2] & 0x0F);
			a->raw_checksum ^= unpacked[0];
		}
		const uint32_t t1 = pnx_platform_now_ms();
		a->raw_ms += (t1 - t0);
		a->raw_calls += DECODE_BATCH;

		a->frame_in_step++;
		if (a->frame_in_step >= DECODE_FRAMES_PER_UNIT)
		{
			pnx_log("raw unpack %uB->%upx %uus*%u", ALL_PACKED_BYTES, ALL_PIXELS,
					(unsigned)us_per_call(a->raw_ms, a->raw_calls), (unsigned)a->raw_calls);
			a->phase		 = PHASE_PRIME_LZSS;
			a->frame_in_step = 0;
		}
	}
	else if (a->phase == PHASE_PRIME_LZSS)
	{
		a->phase		 = PHASE_DECODE_LZSS;
		a->frame_in_step = 0;
	}
	else if (a->phase == PHASE_DECODE_LZSS)
	{
		static uint8_t lzss_src[ALL_PACKED_BYTES];
		static uint8_t lzss_dst[ALL_PACKED_BYTES];
		size_t lzss_sz = 0;
		pnx_platform_resource_size(RESOURCE_ID_LZSS_ALL, &lzss_sz);
		if (lzss_sz > sizeof(lzss_src))
			lzss_sz = sizeof(lzss_src);
		pnx_platform_resource_read(RESOURCE_ID_LZSS_ALL, 0, lzss_src, lzss_sz);

		const uint32_t t0 = pnx_platform_now_ms();
		for (uint32_t i = 0; i < DECODE_BATCH; i++)
		{
			const size_t got = pnx_lzss_decode(lzss_src, lzss_sz, lzss_dst, sizeof(lzss_dst));
			a->lzss_checksum ^= (uint32_t)got;
		}
		const uint32_t t1 = pnx_platform_now_ms();
		a->lzss_ms += (t1 - t0);
		a->lzss_calls += DECODE_BATCH;

		a->frame_in_step++;
		if (a->frame_in_step >= DECODE_FRAMES_PER_UNIT)
		{
			pnx_log("lzss whole-blob %uB->%upx %uus*%u", ALL_PACKED_BYTES, ALL_PIXELS,
					(unsigned)us_per_call(a->lzss_ms, a->lzss_calls), (unsigned)a->lzss_calls);

			// Pixel-weighted, not a naive per-call average: units range from 80 to 384
			// pixels, so summing/averaging decode_ms across them directly would let the
			// small ones and large ones distort each other's contribution. sum(time) /
			// sum(pixels) -- decode_calls/raw_calls/lzss_calls are already TOTAL call
			// counts (accumulated across all DECODE_FRAMES_PER_UNIT batches, see
			// step_unit), not batch counts, so multiplying by n directly (no further
			// division by DECODE_BATCH -- an earlier cut of this line divided by it a
			// second time and inflated every ns/pixel figure here ~200x, caught by a
			// real device run whose per-unit lines were sane but SUMMARY wasn't) gives
			// total pixels actually processed. uint64_t intermediates: total ms * 1e6
			// gets within single digits of uint32_t's ceiling on a slow sweep.
			uint64_t bp_total_ms = 0, bp_total_px = 0;
			for (uint8_t i = 0; i < UNIT_COUNT; i++)
			{
				bp_total_ms += a->results[i].decode_ms;
				bp_total_px += (uint64_t)s_units[i].n * a->results[i].decode_calls;
			}
			const uint32_t bp_ns_px	  = bp_total_px ? (uint32_t)((bp_total_ms * 1000000u) / bp_total_px) : 0;
			const uint32_t raw_ns_px  = a->raw_calls
				? (uint32_t)(((uint64_t)a->raw_ms * 1000000u) / ((uint64_t)ALL_PIXELS * a->raw_calls))
				: 0;
			const uint32_t lzss_ns_px = a->lzss_calls
				? (uint32_t)(((uint64_t)a->lzss_ms * 1000000u) / ((uint64_t)ALL_PIXELS * a->lzss_calls))
				: 0;
			pnx_log("SUMMARY bp=%uns/px raw=%uns/px lzss=%uns/px(wholeblob)", (unsigned)bp_ns_px,
					(unsigned)raw_ns_px, (unsigned)lzss_ns_px);

			a->phase = PHASE_DONE;
		}
	}

	if (a->has_font)
	{
		switch (a->phase)
		{
			case PHASE_IDLE:
				pnx_format(a->status, sizeof(a->status), "SELECT to start (attach logs first)");
				break;
			case PHASE_PRIME_UNIT:
			case PHASE_DECODE_UNIT:
				pnx_format(a->status, sizeof(a->status), "unit %u/%u %s", (unsigned)a->unit + 1,
						   UNIT_COUNT, s_units[a->unit].name);
				break;
			case PHASE_PRIME_RAW:
			case PHASE_DECODE_RAW:
				pnx_format(a->status, sizeof(a->status), "raw unpack sweep");
				break;
			case PHASE_PRIME_LZSS:
			case PHASE_DECODE_LZSS:
				pnx_format(a->status, sizeof(a->status), "lzss sweep");
				break;
			case PHASE_DONE:
				pnx_format(a->status, sizeof(a->status), "done -- see log -- SELECT to rerun");
				break;
		}
		pnx_text_draw(target, &a->font, "pnx bitplanebench", 10, 20, 0xFF);
		pnx_text_draw(target, &a->font, a->status, 10, 40, 0xC7);

		if (a->phase == PHASE_PRIME_UNIT || a->phase == PHASE_DECODE_UNIT)
		{
			const UnitResult* r = &a->results[a->unit];
			pnx_format(a->line[0], sizeof(a->line[0]), "%uB  %u calls", (unsigned)r->compressed_bytes,
					   (unsigned)r->decode_calls);
			pnx_format(a->line[1], sizeof(a->line[1]), "%uus/call",
					   (unsigned)us_per_call(r->decode_ms, r->decode_calls));
			pnx_text_draw(target, &a->font, a->line[0], 10, 62, 0xFF);
			pnx_text_draw(target, &a->font, a->line[1], 10, 78, 0xFF);
		}
	}

	pnx_diag_frame(elapsed_ms, pnx_platform_now_ms() - work_start);
}

int main(void)
{
	static App a;
	memset(&a, 0, sizeof(a));

	if (!pnx_arena_init_max(&a.arena, "app", PNX_ARENA_HEAP_RESERVE, 4))
	{
		pnx_platform_log("arena init failed");
		return 1;
	}

	static const uint32_t RESOURCES[] = PNX_ASSET_RESOURCE_TABLE;
	pnx_assets_init(&a.arena, RESOURCES, PNX_ASSET_COUNT);

	s_units[0]	= (UnitSpec){ RESOURCE_ID_BPEG_HERO0, 384, "hero0" };
	s_units[1]	= (UnitSpec){ RESOURCE_ID_BPEG_HERO1, 384, "hero1" };
	s_units[2]	= (UnitSpec){ RESOURCE_ID_BPEG_HERO2, 384, "hero2" };
	s_units[3]	= (UnitSpec){ RESOURCE_ID_BPEG_NPC0, 384, "npc0" };
	s_units[4]	= (UnitSpec){ RESOURCE_ID_BPEG_TILE0, 256, "tile0" };
	s_units[5]	= (UnitSpec){ RESOURCE_ID_BPEG_TILE5, 256, "tile5" };
	s_units[6]	= (UnitSpec){ RESOURCE_ID_BPEG_TILE10, 256, "tile10" };
	s_units[7]	= (UnitSpec){ RESOURCE_ID_BPEG_TILE20, 256, "tile20" };
	s_units[8]	= (UnitSpec){ RESOURCE_ID_BPEG_CAR0, 256, "car0" };
	s_units[9]	= (UnitSpec){ RESOURCE_ID_BPEG_CAR3, 80, "car3" };
	s_units[10] = (UnitSpec){ RESOURCE_ID_BPEG_SOLID, 256, "solid" };
	s_units[11] = (UnitSpec){ RESOURCE_ID_BPEG_CHECKER, 256, "checker" };
	s_units[12] = (UnitSpec){ RESOURCE_ID_BPEG_ALL16, 256, "all16" };

	a.has_font = pnx_font_load(&a.font, PNX_ASSET_FONT_BENCH);
	if (!a.has_font)
		pnx_log("bitplanebench: font would not load -- nothing to draw");

	a.phase = PHASE_IDLE;

	pnx_platform_run(frame, &a);

	pnx_arena_destroy(&a.arena);
	return 0;
}
