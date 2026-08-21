// Host tests for pnx_hud_vars.c.
//
// No asset/platform dependency: the module only ever operates on storage the CALLER
// provides (see pnx_hud_vars.h's own comment on why), so a test just declares its own
// arrays the way a real game's generated PNX_HUD_VAR_COUNT would.

#include "../src/pnx/gfx/pnx_hud_vars.h"

#include <stdio.h>
#include <string.h>

extern int s_failures;
extern int s_checks;

#define HV_CHECK_EQ(a, b)                                                                    \
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

#define HV_CHECK_STR(a, b)                                                             \
	do                                                                                 \
	{                                                                                  \
		s_checks++;                                                                    \
		if (strcmp((a), (b)) != 0)                                                     \
		{                                                                              \
			printf("  FAIL %s:%d  %s == %s  (%s vs %s)\n", __FILE__, __LINE__, #a, #b, \
				   (a), (b));                                                          \
			s_failures++;                                                              \
		}                                                                              \
	} while (0)

#define HV_CHECK(cond)                                               \
	do                                                               \
	{                                                                \
		s_checks++;                                                  \
		if (!(cond))                                                 \
		{                                                            \
			printf("  FAIL %s:%d  %s\n", __FILE__, __LINE__, #cond); \
			s_failures++;                                            \
		}                                                            \
	} while (0)

#define COUNT 4

static void test_ints(void)
{
	int32_t ints[COUNT];
	char text[COUNT][PNX_HUD_VAR_TEXT_LEN];
	pnx_hud_vars_init(ints, text, COUNT);

	// Zeroed by init, not left as whatever garbage the caller's array happened to hold.
	HV_CHECK_EQ(pnx_hud_var_get_i32(0), 0);

	pnx_hud_var_set_i32(0, 42);
	HV_CHECK_EQ(pnx_hud_var_get_i32(0), 42);
	pnx_hud_var_set_i32(3, -7); // last valid id (COUNT-1)
	HV_CHECK_EQ(pnx_hud_var_get_i32(3), -7);
	HV_CHECK_EQ(pnx_hud_var_get_i32(1), 0); // untouched ids stay at their init value

	// Out of range: reads as 0, writes are silently dropped -- never a crash, never
	// scribbling past the caller's own array.
	HV_CHECK_EQ(pnx_hud_var_get_i32(COUNT), 0);
	HV_CHECK_EQ(pnx_hud_var_get_i32(255), 0);
	pnx_hud_var_set_i32(COUNT, 999); // must not corrupt anything in-range
	HV_CHECK_EQ(pnx_hud_var_get_i32(0), 42);
	HV_CHECK_EQ(pnx_hud_var_get_i32(3), -7);
}

static void test_text(void)
{
	int32_t ints[COUNT];
	char text[COUNT][PNX_HUD_VAR_TEXT_LEN];
	pnx_hud_vars_init(ints, text, COUNT);

	// Zeroed by init: "" before anything is ever set, not garbage, and never NULL.
	HV_CHECK_STR(pnx_hud_var_get_text(0), "");

	pnx_hud_var_set_text(1, "GRANDPA");
	HV_CHECK_STR(pnx_hud_var_get_text(1), "GRANDPA");

	// Longer than PNX_HUD_VAR_TEXT_LEN-1 (15 chars): truncated, still NUL-terminated,
	// never overruns the 16-byte slot.
	pnx_hud_var_set_text(2, "a string that is definitely too long to fit");
	HV_CHECK_EQ(strlen(pnx_hud_var_get_text(2)), PNX_HUD_VAR_TEXT_LEN - 1);
	HV_CHECK_EQ(pnx_hud_var_get_text(2)[PNX_HUD_VAR_TEXT_LEN - 1], '\0');

	// Out of range: reads as "", write silently dropped.
	HV_CHECK_STR(pnx_hud_var_get_text(COUNT), "");
	pnx_hud_var_set_text(COUNT, "nope");
	HV_CHECK_STR(pnx_hud_var_get_text(1), "GRANDPA"); // untouched
}

// A project that declares only int (or only text) [[hud_var]]s passes NULL for the
// other array -- must be a safe no-op, not a null-deref, for every id of that type
// (there ARE no valid ids of that type, since none were declared).
static void test_null_storage(void)
{
	int32_t ints[COUNT];
	pnx_hud_vars_init(ints, NULL, COUNT);

	HV_CHECK_STR(pnx_hud_var_get_text(0), "");
	pnx_hud_var_set_text(0, "should not crash");
	HV_CHECK_STR(pnx_hud_var_get_text(0), ""); // still empty: nothing to write into

	pnx_hud_var_set_i32(0, 5); // the int side still works fine
	HV_CHECK_EQ(pnx_hud_var_get_i32(0), 5);

	char text[COUNT][PNX_HUD_VAR_TEXT_LEN];
	pnx_hud_vars_init(NULL, text, COUNT);

	HV_CHECK_EQ(pnx_hud_var_get_i32(0), 0);
	pnx_hud_var_set_i32(0, 5);
	HV_CHECK_EQ(pnx_hud_var_get_i32(0), 0); // still 0: nothing to write into

	pnx_hud_var_set_text(0, "fine");
	HV_CHECK_STR(pnx_hud_var_get_text(0), "fine");
}

// Re-init (a scene reset, a restart) clears whatever the PREVIOUS run left behind --
// confirms init actually zeroes rather than only zeroing on the very first call.
static void test_reinit_clears_stale_values(void)
{
	int32_t ints[COUNT];
	char text[COUNT][PNX_HUD_VAR_TEXT_LEN];
	pnx_hud_vars_init(ints, text, COUNT);
	pnx_hud_var_set_i32(0, 123);
	pnx_hud_var_set_text(0, "stale");

	pnx_hud_vars_init(ints, text, COUNT);
	HV_CHECK_EQ(pnx_hud_var_get_i32(0), 0);
	HV_CHECK_STR(pnx_hud_var_get_text(0), "");
}

void test_hud_vars(void)
{
	printf("hud_vars\n");

	test_ints();
	test_text();
	test_null_storage();
	test_reinit_clears_stale_values();
}
