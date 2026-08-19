#include "pnx_lzss.h"

#if PNX_USE_MAP_COMPRESS

size_t pnx_lzss_decode(const uint8_t* src, size_t src_len, uint8_t* dst, size_t dst_len)
{
	size_t si = 0, di = 0;

	while (di < dst_len && si < src_len)
	{
		const uint8_t control = src[si++];

		for (uint8_t bit = 0; bit < 8 && di < dst_len; bit++)
		{
			if (control & (uint8_t)(1u << bit))
			{
				// Literal: one byte, straight through.
				if (si >= src_len)
					return di; // truncated stream
				dst[di++] = src[si++];
			}
			else
			{
				// Match: (distance-1) low byte, then (length-3) << 4 | (distance-1) high
				// nibble -- see this file's own header comment for the token layout.
				if (si + 2 > src_len)
					return di;
				const uint32_t dist = (uint32_t)(src[si] | ((src[si + 1] & 0x0Fu) << 8)) + 1;
				const uint8_t len	= (uint8_t)((src[si + 1] >> 4) + 3);
				si += 2;

				// A well-formed stream never references before the start of `dst`; a
				// corrupt or truncated one might, and writing from a negative offset
				// would read outside the buffer this function was handed. Refusing is
				// cheaper than the bounds check would be on every ordinary byte.
				if (dist > di)
					return di;

				const size_t from = di - dist;
				for (uint8_t k = 0; k < len && di < dst_len; k++)
					dst[di++] = dst[from + k];
			}
		}
	}
	return di;
}

#endif // PNX_USE_MAP_COMPRESS
