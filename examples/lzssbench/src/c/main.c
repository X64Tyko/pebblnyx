// LZSS decode cost, on the watch. M12 (docs/ROADMAP.md) built cell-dictionary + LZSS map
// compression and measured its resource-size and runtime-code-size cost for real, but left
// one number unmeasured: how long `pnx_lzss_decode` actually takes when a compressed bank
// streams in. "Small" was never a measured claim -- this is what makes it one.
//
// Fourteen real, pipeline-built banks across four map sizes (16x16 up to 192x192, the same
// footprint as `worldtiles/plain`'s field -- the top of this sweep is not a hypothetical),
// each read once from flash and then decoded many times over, timed by REPETITION for the
// same reason every other pnx spike is: `pnx_platform_now_ms()` has 1 ms resolution and a
// single decode of a few hundred bytes is microseconds. Decode work is batched several
// calls at a time within one frame, timed as one bracket -- never a single call divided by
// one, and never spread thin enough to make the watchdog a factor.
//
// Read cost is sampled too, once per bank (flash reads already have their own measured
// cost model in MEASUREMENTS.md; this app is not re-deriving it, only sanity-checking a
// real compressed bank's read against it while it is already in hand).
//
// HOW IT REPORTS, AND WHY: everything the framework's other benches already settled.
// `pebble install --logs` attaches after init() runs, so results go to the SCREEN first
// and the log second, and the log is only flushed (pnx_diag_flush) on the SELECT press
// that starts the run, long after init. SELECT re-runs the whole sweep.
//
// WHICH PLATFORM this runs on is not printed -- the framework does not expose one, and
// game code reaching past it for `PBL_PLATFORM_*` is exactly the boundary pnx.h keeps
// closed. Whoever is reading the log already knows which emulator or device it came from.

#include "pnx/pnx.h"
#include "pnx/assets/pnx_lzss.h"
#include "assets_gen.h"

#include <string.h>

#define PERSIST_BYTES 512
#define SCENE_BYTES	  (4 * 1024)

// The largest bank in this sweep is ~531 B compressed, ~4.1 KB decoded (see the resource
// budget the pipeline printed when this was built) -- both buffers carry headroom over
// that, but not much: `aplite`'s entire app heap is a few KB (docs/ROADMAP.md's platform
// table), and this bench has to fit there too, not just on the roomier platforms.
#define SRC_BUF_BYTES 768u
#define DST_BUF_BYTES 4352u

// 200 decodes timed as one bracket, 10 brackets (frames) per bank -- 2000 calls total.
// The FIRST cut of this bench used 10 calls/bracket and got real, noisy numbers back on
// `basalt` (cortex-m4): a bank that decodes in low single-digit microseconds means 10
// calls can finish inside the SAME 1 ms tick they started in more often than not, so most
// brackets read back as 0 ms and the rest as 1 ms of pure quantisation, not signal. 200
// calls pushes a fast platform's bracket total well clear of that floor while a
// pessimistic 200 us/call (far above anything measured on any platform this framework
// targets) still keeps one bracket to ~40 ms -- inside a real frame budget, and nowhere
// near the watchdog even if it were not.
#define DECODE_BATCH		   200u
#define DECODE_FRAMES_PER_BANK 10u

#define BANK_COUNT 14

typedef struct
{
	uint32_t resource_id;
	uint8_t group; // index into GROUP_SIZE / GROUP_LABEL
} BankSpec;

// Filled in main() -- RESOURCE_ID_* are SDK macros, not compile-time array constants, so
// this cannot be `static const` at file scope without repeating every id twice.
static BankSpec s_banks[BANK_COUNT];

static const uint16_t GROUP_SIZE[4] = { 16, 32, 96, 192 };

typedef struct
{
	uint32_t compressed_bytes;
	uint32_t decoded_bytes; // 0 until the first decode of this bank returns
	uint32_t read_us;		// one sample
	uint32_t decode_ms;		// accumulated across DECODE_FRAMES_PER_BANK brackets
	uint32_t decode_calls;
	uint32_t checksum; // XOR of every decode's return value -- keeps the loop live
} BankResult;

typedef enum
{
	PHASE_IDLE,
	PHASE_PRIME, // one frame: read this bank's compressed bytes into src_buf
	PHASE_DECODE,
	PHASE_DONE,
} Phase;

typedef struct
{
	PnxArena persistent, scene;
	PnxFont font;
	bool has_font;

	Phase phase;
	uint8_t bank;			// which of BANK_COUNT is current
	uint32_t frame_in_bank; // 0-based within PHASE_DECODE for the current bank

	uint8_t src_buf[SRC_BUF_BYTES];
	uint8_t dst_buf[DST_BUF_BYTES];

	BankResult results[BANK_COUNT];

	char status[64];
	char line[5][40];
} App;

static uint32_t us_per_call(const BankResult* r)
{
	return r->decode_calls ? (r->decode_ms * 1000u) / r->decode_calls : 0u;
}

