#include "pnx_bitplane.h"

#if PNX_USE_BITPLANE_COMPRESS

// Bit-level reader, MSB-first within each byte -- matches tools/bpeg_encode.py's
// BitWriter exactly (see that file for the encoder, and pnx_bitplane.h for the format).
typedef struct
{
	const uint8_t* data;
	size_t len_bits;
	size_t pos;
	bool overrun;
} BitReader;

static inline bool bp_read_bit(BitReader* r)
{
	if (r->pos >= r->len_bits)
	{
		r->overrun = true;
		return 0;
	}
	const uint8_t byte	= r->data[r->pos >> 3];
	const uint8_t shift = (uint8_t)(7 - (r->pos & 7));
	r->pos++;
	return (byte >> shift) & 1;
}

static uint32_t bp_read_bits_msb(BitReader* r, uint8_t n)
{
	uint32_t v = 0;
	for (uint8_t i = 0; i < n; i++)
		v = (v << 1) | bp_read_bit(r);
	return v;
}

// Elias-gamma: unary exponent (n-1 one-bits then a zero, n bits total) then an n-bit
// mantissa, value = (1<<n) + mantissa - 1. See pnx_bitplane.h's own comment for why --
// unbounded run length, no fixed-width cap to split long runs against.
static uint32_t bp_read_elias_gamma(BitReader* r)
{
	uint8_t n_bits = 1;
	while (bp_read_bit(r))
		n_bits++;
	const uint32_t mantissa = bp_read_bits_msb(r, n_bits);
	return (1u << n_bits) + mantissa - 1;
}

// Packs `scratch[0..n)` (one real 4bpp palette index per byte) into `dst`, 2 pixels/byte,
// high nibble first -- pack_unit_4bpp's own layout, unconditionally: this is the only
// place decoded pixels ever leave the module, whichever path (raw/k==1/bitplane) produced
// them.
static void bp_pack(const uint8_t* scratch, uint8_t* dst, uint16_t n)
{
	for (uint16_t i = 0; i < n; i += 2)
	{
		const uint8_t hi = scratch[i];
		const uint8_t lo = (uint16_t)(i + 1) < n ? scratch[i + 1] : 0;
		dst[i / 2]		 = (uint8_t)((hi << 4) | lo);
	}
}

bool pnx_bitplane_decode(const uint8_t* src, size_t src_len, uint8_t* dst, uint8_t* scratch,
						 uint16_t n)
{
	if (n == 0 || src_len == 0)
		return false;

	const uint8_t header = src[0];
	const bool raw_flag	 = (header & 0x80) != 0;

	if (raw_flag)
	{
		// Already packed on disk (the encoder's own escape hatch is pack_unit_4bpp's
		// layout verbatim) -- straight copy, no scratch needed at all.
		const size_t need = ((size_t)n + 1) / 2;
		if (src_len < 1 + need)
			return false;
		for (size_t i = 0; i < need; i++)
			dst[i] = src[1 + i];
		return true;
	}

	const uint8_t k		   = (header & 0x0F) + 1;
	const size_t off_bytes = ((size_t)k + 1) / 2;
	if (src_len < 1 + off_bytes)
		return false;

	uint8_t offset_table[16];
	const uint8_t* off_src = src + 1;
	for (uint8_t i = 0; i < k; i++)
		offset_table[i] = (i & 1) == 0 ? (off_src[i / 2] >> 4) : (off_src[i / 2] & 0x0F);

	if (k == 1)
	{
		for (uint16_t i = 0; i < n; i++)
			scratch[i] = offset_table[0];
		bp_pack(scratch, dst, n);
		return true;
	}

	const uint8_t bits = k <= 2 ? 1 : k <= 4 ? 2
		: k <= 8							 ? 3
											 : 4;

	BitReader r = {
		.data	  = src + 1 + off_bytes,
		.len_bits = (src_len - 1 - off_bytes) * 8,
		.pos	  = 0,
		.overrun  = false,
	};

	// One local-index accumulator per pixel, bits OR'd in plane by plane, into `scratch`
	// -- can't pack 2-to-a-byte here: pixel i and pixel i+1's bits for the SAME plane
	// land in what would be different nibbles of a shared output byte, so the natural
	// per-plane, per-run write pattern needs one full byte per pixel until every plane
	// has contributed. Packing happens once, after the offset_table remap below.
	for (uint16_t i = 0; i < n; i++)
		scratch[i] = 0;

	for (uint8_t p = 0; p < bits; p++)
	{
		uint8_t current = (uint8_t)bp_read_bit(&r);
		uint16_t pos	= 0;
		while (pos < n)
		{
			const uint32_t run = bp_read_elias_gamma(&r);
			if (r.overrun || run == 0 || (uint32_t)pos + run > n)
				return false;
			if (current)
			{
				for (uint32_t i = 0; i < run; i++)
					scratch[pos + i] |= (uint8_t)(1u << p);
			}
			pos = (uint16_t)(pos + run);
			current ^= 1;
		}
	}

	for (uint16_t i = 0; i < n; i++)
	{
		if (scratch[i] >= k)
			return false;
		scratch[i] = offset_table[scratch[i]];
	}
	bp_pack(scratch, dst, n);
	return true;
}

#endif // PNX_USE_BITPLANE_COMPRESS
