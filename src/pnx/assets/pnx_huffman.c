#include "pnx_huffman.h"

#if PNX_COMPRESS_MODE == PNX_COMPRESS_HUFFMAN

typedef struct
{
	const uint8_t* data;
	size_t len_bits;
	size_t pos;
	bool overrun;
} BitReader;

static inline bool hf_read_bit(BitReader* r)
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

static uint32_t hf_read_bits_msb(BitReader* r, uint8_t n)
{
	uint32_t v = 0;
	for (uint8_t i = 0; i < n; i++)
		v = (v << 1) | hf_read_bit(r);
	return v;
}

// Wire field widths for the shared/global table itself (tools/bpeg2_prototype.py's
// SHARED_COLS_BITS/SHARED_CLEN_BITS) -- wider than the per-unit format's own 6b cols/3b
// code-length fields because a pooled table's alphabet and code lengths run well past
// what one unit's own small alphabet needs.
#define HF_SHARED_COLS_BITS 12
#define HF_SHARED_CLEN_BITS 5

bool pnx_huffman_table_peek_cols(const uint8_t* data, size_t len, uint16_t* out_cols)
{
	if (len < 2) // HF_SHARED_COLS_BITS (12) needs at least 2 bytes to be present at all
		return false;
	BitReader r		 = { .data = data, .len_bits = len * 8, .pos = 0, .overrun = false };
	const uint16_t c = (uint16_t)hf_read_bits_msb(&r, HF_SHARED_COLS_BITS);
	if (r.overrun)
		return false;
	*out_cols = c;
	return true;
}

bool pnx_huffman_table_parse(PnxHuffmanTable* table, const uint8_t* data, size_t len,
							 uint16_t* symbols_buf, uint16_t symbols_buf_cap)
{
	BitReader r = { .data = data, .len_bits = len * 8, .pos = 0, .overrun = false };

	const uint16_t cols	   = (uint16_t)hf_read_bits_msb(&r, HF_SHARED_COLS_BITS);
	const uint8_t run_bits = (uint8_t)(hf_read_bits_msb(&r, 5) + 1);
	if (r.overrun || cols > PNX_HUFFMAN_TABLE_MAX_COLS || cols > symbols_buf_cap)
		return false;

	uint16_t tmp_run[PNX_HUFFMAN_TABLE_MAX_COLS];
	uint8_t tmp_len[PNX_HUFFMAN_TABLE_MAX_COLS];

	for (uint16_t i = 0; i <= PNX_HUFFMAN_MAX_CODE_LEN; i++)
		table->count[i] = 0;

	for (uint16_t i = 0; i < cols; i++)
	{
		const uint16_t run_len = (uint16_t)hf_read_bits_msb(&r, run_bits);
		const uint8_t clen	   = (uint8_t)(hf_read_bits_msb(&r, HF_SHARED_CLEN_BITS) + 1);
		if (clen == 0 || clen > PNX_HUFFMAN_MAX_CODE_LEN)
			return false;
		hf_read_bits_msb(&r, clen); // code value: consumed to stay aligned, not used --
									// see this function's own comment.
		if (r.overrun)
			return false;
		tmp_run[i] = run_len;
		tmp_len[i] = clen;
		table->count[clen]++;
	}

	// Standard canonical-Huffman reconstruction from lengths alone: code 0 opens length
	// 1's range; every step to the next length doubles what's been assigned so far (one
	// more bit of precision) after adding this length's own symbol count. Matches
	// tools/bpeg2_prototype.py's canonical_codes() bit for bit (that function computes
	// the same recurrence via `code <<= (length - prev_len)`, skipping unused lengths in
	// one shift instead of one bit at a time -- same result either way).
	uint16_t code	= 0;
	uint16_t offset = 0;
	for (uint8_t clen = 1; clen <= PNX_HUFFMAN_MAX_CODE_LEN; clen++)
	{
		table->first_code[clen]	  = code;
		table->first_symbol[clen] = offset;
		code					  = (uint16_t)((code + table->count[clen]) << 1);
		offset					  = (uint16_t)(offset + table->count[clen]);
	}

	uint16_t cursor[PNX_HUFFMAN_MAX_CODE_LEN + 1];
	for (uint16_t i = 1; i <= PNX_HUFFMAN_MAX_CODE_LEN; i++)
		cursor[i] = table->first_symbol[i];
	for (uint16_t i = 0; i < cols; i++)
		symbols_buf[cursor[tmp_len[i]]++] = tmp_run[i];

	table->symbols	= symbols_buf;
	table->cols		= cols;
	table->run_bits = run_bits;
	return true;
}

