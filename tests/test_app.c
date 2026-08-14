// Host tests for pnx_app: the state stack and the frame loop built on it.
//
// Two synthetic states (never a real game's) so the ordering of enter/exit/suspend/resume
// can be asserted directly, and a fake context struct that counts calls instead of doing
// anything -- the point of this file is the STACK's behaviour, not any one game's.

#include "../src/pnx/app/pnx_app.h"
#include "../src/pnx/core/pnx_diag.h"
#include "../src/pnx/platform/pnx_platform_host.h"

#include <stdio.h>
#include <string.h>

extern int s_failures;
extern int s_checks;

#define AP_CHECK(cond)                                               \
	do                                                               \
	{                                                                \
		s_checks++;                                                  \
		if (!(cond))                                                 \
		{                                                            \
			printf("  FAIL %s:%d  %s\n", __FILE__, __LINE__, #cond); \
			s_failures++;                                            \
		}                                                            \
	} while (0)

#define AP_CHECK_EQ(a, b)                                                                    \
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

void test_app(void);

// One log line per call, in a fixed-size ring, so a test can assert the exact ORDER
// hooks fired in -- which is most of what the stack promises and pnx_app_covered() alone
// cannot show.
#define LOG_LEN 64
static char s_log[LOG_LEN][16];
static int s_log_n;

static void logged(const char* what)
{
	if (s_log_n < LOG_LEN)
	{
		strncpy(s_log[s_log_n], what, sizeof(s_log[0]) - 1);
		s_log[s_log_n][sizeof(s_log[0]) - 1] = '\0';
		s_log_n++;
	}
}

static void log_reset(void)
{
	s_log_n = 0;
}

static int log_eq(int i, const char* what)
{
	return i < s_log_n && strcmp(s_log[i], what) == 0;
}

typedef struct
{
	int ticks;
} Ctx;

// State "A": logs every hook, and counts its own ticks so the accumulator's behaviour
// (fixed step, clamped, zeroed on cover) can be checked from outside pnx_app.
static void a_enter(void* ctx)
{
	(void)ctx;
	logged("a.enter");
}
static void a_exit(void* ctx)
{
	(void)ctx;
	logged("a.exit");
}
static void a_suspend(void* ctx)
{
	(void)ctx;
	logged("a.suspend");
}
static void a_resume(void* ctx)
{
	(void)ctx;
	logged("a.resume");
}
static void a_tick(void* ctx)
{
	((Ctx*)ctx)->ticks++;
	logged("a.tick");
}
static void a_draw(void* ctx, PnxTarget* t)
{
	(void)ctx;
	(void)t;
	logged("a.draw");
}
static void a_frame(void* ctx)
{
	(void)ctx;
	logged("a.frame");
}

static const PnxAppOps A_OPS = {
	.enter	 = a_enter,
	.exit	 = a_exit,
	.suspend = a_suspend,
	.resume	 = a_resume,
	.tick	 = a_tick,
	.draw	 = a_draw,
	.frame	 = a_frame,
};

// State "B": a second, distinct state for push/pop/replace ordering. No tick/draw of its
// own to keep the log focused on stack transitions.
static void b_enter(void* ctx)
{
	(void)ctx;
	logged("b.enter");
}
static void b_exit(void* ctx)
{
	(void)ctx;
	logged("b.exit");
}
static void b_suspend(void* ctx)
{
	(void)ctx;
	logged("b.suspend");
}
static void b_resume(void* ctx)
{
	(void)ctx;
	logged("b.resume");
}

static const PnxAppOps B_OPS = {
	.enter	 = b_enter,
	.exit	 = b_exit,
	.suspend = b_suspend,
	.resume	 = b_resume,
};

void test_app(void)
{
	printf("app\n");
	Ctx ctx;

	// --- push calls enter; a second push suspends the first and enters the second
	memset(&ctx, 0, sizeof(ctx));
	pnx_app_init(&ctx);
	log_reset();
	pnx_app_push(&A_OPS);
	AP_CHECK_EQ(pnx_app_depth(), 1);
	AP_CHECK(log_eq(0, "a.enter"));

	pnx_app_push(&B_OPS);
	AP_CHECK_EQ(pnx_app_depth(), 2);
	AP_CHECK(log_eq(1, "a.suspend"));
	AP_CHECK(log_eq(2, "b.enter"));

	// --- pop exits the top and resumes what is beneath
	log_reset();
	pnx_app_pop();
	AP_CHECK_EQ(pnx_app_depth(), 1);
	AP_CHECK(log_eq(0, "b.exit"));
	AP_CHECK(log_eq(1, "a.resume"));

	// --- pop on the last state exits it and leaves the stack empty; a further pop is a
	// no-op rather than a crash
	log_reset();
	pnx_app_pop();
	AP_CHECK_EQ(pnx_app_depth(), 0);
	AP_CHECK(log_eq(0, "a.exit"));
	pnx_app_pop();
	AP_CHECK_EQ(pnx_app_depth(), 0);

	// --- replace exits the current top and enters the new one with no suspend/resume pair,
	// and pushes plainly onto an empty stack
	memset(&ctx, 0, sizeof(ctx));
	pnx_app_init(&ctx);
	pnx_app_push(&A_OPS);
	log_reset();
	pnx_app_replace(&B_OPS);
	AP_CHECK_EQ(pnx_app_depth(), 1);
	AP_CHECK(log_eq(0, "a.exit"));
	AP_CHECK(log_eq(1, "b.enter"));
	AP_CHECK_EQ(s_log_n, 2); // specifically NOT a.suspend or b.resume

	// --- push past PNX_APP_MAX_DEPTH is refused, and the state that got suspended for the
	// attempt is resumed rather than left paused for a push that never happened
	memset(&ctx, 0, sizeof(ctx));
	pnx_app_init(&ctx);
	for (int i = 0; i < PNX_APP_MAX_DEPTH; i++)
		pnx_app_push(&B_OPS);
	AP_CHECK_EQ(pnx_app_depth(), PNX_APP_MAX_DEPTH);
	log_reset();
	pnx_app_push(&A_OPS);
	AP_CHECK_EQ(pnx_app_depth(), PNX_APP_MAX_DEPTH); // refused, not grown
	AP_CHECK(log_eq(0, "b.suspend"));
	AP_CHECK(log_eq(1, "b.resume"));
	AP_CHECK_EQ(s_log_n, 2); // never reached a.enter

	// --- the frame loop: fixed-step ticks, and a frame carrying less than one tick's worth
	// of elapsed time ticks zero times
	memset(&ctx, 0, sizeof(ctx));
	pnx_app_init(&ctx);
	pnx_app_push(&A_OPS);
	log_reset();
	pnx_app_frame(NULL, PNX_TICK_MS - 1, pnx_host_target());
	AP_CHECK_EQ(ctx.ticks, 0);
	AP_CHECK(log_eq(0, "a.frame"));
	AP_CHECK(log_eq(1, "a.draw"));

	pnx_app_frame(NULL, 1, pnx_host_target()); // completes the tick the previous frame owed
	AP_CHECK_EQ(ctx.ticks, 1);

	// --- several ticks' worth of elapsed time in one frame runs the tick loop that many
	// times, up to the clamp
	memset(&ctx, 0, sizeof(ctx));
	pnx_app_init(&ctx);
	pnx_app_push(&A_OPS);
	pnx_app_frame(NULL, PNX_TICK_MS * 3, pnx_host_target());
	AP_CHECK_EQ(ctx.ticks, 3);

	memset(&ctx, 0, sizeof(ctx));
	pnx_app_init(&ctx);
	pnx_app_push(&A_OPS);
	pnx_app_frame(NULL, PNX_TICK_MS * (PNX_MAX_CATCHUP_TICKS + 50), pnx_host_target());
	AP_CHECK_EQ(ctx.ticks, PNX_MAX_CATCHUP_TICKS);

	// --- throttle-aware pausing: FOCUS_LOST suspends the top and stops ticks outright, not
	// just clamped ones; FOCUS_GAINED resumes it and ticking picks back up
	memset(&ctx, 0, sizeof(ctx));
	pnx_app_init(&ctx);
	pnx_app_push(&A_OPS);
	AP_CHECK(!pnx_app_covered());

	pnx_host_queue_event((PnxEvent){ .type = PNX_EVENT_FOCUS_LOST });
	log_reset();
	pnx_app_frame(NULL, PNX_TICK_MS * 5, pnx_host_target());
	AP_CHECK(pnx_app_covered());
	AP_CHECK(log_eq(0, "a.suspend"));
	AP_CHECK_EQ(ctx.ticks, 0); // not even one -- covered arrived this same frame
	// frame() still ran even though tick() did not -- see PNX_USE_APP header note on why.
	AP_CHECK(log_eq(1, "a.frame"));

	// A second covered frame carrying seconds of elapsed time still ticks zero times.
	pnx_app_frame(NULL, PNX_TICK_MS * 200, pnx_host_target());
	AP_CHECK_EQ(ctx.ticks, 0);

	pnx_host_queue_event((PnxEvent){ .type = PNX_EVENT_FOCUS_GAINED });
	log_reset();
	pnx_app_frame(NULL, PNX_TICK_MS, pnx_host_target());
	AP_CHECK(!pnx_app_covered());
	AP_CHECK(log_eq(0, "a.resume"));
	AP_CHECK_EQ(ctx.ticks, 1); // ticking from a zeroed accumulator, not a caught-up one

	// --- non-lifecycle events are forwarded, not swallowed; lifecycle ones never are
	memset(&ctx, 0, sizeof(ctx));
	pnx_app_init(&ctx);
	pnx_app_push(&A_OPS);
	pnx_host_queue_event((PnxEvent){ .type = PNX_EVENT_BUTTON_DOWN, .button = PNX_BUTTON_UP });
	pnx_host_queue_event((PnxEvent){ .type = PNX_EVENT_FOCUS_LOST });
	pnx_host_queue_event((PnxEvent){ .type = PNX_EVENT_FOCUS_GAINED });
	pnx_host_queue_event((PnxEvent){ .type = PNX_EVENT_BUTTON_UP, .button = PNX_BUTTON_UP });
	pnx_app_frame(NULL, 0, pnx_host_target());

	PnxEvent ev;
	AP_CHECK(pnx_app_poll_event(&ev));
	AP_CHECK_EQ(ev.type, PNX_EVENT_BUTTON_DOWN);
	AP_CHECK(pnx_app_poll_event(&ev));
	AP_CHECK_EQ(ev.type, PNX_EVENT_BUTTON_UP);
	AP_CHECK(!pnx_app_poll_event(&ev)); // the two focus events were consumed, not forwarded

	// --- a tick that changes the stack mid-loop hands any ticks still owed this frame, and
	// the frame/draw calls after the loop, to whoever is on top NOW -- not the state that
	// popped itself, and not a stale pointer
	memset(&ctx, 0, sizeof(ctx));
	pnx_app_init(&ctx);
	pnx_app_push(&A_OPS);
	pnx_app_pop();											 // stack now empty
	pnx_app_frame(NULL, PNX_TICK_MS * 3, pnx_host_target()); // must not crash on an empty top
	AP_CHECK_EQ(pnx_app_depth(), 0);

	// --- pnx_app_frame feeds pnx_diag_frame() itself -- a game's FPS readout is otherwise
	// permanently 0.0, since pnx_diag_stats() returns a real (non-NULL) pointer to a struct
	// that just never got updated, and nothing before this test happens to call
	// pnx_diag_frame directly. Every pnx_app_frame call above already contributes to the
	// same rolling window (1000ms, see pnx_diag.c), so by here it must have flushed at
	// least once.
	const PnxFrameStats* st = pnx_diag_stats();
	AP_CHECK(st != NULL);
	AP_CHECK(st && st->frames > 0);
}
