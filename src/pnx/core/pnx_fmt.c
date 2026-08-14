#include "pnx_fmt.h"

#include <stdint.h>
#include <stdbool.h>

// Longest output of any single conversion: 10 digits for a 32-bit decimal, plus sign
// and terminator.
#define NUM_BUF 13

typedef struct
{
	char* buf;
	size_t size;
	size_t used; // counts what WOULD be written, so truncation is detectable
} Sink;

static void emit(Sink* s, char c)
{
	// Reserve the last byte for the terminator rather than overwriting it.
	if (s->size > 0 && s->used + 1 < s->size)
	{
		s->buf[s->used] = c;
	}
	s->used++;
}

static void emit_pad(Sink* s, char c, int count)
{
	for (int i = 0; i < count; i++)
		emit(s, c);
}

// Renders `value` in `base` into the END of tmp and returns a pointer to the first
// digit. Building backwards avoids a second reversal pass.
// 32-bit deliberately. A uint64_t here makes the compiler emit calls to __udivmoddi4,
// the software 64-bit division helper, which measured at 754 bytes -- more than 1% of
// the entire app budget, spent so that %llu could work on a platform where nothing is
// 64-bit. See the note on length modifiers in pnx_fmt.h.
static char* render_uint(uint32_t value, unsigned base, bool upper, char* tmp)
{
	const char* digits = upper ? "0123456789ABCDEF" : "0123456789abcdef";
	char* p			   = tmp + NUM_BUF;
	*--p			   = '\0';

	if (value == 0)
	{
		*--p = '0';
		return p;
	}
	while (value != 0)
	{
		*--p = digits[value % base];
		value /= base;
	}
	return p;
}

static void emit_str(Sink* s, const char* str, int width, bool left_align, char pad)
{
	if (!str)
		str = "(null)";

	int len = 0;
	while (str[len] != '\0')
		len++;

	const int padding = width > len ? width - len : 0;

	if (!left_align)
		emit_pad(s, pad, padding);
	for (int i = 0; i < len; i++)
		emit(s, str[i]);
	if (left_align)
		emit_pad(s, ' ', padding);
}

int pnx_vformat(char* buf, size_t size, const char* fmt, va_list ap)
{
	Sink s = { buf, size, 0 };

	if (!fmt)
		fmt = "(null)";

	for (const char* p = fmt; *p != '\0'; p++)
	{
		if (*p != '%')
		{
			emit(&s, *p);
			continue;
		}

		p++;
		if (*p == '\0')
			break; // trailing '%' with nothing after it

		// ---- flags
		bool left_align = false;
		char pad		= ' ';
		for (;; p++)
		{
			if (*p == '-')
				left_align = true;
			else if (*p == '0')
				pad = '0';
			else
				break;
		}

		// ---- width
		int width = 0;
		while (*p >= '0' && *p <= '9')
		{
			width = width * 10 + (*p - '0');
			p++;
		}

		// ---- length modifier. Accepted and ignored: long, size_t and int are all 32 bits
		// in this ABI, so 'l' and 'z' change nothing. 'll' is NOT supported -- see header.
		while (*p == 'l' || *p == 'z')
			p++;

		char tmp[NUM_BUF];
		bool negative	   = false;
		uint32_t magnitude = 0;
		unsigned base	   = 10;
		bool upper		   = false;

		switch (*p)
		{
			case 's':
				emit_str(&s, va_arg(ap, const char*), width, left_align, ' ');
				continue;

			case 'c':
				{
					// char promotes to int through varargs.
					const char c	  = (char)va_arg(ap, int);
					const char one[2] = { c, '\0' };
					emit_str(&s, one, width, left_align, ' ');
					continue;
				}

			case '%':
				emit(&s, '%');
				continue;

			case 'd':
			case 'i':
				{
					const int32_t v = va_arg(ap, int);
					negative		= v < 0;
					// Negated through unsigned so INT32_MIN, which has no positive counterpart,
					// does not overflow on the way.
					magnitude = negative ? (uint32_t)(-(v + 1)) + 1u : (uint32_t)v;
					break;
				}

			case 'u':
				magnitude = va_arg(ap, unsigned int);
				break;

			case 'X':
				upper = true;
				/* fall through */
			case 'x':
				base	  = 16;
				magnitude = va_arg(ap, unsigned int);
				break;

			case 'p':
				{
					const uintptr_t v = (uintptr_t)va_arg(ap, void*);
					emit(&s, '0');
					emit(&s, 'x');
					base	  = 16;
					magnitude = (uint32_t)v;
					break;
				}

			default:
				// Unknown conversion: emit it literally rather than silently swallowing it, so
				// a typo in a format string is visible instead of producing missing output.
				emit(&s, '%');
				emit(&s, *p);
				continue;
		}

		char* digits = render_uint(magnitude, base, upper, tmp);

		int len = 0;
		while (digits[len] != '\0')
			len++;
		if (negative)
			len++;

		const int padding = width > len ? width - len : 0;

		// Zero padding goes AFTER the sign ("-007"), space padding before it ("  -7").
		if (!left_align && pad == ' ')
			emit_pad(&s, ' ', padding);
		if (negative)
			emit(&s, '-');
		if (!left_align && pad == '0')
			emit_pad(&s, '0', padding);

		for (int i = 0; digits[i] != '\0'; i++)
			emit(&s, digits[i]);

		if (left_align)
			emit_pad(&s, ' ', padding);
	}

	if (size > 0)
	{
		const size_t terminator = s.used < size ? s.used : size - 1;
		buf[terminator]			= '\0';
	}

	return (int)s.used;
}

int pnx_format(char* buf, size_t size, const char* fmt, ...)
{
	va_list ap;
	va_start(ap, fmt);
	const int n = pnx_vformat(buf, size, fmt, ap);
	va_end(ap);
	return n;
}
