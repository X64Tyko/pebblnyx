// See pnx_save.h for the design. This file is the chunking and the one in-flight writer;
// it never decides what goes into a save or when -- that is the game's, via begin/step/
// write/load.

#include "pnx_save.h"

#if PNX_USE_SAVE

#include "../core/pnx_diag.h"

#include <string.h>

// Persist keys PNX_SAVE_KEY_BASE + slot*PNX_SAVE_CHUNKS_PER_SLOT .. +CHUNKS_PER_SLOT-1
// belong to this module. A game that also uses persist directly for something else should
// keep clear of this range; there is no registry to check that automatically, the same as
// there is no registry of resource ids.
#ifndef PNX_SAVE_KEY_BASE
#define PNX_SAVE_KEY_BASE 0
#endif

static uint32_t key_for(PnxSaveSlot slot, uint8_t chunk) {
  return PNX_SAVE_KEY_BASE + (uint32_t)slot * PNX_SAVE_CHUNKS_PER_SLOT + chunk;
}

// Not cryptographic -- it exists to catch a save torn by, say, the device dying mid-write,
// not to resist tampering. A rotate-and-add over the payload is enough for that and costs
// nothing worth measuring next to a persist write.
static uint16_t checksum16(const uint8_t *data, size_t len) {
  uint16_t sum = 0x5A5A;   // non-zero seed so an all-zero payload does not checksum to 0
  for (size_t i = 0; i < len; i++) {
    sum = (uint16_t)((sum << 1) | (sum >> 15));
    sum = (uint16_t)(sum + data[i]);
  }
  return sum;
}

// ------------------------------------------------------------------------- writer
//
// One in-flight save at a time. A second game system trying to save concurrently is a
// design error worth a log line, not a case worth queuing for -- see pnx_save_begin.

typedef struct {
  bool active;
  PnxSaveSlot slot;
  const uint8_t *data;
  uint16_t bytes;
  uint16_t checksum;
  uint8_t chunk_count;
  uint8_t next_chunk;
  uint8_t version;
} SaveWriter;

static SaveWriter s_writer;

// Writes s_writer's NEXT chunk and advances it. Shared by the incremental path
// (pnx_save_begin/step) and the blocking one (pnx_save_write/flush) so there is exactly
// one place that knows how a chunk is laid out.
static bool write_chunk(void) {
  const uint8_t chunk = s_writer.next_chunk;
  uint8_t buf[PNX_PERSIST_KEY_BYTES];
  size_t buf_len;

  if (chunk == 0) {
    buf[0] = 'P';
    buf[1] = 'S';
    buf[2] = s_writer.version;
    buf[3] = s_writer.chunk_count;
    buf[4] = (uint8_t)(s_writer.bytes & 0xFF);
    buf[5] = (uint8_t)(s_writer.bytes >> 8);
    buf[6] = (uint8_t)(s_writer.checksum & 0xFF);
    buf[7] = (uint8_t)(s_writer.checksum >> 8);
    const size_t n = s_writer.bytes < PNX_SAVE_CHUNK0_PAYLOAD
                    ? s_writer.bytes : PNX_SAVE_CHUNK0_PAYLOAD;
    memcpy(buf + PNX_SAVE_HEADER_BYTES, s_writer.data, n);
    buf_len = PNX_SAVE_HEADER_BYTES + n;
  } else {
    const size_t offset = PNX_SAVE_CHUNK0_PAYLOAD + (size_t)(chunk - 1) * PNX_PERSIST_KEY_BYTES;
    const size_t remaining = s_writer.bytes - offset;
    const size_t n = remaining < PNX_PERSIST_KEY_BYTES ? remaining : PNX_PERSIST_KEY_BYTES;
    memcpy(buf, s_writer.data + offset, n);
    buf_len = n;
  }

  if (!pnx_platform_persist_write(key_for(s_writer.slot, chunk), buf, buf_len)) {
    pnx_log("save: slot %u chunk %u write failed -- abandoned",
            (unsigned)s_writer.slot, (unsigned)chunk);
    s_writer.active = false;
    return false;
  }

  s_writer.next_chunk = (uint8_t)(chunk + 1);
  if (s_writer.next_chunk >= s_writer.chunk_count) s_writer.active = false;
  return true;
}

bool pnx_save_begin(PnxSaveSlot slot, const void *data, size_t bytes, uint8_t version) {
  if (!data || bytes > PNX_SAVE_MAX_PAYLOAD) return false;

  if (s_writer.active) {
    pnx_log("save: slot %u begun before slot %u finished -- the first is abandoned",
            (unsigned)slot, (unsigned)s_writer.slot);
  }

  uint8_t chunk_count = 1;
  if (bytes > PNX_SAVE_CHUNK0_PAYLOAD) {
    const size_t rest = bytes - PNX_SAVE_CHUNK0_PAYLOAD;
    chunk_count = (uint8_t)(1 + (rest + PNX_PERSIST_KEY_BYTES - 1) / PNX_PERSIST_KEY_BYTES);
  }

  s_writer.active = true;
  s_writer.slot = slot;
  s_writer.data = (const uint8_t *)data;
  s_writer.bytes = (uint16_t)bytes;
  s_writer.chunk_count = chunk_count;
  s_writer.next_chunk = 0;
  s_writer.version = version;
  s_writer.checksum = checksum16(s_writer.data, bytes);

  return write_chunk();
}

