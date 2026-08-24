#include "pnx_assets.h"

#if PNX_USE_ASSETS

#include "pnx_lzss.h"
#include "pnx_bitplane.h"
#include "../platform/pnx_platform.h"
#include "../core/pnx_diag.h"

#include <string.h>

#ifndef PNX_SCENE_MAX_ATLASES
#define PNX_SCENE_MAX_ATLASES 4
#endif
#ifndef PNX_SCENE_MAX_SPRITES
#define PNX_SCENE_MAX_SPRITES 8
#endif
#ifndef PNX_SCENE_MAX_NINE_SLICES
#define PNX_SCENE_MAX_NINE_SLICES 4
#endif
// Two covers the shape E7 was written around -- a small HUD face and a larger dialogue
// one. Raising it costs sizeof(PnxFont) of bss per slot and nothing else.
#ifndef PNX_SCENE_MAX_FONTS
#define PNX_SCENE_MAX_FONTS 2
#endif

static const uint8_t* s_scene_table;
static uint8_t s_scene_count;

// 0 is a real orientation, so "nobody has said yet" needs a value of its own.
#define PNX_ORIENT_UNSET 0xFF
static uint8_t s_orientation = PNX_ORIENT_UNSET;

static PnxAtlas s_atlases[PNX_SCENE_MAX_ATLASES];
static PnxSprite s_sprites[PNX_SCENE_MAX_SPRITES];
static PnxNineSlice s_nine_slices[PNX_SCENE_MAX_NINE_SLICES];
static PnxFont s_fonts[PNX_SCENE_MAX_FONTS];
static PnxMap s_map;
static PnxDialog s_dialog;
static uint8_t s_atlas_count, s_sprite_count, s_nine_slice_count, s_font_count;
static bool s_have_map, s_have_dialog;

static PnxPalette* s_palettes;
static uint16_t s_palette_count;
// s_arena is the one double-ended arena the project handed pnx_assets_init; every load
// goes through it. s_persistent_mode says which end the NEXT load allocates from --
// true routes to the low/persistent cursor (pnx_arena_alloc), false to the high/scene
// one (pnx_arena_alloc_hi) -- replacing what used to be a choice between two separate
// arena pointers. pnx_assets_persistent flips it; pnx_scenes_load's temporary swap
// (below) saves and restores it the same way it used to save and restore s_arena.
static PnxArena* s_arena;
static bool s_persistent_mode;
static const uint32_t* s_resources;
static uint16_t s_resource_count;
static uint32_t s_bytes_loaded;

static void* arena_alloc(size_t bytes, size_t align)
{
	return s_persistent_mode ? pnx_arena_alloc(s_arena, bytes, align)
							 : pnx_arena_alloc_hi(s_arena, bytes, align);
}

static void* arena_calloc(size_t bytes, size_t align)
{
	return s_persistent_mode ? pnx_arena_calloc(s_arena, bytes, align)
							 : pnx_arena_calloc_hi(s_arena, bytes, align);
}

#define ARENA_ALLOC_ARRAY(type, count)	((type*)arena_alloc(sizeof(type) * (size_t)(count), _Alignof(type)))
#define ARENA_CALLOC_ARRAY(type, count) ((type*)arena_calloc(sizeof(type) * (size_t)(count), _Alignof(type)))

bool pnx_assets_init(PnxArena* arena, const uint32_t* resources, uint16_t count)
{
	if (!arena || !resources || count == 0)
		return false;
	s_arena			  = arena;
	s_persistent_mode = false;
	s_resources		  = resources;
	s_resource_count  = count;
	s_bytes_loaded	  = 0;
	s_palettes		  = NULL;
	s_palette_count	  = 0;
	// Cleared here rather than left standing, so a second init starts from no expectation
	// instead of inheriting one. Which is why the expectation is set AFTER init.
	s_orientation = PNX_ORIENT_UNSET;
	return true;
}

bool pnx_assets_expect_orientation(uint8_t orientation)
{
	if (orientation >= PNX_ORIENT_COUNT)
		return false;
	s_orientation = orientation;
	return true;
}

uint8_t pnx_assets_orientation(void)
{
	return s_orientation;
}

bool pnx_assets_persistent(bool on)
{
	const bool was	  = s_persistent_mode;
	s_persistent_mode = on;
	return was;
}

// ---------------------------------------------------------------------- palettes

static const uint8_t* load_blob(uint16_t asset_id, const char* magic, uint8_t* out_a,
								uint8_t* out_b, uint8_t* out_c, size_t* out_payload);

bool pnx_palettes_load(uint16_t asset_id)
{
#if PNX_DISPLAY_BW
	// A no-op, deliberately -- see PnxPalette's own comment (pnx_assets.h) for why nothing
	// on a 1-bit build ever indexes a palette. Kept callable, not compiled away, so game
	// init code stays the same source on every platform; the asset id argument is unused
	// because there is no resource to read.
	(void)asset_id;
	return true;
#else
	uint8_t count		= 0;
	size_t payload		= 0;
	const uint8_t* data = load_blob(asset_id, "PP", &count, NULL, NULL, &payload);
	if (!data)
		return false;

	if (count == 0 || payload != (size_t)count * sizeof(PnxPalette))
	{
		pnx_log("palettes %u: %u palettes needs %u bytes, blob has %u", asset_id, count,
				(unsigned)(count * sizeof(PnxPalette)), (unsigned)payload);
		return false;
	}
	if (count > PNX_PALETTE_SLOTS)
	{
		// Loud, because the alternative is drawing in whatever colours happen to be there,
		// which presents as an art bug rather than a configuration one.
		pnx_log("palettes %u: project needs %u slots, PNX_PALETTE_SLOTS is %u -- raise it",
				asset_id, count, PNX_PALETTE_SLOTS);
		return false;
	}

	s_palettes		= (PnxPalette*)(const void*)data;
	s_palette_count = count;
	return true;
#endif
}

const PnxPalette* pnx_palette(uint8_t slot)
{
	return slot < s_palette_count ? &s_palettes[slot] : NULL;
}

uint16_t pnx_palette_count(void)
{
	return s_palette_count;
}

#if !PNX_DISPLAY_BW
void pnx_decode_4bpp(const uint8_t* src, const PnxPalette* palette, uint8_t* dst,
					 uint16_t pixels)
{
	if (!palette)
		return;
	for (uint16_t i = 0; i < pixels; i += 2)
	{
		const uint8_t packed = src[i >> 1];
		const uint8_t hi = packed >> 4, lo = packed & 0x0F;
		if (hi != PNX_PALETTE_TRANSPARENT)
			dst[i] = palette->entries[hi];
		if (lo != PNX_PALETTE_TRANSPARENT)
			dst[i + 1] = palette->entries[lo];
	}
}
#endif // !PNX_DISPLAY_BW

uint32_t pnx_assets_bytes_loaded(void)
{
	return s_bytes_loaded;
}

// Reads a whole blob into the arena and checks its header.
//
// The size check is the one that earns its keep: it catches a resource that was
// truncated, half-written, or built by a different version of the pipeline. Without it
// those present as garbage pixels or a wild pointer, with nothing pointing at the cause.
// `dst` is where the blob lands: NULL to bump the scene arena, or a caller buffer of `cap`
// bytes. The second exists for the map's atlas pool, whose slots are reused as atlases are
// evicted and reloaded -- something a bump arena cannot express, and the only reason this
// takes a destination at all. Every check below is the same either way, which is the point:
// there is still exactly one door.
static const uint8_t* load_blob_into(uint16_t asset_id, const char* magic, uint8_t* dst,
									 size_t cap, uint8_t* out_a, uint8_t* out_b, uint8_t* out_c,
									 uint8_t* out_d, size_t* out_payload)
{
	if ((!s_arena && !dst) || asset_id >= s_resource_count)
	{
		pnx_log("asset %u: out of range (have %u)", asset_id, s_resource_count);
		return NULL;
	}

	const uint32_t resource = s_resources[asset_id];
	size_t size				= 0;
	if (!pnx_platform_resource_size(resource, &size) || size < PNX_BLOB_HEADER_BYTES)
	{
		pnx_log("asset %u: missing or too small (%u bytes)", asset_id, (unsigned)size);
		return NULL;
	}

	uint8_t* buf = dst;
	if (!buf)
	{
		buf = (uint8_t*)arena_alloc(size, 4);
		if (!buf)
		{
			pnx_log("asset %u: arena full, needed %u, %u free", asset_id, (unsigned)size,
					(unsigned)pnx_arena_remaining(s_arena));
			return NULL;
		}
	}
	else if (size > cap)
	{
		pnx_log("asset %u: %u bytes will not fit a %u-byte slot", asset_id, (unsigned)size,
				(unsigned)cap);
		return NULL;
	}

	// One call, whole blob. A resource_read's cost tracks the resource's own size, not the
	// bytes requested -- splitting this into a header read plus a payload read would cost
	// the whole size-driven touch twice, not an extra ~29us; see docs/MEASUREMENTS.md's
	// "Flash / resource reads". A map pays that price on purpose (pnx_map_load) because
	// sizing its resident block needs the preamble before the payload can be allocated;
	// nothing else here has that excuse.
	const size_t got = pnx_platform_resource_read(resource, 0, buf, size);
	if (got != size)
	{
		pnx_log("asset %u: short read, %u of %u", asset_id, (unsigned)got, (unsigned)size);
		return NULL;
	}
	s_bytes_loaded += (uint32_t)size;

	if (buf[0] != (uint8_t)magic[0] || buf[1] != (uint8_t)magic[1])
	{
		pnx_log("asset %u: wrong type, got %c%c want %s", asset_id, buf[0], buf[1], magic);
		return NULL;
	}
	if (buf[2] != PNX_BLOB_VERSION)
	{
		pnx_log("asset %u: format v%u, runtime expects v%u -- rebuild assets", asset_id, buf[2],
				PNX_BLOB_VERSION);
		return NULL;
	}

	// Orientation, checked here because this is the one door every blob comes through.
	//
	// Content is rotated at build time, so a portrait atlas in a landscape bundle is not a
	// mismatch the renderer could ever notice -- it is simply a picture lying on its side,
	// and a map whose walls are in the wrong places. That is a debugging session; this is
	// one comparison.
	//
	// Without an explicit expectation the first blob loaded sets it, which still catches
	// the case that actually happens: a rebuild in the other orientation that left one
	// stale resource behind. Call pnx_assets_expect_orientation(PNX_ORIENTATION) at
	// start-up -- the generated header defines it -- and even a uniformly stale bundle is
	// refused.
	if (s_orientation == PNX_ORIENT_UNSET)
	{
		s_orientation = buf[7];
	}
	else if (buf[7] != s_orientation)
	{
		pnx_log(
			"asset %u: built for orientation %u, project is %u -- stale resource, "
			"rebuild assets",
			asset_id, buf[7], s_orientation);
		return NULL;
	}

	if (out_a)
		*out_a = buf[3];
	if (out_b)
		*out_b = buf[4];
	if (out_c)
		*out_c = buf[5];
	if (out_d)
		*out_d = buf[6];
	if (out_payload)
		*out_payload = size - PNX_BLOB_HEADER_BYTES;

	return buf + PNX_BLOB_HEADER_BYTES;
}

static const uint8_t* load_blob_4(uint16_t asset_id, const char* magic, uint8_t* a, uint8_t* b,
								  uint8_t* c, uint8_t* d, size_t* payload)
{
	return load_blob_into(asset_id, magic, NULL, 0, a, b, c, d, payload);
}

static const uint8_t* load_blob(uint16_t asset_id, const char* magic, uint8_t* a, uint8_t* b,
								uint8_t* c, size_t* payload)
{
	return load_blob_4(asset_id, magic, a, b, c, NULL, payload);
}

const uint8_t* pnx_blob_load(uint16_t asset_id, const char* magic, uint8_t* a, uint8_t* b,
							 uint8_t* c, uint8_t* d, size_t* payload)
{
	return load_blob_4(asset_id, magic, a, b, c, d, payload);
}

// Both tables are padded to 4 so the pixel block starts aligned.
static size_t pad4(size_t n)
{
	return (n + 3u) & ~(size_t)3u;
}

// Byte-at-a-time, not a cast through a u16/u32 pointer: nothing here guarantees the
// alignment a direct dereference would need, and this runs cold enough (per blob, not per
// pixel) that the loop costs nothing worth avoiding it for.
static uint32_t read_u32(const uint8_t* p)
{
	return (uint32_t)p[0] | ((uint32_t)p[1] << 8) | ((uint32_t)p[2] << 16) |
		((uint32_t)p[3] << 24);
}

static uint16_t read_u16(const uint8_t* p)
{
	return (uint16_t)(p[0] | ((uint16_t)p[1] << 8));
}

