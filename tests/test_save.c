// Host tests for pnx_save.
//
// The host's persist table is in-memory rather than a file (see pnx_platform_host.c), so
// what these tests actually prove is round-tripping and call counts within one process --
// exactly what a save format needs proven, since "does the chunking add up" and "how many
// writes does it cost" are not properties of any one platform's flash.

#include "../src/pnx/save/pnx_save.h"
#include "../src/pnx/platform/pnx_platform_host.h"

#include <stdio.h>
#include <string.h>

extern int s_failures;
extern int s_checks;

#define SV_CHECK(cond) do {                                                \
    s_checks++;                                                            \
    if (!(cond)) {                                                         \
      printf("  FAIL %s:%d  %s\n", __FILE__, __LINE__, #cond);             \
      s_failures++;                                                        \
    }                                                                      \
  } while (0)

#define SV_CHECK_EQ(a, b) do {                                             \
    s_checks++;                                                            \
    const long _a = (long)(a), _b = (long)(b);                             \
    if (_a != _b) {                                                        \
      printf("  FAIL %s:%d  %s == %s  (%ld vs %ld)\n",                     \
             __FILE__, __LINE__, #a, #b, _a, _b);                          \
      s_failures++;                                                        \
    }                                                                      \
  } while (0)

void test_save(void);

#define SLOT_A ((PnxSaveSlot)0)
#define SLOT_B ((PnxSaveSlot)1)

typedef struct {
  char tag[8];
  int32_t value;
  uint8_t flags;
} SmallPayload;