static void start_bench(App* a)
{
	memset(a->results, 0, sizeof(a->results));
	a->phase		 = PHASE_PRIME;
	a->bank			 = 0;
	a->frame_in_bank = 0;

	pnx_diag_flush();
	pnx_log("lzss: run started -- %u banks, 4 sizes", BANK_COUNT);
}

// One bank's worth of read + decode work, spread across PHASE_PRIME (one frame) and
// PHASE_DECODE (DECODE_FRAMES_PER_BANK frames). Returns true once this bank is fully
// measured and the caller should advance to the next one (or PHASE_DONE).
static bool step_bank(App* a)
{
	BankResult* r = &a->results[a->bank];
	uint32_t rid  = s_banks[a->bank].resource_id;

	if (a->phase == PHASE_PRIME)
	{
		size_t sz = 0;
		pnx_platform_resource_size(rid, &sz);
		if (sz > SRC_BUF_BYTES)
			sz = SRC_BUF_BYTES; // cannot happen against real content; a bound, not a guess

		const uint32_t t0 = pnx_platform_now_ms();
		pnx_platform_resource_read(rid, 0, a->src_buf, sz);
		const uint32_t t1 = pnx_platform_now_ms();
		// A single 1ms-quantised sample -- a sanity check against MEASUREMENTS.md's own
		// flash-read model, not a re-measurement of it. Logged as-is, quantisation and all.
		r->read_us = (t1 - t0) * 1000u;

		// Every bank resource carries the same PNX_BLOB_HEADER_BYTES stamp (magic/
		// orientation) every blob does, ahead of its LZSS body -- pnx_assets.c's own
		// runtime loader skips exactly this many bytes before decoding a bank, so this
		// bench has to as well or every stream "decodes" starting mid-token.
		r->compressed_bytes =
			(sz > PNX_BLOB_HEADER_BYTES) ? (uint32_t)(sz - PNX_BLOB_HEADER_BYTES) : 0u;

		a->phase		 = PHASE_DECODE;
		a->frame_in_bank = 0;
		return false;
	}

	// PHASE_DECODE: one timed bracket of DECODE_BATCH calls per frame.
	const uint32_t t0 = pnx_platform_now_ms();
	for (uint32_t i = 0; i < DECODE_BATCH; i++)
	{
		size_t got = pnx_lzss_decode(a->src_buf + PNX_BLOB_HEADER_BYTES, r->compressed_bytes,
									 a->dst_buf, DST_BUF_BYTES);
		r->checksum ^= (uint32_t)got;
		r->decoded_bytes = (uint32_t)got; // same every call; cheap to just keep overwriting
	}
	const uint32_t t1 = pnx_platform_now_ms();
	r->decode_ms += (t1 - t0);
	r->decode_calls += DECODE_BATCH;

	a->frame_in_bank++;
	if (a->frame_in_bank < DECODE_FRAMES_PER_BANK)
		return false;

	// This bank is done. Log it now rather than batching every log to the end -- if a
	// device build gets killed partway through a slow platform's sweep, whatever ran is
	// still on record instead of lost with the rest.
	//
	// Kept well under PNX_LOG_LINE_LEN (96 B, pnx_diag.c) on purpose: the first cut of this
	// line spelled every field out ("lzssbench: 192x192 bank 13  ... decode 25us/call (2000
	// calls)  sum 00000000") and silently lost its tail to that truncation on every
	// platform this ran on -- worst case here landed right at 96-100 B depending on how
	// many digits the numbers happened to have that run, so it was not even a consistent
	// cut. This form's worst case (4-digit read us, 3-digit decode us, 8 hex digits) stays
	// under 60 B.
	pnx_log("lzss s%u b%u %uB->%uB r%uus %uus*%u cs=%08x",
			(unsigned)GROUP_SIZE[s_banks[a->bank].group], (unsigned)a->bank,
			(unsigned)r->compressed_bytes, (unsigned)r->decoded_bytes, (unsigned)r->read_us,
			(unsigned)us_per_call(r), (unsigned)r->decode_calls, (unsigned)r->checksum);

	a->phase = PHASE_PRIME;
	return true;
}