// SCALED rects and COMPLEX masks, appended after an atlas's pixel payload (both layouts,
// both bit depths -- the shape data does not depend on either). Sparse, so most atlases
// have `remaining` bytes left over to say "none of either" in 4 bytes and stop.
//
// Incremental, not one `expected == payload` equality the way the fixed-size portion
// above is checked: the whole point of a sparse table is that its size is not knowable
// until its own count is read, so this walks forward and the caller checks what is left
// AFTER, in `*out_consumed`, against what the blob actually holds.
static bool parse_atlas_shapes(const uint8_t* p, size_t remaining, uint16_t tile_count,
							   uint8_t tile_px, uint16_t asset_id, PnxAtlas* out,
							   size_t* out_consumed)
{
	const size_t mask_bytes = ((size_t)tile_px * tile_px + 7) / 8;
	size_t at				= 0;

	if (remaining < at + 2)
		goto short_read;
	const uint16_t scaled_count = read_u16(p + at);
	at += 2;
	const size_t scaled_span = (size_t)scaled_count * 6;
	if (remaining < at + scaled_span)
		goto short_read;
	out->scaled_rects = p + at;
	out->scaled_count = scaled_count;
	at += scaled_span;

	if (remaining < at + 2)
		goto short_read;
	const uint16_t complex_count = read_u16(p + at);
	at += 2;
	const size_t complex_span = (size_t)complex_count * (2 + mask_bytes);
	if (remaining < at + complex_span)
		goto short_read;
	out->complex_masks = p + at;
	out->complex_count = complex_count;
	at += complex_span;

	// A tile index out of range means a corrupt or hand-built blob -- refused here rather
	// than left for pnx_atlas_tile_scaled_rect/pnx_atlas_tile_complex_mask to read
	// whatever a bad index happens to point at.
	for (uint16_t i = 0; i < scaled_count; i++)
	{
		const uint16_t t = read_u16(out->scaled_rects + (size_t)i * 6);
		if (t >= tile_count)
		{
			pnx_log("atlas %u: scaled rect %u names tile %u of %u", asset_id, i, t,
					tile_count);
			return false;
		}
	}
	for (uint16_t i = 0; i < complex_count; i++)
	{
		const uint16_t t = read_u16(out->complex_masks + (size_t)i * (2 + mask_bytes));
		if (t >= tile_count)
		{
			pnx_log("atlas %u: complex mask %u names tile %u of %u", asset_id, i, t,
					tile_count);
			return false;
		}
	}

	*out_consumed = at;
	return true;

short_read:
	pnx_log("atlas %u: shape tables run past the end of the blob", asset_id);
	return false;
}

// SCALED rects / COMPLEX masks, appended after a sprite's pixel payload -- same sparse
// shape as parse_atlas_shapes, keyed by FRAME index instead of tile id. A COMPLEX
// record's mask_bytes is NOT one shared constant the way an atlas's tile_px is: frames
// are not uniform size (see PnxSprite's own comment), so each record is only as wide as
// the frame it names, and this has to walk them one at a time rather than index by a
// fixed stride.
static bool parse_sprite_shapes(const uint8_t* p, size_t remaining, uint8_t frame_count,
								PnxSprite* out, uint16_t asset_id, size_t* out_consumed)
{
	size_t at = 0;

	if (remaining < at + 2)
		goto short_read;
	const uint16_t scaled_count = read_u16(p + at);
	at += 2;
	const size_t scaled_span = (size_t)scaled_count * 6;
	if (remaining < at + scaled_span)
		goto short_read;
	out->scaled_rects = p + at;
	out->scaled_count = scaled_count;
	at += scaled_span;
	for (uint16_t i = 0; i < scaled_count; i++)
	{
		const uint16_t f = read_u16(out->scaled_rects + (size_t)i * 6);
		if (f >= frame_count)
		{
			pnx_log("sprite %u: scaled rect %u names frame %u of %u", asset_id, i, f,
					frame_count);
			return false;
		}
	}

	if (remaining < at + 2)
		goto short_read;
	const uint16_t complex_count = read_u16(p + at);
	at += 2;
	out->complex_masks = p + at;
	out->complex_count = complex_count;
	for (uint16_t i = 0; i < complex_count; i++)
	{
		if (remaining < at + 2)
			goto short_read;
		const uint16_t f = read_u16(p + at);
		if (f >= frame_count)
		{
			pnx_log("sprite %u: complex mask %u names frame %u of %u", asset_id, i, f,
					frame_count);
			return false;
		}
		PnxSpriteFrame fr;
		pnx_sprite_frame_get(out, (uint8_t)f, &fr);
		const size_t mask_bytes = ((size_t)fr.w * fr.h + 7) / 8;
		const size_t rec		= 2 + mask_bytes;
		if (remaining < at + rec)
			goto short_read;
		at += rec;
	}

	*out_consumed = at;
	return true;

short_read:
	pnx_log("sprite %u: shape tables run past the end of the blob", asset_id);
	return false;
}

// `scratch` is PnxMap's own `lzss_src` when this atlas is being loaded into a map's pool
// slot (`dst` non-NULL), NULL otherwise -- only that path ever reads it, and only when
// PNX_USE_ATLAS_COMPRESS is on. See the compressed+dst branch below for why it is needed
// at all: decoding into `dst` in place would overwrite compressed bytes before they are
// fully read, since decoded output is never smaller than what produced it.
static bool atlas_load_into(PnxAtlas* out, uint16_t asset_id, uint8_t* dst, size_t cap,
							uint8_t* scratch)
{
#if !PNX_DISPLAY_BW
	// A 1-bit build never indexes a palette at all (see PnxPalette's own comment,
	// pnx_assets.h) -- its pixels are already ink/paper/dither, baked in at build time --
	// so there is nothing to require loaded first.
	if (!s_palettes)
	{
		pnx_log("atlas %u: load palettes first -- atlases carry indices, not colours",
				asset_id);
		return false;
	}
#endif

	uint8_t tile_px = 0, count_lo = 0, layout = 0, atlas_flags = 0;
	size_t payload		= 0;
	const uint8_t* data = load_blob_into(asset_id, "PA", dst, cap, &tile_px, &count_lo,
										 &layout, &atlas_flags, &payload);
	if (!data)
		return false;

	const uint16_t tile_count = count_lo;
	// One format, unconditionally: w*h/4 (~bw, pack_unit_2bpp in tools/pnx_assets.py) on a
	// 1-bit build, w*h/2 (4bpp indexed) on a colour one. A build is exclusively one or the
	// other (PNX_DISPLAY_BW) -- there is no per-blob format to detect any more.
#if PNX_DISPLAY_BW
	const size_t tile_bytes = (size_t)tile_px * tile_px / 4;
#else
	const size_t tile_bytes = (size_t)tile_px * tile_px / 2;
#endif
	const size_t tables = pad4(tile_count) * 2;

	out->metatiles	   = NULL;
	out->subtile_count = 0;
	out->sub_bytes	   = 0;

	// `pixel_off`/`pixel_len` describe the LOGICAL (uncompressed, resident) layout either
	// way -- metadata (assign/flags/metatile table) sits at the same offsets whether the
	// pixel body that follows is compressed on disk or not, so this half is unaffected by
	// compression and unchanged from before this feature existed.
	size_t pixel_off, pixel_len, subs = 0;

	if (layout == 0)
	{
		if (tile_px == 0 || tile_count == 0)
		{
			pnx_log("atlas %u: %u tiles of %upx -- refused", asset_id, tile_count, tile_px);
			return false;
		}
		pixel_off = tables;
		pixel_len = (size_t)tile_count * tile_bytes;

		out->tile_bytes	  = (uint8_t)tile_bytes;
		out->tile_palette = data;
		out->tile_flags	  = data + pad4(tile_count);
	}
	else
	{
		// u16 subtile_count, u16 pad, palettes, flags, metatile table, quadrant bank.
		if (payload < 4)
			return false;
		subs					 = (size_t)(data[0] | (data[1] << 8));
		const size_t sub_bytes	 = tile_bytes / 4;
		const size_t table_bytes = (size_t)tile_count * 4 * 2;

		if (tile_px == 0 || tile_count == 0 || subs == 0)
		{
			pnx_log("atlas %u: %u tiles, %u subtiles -- refused", asset_id, tile_count,
					(unsigned)subs);
			return false;
		}
		pixel_off = 4 + tables + table_bytes;
		pixel_len = subs * sub_bytes;

		out->tile_bytes	   = (uint8_t)tile_bytes;
		out->tile_palette  = data + 4;
		out->tile_flags	   = data + 4 + pad4(tile_count);
		out->metatiles	   = (const uint16_t*)(const void*)(data + 4 + tables);
		out->subtile_count = (uint16_t)subs;
		out->sub_bytes	   = (uint8_t)sub_bytes;

		for (uint32_t i = 0; i < (uint32_t)tile_count * 4; i++)
		{
			if (out->metatiles[i] >= subs)
			{
				pnx_log("atlas %u: metatile quadrant %u indexes subtile %u of %u", asset_id,
						(unsigned)i, out->metatiles[i], (unsigned)subs);
				return false;
			}
		}
	}

	out->tile_px	= tile_px;
	out->tile_count = tile_count;

	const size_t expected = pixel_off + pixel_len; // uncompressed total through pixels

	// Bit 0 of the atlas blob's own spare header byte: this atlas's pixel body is
	// LZSS-compressed (`compress_atlases`). Same posture as compressed sprites/maps: a
	// blob built compressed against a runtime with the flag off refuses to load rather
	// than reading garbage.
	const bool compressed = (atlas_flags & 1) != 0;
#if !PNX_USE_ATLAS_COMPRESS
	if (compressed)
	{
		pnx_log(
			"atlas %u: built with compress_atlases, but PNX_USE_ATLAS_COMPRESS is 0 -- set "
			"it in pnx_config.h, or rebuild the project without compress_atlases",
			asset_id);
		return false;
	}
#endif

	const uint8_t* pixels_ptr;
	const uint8_t* shapes_ptr;
	size_t shapes_budget;

#if PNX_USE_ATLAS_COMPRESS
	if (compressed)
	{
		// 2-byte compressed length, then 2-byte TRUE uncompressed pixel length, then the
		// LZSS stream itself (tools/pnx_assets.py's compress_pixel_body, tag_len=True for
		// atlases only). The true length is redundant with `pixel_len` in the ordinary
		// case -- both sides agree on tile_px/tile_count/build depth -- but it is the ONLY
		// place that agreement is actually checked rather than assumed: pnx_lzss_decode
		// stops at whichever of `dst_len`/`src_len` is smaller BY DESIGN (a bounded decode
		// into a too-small buffer is a real, load-bearing case elsewhere -- see its own
		// comment), so it cannot double as this check, and tile_count alone is bit-depth
		// independent, so it cannot catch a colour blob loaded on a PNX_DISPLAY_BW build
		// (or the reverse) either. Without this field that mismatch decodes "successfully"
		// into half the pixel data it should, silently, rather than refusing.
		if (payload < pixel_off + 4)
		{
			pnx_log("atlas %u: too small for a compressed length prefix", asset_id);
			return false;
		}
		const uint8_t* len_field	  = data + pixel_off;
		const size_t compressed_len	  = (size_t)(len_field[0] | ((size_t)len_field[1] << 8));
		const size_t true_pixel_len	  = (size_t)(len_field[2] | ((size_t)len_field[3] << 8));
		const size_t compressed_start = pixel_off + 4;
		if (true_pixel_len != pixel_len)
		{
			pnx_log(
				"atlas %u: built for %u pixel bytes, this build's tile_px/depth expects "
				"%u -- wrong-depth or stale resource",
				asset_id, (unsigned)true_pixel_len, (unsigned)pixel_len);
			return false;
		}
		if (payload < compressed_start + compressed_len)
		{
			pnx_log("atlas %u: compressed pixel stream runs past the blob", asset_id);
			return false;
		}

		if (dst)
		{
			// The compressed bytes + shapes tail are still sitting in `dst` at `pixel_off`
			// (load_blob_into read the whole, smaller, compressed blob straight in) --
			// decoding pixels there in place would overwrite them before the shapes copy
			// below gets to read them, so they are preserved in the map's own scratch
			// first. `scratch` is sized (pnx_map_load's max_scratch) to cover at least one
			// pool slot's uncompressed size, comfortably more than `payload - pixel_off`.
			const size_t preserve = payload - pixel_off;
			memcpy(scratch, data + pixel_off, preserve);

			uint8_t* dst_pixels = dst + PNX_BLOB_HEADER_BYTES + pixel_off;
			const size_t got	= pnx_lzss_decode(scratch + 4, compressed_len, dst_pixels, pixel_len);
			if (got != pixel_len)
			{
				pnx_log("atlas %u: LZSS stream decoded %u of %u expected bytes", asset_id,
						(unsigned)got, (unsigned)pixel_len);
				return false;
			}
			const size_t shapes_len = preserve - 4 - compressed_len;
			memcpy(dst_pixels + pixel_len, scratch + 4 + compressed_len, shapes_len);

			pixels_ptr	  = data + pixel_off;
			shapes_ptr	  = data + expected;
			shapes_budget = shapes_len;
		}
		else
		{
			uint8_t* decoded = (uint8_t*)arena_alloc(pixel_len, 4);
			if (!decoded)
			{
				pnx_log("atlas %u: arena full, needed %u for decoded pixels, %u free",
						asset_id, (unsigned)pixel_len, (unsigned)pnx_arena_remaining(s_arena));
				return false;
			}
			const size_t got = pnx_lzss_decode(data + compressed_start, compressed_len,
											   decoded, pixel_len);
			if (got != pixel_len)
			{
				pnx_log("atlas %u: LZSS stream decoded %u of %u expected bytes", asset_id,
						(unsigned)got, (unsigned)pixel_len);
				return false;
			}
			pixels_ptr	  = decoded;
			shapes_ptr	  = data + compressed_start + compressed_len;
			shapes_budget = payload - compressed_start - compressed_len;
		}
	}
	else
#endif
	{
		if (payload < expected)
		{
			pnx_log("atlas %u: %u tiles of %upx needs %u bytes, blob has %u", asset_id,
					tile_count, tile_px, (unsigned)expected, (unsigned)payload);
			return false;
		}
		pixels_ptr	  = data + pixel_off;
		shapes_ptr	  = data + expected;
		shapes_budget = payload - expected;
	}
	out->pixels = pixels_ptr;

	// SCALED rects / COMPLEX masks, appended after the pixel payload in either layout.
	size_t shapes_consumed = 0;
	if (!parse_atlas_shapes(shapes_ptr, shapes_budget, tile_count, tile_px, asset_id, out,
							&shapes_consumed))
		return false;
	if (shapes_consumed != shapes_budget)
	{
		pnx_log("atlas %u: %u bytes past the shape tables go unaccounted for", asset_id,
				(unsigned)(shapes_budget - shapes_consumed));
		return false;
	}

#if !PNX_DISPLAY_BW
	// tile_palette carries a slot per tile regardless of build -- the pipeline never
	// strips it from a ~bw blob, since it is cheap and a project may want it back one day
	// -- but nothing on a 1-bit build looks a palette up by it, so there is nothing to
	// validate here either.
	for (uint16_t i = 0; i < tile_count; i++)
	{
		if (out->tile_palette[i] >= s_palette_count)
		{
			pnx_log("atlas %u: tile %u wants palette %u, only %u loaded", asset_id, i,
					out->tile_palette[i], s_palette_count);
			return false;
		}
	}
#endif
	return true;
}