void test_save(void) {
  printf("save\n");

  // --- a small payload (well under one chunk) round-trips in one write, one chunk
  pnx_host_reset();
  {
    SmallPayload in = { "abc", 12345, 0x7};
    SV_CHECK(pnx_save_write(SLOT_A, &in, sizeof(in), 1));
    SV_CHECK_EQ(pnx_host_persist_writes(), 1);
    SV_CHECK(!pnx_save_pending(SLOT_A));

    SmallPayload out = {0};
    size_t got = 0;
    SV_CHECK(pnx_save_exists(SLOT_A));
    SV_CHECK(pnx_save_load(SLOT_A, &out, sizeof(out), 1, &got));
    SV_CHECK_EQ(got, sizeof(out));
    SV_CHECK(memcmp(&in, &out, sizeof(in)) == 0);
  }

  // --- two slots never collide
  pnx_host_reset();
  {
    uint32_t a = 111, b = 222;
    SV_CHECK(pnx_save_write(SLOT_A, &a, sizeof(a), 1));
    SV_CHECK(pnx_save_write(SLOT_B, &b, sizeof(b), 1));

    uint32_t got_a = 0, got_b = 0;
    SV_CHECK(pnx_save_load(SLOT_A, &got_a, sizeof(got_a), 1, NULL));
    SV_CHECK(pnx_save_load(SLOT_B, &got_b, sizeof(got_b), 1, NULL));
    SV_CHECK_EQ(got_a, 111);
    SV_CHECK_EQ(got_b, 222);
  }

  // --- a payload spanning several chunks: one persist call per chunk, spread one per
  // pnx_save_step call -- the shape "a save spread across frames" actually takes.
  pnx_host_reset();
  {
    uint8_t big[600];
    for (size_t i = 0; i < sizeof(big); i++) big[i] = (uint8_t)(i * 7 + 3);

    SV_CHECK(pnx_save_begin(SLOT_A, big, sizeof(big), 1));
    // Chunk 0 carries PNX_SAVE_CHUNK0_PAYLOAD bytes; the rest is ceil'd over full keys.
    const size_t rest = sizeof(big) - PNX_SAVE_CHUNK0_PAYLOAD;
    const size_t expect_chunks =
        1 + (rest + PNX_PERSIST_KEY_BYTES - 1) / PNX_PERSIST_KEY_BYTES;
    SV_CHECK(expect_chunks > 1);   // the scenario is only meaningful if it actually spans

    uint32_t frames = 1;   // begin() already wrote chunk 0
    while (pnx_save_pending(SLOT_A)) {
      SV_CHECK(pnx_save_step(SLOT_A));
      frames++;
      SV_CHECK(frames < 100);   // runaway guard, not a real limit
    }
    SV_CHECK_EQ(frames, expect_chunks);
    SV_CHECK_EQ(pnx_host_persist_writes(), expect_chunks);

    uint8_t out[600] = {0};
    size_t got = 0;
    SV_CHECK(pnx_save_load(SLOT_A, out, sizeof(out), 1, &got));
    SV_CHECK_EQ(got, sizeof(big));
    SV_CHECK(memcmp(big, out, sizeof(big)) == 0);
  }

  // --- a payload that exactly fills every chunk in the slot is still accepted; one byte
  // more is refused rather than silently truncated
  pnx_host_reset();
  {
    static uint8_t max_payload[PNX_SAVE_MAX_PAYLOAD];
    memset(max_payload, 0xAB, sizeof(max_payload));
    SV_CHECK(pnx_save_write(SLOT_A, max_payload, sizeof(max_payload), 1));

    static uint8_t too_big[PNX_SAVE_MAX_PAYLOAD + 1];
    SV_CHECK(!pnx_save_write(SLOT_A, too_big, sizeof(too_big), 1));
  }

  // --- a newer save is refused, and the caller finds out via pnx_save_peek_version
  // without decoding a payload it does not understand
  pnx_host_reset();
  {
    uint32_t v = 999;
    SV_CHECK(pnx_save_write(SLOT_A, &v, sizeof(v), 5));

    uint8_t seen = 0;
    SV_CHECK(pnx_save_peek_version(SLOT_A, &seen));
    SV_CHECK_EQ(seen, 5);

    uint32_t out = 0;
    SV_CHECK(!pnx_save_load(SLOT_A, &out, sizeof(out), 4, NULL));   // 4 < 5, refused
    SV_CHECK(pnx_save_load(SLOT_A, &out, sizeof(out), 5, NULL));    // exact version, fine
    SV_CHECK(pnx_save_load(SLOT_A, &out, sizeof(out), 9, NULL));    // caller understands more
  }

  // --- a torn write is caught by the checksum, not silently accepted
  pnx_host_reset();
  {
    uint32_t v = 42;
    SV_CHECK(pnx_save_write(SLOT_A, &v, sizeof(v), 1));

    // Flip a payload byte directly in persist, as if the last chunk write had landed
    // wrong -- pnx_save has no path that does this itself, which is the point of forcing
    // it in from outside.
    uint8_t chunk0[PNX_PERSIST_KEY_BYTES];
    size_t got = 0;
    SV_CHECK(pnx_platform_persist_read(0, chunk0, sizeof(chunk0), &got));
    chunk0[PNX_SAVE_HEADER_BYTES] ^= 0xFF;
    SV_CHECK(pnx_platform_persist_write(0, chunk0, got));

    uint32_t out = 0;
    SV_CHECK(!pnx_save_load(SLOT_A, &out, sizeof(out), 1, NULL));
  }

  // --- an empty slot is neither present nor loadable, and delete is harmless on it
  pnx_host_reset();
  {
    SV_CHECK(!pnx_save_exists(SLOT_A));
    uint32_t out = 0;
    SV_CHECK(!pnx_save_load(SLOT_A, &out, sizeof(out), 255, NULL));
    pnx_save_delete(SLOT_A);   // must not crash on a slot that was never written
  }

  // --- delete actually clears it
  pnx_host_reset();
  {
    uint32_t v = 7;
    SV_CHECK(pnx_save_write(SLOT_A, &v, sizeof(v), 1));
    SV_CHECK(pnx_save_exists(SLOT_A));
    SV_CHECK(pnx_save_delete(SLOT_A));
    SV_CHECK(!pnx_save_exists(SLOT_A));
  }

  // --- starting a second save before the first finishes abandons the first rather than
  // interleaving their chunks
  pnx_host_reset();
  {
    uint8_t first[600];
    memset(first, 0x11, sizeof(first));
    SV_CHECK(pnx_save_begin(SLOT_A, first, sizeof(first), 1));
    SV_CHECK(pnx_save_pending(SLOT_A));

    uint32_t second = 555;
    SV_CHECK(pnx_save_begin(SLOT_A, &second, sizeof(second), 1));
    SV_CHECK(!pnx_save_pending(SLOT_A));   // the second was small enough to finish in begin()

    uint32_t out = 0;
    SV_CHECK(pnx_save_load(SLOT_A, &out, sizeof(out), 1, NULL));
    SV_CHECK_EQ(out, 555);
  }
}
