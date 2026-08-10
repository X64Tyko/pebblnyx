// Bump-allocated arena.
//
// Heap-backed, deliberately. Static arrays are capped at 65,535 bytes shared with all
// code, so anything of size must be malloc'd at runtime -- see docs/MEASUREMENTS.md.
// One malloc per arena, then pointer bumps: no per-object allocator overhead, no
// fragmentation, and freeing is resetting a cursor.
//
// There is no individual free. Arenas are for things whose lifetime matches a scene or
// the whole program; if something needs individual freeing it does not belong here.

#pragma once

#include <stddef.h>
#include <stdint.h>
#include <stdbool.h>

typedef struct {
  uint8_t *base;      // aligned start of usable space
  uint8_t *raw;       // the malloc'd pointer, for free()
  size_t capacity;
  size_t used;
  size_t peak;        // high-water mark, for budgeting
  const char *name;   // for diagnostics
} PnxArena;

// `align` must be a power of two. 4 is right for almost everything here: cache-line
// alignment measured as a trade rather than a win (+24% on narrow access, -13% on
// wide), so paying 64-byte alignment by default would cost RAM for nothing.
bool pnx_arena_init(PnxArena *a, const char *name, size_t capacity, size_t align);
void pnx_arena_destroy(PnxArena *a);

// Returns NULL when exhausted rather than aborting: on a device with no console, a
// caller that checks can degrade gracefully where a crash cannot.
void *pnx_arena_alloc(PnxArena *a, size_t bytes, size_t align);

// Zeroed variant, for anything that will be read before it is written.
void *pnx_arena_calloc(PnxArena *a, size_t bytes, size_t align);

// Frees everything at once. The memory stays mapped, so a scene reload costs no malloc.
void pnx_arena_reset(PnxArena *a);

static inline size_t pnx_arena_remaining(const PnxArena *a) {
  return a->capacity - a->used;
}

#define PNX_ARENA_ALLOC_ARRAY(arena, type, count) \
  ((type *)pnx_arena_alloc((arena), sizeof(type) * (size_t)(count), _Alignof(type)))

#define PNX_ARENA_CALLOC_ARRAY(arena, type, count) \
  ((type *)pnx_arena_calloc((arena), sizeof(type) * (size_t)(count), _Alignof(type)))