bool pnx_atlas_load(PnxAtlas* out, uint16_t asset_id)
{
	return atlas_load_into(out, asset_id, NULL, 0, NULL);
}

bool pnx_sprite_load(PnxSprite* out, uint16_t asset_id)
{
#if !PNX_DISPLAY_BW
	if (!s_palettes)
	{
		pnx_log("sprite %u: load palettes first", asset_id);
		return false;
	}
#endif

	uint8_t frame_count = 0;
	uint8_t flags		= 0;
	size_t payload		= 0;
	const uint8_t* data = load_blob(asset_id, "PS", &frame_count, &flags, NULL, &payload);
	if (!data)
		return false;
	if (frame_count == 0)
	{
		pnx_log("sprite %u: zero frames", asset_id);
		return false;
	}

	// Bit 0: pixels are LZSS-compressed (compress_sprites = true in the manifest). Bit 1:
	// bitplane/Elias-gamma-compressed (compress_sprites = "bitplane", experimental --
	// pnx_bitplane.h). Never both -- tools/pnx_assets.py's finish_sprite picks exactly
	// one path per build. A sprite built against a runtime with the matching
	// PNX_USE_*_COMPRESS off refuses to load rather than reading garbage pixels -- same
	// posture as compressed maps/atlases.
	const bool compressed = (flags & 1) != 0;
	const bool bitplane	  = (flags & 2) != 0;
#if !PNX_USE_SPRITE_COMPRESS
	if (compressed)
	{
		pnx_log("sprite %u: built LZSS-compressed, PNX_USE_SPRITE_COMPRESS is off here",
				asset_id);
		return false;
	}
#endif
#if !PNX_USE_BITPLANE_COMPRESS
	if (bitplane)
	{
		pnx_log(
			"sprite %u: built bitplane-compressed, PNX_USE_BITPLANE_COMPRESS is off "
			"here",
			asset_id);
		return false;
	}
#endif

	// frame_meta, then frame_palette (padded to 4), then either every frame's pixels
	// packed back-to-back or (compressed) a 2-byte COMPRESSED length followed by that
	// many bytes of LZSS stream, then the sparse SCALED/COMPLEX shape tables -- see
	// PnxSprite's own comment. The length is the compressed count, not the uncompressed
	// one: uncompressed size is already derivable from frame_meta (what sizes the
	// decode buffer below), but nothing else says where the stream ends and shapes
	// begin, since pnx_lzss_decode reports bytes written, not bytes consumed. Not one
	// `expected == payload` equality the way the old fixed-stride format could check: a
	// frame's own pixel span comes from its own record, and the shape
	// tables are sparse, so this is validated incrementally instead.
	const size_t meta_span = (size_t)frame_count * PNX_SPRITE_FRAME_BYTES;
	const size_t pal_span  = pad4(frame_count);
	if (payload < meta_span + pal_span)
	{
		pnx_log("sprite %u: %u bytes too small for %u frame(s)", asset_id, (unsigned)payload,
				frame_count);
		return false;
	}

	out->frame_meta	   = data;
	out->frame_palette = data + meta_span;
	out->frame_count   = frame_count;
	out->scaled_rects  = NULL;
	out->complex_masks = NULL;
	out->scaled_count  = 0;
	out->complex_count = 0;

	// The logical (uncompressed) size of the pixel region -- what a decode buffer must
	// be sized to, and, uncompressed, what the raw blob's pixel region itself spans.
	// Computed the same way regardless of compression: a frame's offset/w/h are always
	// logical (post-decode) values.
	size_t pixel_logical_size = 0;
	for (uint8_t i = 0; i < frame_count; i++)
	{
		const uint8_t* e   = out->frame_meta + (size_t)i * PNX_SPRITE_FRAME_BYTES;
		const uint32_t off = (uint32_t)(e[0] | ((uint32_t)e[1] << 8));
		const uint8_t w = e[2], h = e[3];
#if PNX_DISPLAY_BW
		// pack_unit_2bpp (tools/pnx_assets.py) pads its last, possibly-partial group of 4
		// pixels out to a full byte rather than requiring w*h to divide evenly -- so the
		// byte count here has to round up the same way, not truncate. A w*h%2==0 check
		// (right for the 4bpp branch below) is not strict enough to catch every mismatch:
		// w*h=646 is even but still needs ceil(646/4)=162 bytes, one more than 646/4=161
		// truncates to, and the byte that formula silently dropped is exactly what a real
		// sprite frame (Need4Pebble's touring_crash, 19x34 rotated) hit first.
		if (w == 0 || h == 0)
		{
			pnx_log("sprite %u: frame %u is %ux%u, invalid for 2bpp packing", asset_id, i, w,
					h);
			return false;
		}
		const size_t fb = ((size_t)w * h + 3) / 4;
#else
		// One format, unconditionally -- see atlas_load_into's own comment. 4bpp packs
		// two pixels per byte with no padding (pack_unit_4bpp), so w*h must be exactly
		// even or the packer itself would have failed at build time.
		if (w == 0 || h == 0 || (((uint32_t)w * h) % 2) != 0)
		{
			pnx_log("sprite %u: frame %u is %ux%u, invalid for 4bpp packing", asset_id, i, w,
					h);
			return false;
		}
		const size_t fb = (size_t)w * h / 2;
#endif
		const size_t end = (size_t)off + fb;
		if (!compressed && !bitplane)
		{
			// Uncompressed, pixels live directly in the blob, so this doubles as the
			// bounds check the compressed path instead gets from pnx_lzss_decode's own
			// dst_len bound (the bitplane path gets its own bound from
			// pnx_bitplane_decode's own n/scratch_cap arguments, same idea).
			if (end > payload - meta_span - pal_span)
			{
				pnx_log("sprite %u: frame %u's pixels run past the blob", asset_id, i);
				return false;
			}
		}
		if (end > pixel_logical_size)
			pixel_logical_size = end;
	}

	const uint8_t* shapes_src;
	size_t shapes_budget;
	if (compressed)
	{
#if PNX_USE_SPRITE_COMPRESS
		if (payload < meta_span + pal_span + 2)
		{
			pnx_log("sprite %u: too small for a compressed length prefix", asset_id);
			return false;
		}
		const uint8_t* len_field	  = data + meta_span + pal_span;
		const size_t compressed_len	  = (size_t)(len_field[0] | ((size_t)len_field[1] << 8));
		const size_t compressed_start = meta_span + pal_span + 2;
		if (payload < compressed_start + compressed_len)
		{
			pnx_log("sprite %u: compressed pixel stream runs past the blob", asset_id);
			return false;
		}

		uint8_t* decoded = (uint8_t*)arena_alloc(pixel_logical_size, 4);
		if (!decoded)
		{
			pnx_log("sprite %u: arena full, needed %u for decoded pixels, %u free", asset_id,
					(unsigned)pixel_logical_size, (unsigned)pnx_arena_remaining(s_arena));
			return false;
		}
		const size_t decoded_got = pnx_lzss_decode(data + compressed_start, compressed_len,
												   decoded, pixel_logical_size);
		if (decoded_got != pixel_logical_size)
		{
			pnx_log("sprite %u: LZSS stream decoded %u of %u expected bytes", asset_id,
					(unsigned)decoded_got, (unsigned)pixel_logical_size);
			return false;
		}
		out->pixels	  = decoded;
		shapes_src	  = data + compressed_start + compressed_len;
		shapes_budget = payload - compressed_start - compressed_len;
#else
		return false; // unreachable: refused above when PNX_USE_SPRITE_COMPRESS is off
#endif
	}
	else if (bitplane)
	{
#if PNX_USE_BITPLANE_COMPRESS
		// Recovers the SAME deduped-unit ordering tools/pnx_assets.py's
		// compress_sprite_pixels_bitplane used (ascending DISTINCT frame_meta offsets)
		// -- see that function's own comment for why this is derived here rather than
		// stored on disk: frame_meta already has to be loaded either way, so this is one
		// dedup computation arrived at twice, not a second copy of the same information.
		// O(frame_count^2), fine at the frame counts a sprite sheet actually has.
		uint16_t unit_count		 = 0;
		uint32_t max_unit_pixels = 0;
		{
			int32_t last = -1;
			for (;;)
			{
				int32_t best   = -1;
				uint8_t best_w = 0, best_h = 0;
				for (uint8_t i = 0; i < frame_count; i++)
				{
					const uint8_t* e  = out->frame_meta + (size_t)i * PNX_SPRITE_FRAME_BYTES;
					const int32_t off = (int32_t)(e[0] | ((uint32_t)e[1] << 8));
					if (off > last && (best == -1 || off < best))
					{
						best   = off;
						best_w = e[2];
						best_h = e[3];
					}
				}
				if (best == -1)
					break;
				const uint32_t px = (uint32_t)best_w * best_h;
				if (px > max_unit_pixels)
					max_unit_pixels = px;
				unit_count++;
				last = best;
			}
		}

		const size_t table_bytes = (size_t)(unit_count + 1) * 2;
		if (payload < meta_span + pal_span + table_bytes)
		{
			pnx_log("sprite %u: too small for a %u-unit bitplane offset table", asset_id,
					(unsigned)unit_count);
			return false;
		}
		const uint8_t* table	   = data + meta_span + pal_span;
		const uint8_t* stream_base = table + table_bytes;
		const size_t stream_budget = payload - meta_span - pal_span - table_bytes;

		uint8_t* decoded = (uint8_t*)arena_alloc(pixel_logical_size, 4);
		uint8_t* scratch = (uint8_t*)arena_alloc(max_unit_pixels, 4);
		if (!decoded || !scratch)
		{
			pnx_log("sprite %u: arena full for bitplane decode, needed %u + %u, %u free",
					asset_id, (unsigned)pixel_logical_size, (unsigned)max_unit_pixels,
					(unsigned)pnx_arena_remaining(s_arena));
			return false;
		}

		int32_t last = -1;
		for (uint16_t u = 0; u < unit_count; u++)
		{
			int32_t best   = -1;
			uint8_t best_w = 0, best_h = 0;
			for (uint8_t i = 0; i < frame_count; i++)
			{
				const uint8_t* e  = out->frame_meta + (size_t)i * PNX_SPRITE_FRAME_BYTES;
				const int32_t off = (int32_t)(e[0] | ((uint32_t)e[1] << 8));
				if (off > last && (best == -1 || off < best))
				{
					best   = off;
					best_w = e[2];
					best_h = e[3];
				}
			}
			last = best;

			const uint16_t start = read_u16(table + (size_t)u * 2);
			const uint16_t stop	 = read_u16(table + (size_t)(u + 1) * 2);
			if (stop < start || (size_t)stop > stream_budget)
			{
				pnx_log("sprite %u: bitplane unit %u's offsets run past the blob", asset_id,
						u);
				return false;
			}
			const uint16_t n = (uint16_t)((uint32_t)best_w * best_h);
			if (!pnx_bitplane_decode(stream_base + start, (size_t)(stop - start),
									 decoded + best, scratch, n))
			{
				pnx_log("sprite %u: bitplane unit %u failed to decode", asset_id, u);
				return false;
			}
		}

		out->pixels				  = decoded;
		const uint16_t stream_end = read_u16(table + (size_t)unit_count * 2);
		shapes_src				  = stream_base + stream_end;
		shapes_budget			  = stream_budget - stream_end;
#else
		return false; // unreachable: refused above when PNX_USE_BITPLANE_COMPRESS is off
#endif
	}
	else
	{
		out->pixels	  = data + meta_span + pal_span;
		shapes_src	  = out->pixels + pixel_logical_size;
		shapes_budget = payload - meta_span - pal_span - pixel_logical_size;
	}

	size_t shapes_consumed = 0;
	if (!parse_sprite_shapes(shapes_src, shapes_budget, frame_count, out, asset_id,
							 &shapes_consumed))
		return false;
	if (shapes_consumed != shapes_budget)
	{
		pnx_log("sprite %u: %u bytes past the shape tables go unaccounted for", asset_id,
				(unsigned)(shapes_budget - shapes_consumed));
		return false;
	}

#if !PNX_DISPLAY_BW
	for (uint8_t i = 0; i < frame_count; i++)
	{
		if (out->frame_palette[i] >= s_palette_count)
		{
			pnx_log("sprite %u: frame %u wants palette %u, only %u loaded", asset_id, i,
					out->frame_palette[i], s_palette_count);
			return false;
		}
	}
#endif
	return true;
}