bool pnx_save_pending(PnxSaveSlot slot) {
  return s_writer.active && s_writer.slot == slot;
}

bool pnx_save_step(PnxSaveSlot slot) {
  if (!pnx_save_pending(slot)) return false;
  return write_chunk();
}

bool pnx_save_flush(PnxSaveSlot slot) {
  if (!pnx_save_pending(slot)) return false;
  while (s_writer.active) {
    if (!write_chunk()) return false;
  }
  return true;
}

bool pnx_save_write(PnxSaveSlot slot, const void *data, size_t bytes, uint8_t version) {
  if (!pnx_save_begin(slot, data, bytes, version)) return false;
  // A payload that fits in chunk 0 is already fully written by begin() -- pending is
  // false at this point because there is nothing left to do, not because anything failed.
  // pnx_save_flush's own "nothing pending" check would read that as an error, so it is
  // only called when there is genuinely a chunk left to flush.
  return !pnx_save_pending(slot) || pnx_save_flush(slot);
}

// ---------------------------------------------------------------------------- reader

bool pnx_save_peek_version(PnxSaveSlot slot, uint8_t *out_version) {
  uint8_t buf[PNX_SAVE_HEADER_BYTES];
  size_t got = 0;
  if (!pnx_platform_persist_read(key_for(slot, 0), buf, sizeof(buf), &got)) return false;
  if (got < PNX_SAVE_HEADER_BYTES || buf[0] != 'P' || buf[1] != 'S') return false;
  if (out_version) *out_version = buf[2];
  return true;
}

bool pnx_save_exists(PnxSaveSlot slot) {
  return pnx_save_peek_version(slot, NULL);
}

bool pnx_save_load(PnxSaveSlot slot, void *out, size_t max_bytes, uint8_t version,
                   size_t *out_bytes) {
  if (!out) return false;

  uint8_t buf0[PNX_PERSIST_KEY_BYTES];
  size_t got0 = 0;
  if (!pnx_platform_persist_read(key_for(slot, 0), buf0, sizeof(buf0), &got0)) return false;
  if (got0 < PNX_SAVE_HEADER_BYTES || buf0[0] != 'P' || buf0[1] != 'S') return false;

  const uint8_t file_version = buf0[2];
  const uint8_t chunk_count = buf0[3];
  const uint16_t payload_bytes = (uint16_t)(buf0[4] | (buf0[5] << 8));
  const uint16_t stored_checksum = (uint16_t)(buf0[6] | (buf0[7] << 8));

  if (file_version > version) {
    pnx_log("save: slot %u is version %u, newer than %u understood -- refused",
            (unsigned)slot, (unsigned)file_version, (unsigned)version);
    return false;
  }
  if (chunk_count == 0 || chunk_count > PNX_SAVE_CHUNKS_PER_SLOT) return false;
  if (payload_bytes > PNX_SAVE_MAX_PAYLOAD || payload_bytes > max_bytes) return false;

  const size_t n0 = payload_bytes < PNX_SAVE_CHUNK0_PAYLOAD
                   ? payload_bytes : PNX_SAVE_CHUNK0_PAYLOAD;
  memcpy(out, buf0 + PNX_SAVE_HEADER_BYTES, n0);
  size_t have = n0;

  for (uint8_t c = 1; c < chunk_count; c++) {
    uint8_t buf[PNX_PERSIST_KEY_BYTES];
    size_t got = 0;
    if (!pnx_platform_persist_read(key_for(slot, c), buf, sizeof(buf), &got)) return false;
    const size_t remaining = payload_bytes - have;
    const size_t n = remaining < got ? remaining : got;
    memcpy((uint8_t *)out + have, buf, n);
    have += n;
  }

  if (have != payload_bytes) return false;
  if (checksum16((const uint8_t *)out, payload_bytes) != stored_checksum) {
    pnx_log("save: slot %u failed its checksum -- refused", (unsigned)slot);
    return false;
  }

  if (out_bytes) *out_bytes = payload_bytes;
  return true;
}

bool pnx_save_delete(PnxSaveSlot slot) {
  bool any = false;
  for (uint8_t c = 0; c < PNX_SAVE_CHUNKS_PER_SLOT; c++) {
    if (pnx_platform_persist_delete(key_for(slot, c))) any = true;
  }
  return any;
}

#endif  // PNX_USE_SAVE
