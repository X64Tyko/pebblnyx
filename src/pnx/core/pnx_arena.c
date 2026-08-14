#include "pnx_arena.h"

#include <stdlib.h>
#include <string.h>

static size_t align_up(size_t v, size_t align)
{
	if (align < 1)
		align = 1;
	return (v + (align - 1)) & ~(align - 1);
}

bool pnx_arena_init(PnxArena* a, const char* name, size_t capacity, size_t align)
{
	if (!a || capacity == 0)
		return false;
	if (align < 1)
		align = 1;

	memset(a, 0, sizeof(*a));

	// Over-allocate by `align` so the base can be moved forward to an aligned address.
	// malloc only guarantees 8-byte alignment, which is not enough if a caller wants
	// cache-line or wider.
	a->raw = (uint8_t*)malloc(capacity + align);
	if (!a->raw)
		return false;

	const uintptr_t aligned = align_up((uintptr_t)a->raw, align);
	a->base = (uint8_t*)aligned;
	a->capacity = capacity;
	a->used = 0;
	a->peak = 0;
	a->name = name;
	return true;
}

void pnx_arena_destroy(PnxArena* a)
{
	if (!a)
		return;
	free(a->raw);
	memset(a, 0, sizeof(*a));
}

void* pnx_arena_alloc(PnxArena* a, size_t bytes, size_t align)
{
	if (!a || !a->base || bytes == 0)
		return NULL;

	const size_t start = align_up(a->used, align);
	if (start + bytes > a->capacity)
		return NULL;  // caller decides how to fail

	void* p = a->base + start;
	a->used = start + bytes;
	if (a->used > a->peak)
		a->peak = a->used;
	return p;
}

void* pnx_arena_calloc(PnxArena* a, size_t bytes, size_t align)
{
	void* p = pnx_arena_alloc(a, bytes, align);
	if (p)
		memset(p, 0, bytes);
	return p;
}

void pnx_arena_reset(PnxArena* a)
{
	if (!a)
		return;
	// peak is deliberately preserved across resets: it is the number you budget against,
	// and it would be useless if a scene change cleared it.
	a->used = 0;
}