bool pnx_nineslice_load(PnxNineSlice* out, uint16_t asset_id)
{
#if !PNX_DISPLAY_BW
	if (!s_palettes)
	{
		pnx_log("nine_slice %u: load palettes first", asset_id);
		return false;
	}
#endif

	uint8_t w = 0, h = 0;
	size_t payload		= 0;
	const uint8_t* data = load_blob(asset_id, "N9", &w, &h, NULL, &payload);
	if (!data)
		return false;

	// One packed image, not nine -- see PnxNineSlice's own comment. The four border bytes
	// come first in the body (not in the fixed header: it has one spare field beyond
	// w/h, not four), then the pixels, same as sprite frame data.
	//
	// BW rounds up (pack_unit_2bpp pads its last partial 4-pixel group to a full byte,
	// so a w*h not divisible by 4 still costs a whole byte for the remainder) -- see
	// pnx_sprite_load's own comment on this exact formula for why w*h%2==0 is not a
	// strict enough guard to catch it.
#if PNX_DISPLAY_BW
	const size_t pixel_bytes = ((size_t)w * h + 3) / 4;
#else
	const size_t pixel_bytes = (size_t)w * h / 2;
#endif
	const size_t expected = 4 + pixel_bytes;
	if (w == 0 || h == 0 || payload != expected)
	{
		pnx_log("nine_slice %u: %ux%u needs %u bytes, blob has %u", asset_id, w, h,
				(unsigned)expected, (unsigned)payload);
		return false;
	}

	const uint8_t bl = data[0], bt = data[1], br = data[2], bb = data[3];
	if ((uint16_t)(bl + br) > w || (uint16_t)(bt + bb) > h)
	{
		pnx_log("nine_slice %u: borders %u/%u/%u/%u exceed %ux%u", asset_id, bl, bt, br, bb, w,
				h);
		return false;
	}

	out->pixels	  = data + 4;
	out->w		  = w;
	out->h		  = h;
	out->border_l = bl;
	out->border_t = bt;
	out->border_r = br;
	out->border_b = bb;
	return true;
}

// ---------------------------------------------------------------------------- maps
//
// The map is the one asset that is not read whole. Its preamble -- the atlas table, the
// flag table, the warps and the WorldTile index -- goes into the scene arena and stays;
// its cells arrive a WorldTile at a time into a pool, and its atlases into a second pool
// beside them.
//
// This is a deliberate exception to the residency rule at the top of pnx_assets.h, and it
// is worth being exact about why. That rule rejects streaming a TILE at a time: ~29 us per
// read call, against 725 tiles on screen, is 6.7 ms of frame spent reading art that is
// already in RAM. A WorldTile is a different trade -- one call brings 256 cells, so the
// per-call cost is amortised 256-fold, and it is paid when the player crosses a boundary
// rather than every frame. The rule is unchanged; the unit is.

// Defined below with the rest of the streamer; pnx_map_load calls it to fill a map small
// enough to be held whole before it returns.
static bool worldtile_load_run(PnxMap* m, PnxMapLayer* l, uint32_t first, uint8_t slot,
							   uint8_t count);

// log2 for the power-of-two WorldTile size, so a cell finds its WorldTile by shifting.
// The pipeline refuses anything that is not a power of two, so this cannot fail.
static uint8_t shift_of(uint8_t n)
{
	uint8_t s = 0;
	while ((1u << s) < n)
		s++;
	return s;
}

// A generously-sized worst-case probe: the blob header, the 12-byte shared fixed section,
// the atlas table at its largest (PNX_MAP_MAX_ATLASES entries), the pool_offset table at
// its largest, and the per-layer fixed directory at its largest (PNX_MAP_MAX_LAYERS
// entries) -- every one of those is capped by a compile-time constant, so one read this
// size always covers all of it regardless of what a real map actually declares, the same
// way the old single-layer probe was sized to the (then-fixed) preamble header. What comes
// after this -- each layer's own wt_mask, the palette, the warps, the cell dictionary -- is
// genuinely variable length, and is read a second time once this probe has said how big.
#define MAP_PROBE_BYTES                                                                     \
	(PNX_BLOB_HEADER_BYTES + 12 + PNX_MAP_MAX_ATLASES * 4 + (PNX_MAP_MAX_ATLASES + 1) * 4 + \
	 PNX_MAP_MAX_LAYERS * 14)

