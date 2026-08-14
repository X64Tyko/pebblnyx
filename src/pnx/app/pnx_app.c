// See pnx_app.h for the design.

#include "pnx_app.h"

#if PNX_USE_APP

#include "../core/pnx_diag.h"

#include <string.h>

static const PnxAppOps* s_stack[PNX_APP_MAX_DEPTH];
static uint8_t s_depth;
static void* s_ctx;
static uint32_t s_accumulator_ms;
static bool s_covered;

// Events forwarded past the lifecycle ones pnx_app consumes for itself. Sized the same as
// the platform queue it drains -- see EVENT_QUEUE_LEN in pnx_platform_pebble.c -- since it
// can hold at most that many before the platform's own queue would already be dropping.
#define FWD_QUEUE_LEN 16
static PnxEvent s_fwd[FWD_QUEUE_LEN];
static uint8_t s_fwd_head, s_fwd_count;

static const PnxAppOps* top(void)
{
	return s_depth ? s_stack[s_depth - 1] : NULL;
}

void pnx_app_init(void* ctx)
{
	s_ctx			 = ctx;
	s_depth			 = 0;
	s_accumulator_ms = 0;
	s_covered		 = false;
	s_fwd_head = s_fwd_count = 0;
}

void pnx_app_push(const PnxAppOps* ops)
{
	if (!ops)
		return;

	const PnxAppOps* was = top();
	if (was && was->suspend)
		was->suspend(s_ctx);

	if (s_depth >= PNX_APP_MAX_DEPTH)
	{
		pnx_log("app: push refused at depth %u", (unsigned)s_depth);
		// The state we just suspended is still on top and still correct to keep running --
		// undo the suspend rather than leave it paused for a push that never happened.
		if (was && was->resume)
			was->resume(s_ctx);
		return;
	}

	s_stack[s_depth++] = ops;
	s_accumulator_ms   = 0;
	if (ops->enter)
		ops->enter(s_ctx);
}

void pnx_app_pop(void)
{
	if (s_depth == 0)
		return;

	const PnxAppOps* was = s_stack[--s_depth];
	if (was->exit)
		was->exit(s_ctx);

	s_accumulator_ms	 = 0;
	const PnxAppOps* now = top();
	if (now && now->resume)
		now->resume(s_ctx);
}

void pnx_app_replace(const PnxAppOps* ops)
{
	if (!ops)
		return;

	if (s_depth > 0)
	{
		const PnxAppOps* was = s_stack[--s_depth];
		if (was->exit)
			was->exit(s_ctx);
	}

	if (s_depth >= PNX_APP_MAX_DEPTH)
	{
		pnx_log("app: replace refused at depth %u", (unsigned)s_depth);
		return;
	}

	s_stack[s_depth++] = ops;
	s_accumulator_ms   = 0;
	if (ops->enter)
		ops->enter(s_ctx);
}

uint8_t pnx_app_depth(void)
{
	return s_depth;
}

bool pnx_app_covered(void)
{
	return s_covered;
}

static void forward_push(PnxEvent ev)
{
	// Drop the newest on overflow, the same policy pnx_platform_pebble.c's own queue uses:
	// the queue only fills if nothing is draining it, and in that case the oldest events are
	// the ones still describing a gesture in progress.
	if (s_fwd_count >= FWD_QUEUE_LEN)
		return;
	const uint8_t slot = (uint8_t)((s_fwd_head + s_fwd_count) % FWD_QUEUE_LEN);
	s_fwd[slot]		   = ev;
	s_fwd_count++;
}

bool pnx_app_poll_event(PnxEvent* out)
{
	if (s_fwd_count == 0)
		return false;
	*out	   = s_fwd[s_fwd_head];
	s_fwd_head = (uint8_t)((s_fwd_head + 1) % FWD_QUEUE_LEN);
	s_fwd_count--;
	return true;
}

static void pump_events(void)
{
	PnxEvent ev;
	while (pnx_platform_poll_event(&ev))
	{
		if (ev.type == PNX_EVENT_FOCUS_LOST)
		{
			s_covered		   = true;
			s_accumulator_ms   = 0;
			const PnxAppOps* t = top();
			if (t && t->suspend)
				t->suspend(s_ctx);
		}
		else if (ev.type == PNX_EVENT_FOCUS_GAINED)
		{
			s_covered		   = false;
			const PnxAppOps* t = top();
			if (t && t->resume)
				t->resume(s_ctx);
		}
		else
		{
			forward_push(ev);
		}
	}
}

void pnx_app_frame(void* unused_ctx, uint32_t elapsed_ms, PnxTarget* target)
{
	(void)unused_ctx;
	const uint32_t work_start = pnx_platform_now_ms();
	pump_events();

	const PnxAppOps* t = top();
	if (!t)
	{
		pnx_diag_frame(elapsed_ms, pnx_platform_now_ms() - work_start);
		return;
	}

	if (t->input)
		t->input(s_ctx);
	// input() is the usual place a game reacts to a button press, and reacting can mean
	// pushing or popping -- BACK opening a menu, say. Re-reading top here means that takes
	// effect for the REST of this same frame; without it, the tick/frame/draw calls below
	// would run against the state that was on top when the frame started, and a menu opened
	// this frame would draw one frame late.
	t = top();
	if (!t)
	{
		pnx_diag_frame(elapsed_ms, pnx_platform_now_ms() - work_start);
		return;
	}

	// Throttle-aware pausing: while covered, the accumulator is not fed at all, so ticks
	// stop outright rather than being reconstructed from a multi-second elapsed_ms on
	// return. PNX_MAX_CATCHUP_TICKS below is the separate, ordinary-jitter clamp -- it still
	// applies whenever this branch DOES run, covered or not.
	if (!s_covered)
	{
		s_accumulator_ms += elapsed_ms;
		const uint32_t max_ms = (uint32_t)PNX_TICK_MS * PNX_MAX_CATCHUP_TICKS;
		if (s_accumulator_ms > max_ms)
			s_accumulator_ms = max_ms;

		while (s_accumulator_ms >= (uint32_t)PNX_TICK_MS)
		{
			s_accumulator_ms -= PNX_TICK_MS;
			if (t->tick)
				t->tick(s_ctx);
			// A tick may have pushed, popped or replaced -- re-read top so any ticks still owed
			// this frame, and the frame/draw calls below, go to whoever is actually in charge
			// now rather than a state that just exited.
			t = top();
			if (!t)
			{
				pnx_diag_frame(elapsed_ms, pnx_platform_now_ms() - work_start);
				return;
			}
		}
	}

	if (t->frame)
		t->frame(s_ctx);
	if (t->draw)
		t->draw(s_ctx, target);

	// Owned here rather than left to each state's draw(): every game gets a correct frame-
	// rate reading for free, and it covers frames a state's own draw() never ran on (the
	// early returns above) the same as the ones it did.
	pnx_diag_frame(elapsed_ms, pnx_platform_now_ms() - work_start);
}

#endif // PNX_USE_APP