// A signed two-point estimate, same shape as flashbench's fit_two_point: the smallest and
// largest DECODED bank in the sweep, fit to a fixed cost plus a per-byte slope. Decoded
// bytes rather than compressed bytes because that is what a developer sizing a map is
// actually choosing -- cell count, not how well it happened to compress.
static void report_fit(App* a)
{
	uint32_t min_i = 0, max_i = 0;
	for (uint8_t i = 1; i < BANK_COUNT; i++)
	{
		if (a->results[i].decoded_bytes < a->results[min_i].decoded_bytes)
			min_i = i;
		if (a->results[i].decoded_bytes > a->results[max_i].decoded_bytes)
			max_i = i;
	}
	const BankResult* lo = &a->results[min_i];
	const BankResult* hi = &a->results[max_i];
	const int32_t db	 = (int32_t)hi->decoded_bytes - (int32_t)lo->decoded_bytes;
	const int32_t dc_us	 = (int32_t)us_per_call(hi) - (int32_t)us_per_call(lo);
	// ns/byte, not us/byte: a copy/literal loop over a few thousand bytes costs single-digit
	// microseconds total, so a per-byte slope in us truncates to 0 at integer precision --
	// ns is the unit that actually fits this data.
	const int32_t slope_ns = db ? (dc_us * 1000) / db : 0;
	const int32_t fixed_us =
		(int32_t)us_per_call(lo) - (slope_ns * (int32_t)lo->decoded_bytes) / 1000;

	pnx_format(a->line[3], sizeof(a->line[3]), "~%dus + %dns/B decoded", (int)fixed_us,
			   (int)slope_ns);
	pnx_log(
		"lzssbench: FIT %uB..%uB decoded -- ~%dus fixed + %dns per decoded byte "
		"(from bank %u -> bank %u)",
		(unsigned)lo->decoded_bytes, (unsigned)hi->decoded_bytes, (int)fixed_us,
		(int)slope_ns, (unsigned)min_i, (unsigned)max_i);
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

	if (a->phase == PHASE_PRIME || a->phase == PHASE_DECODE)
	{
		if (step_bank(a))
		{
			a->bank++;
			if (a->bank >= BANK_COUNT)
			{
				a->phase = PHASE_DONE;
				report_fit(a);
			}
		}
	}

	if (a->has_font)
	{
		switch (a->phase)
		{
			case PHASE_IDLE:
				pnx_format(a->status, sizeof(a->status), "SELECT to start (attach logs first)");
				break;
			case PHASE_PRIME:
			case PHASE_DECODE:
				pnx_format(a->status, sizeof(a->status), "bank %u/%u  %ux%u", (unsigned)a->bank + 1,
						   BANK_COUNT, (unsigned)GROUP_SIZE[s_banks[a->bank].group],
						   (unsigned)GROUP_SIZE[s_banks[a->bank].group]);
				break;
			case PHASE_DONE:
				pnx_format(a->status, sizeof(a->status), "done -- SELECT to rerun");
				break;
		}
		pnx_text_draw(target, &a->font, "pnx lzssbench", 10, 20, 0xFF);
		pnx_text_draw(target, &a->font, a->status, 10, 40, 0xC7);

		if (a->phase == PHASE_DECODE || a->phase == PHASE_PRIME)
		{
			const BankResult* r = &a->results[a->bank];
			pnx_format(a->line[0], sizeof(a->line[0]), "%uB->%uB", (unsigned)r->compressed_bytes,
					   (unsigned)r->decoded_bytes);
			pnx_format(a->line[1], sizeof(a->line[1]), "%u calls  %uus/call",
					   (unsigned)r->decode_calls, (unsigned)us_per_call(r));
			pnx_text_draw(target, &a->font, a->line[0], 10, 62, 0xFF);
			pnx_text_draw(target, &a->font, a->line[1], 10, 78, 0xFF);
		}
		if (a->phase == PHASE_DONE)
		{
			pnx_text_draw(target, &a->font, "see log for all 14 banks", 10, 62, 0xFF);
			pnx_text_draw(target, &a->font, a->line[3], 10, 82, 0xF0);
		}
	}

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

	s_banks[0]	= (BankSpec){ RESOURCE_ID_S16_0, 0 };
	s_banks[1]	= (BankSpec){ RESOURCE_ID_S32_0, 1 };
	s_banks[2]	= (BankSpec){ RESOURCE_ID_S96_0, 2 };
	s_banks[3]	= (BankSpec){ RESOURCE_ID_S96_1, 2 };
	s_banks[4]	= (BankSpec){ RESOURCE_ID_S96_2, 2 };
	s_banks[5]	= (BankSpec){ RESOURCE_ID_S192_0, 3 };
	s_banks[6]	= (BankSpec){ RESOURCE_ID_S192_1, 3 };
	s_banks[7]	= (BankSpec){ RESOURCE_ID_S192_2, 3 };
	s_banks[8]	= (BankSpec){ RESOURCE_ID_S192_3, 3 };
	s_banks[9]	= (BankSpec){ RESOURCE_ID_S192_4, 3 };
	s_banks[10] = (BankSpec){ RESOURCE_ID_S192_5, 3 };
	s_banks[11] = (BankSpec){ RESOURCE_ID_S192_6, 3 };
	s_banks[12] = (BankSpec){ RESOURCE_ID_S192_7, 3 };
	s_banks[13] = (BankSpec){ RESOURCE_ID_S192_8, 3 };

	a.has_font = pnx_font_load(&a.font, PNX_ASSET_FONT_BENCH);
	if (!a.has_font)
		pnx_log("lzssbench: font would not load -- nothing to draw");

	a.phase = PHASE_IDLE;

	pnx_platform_run(frame, &a);

	pnx_arena_destroy(&a.scene);
	pnx_arena_destroy(&a.persistent);
	return 0;
}