bool pnx_map_load(PnxMap* out, uint16_t asset_id)
{
	if (!s_arena || asset_id >= s_resource_count)
	{
		pnx_log("map %u: out of range (have %u)", asset_id, s_resource_count);
		return false;
	}
	const uint32_t resource = s_resources[asset_id];

	// The blob header's four generic bytes now carry layer_count/primary_layer/warp_count
	// (v14) rather than a single grid's own w/h/warp_count/worldtile -- see MAP_PROBE_BYTES'
	// own comment for why one fixed-size probe still covers everything up to the genuinely
	// variable tables (each layer's wt_mask, the palette, the warps, the cell dictionary).
	//
	// MAP_PROBE_BYTES is a worst-case UPPER bound (8 atlases, 4 layers), not a guaranteed
	// lower one -- a small map's WHOLE resource (this is the map's own resident preamble,
	// never the banks) can be smaller than that, and asking to read past the end of a real
	// resource is refused outright rather than handed back short. So the probe is clamped
	// to the resource's real size first; what matters is only that the clamped read still
	// reaches every fixed field this function indexes into `head` for below, which the
	// checks right after it confirm explicitly rather than assuming.
	size_t resource_bytes = 0;
	if (!pnx_platform_resource_size(resource, &resource_bytes))
	{
		pnx_log("map %u: resource missing", asset_id);
		return false;
	}
	uint8_t head[MAP_PROBE_BYTES];
	size_t probe_len = resource_bytes < sizeof(head) ? resource_bytes : sizeof(head);
	if (probe_len < PNX_BLOB_HEADER_BYTES + 12 ||
		pnx_platform_resource_read(resource, 0, head, probe_len) != probe_len)
	{
		pnx_log("map %u: too small to hold a header", asset_id);
		return false;
	}
	if (head[0] != 'P' || head[1] != 'M')
	{
		pnx_log("map %u: wrong type, got %c%c want PM", asset_id, head[0], head[1]);
		return false;
	}
	if (head[2] != PNX_BLOB_VERSION)
	{
		pnx_log("map %u: format v%u, runtime expects v%u -- rebuild assets", asset_id, head[2],
				PNX_BLOB_VERSION);
		return false;
	}
	if (s_orientation != PNX_ORIENT_UNSET && head[7] != s_orientation)
	{
		pnx_log("map %u: built for orientation %u, project is %u -- stale resource", asset_id,
				head[7], s_orientation);
		return false;
	}

	const uint8_t layer_count = head[3], primary_layer = head[4], warps = head[5];
	if (layer_count == 0 || layer_count > PNX_MAP_MAX_LAYERS || primary_layer >= layer_count)
	{
		pnx_log("map %u: %u layers, primary %u -- refused", asset_id, layer_count,
				primary_layer);
		return false;
	}

	const uint8_t* pre		  = head + PNX_BLOB_HEADER_BYTES;
	const uint8_t atlas_count = pre[0], tile_px = pre[1], flags = pre[2], atlas_slots = pre[3];
	const uint16_t tile_total = read_u16(pre + 4);
	const uint16_t dict_count = read_u16(pre + 6);
	const uint32_t pool_bytes = read_u32(pre + 8);
	const uint8_t idx_width	  = dict_count <= 256 ? 1 : 2;

	if (tile_px == 0 || atlas_count == 0 || atlas_count > PNX_MAP_MAX_ATLASES ||
		atlas_slots == 0 || atlas_slots > atlas_count || dict_count == 0)
	{
		pnx_log("map %u: %u atlases in %u slots, %u dict entries -- refused", asset_id,
				atlas_count, atlas_slots, dict_count);
		return false;
	}

	// The per-layer fixed directory sits right after the shared atlas + pool_offset tables,
	// both sized now that atlas_count/atlas_slots are known. MAP_PROBE_BYTES covers this
	// whole region at ITS worst case, but the probe actually read was clamped to the
	// resource's real size above -- so this checks the real map, not the format ceiling,
	// actually reached that far before indexing into it.
	const size_t fixed_region =
		12 + (size_t)atlas_count * 4 + ((size_t)atlas_slots + 1) * 4 + (size_t)layer_count * 14;
	if (PNX_BLOB_HEADER_BYTES + fixed_region > probe_len)
	{
		pnx_log("map %u: %u atlases/%u layers need %u B of fixed header, resource has %u",
				asset_id, atlas_count, layer_count, (unsigned)fixed_region,
				(unsigned)(probe_len - PNX_BLOB_HEADER_BYTES));
		return false;
	}
	const uint8_t* layer_dir =
		pre + 12 + (size_t)atlas_count * 4 + ((size_t)atlas_slots + 1) * 4;

	// `resident` is everything the real read below needs, sized exactly once. `max_scratch`
	// is the biggest bank any layer owns -- computed unconditionally, same as always, since
	// computing it costs nothing; only ALLOCATING it costs arena, gated on `out->compressed`
	// below exactly as before this feature existed. `atlas_scratch` is the SEPARATE,
	// independent contribution one compressed atlas pool slot could need -- kept apart from
	// `max_scratch` rather than folded in unconditionally, because that was measured to cost
	// real arena on maps that reference no compressed atlas at all (a pool-slot's worth,
	// several KB on real content, reserved whether or not it was ever needed).
	size_t resident = 12 + (size_t)atlas_count * 4 + ((size_t)atlas_slots + 1) * 4 +
		(size_t)layer_count * 14;
	size_t max_scratch = 0;
#if PNX_USE_ATLAS_COMPRESS
	size_t atlas_scratch = 0;
	// Probed for real: one cheap 8-byte header read per atlas this map references (bit 0
	// of the generic blob header's `d` byte, same convention pnx_sprite_load/
	// atlas_load_into check), rather than assuming the worst case for every map that merely
	// has the feature compiled in.
	for (uint8_t i = 0; i < atlas_count; i++)
	{
		const uint16_t atlas_asset = read_u16(pre + 12 + (size_t)i * 4);
		if (atlas_asset >= s_resource_count)
			continue; // caught properly, with a real error, in the loop that populates out->atlas
		uint8_t hdr[PNX_BLOB_HEADER_BYTES];
		if (pnx_platform_resource_read(s_resources[atlas_asset], 0, hdr, sizeof(hdr)) ==
				sizeof(hdr) &&
			(hdr[6] & 1) != 0)
		{
			// Slots are only uniform-width when there is eviction (atlas_slots <
			// atlas_count, atlas_pool_layout's stride case) -- when every atlas gets its
			// own slot (the common case) each is sized to ITS OWN resident bytes, so
			// pool_bytes / atlas_slots is only the average, not a safe bound for any one
			// slot. Read the real per-slot widths from the pool_offset table (already
			// within `probe_len`, checked by `fixed_region` above) and take the largest --
			// this compressed atlas could land in any of them.
			const uint8_t* pool_off_tbl = pre + 12 + (size_t)atlas_count * 4;
			for (uint8_t s = 0; s < atlas_slots; s++)
			{
				const uint32_t from = read_u32(pool_off_tbl + (size_t)s * 4);
				const uint32_t to	= read_u32(pool_off_tbl + (size_t)(s + 1) * 4);
				if (to - from > atlas_scratch)
					atlas_scratch = to - from;
			}
			break;
		}
	}
#endif
	for (uint8_t li = 0; li < layer_count; li++)
	{
		const uint8_t* ld = layer_dir + (size_t)li * 14;
		const uint8_t lw = ld[0], lh = ld[1], worldtile = ld[2], cols = ld[3], rows = ld[4];
		const uint8_t bank_shift  = ld[5];
		const uint16_t slot_bytes = read_u16(ld + 7);
		if (lw == 0 || lh == 0 || cols == 0 || rows == 0 || worldtile == 0 ||
			(worldtile & (worldtile - 1)) || slot_bytes == 0)
		{
			pnx_log("map %u: layer %u is %ux%u in %ux%u WorldTiles of %u -- refused", asset_id,
					li, lw, lh, cols, rows, worldtile);
			return false;
		}
		resident += pad4((size_t)cols * rows);
		const size_t scratch = (size_t)(1u << bank_shift) * slot_bytes;
		if (scratch > max_scratch)
			max_scratch = scratch;
	}
	// No flag table any more (v11): collision/warp are read out of each cell's own u16 and
	// the tile's owning atlas, not a map-owned plane -- so the only optional table left is
	// the palette remap (flags bit 0).
	resident += ((flags & 1) ? pad4(tile_total) : 0) + pad4((size_t)warps * sizeof(PnxWarp)) +
		pad4((size_t)dict_count * 2);

	uint8_t* pre_mem = (uint8_t*)arena_alloc(resident, 4);
	if (!pre_mem)
	{
		pnx_log("map %u: arena full, needed %u for the preamble, %u free", asset_id,
				(unsigned)resident, (unsigned)pnx_arena_remaining(s_arena));
		return false;
	}
	if (pnx_platform_resource_read(resource, PNX_BLOB_HEADER_BYTES, pre_mem, resident) !=
		resident)
	{
		pnx_log("map %u: short read of the %u-byte preamble", asset_id, (unsigned)resident);
		return false;
	}
	s_bytes_loaded += (uint32_t)(sizeof(head) + resident);

	const uint8_t* at = pre_mem + 12;
	uint16_t base	  = 0;
	for (uint8_t i = 0; i < atlas_count; i++)
	{
		out->atlas[i].asset		 = read_u16(at);
		out->atlas[i].first_tile = read_u16(at + 2);
		out->atlas[i].slot		 = PNX_MAP_NO_SLOT;
		if (out->atlas[i].first_tile != base)
		{
			pnx_log(
				"map %u: atlas %u starts at tile %u, expected %u -- the id space must be "
				"contiguous",
				asset_id, i, out->atlas[i].first_tile, base);
			return false;
		}
		// The last atlas's slice runs to the end of the id space; the others end where the
		// next begins. Derived rather than stored, so the two can never disagree.
		base = (i + 1 < atlas_count) ? read_u16(at + 6) : tile_total;
		if (base < out->atlas[i].first_tile || base > tile_total)
		{
			pnx_log("map %u: atlas %u claims tiles %u..%u of %u", asset_id, i,
					out->atlas[i].first_tile, base, tile_total);
			return false;
		}
		out->atlas[i].tile_count = (uint16_t)(base - out->atlas[i].first_tile);
		at += 4;
	}

	out->pool_offset = at;
	at += ((size_t)atlas_slots + 1) * 4;
	if (read_u32(out->pool_offset + (size_t)atlas_slots * 4) != pool_bytes)
	{
		pnx_log("map %u: atlas pool says %u bytes, its slot table ends at %u", asset_id,
				(unsigned)pool_bytes,
				(unsigned)read_u32(out->pool_offset + (size_t)atlas_slots * 4));
		return false;
	}

	out->pool		= ARENA_CALLOC_ARRAY(PnxAtlas, atlas_slots);
	out->pool_mem	= (uint8_t*)arena_alloc(pool_bytes, 4);
	out->pool_owner = (uint8_t*)arena_alloc(atlas_slots, 4);
	out->pool_pins	= (uint8_t*)arena_alloc(atlas_slots, 4);
	if (!out->pool || !out->pool_mem || !out->pool_owner || !out->pool_pins)
	{
		pnx_log("map %u: arena full, needed %u for %u atlas slots, %u free", asset_id,
				(unsigned)pool_bytes, atlas_slots, (unsigned)pnx_arena_remaining(s_arena));
		return false;
	}
	memset(out->pool_owner, PNX_MAP_NO_SLOT, atlas_slots);
	memset(out->pool_pins, 0, atlas_slots);

	// Per-layer directory, then per-layer wt_mask -- two contiguous zones in that order,
	// matching how the pipeline wrote them (finish_map, tools/pnx_assets.py) and matching
	// the layout the probe above sized: every layer's fixed 13 bytes first, THEN every
	// layer's own wt_mask, never interleaved, which is what let one probe size the whole
	// thing without needing any layer's own cols/rows in advance.
	// 14 bytes/layer, not 13: parallax_pct_x (ld[11]) and parallax_pct_y (ld[12]) each
	// get their own byte -- an independent X/Y scroll rate needs a real second field,
	// not a repacking of the one shared byte this used to be. `wrap` shifts to ld[13].
	const uint8_t* dir = at;
	at += (size_t)layer_count * 14;
	for (uint8_t li = 0; li < layer_count; li++)
	{
		const uint8_t* ld = dir + (size_t)li * 14;
		PnxMapLayer* l	  = &out->layers[li];
		const uint8_t lw = ld[0], lh = ld[1], worldtile = ld[2], cols = ld[3], rows = ld[4];
		const uint8_t bank_shift = ld[5], want_slots = ld[6];
		const uint16_t slot_bytes		= read_u16(ld + 7);
		const uint16_t first_bank_asset = read_u16(ld + 9);
		const uint8_t parallax_pct_x = ld[11], parallax_pct_y = ld[12], wrap = ld[13];

		const uint16_t n		  = (uint16_t)cols * rows;
		const uint16_t bank_count = (uint16_t)(((n - 1) >> bank_shift) + 1);
		if (want_slots == 0 || first_bank_asset == 0 ||
			(uint32_t)first_bank_asset + bank_count > s_resource_count)
		{
			pnx_log("map %u: layer %u -- %u banks from asset %u, past the %u the project has",
					asset_id, li, bank_count, first_bank_asset, s_resource_count);
			return false;
		}
		// Every bank checked once, here, rather than on every read -- see the map-wide
		// comment this replaces for why per-read stamping is not the alternative.
		for (uint16_t b = 0; b < bank_count; b++)
		{
			uint8_t bh[PNX_BLOB_HEADER_BYTES];
			const uint32_t res = s_resources[first_bank_asset + b];
			if (pnx_platform_resource_read(res, 0, bh, sizeof(bh)) != sizeof(bh) ||
				bh[0] != 'P' || bh[1] != 'K' || bh[2] != PNX_BLOB_VERSION ||
				(s_orientation != PNX_ORIENT_UNSET && bh[7] != s_orientation))
			{
				pnx_log(
					"map %u: layer %u bank %u is not a v%u %s WorldTile bank -- rebuild "
					"assets",
					asset_id, li, b, PNX_BLOB_VERSION,
					s_orientation == PNX_ORIENT_UNSET ? "current" : "matching");
				return false;
			}
			s_bytes_loaded += sizeof(bh);
		}

		l->wt_mask = at;
		at += pad4(n);

		// The slot count is the smaller of what the pipeline asked for and what this layer
		// actually has -- a layer of four WorldTiles never needs nine slots.
		const uint8_t slots = want_slots < n ? want_slots : (uint8_t)n;
		l->slots			= ARENA_CALLOC_ARRAY(PnxWorldTile, slots);
		l->slot_mem			= (uint8_t*)arena_alloc((size_t)slots * slot_bytes, 4);
		l->wt_slot			= (uint8_t*)arena_alloc(n, 4);
		if (!l->slots || !l->slot_mem || !l->wt_slot)
		{
			pnx_log(
				"map %u: layer %u -- arena full, needed %u for %u WorldTile slots, %u free",
				asset_id, li, (unsigned)((size_t)slots * slot_bytes), slots,
				(unsigned)pnx_arena_remaining(s_arena));
			return false;
		}
		memset(l->wt_slot, PNX_MAP_NO_SLOT, n);

		l->first_bank_asset = first_bank_asset;
		l->bank_shift		= bank_shift;
		l->slot_bytes		= slot_bytes;
		l->w				= lw;
		l->h				= lh;
		l->slot_count		= slots;
		l->wt_cols			= cols;
		l->wt_rows			= rows;
		l->worldtile		= worldtile;
		l->wt_shift			= shift_of(worldtile);
		l->parallax_pct_x	= parallax_pct_x;
		l->parallax_pct_y	= parallax_pct_y;
		l->wrap				= wrap != 0;
		// Only when this layer's atlases fit too: with fewer atlas slots than atlases,
		// holding every WorldTile at once is not something the (shared) pool can express.
		l->held_whole = (slots == n && atlas_slots >= atlas_count);
	}

	out->tile_palette = NULL;
	if (flags & 1)
	{
		out->tile_palette = at;
		at += pad4(tile_total);
	}
	// PnxWarp is five u8 fields, so it has no padding and maps directly onto the packed
	// bytes the pipeline writes. _Static_assert below keeps that true.
	out->warps = (const PnxWarp*)(const void*)at;
	at += pad4((size_t)warps * sizeof(PnxWarp));

	// The cell dictionary: dict_count entries, 2 bytes each, read the same byte-at-a-time
	// way pnx_map_entry (pnx_assets.h) already reads a cell's own entry word -- endianness
	// portable, not just alignment-safe, the same reason nothing else in this preamble casts
	// a multi-byte field straight onto blob bytes.
	out->cell_dict = at;

	out->resource	   = resource;
	out->tile_count	   = tile_total;
	out->warp_count	   = warps;
	out->atlas_count   = atlas_count;
	out->atlas_slots   = atlas_slots;
	out->dict_count	   = dict_count;
	out->idx_width	   = idx_width;
	out->layer_count   = layer_count;
	out->primary_layer = primary_layer;
	// Carried by the map rather than read off a loaded atlas: the streamer works in world
	// pixels and has to size its window BEFORE any atlas is resident. The atlas load checks
	// the two agree.
	out->tile_px = tile_px;

	// Bit 1 of `flags`: this map's banks are LZSS-compressed (`compress_maps`, M12) -- one
	// flag for the whole map, not per layer (see PnxMap's own comment). Bit 0 is the
	// palette remap, above; both ride the same byte rather than growing the header for a
	// second single bit.
#if PNX_USE_MAP_COMPRESS
	out->compressed = (flags & 2) != 0;
#else
	if (flags & 2)
	{
		pnx_log(
			"map %u: built with compress_maps, but PNX_USE_MAP_COMPRESS is 0 -- set it "
			"in pnx_config.h, or rebuild the project without compress_maps",
			asset_id);
		return false;
	}
#endif

	// Two independent contributions, each gated on its own real need -- `out->compressed`
	// for the bank one (this map's own banks may or may not be compressed regardless of
	// `PNX_USE_MAP_COMPRESS` being compiled in), the atlas probe result for the other
	// (folded into `atlas_scratch` above only if something was actually found). Neither
	// gate gets bypassed just because a flag is compiled in -- that was the bug this
	// comment replaces the old version of. See PnxMap's own comment for why one shared
	// pair, not one per use, and why this bound covers both the compressed read and the
	// decoded output either way.
#if PNX_USE_MAP_COMPRESS || PNX_USE_ATLAS_COMPRESS
	size_t scratch_needed = 0;
	bool need_dst		  = false; // lzss_dst decodes a whole BANK; a compressed atlas pool
								   // load decodes straight into its own `dst` and never
								   // touches it -- see atlas_load_into's own comment.
#if PNX_USE_MAP_COMPRESS
	if (out->compressed)
	{
		scratch_needed = max_scratch;
		need_dst	   = true;
	}
#endif
#if PNX_USE_ATLAS_COMPRESS
	if (atlas_scratch > scratch_needed)
		scratch_needed = atlas_scratch;
#endif
	if (scratch_needed > 0)
	{
		out->lzss_src = (uint8_t*)arena_alloc(scratch_needed, 4);
		out->lzss_dst = need_dst ? (uint8_t*)arena_alloc(scratch_needed, 4) : NULL;
		if (!out->lzss_src || (need_dst && !out->lzss_dst))
		{
			pnx_log("map %u: arena full, needed %u for LZSS scratch, %u free", asset_id,
					(unsigned)(scratch_needed * (need_dst ? 2 : 1)),
					(unsigned)pnx_arena_remaining(s_arena));
			return false;
		}
	}
#endif

	// **A layer that fits is loaded whole, here, before this returns.** Every small layer
	// is in this case, so it behaves exactly as a map used to before M13: one load point,
	// everything resident, no streaming code ever running. Leaving it to fill lazily would
	// have been cheaper by a few reads and wrong in a way that matters -- a layer that never
	// streams should never be seen to stream, and a game measuring flash traffic would
	// otherwise find a "resident" layer still reading as the player walks.
	for (uint8_t li = 0; li < layer_count; li++)
	{
		PnxMapLayer* l = &out->layers[li];
		if (!l->held_whole)
			continue;
		// One read per bank, not one per WorldTile -- see the map-wide comment this
		// replaces for the seek-cost reasoning.
		const uint16_t n  = (uint16_t)l->wt_cols * l->wt_rows;
		uint16_t per_bank = (uint16_t)(1u << l->bank_shift);
		if (per_bank > PNX_MAP_MAX_RUN)
			per_bank = PNX_MAP_MAX_RUN;
		for (uint16_t i = 0; i < n; i += per_bank)
		{
			const uint16_t run = (n - i) < per_bank ? (uint16_t)(n - i) : per_bank;
			if (!worldtile_load_run(out, l, i, (uint8_t)i, (uint8_t)run))
			{
				pnx_log("map %u: layer %u WorldTiles %u..%u failed while loading whole",
						asset_id, li, i, i + run - 1);
				return false;
			}
		}
	}
	return true;
}

_Static_assert(sizeof(PnxWarp) == 5,
			   "PnxWarp must stay packed: it is cast directly onto blob bytes");

// ------------------------------------------------------------------ the streamer

