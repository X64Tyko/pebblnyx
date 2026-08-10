// Host tests for the platform-independent core.
//
// These run with a normal compiler, which is the whole point of the platform seam: a
// blitter bug or an arena overflow should be catchable in a second on a laptop rather
// than in a minute over Bluetooth with a log stream that drops the first messages.
//
// Deliberately dependency-free. A test framework would be another thing to install for
// something this small.

#include "../src/pnx/core/pnx_fx.h"
#include "../src/pnx/core/pnx_arena.h"
#include "../src/pnx/core/pnx_fmt.h"
#include "../src/pnx/platform/pnx_platform.h"
#include "../src/pnx/platform/pnx_platform_host.h"

#include <stdio.h>
#include <string.h>

// Not static: test_assets.c shares the counters.
int s_failures;
int s_checks;

#define CHECK(cond) do {                                                    \
    s_checks++;                                                             \
    if (!(cond)) {                                                          \
      printf("  FAIL %s:%d  %s\n", __FILE__, __LINE__, #cond);              \
      s_failures++;                                                         \
    }                                                                       \
  } while (0)

#define CHECK_EQ(a, b) do {                                                 \
    s_checks++;                                                             \
    const long _a = (long)(a), _b = (long)(b);                              \
    if (_a != _b) {                                                         \
      printf("  FAIL %s:%d  %s == %s  (%ld vs %ld)\n",                      \
             __FILE__, __LINE__, #a, #b, _a, _b);                           \
      s_failures++;                                                         \
    }                                                                       \
  } while (0)

// --------------------------------------------------------------------- fixed point

static void test_fx(void) {
  printf("fx\n");

  CHECK_EQ(pnx_fx_to_int(pnx_fx_from_int(5)), 5);
  CHECK_EQ(pnx_fx_to_int(pnx_fx_from_int(-5)), -5);

  // The int64 intermediate exists for this case: 300 * 100 needs a 48-bit product
  // partway through, which a 32-bit multiply would wrap, even though the 30000 result
  // sits comfortably in range.
  CHECK_EQ(pnx_fx_to_int(pnx_fx_mul(pnx_fx_from_int(300), pnx_fx_from_int(100))), 30000);

  // The RESULT range is the hard limit and int64 does not extend it: 16.16 holds
  // +/-32767 whole units, so 32768 wraps. Pinned here so it is a documented boundary
  // rather than a surprise the first time a map exceeds it -- 32767px is 2047 tiles at
  // 16px, far past anything that fits in 128KB, but coordinate deltas can compound.
  CHECK_EQ(pnx_fx_to_int(pnx_fx_from_int(32767)), 32767);
  CHECK(pnx_fx_to_int(pnx_fx_from_int(32768)) != 32768);

  CHECK_EQ(pnx_fx_to_int(pnx_fx_div(pnx_fx_from_int(10), pnx_fx_from_int(4))), 2);
  CHECK_EQ(pnx_fx_div(pnx_fx_from_int(1), 0), 0);   // defined, not UB

  // The floor property is the one that matters: world-to-tile conversion must round
  // toward negative infinity, or a pixel at -1 lands in tile 0 instead of tile -1.
  CHECK_EQ(pnx_fx_floor(pnx_fx_from_int(-1)), -1);
  CHECK_EQ(pnx_fx_floor(-1), -1);                    // -1/65536 floors to -1, not 0
  CHECK_EQ(pnx_fx_floor(pnx_fx_from_int(-1) + 1), -1);

  CHECK_EQ(pnx_fx_abs(pnx_fx_from_int(-3)), pnx_fx_from_int(3));
  CHECK_EQ(pnx_fx_clamp(pnx_fx_from_int(20), pnx_fx_from_int(0), pnx_fx_from_int(10)),
           pnx_fx_from_int(10));

  CHECK_EQ(pnx_isqrt(0), 0);
  CHECK_EQ(pnx_isqrt(1), 1);
  CHECK_EQ(pnx_isqrt(144), 12);
  CHECK_EQ(pnx_isqrt(145), 12);      // floors
  CHECK_EQ(pnx_isqrt(-5), 0);        // defined for garbage input
}

// ---------------------------------------------------------------------- formatting

#define CHECK_STR(actual, expected) do {                                    \
    s_checks++;                                                             \
    if (strcmp((actual), (expected)) != 0) {                                \
      printf("  FAIL %s:%d  got \"%s\", want \"%s\"\n",                     \
             __FILE__, __LINE__, (actual), (expected));                     \
      s_failures++;                                                         \
    }                                                                       \
  } while (0)

// The host has a real snprintf, so it can be used as an oracle. The device cannot --
// Pebble's libc has no vsnprintf at all, which is the reason pnx_fmt exists -- so this
// agreement is exactly what these tests are for.
#define CHECK_MATCHES_LIBC(fmt, ...) do {                                   \
    char mine[128], theirs[128];                                            \
    pnx_format(mine, sizeof(mine), fmt, __VA_ARGS__);                       \
    snprintf(theirs, sizeof(theirs), fmt, __VA_ARGS__);                     \
    CHECK_STR(mine, theirs);                                                \
  } while (0)

