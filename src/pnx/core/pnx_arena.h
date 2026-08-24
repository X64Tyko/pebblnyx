// Bump-allocated, double-ended arena.
//
// Heap-backed, deliberately. Static arrays are capped at 65,535 bytes shared with all
// code, so anything of size must be malloc'd at runtime -- see docs/MEASUREMENTS.md.
// One malloc per arena, then pointer bumps: no per-object allocator overhead, no
// fragmentation, and freeing is resetting a cursor.
//
// There is no individual free. Arenas are for things whose lifetime matches a scene or
// the whole program; if something needs individual freeing it does not belong here.
//
// Two cursors share the one malloc'd block, growing toward each other from opposite
// ends: the LOW end (`pnx_arena_alloc`/`used`) for things that live for the whole
// program, the HIGH end (`pnx_arena_alloc_hi`/`used_hi`) for things that live for one
// scene and get thrown away on the next `pnx_arena_reset`. This is what lets a project
// use exactly one arena -- sized to whatever the platform actually has free, via
// `pnx_arena_init_max` -- instead of hand-picking two separate byte budgets: a
// "persistent" allocation made after scene allocations have already advanced the high
// cursor still lands below it, on the low side, so it survives the next scene reset
// regardless of when it was made relative to scene loads.

#pragma once

#include <stddef.h>
#include <stdint.h>
#include <stdbool.h>

typedef struct
{
	uint8_t* base; // aligned start of usable space
	uint8_t* raw;  // the malloc'd pointer, for free()
	size_t capacity;
	size_t used;	  // low cursor: bytes claimed from the bottom, never reset
	size_t used_hi;	  // high cursor: bytes claimed from the top, cleared by pnx_arena_reset
	size_t peak;	  // high-water mark of used + used_hi, for budgeting
	const char* name; // for diagnostics
} PnxArena;

// `align` must be a power of two. 4 is right for almost everything here: cache-line
// alignment measured as a trade rather than a win (+24% on narrow access, -13% on
// wide), so paying 64-byte alignment by default would cost RAM for nothing.
bool pnx_arena_init(PnxArena* a, const char* name, size_t capacity, size_t align);

// Sizes the arena from the platform's actual free heap at the moment of the call,
// minus `reserve` bytes left unclaimed for whatever the SDK itself needs afterward
// (PNX_ARENA_HEAP_RESERVE in pnx_config.h is the project-wide default). Backs off
// geometrically on malloc failure -- real free heap and the biggest block malloc can
// actually hand back are not always the same number, once fragmentation is real -- so
// this fails only when even a small arena won't fit, not merely when the ideal size
// doesn't.
bool pnx_arena_init_max(PnxArena* a, const char* name, size_t reserve, size_t align);

void pnx_arena_destroy(PnxArena* a);

// Returns NULL when exhausted rather than aborting: on a device with no console, a
// caller that checks can degrade gracefully where a crash cannot.
void* pnx_arena_alloc(PnxArena* a, size_t bytes, size_t align);

// Zeroed variant, for anything that will be read before it is written.
void* pnx_arena_calloc(PnxArena* a, size_t bytes, size_t align);

// Same as pnx_arena_alloc/calloc, but bumps the high cursor down from `capacity`
// instead of the low cursor up from 0. This is the scene-lifetime half of the arena.
void* pnx_arena_alloc_hi(PnxArena* a, size_t bytes, size_t align);
void* pnx_arena_calloc_hi(PnxArena* a, size_t bytes, size_t align);

// Frees everything at once, both cursors. This is the whole-arena reset a single-sided
// user (one PnxArena, no persistent/scene split -- examples/empty's "game" arena, most
// host tests) wants: there is only one lifetime in play, so "reset" means "empty it".
// The memory stays mapped, so a reload costs no malloc.
void pnx_arena_reset(PnxArena* a);

// Frees the high (scene) side only, leaving the low (persistent) side exactly as it
// was -- what pnx_assets.c calls at a scene boundary, and the reason a double-ended
// arena exists at all. Never touches `used`; a persistent allocation is never affected
// by this call no matter when it was made relative to scene allocations.
void pnx_arena_reset_hi(PnxArena* a);

static inline size_t pnx_arena_remaining(const PnxArena* a)
{
	return a->capacity - a->used - a->used_hi;
}

#define PNX_ARENA_ALLOC_ARRAY(arena, type, count) \
	((type*)pnx_arena_alloc((arena), sizeof(type) * (size_t)(count), _Alignof(type)))

#define PNX_ARENA_CALLOC_ARRAY(arena, type, count) \
	((type*)pnx_arena_calloc((arena), sizeof(type) * (size_t)(count), _Alignof(type)))

#define PNX_ARENA_ALLOC_ARRAY_HI(arena, type, count) \
	((type*)pnx_arena_alloc_hi((arena), sizeof(type) * (size_t)(count), _Alignof(type)))

#define PNX_ARENA_CALLOC_ARRAY_HI(arena, type, count) \
	((type*)pnx_arena_calloc_hi((arena), sizeof(type) * (size_t)(count), _Alignof(type)))