// Finds `which` in the atlas pool, loading it if it is not there. Returns false only when
// every slot is pinned by a resident WorldTile -- which the pipeline's window check makes
// impossible for content it accepted, so it means the view moved further in one step than
// the pool was sized for.
static bool atlas_pin(PnxMap* m, uint8_t which)
{
	PnxMapAtlas* a = &m->atlas[which];
	if (a->slot != PNX_MAP_NO_SLOT)
		return true;

	uint8_t slot = PNX_MAP_NO_SLOT;
	if (m->atlas_slots >= m->atlas_count)
	{
		// A slot per atlas: slot i belongs to atlas i and is exactly its size, so there is no
		// choice to make and nothing is ever evicted. Picking any other slot would be picking
		// one sized for a different atlas.
		slot = which;
	}
	else
	{
		for (uint8_t i = 0; i < m->atlas_slots; i++)
		{
			if (m->pool_owner[i] == PNX_MAP_NO_SLOT)
			{
				slot = i;
				break;
			}
		}
		if (slot == PNX_MAP_NO_SLOT)
		{
			// Any unpinned slot will do: an atlas nothing resident depends on is as evictable
			// as any other, and every slot is the same size in this case.
			for (uint8_t i = 0; i < m->atlas_slots; i++)
			{
				if (m->pool_pins[i] == 0)
				{
					slot = i;
					break;
				}
			}
		}
		if (slot == PNX_MAP_NO_SLOT)
			return false;
	}

	if (m->pool_owner[slot] != PNX_MAP_NO_SLOT)
	{
		m->atlas[m->pool_owner[slot]].slot = PNX_MAP_NO_SLOT;
	}
	m->pool_owner[slot] = PNX_MAP_NO_SLOT;

	const uint32_t from = read_u32(m->pool_offset + (size_t)slot * 4);
	const uint32_t to	= read_u32(m->pool_offset + ((size_t)slot + 1) * 4);
#if PNX_USE_MAP_COMPRESS || PNX_USE_ATLAS_COMPRESS
	uint8_t* scratch = m->lzss_src;
#else
	uint8_t* scratch = NULL;
#endif
	if (!atlas_load_into(&m->pool[slot], a->asset, m->pool_mem + from, to - from, scratch))
	{
		return false;
	}
	if (m->pool[slot].tile_count != a->tile_count || m->pool[slot].tile_px != m->tile_px)
	{
		pnx_log(
			"map atlas %u holds %u tiles of %upx, the map was built against %u of %upx "
			"-- rebuild assets",
			a->asset, m->pool[slot].tile_count, m->pool[slot].tile_px, a->tile_count,
			m->tile_px);
		return false;
	}
	m->pool_owner[slot] = which;
	a->slot				= slot;
	return true;
}

// Pins and unpins are kept in step by going through these two, because the failure they
// prevent is silent: an atlas whose count never returns to zero is never evictable again,
// so the pool shrinks by one slot per mistake until nothing can load.
static void atlas_unpin_mask(PnxMap* m, uint8_t mask)
{
	for (uint8_t k = 0; k < m->atlas_count; k++)
	{
		const uint8_t slot = m->atlas[k].slot;
		if ((mask >> k & 1) && slot != PNX_MAP_NO_SLOT && m->pool_pins[slot] > 0)
		{
			m->pool_pins[slot]--;
		}
	}
}

static void worldtile_release(PnxMap* m, PnxMapLayer* l, uint8_t slot)
{
	PnxWorldTile* wt = &l->slots[slot];
	if (!wt->live)
		return;
	const uint32_t i = (uint32_t)wt->wy * l->wt_cols + wt->wx;
	atlas_unpin_mask(m, l->wt_mask[i]);
	l->wt_slot[i] = PNX_MAP_NO_SLOT;
	wt->live	  = false;
}

// Pins the atlases one WorldTile needs, and reports which ones it pinned so a later
// failure can hand them back.
//
// Atlases first, always, and pinned as they land rather than at the end. A WorldTile whose
// art is not loaded would draw as holes, and pinning late would let the second atlas this
// WorldTile needs evict the first -- the two-atlas WorldTile in a one-slot pool, which the
// pipeline rejects but which a hand-built blob could still ask for.
//
// The mask lives beside the index rather than in the payload precisely so this ordering is
// possible without reading the payload to find out what it needs. The pool it pins into is
// the map's, shared across every layer (see PnxMap's own comment) -- this is why two
// layers sharing an atlas do not double-load it.
static bool worldtile_pin(PnxMap* m, PnxMapLayer* l, uint32_t i, uint8_t* out_pinned)
{
	const uint8_t mask = l->wt_mask[i];
	uint8_t pinned	   = 0;
	for (uint8_t k = 0; k < m->atlas_count; k++)
	{
		if (!(mask >> k & 1))
			continue;
		if (!atlas_pin(m, k))
		{
			pnx_log("map: WorldTile %u needs atlas %u, and every pool slot is in use",
					(unsigned)i, k);
			atlas_unpin_mask(m, pinned);
			return false;
		}
		m->pool_pins[m->atlas[k].slot]++;
		pinned |= (uint8_t)(1u << k);
	}
	*out_pinned = pinned;
	return true;
}

// Unpacks a payload that has already landed in its slot.
static bool worldtile_accept(PnxMap* m, PnxMapLayer* l, uint32_t i, uint8_t slot)
{
	const uint8_t* dst = l->slot_mem + (size_t)slot * l->slot_bytes;
	PnxWorldTile* wt   = &l->slots[slot];

	// v12: [cw, ch] + cells + ext_count(u16) + ext(ext_count * 3) -- EXTENDED's sparse tag
	// table. Collision/warp already moved into the cell's own u16 (see PnxMap's own
	// comment), so there is no collision override section any more, but this one
	// genuinely needs a per-cell exception list (PnxWorldTile.ext's own comment,
	// pnx_assets.h) -- renamed and repurposed, not reinstated unchanged.
	wt->cell_w = dst[0];
	wt->cell_h = dst[1];
	wt->cells  = dst + 2;

	const size_t cells_need = 2 + (size_t)wt->cell_w * wt->cell_h * m->idx_width;
	if (wt->cell_w == 0 || wt->cell_h == 0 || wt->cell_w > l->worldtile ||
		wt->cell_h > l->worldtile || cells_need + 2 > l->slot_bytes)
	{
		pnx_log("map: WorldTile %u claims %ux%u cells, %u bytes of %u", (unsigned)i,
				wt->cell_w, wt->cell_h, (unsigned)(cells_need + 2), l->slot_bytes);
		return false;
	}

	wt->ext_count = read_u16(dst + cells_need);
	wt->ext		  = dst + cells_need + 2;

	const size_t need = cells_need + 2 + (size_t)wt->ext_count * 3;
	if (need > l->slot_bytes)
	{
		pnx_log("map: WorldTile %u claims %u extended tag(s), %u bytes of %u", (unsigned)i,
				wt->ext_count, (unsigned)need, l->slot_bytes);
		return false;
	}

	wt->wx		  = (uint8_t)(i % l->wt_cols);
	wt->wy		  = (uint8_t)(i / l->wt_cols);
	wt->live	  = true;
	l->wt_slot[i] = slot;
	return true;
}

// Loads `count` consecutive WorldTiles starting at index `first` into `count` consecutive
// pool slots starting at `slot` -- in ONE resource read.
//
// This is the batching half of the fix. A ranged read costs by how far into the resource
// it starts, so the win is not fewer bytes but fewer seeks: a whole bank fetched at once
// pays one seek instead of eight. It works at all because payloads are padded to the slot
// stride, which makes a run of tiles contiguous at both ends -- contiguous in the bank,
// and contiguous in the pool.
//
// The caller guarantees the run stays inside one bank and inside the slot array.
static bool worldtile_load_run(PnxMap* m, PnxMapLayer* l, uint32_t first, uint8_t slot,
							   uint8_t count)
{
	// Sized independently of the stream budget, which counts READS and says nothing about
	// how many WorldTiles one carries. A bank can hold up to 128 of them at the smallest
	// WorldTile size, and the caller loops rather than this growing a 128-byte frame.
	uint8_t pinned[PNX_MAP_MAX_RUN];
	if (count > PNX_MAP_MAX_RUN)
		count = PNX_MAP_MAX_RUN;

	for (uint8_t k = 0; k < count; k++)
	{
		if (worldtile_pin(m, l, first + k, &pinned[k]))
			continue;
		// Whatever was pinned for earlier tiles of this run has to go back, or those atlas
		// slots are held by WorldTiles that never became resident.
		while (k--)
			atlas_unpin_mask(m, pinned[k]);
		return false;
	}

	const uint16_t per_bank	  = (uint16_t)(1u << l->bank_shift);
	const uint16_t bank		  = (uint16_t)(first >> l->bank_shift);
	const size_t local_offset = (size_t)(first & (per_bank - 1)) * l->slot_bytes;
	const size_t len		  = (size_t)count * l->slot_bytes;
	uint8_t* dst			  = l->slot_mem + (size_t)slot * l->slot_bytes;

	const uint32_t resource = s_resources[l->first_bank_asset + bank];

#if PNX_USE_MAP_COMPRESS
	if (m->compressed)
	{
		// Atomic: any one WorldTile from this bank means decoding all of it -- see PnxMap's
		// own comment for why that is the accepted trade. A run never crosses a bank
		// boundary (the caller's own guarantee), so a bank asked for by more than one run
		// within a single stream_window pass decodes more than once -- a known cost this
		// milestone accepts rather than adds a decoded-bank cache to avoid. The scratch
		// pair is the MAP's, shared across every layer (see PnxMap's own comment) -- safe
		// because streaming is sequential, never concurrent across layers.
		size_t compressed_size = 0;
		if (!pnx_platform_resource_size(resource, &compressed_size) ||
			compressed_size <= PNX_BLOB_HEADER_BYTES)
		{
			pnx_log("map: bank %u missing or too small to hold a compressed body", bank);
			for (uint8_t k = 0; k < count; k++)
				atlas_unpin_mask(m, pinned[k]);
			return false;
		}
		const size_t body_len = compressed_size - PNX_BLOB_HEADER_BYTES;
		const size_t scratch  = (size_t)per_bank * l->slot_bytes;
		if (body_len > scratch ||
			pnx_platform_resource_read(resource, PNX_BLOB_HEADER_BYTES, m->lzss_src,
									   body_len) != body_len)
		{
			pnx_log("map: bank %u short read of its %u-byte compressed body", bank,
					(unsigned)body_len);
			for (uint8_t k = 0; k < count; k++)
				atlas_unpin_mask(m, pinned[k]);
			return false;
		}
		s_bytes_loaded += (uint32_t)body_len;

		const size_t decoded = pnx_lzss_decode(m->lzss_src, body_len, m->lzss_dst, scratch);
		if (local_offset + len > decoded)
		{
			pnx_log("map: bank %u decoded %u of the %u bytes WorldTiles %u..%u need", bank,
					(unsigned)decoded, (unsigned)(local_offset + len), (unsigned)first,
					(unsigned)(first + count - 1));
			for (uint8_t k = 0; k < count; k++)
				atlas_unpin_mask(m, pinned[k]);
			return false;
		}
		memcpy(dst, m->lzss_dst + local_offset, len);
	}
	else
#endif
	{
		const size_t offset = PNX_BLOB_HEADER_BYTES + local_offset;
		if (pnx_platform_resource_read(resource, offset, dst, len) != len)
		{
			pnx_log("map: bank %u short read of %u bytes at %u for WorldTiles %u..%u", bank,
					(unsigned)len, (unsigned)offset, (unsigned)first,
					(unsigned)(first + count - 1));
			for (uint8_t k = 0; k < count; k++)
				atlas_unpin_mask(m, pinned[k]);
			return false;
		}
		s_bytes_loaded += (uint32_t)len;
	}

	for (uint8_t k = 0; k < count; k++)
	{
		if (worldtile_accept(m, l, first + k, (uint8_t)(slot + k)))
			continue;
		// Roll the whole run back: a half-accepted run would leave slots marked live with
		// pins that no longer describe them.
		for (uint8_t j = 0; j < k; j++)
			worldtile_release(m, l, (uint8_t)(slot + j));
		for (uint8_t j = k; j < count; j++)
			atlas_unpin_mask(m, pinned[j]);
		return false;
	}
	return true;
}

// Manhattan distance from the window's centre, which is what "furthest from where the
// player is" means on a grid. A WorldTile outside the window scores above everything
// inside it, so eviction always takes an off-screen one first.
static uint32_t wt_distance(const PnxWorldTile* wt, int32_t cx, int32_t cy)
{
	const int32_t dx = (int32_t)wt->wx - cx;
	const int32_t dy = (int32_t)wt->wy - cy;
	return (uint32_t)((dx < 0 ? -dx : dx) + (dy < 0 ? -dy : dy));
}

// Floor division, not truncation, because a camera can sit at a negative x when the map
// is narrower than the screen -- and there truncation rounds toward zero and names the
// wrong WorldTile.
static int32_t floor_div(int32_t a, int32_t b)
{
	return a >= 0 ? a / b : -(((-a) + b - 1) / b);
}

