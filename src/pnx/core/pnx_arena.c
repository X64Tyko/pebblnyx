#include "pnx_arena.h"

#include "../platform/pnx_platform.h"
#include "../pnx_config.h"

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
	a->base					= (uint8_t*)aligned;
	a->capacity				= capacity;
	a->used					= 0;
	a->used_hi				= 0;
	a->peak					= 0;
	a->name					= name;
	return true;
}

bool pnx_arena_init_max(PnxArena* a, const char* name, size_t reserve, size_t align)
{
	const size_t free_bytes = pnx_platform_heap_free_bytes();
	if (free_bytes <= reserve)
		return false;

	size_t want = free_bytes - reserve;
	// The platform's own "free heap" figure and the biggest single block malloc can
	// actually hand back are not always the same number once fragmentation is real, so
	// back off geometrically rather than failing outright on the first miss.
	const size_t floor = 1024;
	while (want >= floor)
	{
		if (pnx_arena_init(a, name, want, align))
			return true;
		want -= want / 10;
	}
	return false;
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
	if (start + bytes > a->capacity - a->used_hi)
		return NULL; // caller decides how to fail

	void* p = a->base + start;
	a->used = start + bytes;
	if (a->used + a->used_hi > a->peak)
		a->peak = a->used + a->used_hi;
	return p;
}

void* pnx_arena_calloc(PnxArena* a, size_t bytes, size_t align)
{
	void* p = pnx_arena_alloc(a, bytes, align);
	if (p)
		memset(p, 0, bytes);
	return p;
}

void* pnx_arena_alloc_hi(PnxArena* a, size_t bytes, size_t align)
{
	if (!a || !a->base || bytes == 0)
		return NULL;
	if (align < 1)
		align = 1;

	// Aligning a high-end allocation means aligning its START, which sits `bytes`
	// below the current top -- so the candidate start has to be computed first and
	// rounded down, not up, or the block could walk past the low cursor unaligned.
	if (bytes > a->capacity - a->used - a->used_hi)
		return NULL; // caller decides how to fail

	const size_t top		  = a->capacity - a->used_hi;
	const uintptr_t candidate = (uintptr_t)(a->base + top - bytes);
	const uintptr_t start	  = candidate & ~((uintptr_t)align - 1);
	const size_t offset		  = (size_t)(start - (uintptr_t)a->base);
	if (offset < a->used)
		return NULL; // alignment pushed it into the low side's territory

	a->used_hi = a->capacity - offset;
	if (a->used + a->used_hi > a->peak)
		a->peak = a->used + a->used_hi;
	return (void*)start;
}

void* pnx_arena_calloc_hi(PnxArena* a, size_t bytes, size_t align)
{
	void* p = pnx_arena_alloc_hi(a, bytes, align);
	if (p)
		memset(p, 0, bytes);
	return p;
}

void pnx_arena_reset(PnxArena* a)
{
	if (!a)
		return;
	// peak is deliberately preserved across resets: it is the number you budget against,
	// and it would be useless if it cleared along with the rest.
	a->used	   = 0;
	a->used_hi = 0;
}

void pnx_arena_reset_hi(PnxArena* a)
{
	if (!a)
		return;
	// Only the high (scene) side resets -- the low (persistent) side is left exactly
	// as it was, which is the entire reason this is a separate function from
	// pnx_arena_reset rather than that function's only behavior.
	a->used_hi = 0;
}