// Standard canonical-Huffman symbol decode: extends `code` one bit at a time and checks,
// at each length, whether it falls in that length's contiguous code range
// [first_code[len], first_code[len]+count[len]) -- true exactly once, at the length the
// encoder actually assigned this run length, since canonical codes never share a prefix
// across lengths. O(code length), not O(table size), unlike a linear scan over every
// entry per bit.
static bool hf_read_symbol(BitReader* r, const PnxHuffmanTable* t, uint16_t* out_run)
{
	uint16_t code = 0;
	for (uint8_t len = 1; len <= PNX_HUFFMAN_MAX_CODE_LEN; len++)
	{
		code = (uint16_t)((code << 1) | hf_read_bit(r));
		if (r->overrun)
			return false;
		const uint16_t c = t->count[len];
		if (c != 0 && code >= t->first_code[len] &&
			(uint32_t)code < (uint32_t)t->first_code[len] + c)
		{
			*out_run = t->symbols[t->first_symbol[len] + (uint16_t)(code - t->first_code[len])];
			return true;
		}
	}
	return false; // malformed stream, or a table that doesn't match what encoded it
}

// Packs `scratch[0..n)` (one real palette index per byte) into `dst` -- identical layout
// to pnx_bitplane.c's own bp_pack (see that file's comment for why the two output
// formats exist); duplicated rather than shared because PNX_COMPRESS_MODE is mutually
// exclusive and pnx_bitplane.c is never compiled into a PNX_COMPRESS_HUFFMAN build.
#if PNX_DISPLAY_BW
static void hf_pack(const uint8_t* scratch, uint8_t* dst, uint16_t n)
{
	for (uint16_t i = 0; i < n; i += 4)
	{
		uint8_t byte = 0;
		for (uint8_t k = 0; k < 4; k++)
		{
			const uint16_t j = (uint16_t)(i + k);
			const uint8_t s	 = j < n ? scratch[j] : 0;
			byte |= (uint8_t)(s << (6 - 2 * k));
		}
		dst[i / 4] = byte;
	}
}
#else
static void hf_pack(const uint8_t* scratch, uint8_t* dst, uint16_t n)
{
	for (uint16_t i = 0; i < n; i += 2)
	{
		const uint8_t hi = scratch[i];
		const uint8_t lo = (uint16_t)(i + 1) < n ? scratch[i + 1] : 0;
		dst[i / 2]		 = (uint8_t)((hi << 4) | lo);
	}
}
#endif

bool pnx_huffman_decode(const PnxHuffmanTable* table, const uint8_t* src, size_t src_len,
						uint8_t* dst, uint8_t* scratch, uint16_t n)
{
	if (!table || n == 0 || src_len == 0)
		return false;

	const uint8_t header = src[0];

	if (header == 0) // k == 1: fill, no offset table, no run coding at all
	{
		if (src_len < 2)
			return false;
		const uint8_t c = src[1] >> 4;
		for (uint16_t i = 0; i < n; i++)
			scratch[i] = c;
		hf_pack(scratch, dst, n);
		return true;
	}

	const bool raw_flag	   = (header & 0x80) != 0;
	const uint8_t k		   = (header & 0x0F) + 1;
	const size_t off_bytes = ((size_t)k + 1) / 2;
	if (src_len < 1 + off_bytes)
		return false;

	uint8_t offset_table[16];
	const uint8_t* off_src = src + 1;
	for (uint8_t i = 0; i < k; i++)
		offset_table[i] = (i & 1) == 0 ? (off_src[i / 2] >> 4) : (off_src[i / 2] & 0x0F);

	if (raw_flag)
	{
		const uint8_t* body	  = src + 1 + off_bytes;
		const size_t body_len = src_len - 1 - off_bytes;
		const size_t need	  = ((size_t)n + 1) / 2; // packed LOCAL indices, 2/byte
		if (body_len < need)
			return false;
		for (uint16_t i = 0; i < n; i++)
		{
			const uint8_t b		= body[i / 2];
			const uint8_t local = (i & 1) == 0 ? (uint8_t)(b >> 4) : (uint8_t)(b & 0x0F);
			if (local >= k)
				return false;
			scratch[i] = offset_table[local];
		}
		hf_pack(scratch, dst, n);
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

	const uint8_t start_bit = (uint8_t)hf_read_bit(&r);
	const uint32_t total	= (uint32_t)bits * n;

	for (uint16_t i = 0; i < n; i++)
		scratch[i] = 0;

	uint32_t pos	= 0;
	uint8_t current = start_bit;
	while (pos < total)
	{
		uint16_t run;
		if (r.overrun || !hf_read_symbol(&r, table, &run) || run == 0 || pos + run > total)
			return false;
		if (current)
		{
			// Same per-pixel plane-bit OR as pnx_bitplane_decode, but over the
			// CONCATENATED bpp*n sequence: bit index `idx` is plane `idx/n`, pixel
			// `idx%n` -- matches tools/bpeg2_prototype.py's concat_bitplanes/
			// _seq_to_pixels exactly (see pnx_huffman.h's own format comment).
			for (uint32_t i = 0; i < run; i++)
			{
				const uint32_t idx = pos + i;
				scratch[idx % n] |= (uint8_t)(1u << (idx / n));
			}
		}
		pos += run;
		current ^= 1;
	}

	for (uint16_t i = 0; i < n; i++)
	{
		if (scratch[i] >= k)
			return false;
		scratch[i] = offset_table[scratch[i]];
	}
	hf_pack(scratch, dst, n);
	return true;
}

#endif // PNX_COMPRESS_MODE == PNX_COMPRESS_HUFFMAN