// `x`/`y`/`w`/`h` are the RAW camera rectangle, not yet scaled for this layer's own
// parallax -- stream_window does that itself (pnx_layer.c's pnx_layers_draw does the same
// scaling for DRAWING; this is the streaming side of the same idea, and the two have to
// agree on what "where this layer is looking" means or a fast-parallax background streams
// against a window the draw call never asked for).
//
// Wrap is honoured for SAMPLING (pnx_map_worldtile_layer, drawing) but this window is
// still clamped to the layer's own [0, wt_cols) / [0, wt_rows) rather than wrapping the
// STREAMING window itself -- a real, accepted scope limit: a wrap layer is meant for a
// screen-sized parallax background, which fits inside its own pool and takes the
// held_whole path below without ever reaching this clamp at all. Only a wrap layer too
// large to hold whole would notice the difference, at one seam, and that is not a shape
// this milestone's own examples build.
static uint8_t stream_window(PnxMap* m, PnxMapLayer* l, int32_t x, int32_t y, int32_t w,
							 int32_t h, uint8_t* budget)
{
	// A layer held whole has nothing to load and nothing it may evict -- the eviction pass
	// below would happily drop WorldTiles outside the window and read them back as the
	// camera returned, turning a layer that fits into one that streams. Every small layer
	// takes this path, so it is also where most of the per-frame cost of streaming goes
	// for games that never needed it.
	if (!l->slots || l->held_whole)
		return 0;

	// 255 (PNX_LAYER_PARALLAX_WORLD, pnx_layer.h) is ordinary 1:1 motion, handed through
	// untouched rather than a 255/255 multiply that happens to be exact -- the same
	// shortcut pnx_layer.c's own scaled_camera takes, and for the same reason: a
	// single-layer map costs nothing extra to stream through this. Not `#include`-ing
	// pnx_layer.h for the constant: assets sits BELOW gfx/layer in this framework's
	// dependency order, and a plain 255 is exactly what that header's own comment defines
	// PNX_MAP_LAYER's parallax_pct_x/y against too. Independent per axis, same reason
	// pnx_layer.c's scaled_camera is: a layer streamed 1:1 vertically but slower
	// horizontally (or vice versa) needs each axis checked on its own.
	// Plain int32_t, not int64_t: x*pct only risks overflow past roughly +-8.4M (INT32_MAX
	// / 254), a world coordinate no pebblnyx project can reach -- WorldTile maps are bounded
	// by flash/resource size (see PT2 flash cost notes), nowhere close to that magnitude.
	// The 64-bit widen this replaced pulled __aeabi_ldivmod/__udivmoddi4 out of libgcc,
	// several hundred bytes even a single caller anywhere in the link brings in -- real
	// money on aplite's 24KB app image.
	if (l->parallax_pct_x != 255)
		x = (x * (int32_t)l->parallax_pct_x) / 255;
	if (l->parallax_pct_y != 255)
		y = (y * (int32_t)l->parallax_pct_y) / 255;

	// One WorldTile of margin on each side, matching worldtile_window() in the pipeline --
	// which is what sized the pool. The two have to agree or the streamer asks for more
	// slots than it was given and thrashes.
	const int32_t span = (int32_t)m->tile_px * l->worldtile;
	int32_t x0 = floor_div(x, span) - 1, y0 = floor_div(y, span) - 1;
	int32_t x1 = floor_div(x + w - 1, span) + 1, y1 = floor_div(y + h - 1, span) + 1;
	if (x0 < 0)
		x0 = 0;
	if (y0 < 0)
		y0 = 0;
	if (x1 >= l->wt_cols)
		x1 = l->wt_cols - 1;
	if (y1 >= l->wt_rows)
		y1 = l->wt_rows - 1;
	if (x1 < x0 || y1 < y0)
		return 0;

	const int32_t cx = (x0 + x1) / 2, cy = (y0 + y1) / 2;
	uint8_t missing = 0;

	// Drop everything outside the window BEFORE loading anything, rather than evicting one
	// slot at a time as the loads demand it.
	//
	// This is not tidiness. Evicting on demand cannot free an ATLAS: a warp asks for a
	// region drawn from a tileset nothing resident uses, the first new WorldTile tries to
	// pin it, and every atlas slot is still held by the fifteen WorldTiles that have not
	// been evicted yet -- because they are only evicted one per load, and each load is what
	// was blocked. The whole window fails and the screen stays empty. Walking never showed
	// it, since the window moves a tile at a time and the old WorldTiles drain gradually;
	// it took a jump to a distant part of the map, which is exactly what a warp is.
	//
	// Safe to do unconditionally: the window already carries the streamer's margin, so
	// anything outside it is not on screen and is not about to be.
	for (uint8_t i = 0; i < l->slot_count; i++)
	{
		const PnxWorldTile* wt = &l->slots[i];
		if (!wt->live)
			continue;
		if (wt->wx < x0 || wt->wx > x1 || wt->wy < y0 || wt->wy > y1)
		{
			worldtile_release(m, l, i);
		}
	}

	for (int32_t wy = y0; wy <= y1; wy++)
	{
		int32_t wx = x0;
		while (wx <= x1)
		{
			const uint32_t first = (uint32_t)wy * l->wt_cols + wx;
			if (l->wt_slot[first] != PNX_MAP_NO_SLOT)
			{
				wx++;
				continue;
			}

			// How far this run of missing WorldTiles goes. Consecutive along the row, which
			// makes them consecutive in the bank, and stopping at the bank boundary because one
			// read cannot cross two resources.
			const uint32_t per_bank = 1u << l->bank_shift;
			int32_t run				= 1;
			while (wx + run <= x1 && l->wt_slot[first + run] == PNX_MAP_NO_SLOT &&
				   ((first + run) >> l->bank_shift) == (first >> l->bank_shift) &&
				   (uint32_t)run < per_bank && run < PNX_MAP_MAX_RUN)
			{
				run++;
			}

			if (*budget == 0)
			{
				missing += (uint8_t)run;
				wx += run;
				continue;
			}

			// Consecutive free slots for a consecutive run, because the read lands in one
			// stretch of pool memory. Shrinking rather than failing: a fragmented pool still
			// loads, just in more reads than it might have.
			uint8_t slot = PNX_MAP_NO_SLOT;
			while (run > 0 && slot == PNX_MAP_NO_SLOT)
			{
				for (uint8_t i = 0; i + run <= l->slot_count; i++)
				{
					uint8_t k = 0;
					while (k < run && !l->slots[i + k].live)
						k++;
					if (k == run)
					{
						slot = i;
						break;
					}
				}
				if (slot == PNX_MAP_NO_SLOT)
					run--;
			}

			if (slot == PNX_MAP_NO_SLOT)
			{
				// Nothing free at all. Evict the resident WorldTile furthest from the middle of
				// what is wanted -- the one the player is walking away from, by construction --
				// and take it alone.
				run			   = 1;
				uint32_t worst = 0;
				for (uint8_t i = 0; i < l->slot_count; i++)
				{
					const uint32_t d = wt_distance(&l->slots[i], cx, cy);
					if (slot == PNX_MAP_NO_SLOT || d > worst)
					{
						slot  = i;
						worst = d;
					}
				}
				worldtile_release(m, l, slot);
			}

			if (!worldtile_load_run(m, l, first, slot, (uint8_t)run))
			{
				missing++;
				wx++;
				continue;
			}
			(*budget)--; // one READ, however many WorldTiles it carried
			wx += run;
		}
	}
	return missing;
}

uint8_t pnx_map_stream(PnxMap* m, int32_t x, int32_t y, int32_t w, int32_t h)
{
	if (!m)
		return 0;
	// ONE budget, shared and decremented across every layer -- not PNX_MAP_STREAM_BUDGET
	// per layer, which would make a four-layer map stream four times the reads a
	// single-layer one does for the same window. Layers earlier in the array get first
	// call on it; a layer that never needs to stream (held whole, or nothing missing)
	// leaves the rest for the next one.
	uint8_t budget	= PNX_MAP_STREAM_BUDGET;
	uint8_t missing = 0;
	for (uint8_t li = 0; li < m->layer_count; li++)
		missing += stream_window(m, &m->layers[li], x, y, w, h, &budget);
	return missing;
}

uint8_t pnx_map_stream_now(PnxMap* m, int32_t x, int32_t y, int32_t w, int32_t h)
{
	if (!m)
		return 0;

	// Each layer gets its OWN full-pool budget, not a shared one: a scene load or a warp
	// has no previous frame to show, so there is nothing to protect by spreading the reads
	// over several, for any layer. Sixteen WorldTiles and four atlases is ~1.5 ms against
	// a 37.33 ms frame; that bound applies per layer here, same as it always did before a
	// map could have more than one.
	uint8_t missing = 0;
	for (uint8_t li = 0; li < m->layer_count; li++)
	{
		uint8_t budget = m->layers[li].slot_count;
		missing += stream_window(m, &m->layers[li], x, y, w, h, &budget);
	}
	return missing;
}

// Summed across every layer.
uint8_t pnx_map_resident(const PnxMap* m)
{
	uint8_t n = 0;
	if (!m)
		return 0;
	for (uint8_t li = 0; li < m->layer_count; li++)
	{
		const PnxMapLayer* l = &m->layers[li];
		for (uint8_t i = 0; i < l->slot_count; i++)
			n += l->slots[i].live ? 1 : 0;
	}
	return n;
}

bool pnx_dialog_load(PnxDialog* out, uint16_t asset_id)
{
	uint8_t entries		= 0;
	size_t payload		= 0;
	const uint8_t* data = load_blob(asset_id, "PD", &entries, NULL, NULL, &payload);
	if (!data)
		return false;

	const size_t index_bytes = (size_t)entries * 4;
	if (entries == 0 || payload < index_bytes)
	{
		pnx_log("dialog %u: %u entries needs %u index bytes, blob has %u", asset_id, entries,
				(unsigned)index_bytes, (unsigned)payload);
		return false;
	}

	// Total page count is the last entry's first + count, which the pipeline guarantees
	// is the end of the page list.
	uint16_t total_pages = 0;
	for (uint16_t i = 0; i < entries; i++)
	{
		const uint8_t* e	 = data + (size_t)i * 4;
		const uint16_t first = (uint16_t)(e[0] | (e[1] << 8));
		const uint16_t count = (uint16_t)(e[2] | (e[3] << 8));
		if (first + count > total_pages)
			total_pages = (uint16_t)(first + count);
	}

	if (payload < index_bytes + (size_t)total_pages * 2)
	{
		pnx_log("dialog %u: %u pages of offsets do not fit in %u bytes", asset_id, total_pages,
				(unsigned)payload);
		return false;
	}

	out->index		 = data;
	out->offsets	 = (const uint16_t*)(const void*)(data + index_bytes);
	out->text		 = data + index_bytes + (size_t)total_pages * 2;
	out->entry_count = entries;
	return true;
}

bool pnx_font_load(PnxFont* out, uint16_t asset_id)
{
	uint8_t depth = 0, line_height = 0, baseline = 0, advance = 0;
	size_t payload = 0;
	const uint8_t* data =
		load_blob_4(asset_id, "PF", &depth, &line_height, &baseline, &advance, &payload);
	if (!data)
		return false;

	if (advance >= PNX_ADVANCE_COUNT)
	{
		pnx_log("font %u: advance axis %u, expected 0-%u", asset_id, advance,
				PNX_ADVANCE_COUNT - 1);
		return false;
	}

	// u16 glyph_count, u16 bitmap_bytes, u8 first_cp, last_cp, fallback, space_advance.
	if (payload < 8)
	{
		pnx_log("font %u: payload %u is shorter than its own header", asset_id,
				(unsigned)payload);
		return false;
	}

	const uint16_t glyphs		= (uint16_t)(data[0] | (data[1] << 8));
	const uint16_t bitmap_bytes = (uint16_t)(data[2] | (data[3] << 8));
	const uint8_t first_cp = data[4], last_cp = data[5];
	const uint8_t fallback = data[6], space_advance = data[7];

	if (depth != 1 && depth != 2)
	{
		pnx_log("font %u: depth %u, expected 1 or 2", asset_id, depth);
		return false;
	}
	if (glyphs == 0 || line_height == 0 || first_cp > last_cp)
	{
		pnx_log("font %u: %u glyphs, %upx line, cp %u..%u -- not a usable font", asset_id,
				glyphs, line_height, first_cp, last_cp);
		return false;
	}
	if (fallback >= glyphs)
	{
		pnx_log("font %u: fallback glyph %u of %u", asset_id, fallback, glyphs);
		return false;
	}

	const size_t index_bytes = (size_t)glyphs * PNX_FONT_GLYPH_BYTES;
	const size_t map_bytes	 = (size_t)(last_cp - first_cp) + 1u;
	const size_t expected	 = 8 + index_bytes + map_bytes + bitmap_bytes;
	if (payload != expected)
	{
		pnx_log("font %u: %u glyphs, %u cp, %u bitmap bytes needs %u, blob has %u", asset_id,
				glyphs, (unsigned)map_bytes, bitmap_bytes, (unsigned)expected,
				(unsigned)payload);
		return false;
	}

	out->glyphs		   = data + 8;
	out->map		   = data + 8 + index_bytes;
	out->bitmaps	   = data + 8 + index_bytes + map_bytes;
	out->glyph_count   = glyphs;
	out->bitmap_bytes  = bitmap_bytes;
	out->depth		   = depth;
	out->line_height   = line_height;
	out->baseline	   = baseline;
	out->space_advance = space_advance;
	out->first_cp	   = first_cp;
	out->last_cp	   = last_cp;
	out->fallback	   = fallback;
	out->advance	   = advance;

	// Both tables are validated once, here, so the blitter can index them per pixel with
	// no checks at all -- the same bargain pnx_atlas_load makes with palette slots. An
	// out-of-range offset would otherwise read arbitrary arena memory as glyph pixels.
	for (uint16_t i = 0; i < glyphs; i++)
	{
		const uint8_t* e   = out->glyphs + (size_t)i * PNX_FONT_GLYPH_BYTES;
		const uint16_t off = (uint16_t)(e[0] | (e[1] << 8));
		const uint8_t w = e[2], h = e[3];
		if (w == 0)
			continue;

		const size_t need = (size_t)off + (size_t)h * pnx_font_row_bytes(out, w);
		if (need > bitmap_bytes)
		{
			pnx_log("font %u: glyph %u (%ux%u at %u) runs %u past its %u bitmap bytes",
					asset_id, i, w, h, off, (unsigned)(need - bitmap_bytes), bitmap_bytes);
			return false;
		}
	}

	for (size_t i = 0; i < map_bytes; i++)
	{
		if (out->map[i] != PNX_FONT_NO_GLYPH && out->map[i] >= glyphs)
		{
			pnx_log("font %u: codepoint %u maps to glyph %u of %u", asset_id,
					(unsigned)(first_cp + i), out->map[i], glyphs);
			return false;
		}
	}
	return true;
}