static void test_fmt(void) {
  printf("fmt\n");

  char buf[64];

  pnx_format(buf, sizeof(buf), "plain");
  CHECK_STR(buf, "plain");

  CHECK_MATCHES_LIBC("%d", 0);
  CHECK_MATCHES_LIBC("%d", -1);
  CHECK_MATCHES_LIBC("%d %d", 42, -42);
  CHECK_MATCHES_LIBC("%u", 4000000000u);
  CHECK_MATCHES_LIBC("%x %X", 0xdeadbeefu, 0xdeadbeefu);
  CHECK_MATCHES_LIBC("%c%c", 'o', 'k');
  CHECK_MATCHES_LIBC("%s/%s", "a", "b");
  // Checked directly rather than against libc: the oracle macro needs at least one
  // variadic argument.
  pnx_format(buf, sizeof(buf), "100%%");
  CHECK_STR(buf, "100%");

  // Padding and alignment, where hand-rolled formatters usually diverge.
  CHECK_MATCHES_LIBC("[%5d]", 42);
  CHECK_MATCHES_LIBC("[%-5d]", 42);
  CHECK_MATCHES_LIBC("[%05d]", 42);
  CHECK_MATCHES_LIBC("[%05d]", -42);    // sign before zeros, not after
  CHECK_MATCHES_LIBC("[%5s]", "ab");
  CHECK_MATCHES_LIBC("[%-5s]", "ab");
  CHECK_MATCHES_LIBC("[%08x]", 0x1234u);

  // INT32_MIN has no positive counterpart; negating it naively overflows.
  CHECK_MATCHES_LIBC("%d", (int)(-2147483647 - 1));

  // Truncation must terminate and report the length it WOULD have needed, or callers
  // cannot tell a full buffer from an exact fit.
  char small[5];
  const int needed = pnx_format(small, sizeof(small), "%s", "abcdefgh");
  CHECK_STR(small, "abcd");
  CHECK_EQ(needed, 8);

  // A zero-size buffer must not be written to at all.
  char guard[2] = { 'Z', 'Z' };
  CHECK_EQ(pnx_format(guard, 0, "hello"), 5);
  CHECK_EQ(guard[0], 'Z');

  // NULL string argument is survivable rather than a crash: a log call is often the
  // thing running when state is already bad.
  pnx_format(buf, sizeof(buf), "%s", (const char *)NULL);
  CHECK_STR(buf, "(null)");

  // An unknown conversion stays visible instead of eating the rest of the output.
  pnx_format(buf, sizeof(buf), "a%qb");
  CHECK_STR(buf, "a%qb");
}

// --------------------------------------------------------------------------- arena

static void test_arena(void) {
  printf("arena\n");

  PnxArena a;
  CHECK(pnx_arena_init(&a, "test", 1024, 4));

  uint8_t *p1 = (uint8_t *)pnx_arena_alloc(&a, 100, 4);
  CHECK(p1 != NULL);
  CHECK_EQ(((uintptr_t)p1) % 4, 0);

  // Alignment must be honoured even when the previous allocation left an odd cursor.
  pnx_arena_alloc(&a, 1, 1);
  uint8_t *p2 = (uint8_t *)pnx_arena_alloc(&a, 4, 8);
  CHECK(p2 != NULL);
  CHECK_EQ(((uintptr_t)p2) % 8, 0);

  // Exhaustion returns NULL rather than aborting, so a caller can degrade.
  CHECK(pnx_arena_alloc(&a, 100000, 4) == NULL);
  CHECK(pnx_arena_alloc(&a, 8, 4) != NULL);   // still usable after a failed request

  const size_t peak_before = a.peak;
  pnx_arena_reset(&a);
  CHECK_EQ(a.used, 0);
  CHECK_EQ(a.peak, peak_before);   // peak survives reset: it is the budgeting number

  int32_t *arr = PNX_ARENA_CALLOC_ARRAY(&a, int32_t, 16);
  CHECK(arr != NULL);
  for (int i = 0; i < 16; i++) CHECK_EQ(arr[i], 0);

  pnx_arena_destroy(&a);
  CHECK(a.base == NULL);

  // Zero capacity is rejected rather than producing a zero-length arena that fails
  // confusingly later.
  PnxArena bad;
  CHECK(!pnx_arena_init(&bad, "bad", 0, 4));
}

// ------------------------------------------------------------------------- target

static void test_target(void) {
  printf("target\n");

  PnxTarget *t = pnx_host_target();
  CHECK_EQ(pnx_target_width(t), 200);
  CHECK_EQ(pnx_target_height(t), 228);

  PnxRow row = pnx_target_row(t, 0);
  CHECK(row.data != NULL);
  CHECK_EQ(row.min_x, 0);
  CHECK_EQ(row.max_x, 199);

  // Out-of-range rows return an empty row rather than reading past the buffer.
  PnxRow bad = pnx_target_row(t, 1000);
  CHECK(bad.data == NULL);
  PnxRow neg = pnx_target_row(t, -1);
  CHECK(neg.data == NULL);

  // Rows are contiguous and distinct, which blitter code relies on.
  PnxRow r0 = pnx_target_row(t, 0);
  PnxRow r1 = pnx_target_row(t, 1);
  CHECK_EQ(r1.data - r0.data, 200);
}

void test_assets(void);
void test_gfx(void);
void test_audio(void);

int main(void) {
  printf("pebblnyx core tests\n");
  test_fx();
  test_fmt();
  test_arena();
  test_target();
  test_gfx();
  test_assets();
  test_audio();

  printf("\n%d checks, %d failures\n", s_checks, s_failures);
  return s_failures == 0 ? 0 : 1;
}