// ---------------------------------------------------------------------- accessors

bool pnx_atlas_tile_scaled_rect(const PnxAtlas* a, uint16_t tile, uint8_t* x, uint8_t* y,
								uint8_t* w, uint8_t* h)
{
	for (uint16_t i = 0; i < a->scaled_count; i++)
	{
		const uint8_t* e = a->scaled_rects + (size_t)i * 6;
		if (read_u16(e) == tile)
		{
			if (x)
				*x = e[2];
			if (y)
				*y = e[3];
			if (w)
				*w = e[4];
			if (h)
				*h = e[5];
			return true;
		}
	}
	return false;
}

const uint8_t* pnx_atlas_tile_complex_mask(const PnxAtlas* a, uint16_t tile)
{
	const size_t mask_bytes = ((size_t)a->tile_px * a->tile_px + 7) / 8;
	for (uint16_t i = 0; i < a->complex_count; i++)
	{
		const uint8_t* e = a->complex_masks + (size_t)i * (2 + mask_bytes);
		if (read_u16(e) == tile)
			return e + 2;
	}
	return NULL;
}

bool pnx_sprite_frame_scaled_rect(const PnxSprite* s, uint8_t frame, uint8_t* x, uint8_t* y,
								  uint8_t* w, uint8_t* h)
{
	for (uint16_t i = 0; i < s->scaled_count; i++)
	{
		const uint8_t* e = s->scaled_rects + (size_t)i * 6;
		if (read_u16(e) == frame)
		{
			if (x)
				*x = e[2];
			if (y)
				*y = e[3];
			if (w)
				*w = e[4];
			if (h)
				*h = e[5];
			return true;
		}
	}
	return false;
}

// Records are not fixed-stride here the way an atlas's are (PnxSprite's own comment): a
// frame's mask is only as wide as that frame, so this walks from the start computing each
// record's own span rather than indexing directly.
const uint8_t* pnx_sprite_frame_complex_mask(const PnxSprite* s, uint8_t frame)
{
	const uint8_t* p = s->complex_masks;
	for (uint16_t i = 0; i < s->complex_count; i++)
	{
		const uint16_t f = read_u16(p);
		PnxSpriteFrame fr;
		pnx_sprite_frame_get(s, (uint8_t)f, &fr);
		const size_t mask_bytes = ((size_t)fr.w * fr.h + 7) / 8;
		if (f == frame)
			return p + 2;
		p += 2 + mask_bytes;
	}
	return NULL;
}

const PnxWarp* pnx_map_warp_at(const PnxMap* m, int32_t x, int32_t y)
{
	// Linear: maps have a handful of warps, so an index would cost more memory than the
	// scan costs time, and this only runs on a tile boundary crossing.
	for (uint8_t i = 0; i < m->warp_count; i++)
	{
		if (m->warps[i].x == x && m->warps[i].y == y)
			return &m->warps[i];
	}
	return NULL;
}

uint16_t pnx_dialog_page_count(const PnxDialog* d, uint16_t entry)
{
	if (entry >= d->entry_count)
		return 0;
	const uint8_t* e = d->index + (size_t)entry * 4;
	return (uint16_t)(e[2] | (e[3] << 8));
}

const char* pnx_dialog_page(const PnxDialog* d, uint16_t entry, uint16_t page)
{
	if (entry >= d->entry_count)
		return NULL;
	const uint8_t* e	 = d->index + (size_t)entry * 4;
	const uint16_t first = (uint16_t)(e[0] | (e[1] << 8));
	const uint16_t count = (uint16_t)(e[2] | (e[3] << 8));
	if (page >= count)
		return NULL;
	return (const char*)(d->text + d->offsets[first + page]);
}

// ------------------------------------------------------------------------ scenes

bool pnx_scenes_load(uint16_t asset_id)
{
	if (!s_arena)
		return false;

	// Read into the persistent (low) side: the table has to survive the scene resets
	// it drives, which is why this forces persistent mode regardless of the caller's
	// current setting, same as before this collapsed to one arena with two cursors.
	const bool saved_mode = s_persistent_mode;
	s_persistent_mode	  = true;

	uint8_t count		= 0;
	size_t payload		= 0;
	const uint8_t* data = load_blob(asset_id, "PC", &count, NULL, NULL, &payload);

	s_persistent_mode = saved_mode;
	if (!data)
		return false;

	if (count == 0 || payload < (size_t)count * 4)
	{
		pnx_log("scenes %u: %u scenes needs %u index bytes, blob has %u", asset_id, count,
				(unsigned)(count * 4), (unsigned)payload);
		return false;
	}

	s_scene_table = data;
	s_scene_count = count;
	return true;
}

bool pnx_scene_load(uint16_t scene_id)
{
	if (!s_scene_table || scene_id >= s_scene_count)
	{
		pnx_log("scene %u: out of range (have %u)", scene_id, s_scene_count);
		return false;
	}

	const uint8_t* entry = s_scene_table + (size_t)scene_id * 4;
	const uint16_t first = (uint16_t)(entry[0] | (entry[1] << 8));
	const uint8_t count	 = entry[2];
	const uint16_t* ids =
		(const uint16_t*)(const void*)(s_scene_table + (size_t)s_scene_count * 4);

	// Everything the previous scene held goes at once. There is no partial free anywhere
	// in the framework, and a scene boundary is the only point that needs one. The
	// persistent side must not be touched by this -- pnx_arena_reset_hi, not
	// pnx_arena_reset, which would also clear it.
	pnx_arena_reset_hi(s_arena);
	s_atlas_count = s_sprite_count = s_nine_slice_count = s_font_count = 0;
	s_have_map = s_have_dialog = false;
	s_palettes				   = NULL;
	s_palette_count			   = 0;

	// Palettes first: atlases and sprites carry indices into them, and refuse to load
	// before the table exists.
	if (!pnx_palettes_load(PNX_ASSET_PALETTES_SLOT))
		return false;

	for (uint8_t i = 0; i < count; i++)
	{
		const uint16_t asset = ids[first + i];
		size_t size			 = 0;
		if (!pnx_platform_resource_size(s_resources[asset], &size) || size < 3)
		{
			pnx_log("scene %u: asset %u unreadable", scene_id, asset);
			return false;
		}

		// Dispatch on the blob's own magic rather than on a type recorded in the scene
		// table, so the two can never disagree.
		uint8_t magic[3] = { 0 };
		pnx_platform_resource_read(s_resources[asset], 0, magic, 3);

		bool ok = false;
		if (magic[0] == 'P' && magic[1] == 'A')
		{
			// A scene atlas is no longer paired with anything -- the map owns and streams the
			// tilesets it draws with -- so the asset id it came from is not kept. It is here for
			// whatever else a scene wants a resident tileset for.
			ok = s_atlas_count < PNX_SCENE_MAX_ATLASES &&
				pnx_atlas_load(&s_atlases[s_atlas_count], asset);
			if (ok)
				s_atlas_count++;
		}
		else if (magic[0] == 'P' && magic[1] == 'S')
		{
			ok = s_sprite_count < PNX_SCENE_MAX_SPRITES &&
				pnx_sprite_load(&s_sprites[s_sprite_count], asset);
			if (ok)
				s_sprite_count++;
		}
		else if (magic[0] == 'N' && magic[1] == '9')
		{
			ok = s_nine_slice_count < PNX_SCENE_MAX_NINE_SLICES &&
				pnx_nineslice_load(&s_nine_slices[s_nine_slice_count], asset);
			if (ok)
				s_nine_slice_count++;
		}
		else if (magic[0] == 'P' && magic[1] == 'M')
		{
			// A map names and owns its own tilesets, so there is nothing to pair here any more.
			// The scene's job ends at loading it; the map's pools do the rest.
			ok		   = pnx_map_load(&s_map, asset);
			s_have_map = ok;
		}
		else if (magic[0] == 'P' && magic[1] == 'D')
		{
			ok			  = pnx_dialog_load(&s_dialog, asset);
			s_have_dialog = ok;
		}
		else if (magic[0] == 'P' && magic[1] == 'F')
		{
			ok = s_font_count < PNX_SCENE_MAX_FONTS &&
				pnx_font_load(&s_fonts[s_font_count], asset);
			if (ok)
				s_font_count++;
		}

		if (!ok)
		{
			pnx_log("scene %u: asset %u (%c%c) failed to load", scene_id, asset, magic[0],
					magic[1]);
			return false;
		}
	}

	pnx_log(
		"scene %u: %u assets, %u atlases, %u sprites, %u nine-slices, %u fonts, scene "
		"%u/%u",
		scene_id, count, s_atlas_count, s_sprite_count, s_nine_slice_count, s_font_count,
		(unsigned)s_arena->used_hi, (unsigned)(s_arena->capacity - s_arena->used));
	return true;
}

const PnxAtlas* pnx_scene_atlas(uint8_t index)
{
	return index < s_atlas_count ? &s_atlases[index] : NULL;
}
const PnxSprite* pnx_scene_sprite(uint8_t index)
{
	return index < s_sprite_count ? &s_sprites[index] : NULL;
}
const PnxNineSlice* pnx_scene_nine_slice(uint8_t index)
{
	return index < s_nine_slice_count ? &s_nine_slices[index] : NULL;
}
const PnxFont* pnx_scene_font(uint8_t index)
{
	return index < s_font_count ? &s_fonts[index] : NULL;
}
PnxMap* pnx_scene_map(void)
{
	return s_have_map ? &s_map : NULL;
}
const PnxDialog* pnx_scene_dialog(void)
{
	return s_have_dialog ? &s_dialog : NULL;
}
uint8_t pnx_scene_atlas_count(void)
{
	return s_atlas_count;
}
uint8_t pnx_scene_sprite_count(void)
{
	return s_sprite_count;
}
uint8_t pnx_scene_nine_slice_count(void)
{
	return s_nine_slice_count;
}
uint8_t pnx_scene_font_count(void)
{
	return s_font_count;
}

#if PNX_USE_BITPLANE_COMPRESS
size_t pnx_bitplane_atlas_fetch(void* ctx, uint16_t atlas_asset, uint16_t tile_index,
								uint8_t* scratch, size_t scratch_cap)
{
	(void)ctx;
	if (atlas_asset >= s_resource_count)
		return 0;
	const uint32_t resource = s_resources[atlas_asset];

	uint8_t header[PNX_BLOB_HEADER_BYTES];
	if (pnx_platform_resource_read(resource, 0, header, sizeof(header)) != sizeof(header))
		return 0;
	if (header[0] != 'P' || header[1] != 'T')
		return 0;
	const uint8_t tile_count = header[4];
	if (tile_index >= tile_count)
		return 0;

	// Two u16 offsets bracketing this tile, straight out of the table -- one small read,
	// not the whole table. Costs the same ~29us fixed overhead as a bigger read would
	// (docs/MEASUREMENTS.md's own flash-read model), so this is a real second read paid
	// on every miss, not a free lookup -- caching the table per-atlas across calls is the
	// obvious follow-up if that shows up as a real cost once this is measured on device.
	uint8_t off_pair[4];
	const size_t off_pos = PNX_BLOB_HEADER_BYTES + (size_t)tile_index * 2;
	if (pnx_platform_resource_read(resource, off_pos, off_pair, sizeof(off_pair)) !=
		sizeof(off_pair))
		return 0;
	const uint16_t start = read_u16(off_pair);
	const uint16_t end	 = read_u16(off_pair + 2);
	if (end < start)
		return 0;
	const size_t tile_len = (size_t)(end - start);
	if (tile_len == 0 || tile_len > scratch_cap)
		return 0;

	const size_t table_bytes = (size_t)(tile_count + 1) * 2;
	const size_t data_pos	 = PNX_BLOB_HEADER_BYTES + table_bytes + start;
	if (pnx_platform_resource_read(resource, data_pos, scratch, tile_len) != tile_len)
		return 0;
	return tile_len;
}
#endif // PNX_USE_BITPLANE_COMPRESS

#endif // PNX_USE_ASSETS
