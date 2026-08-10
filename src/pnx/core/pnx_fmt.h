// Minimal string formatting.
//
// This exists because of a hard platform constraint, not preference. Pebble's libc
// exports `snprintf` but NOT `vsnprintf` -- there is no way to forward a va_list to the
// platform's formatter. Linking newlib's vsnprintf instead fails twice over: its locale
// object collides with libpebble's `setlocale` (multiple definition), and it drags in
// _sbrk/_write/_read/_exit, none of which the app environment provides. The link error
// names none of that.
//
// So any variadic logging or text formatting in the framework has to route through a
// formatter we own. That is cheaper anyway: newlib's is several KB against a 65,535
// byte ceiling shared with the whole game.
//
// Supported: %s %d %i %u %x %X %c %p %% with optional field width and '0' or left-'-'
// padding. The 'l' and 'z' length modifiers are accepted and ignored, because long,
// size_t and int are all 32 bits in this ABI.
//
// NOT supported, both for size reasons measured with tools/size_report.py:
//   %lld / %llu -- 64-bit division pulls in __udivmoddi4, which costs 754 bytes, more
//                  than 1% of the whole app budget, for values this platform does not
//                  have. Cast to 32 bits at the call site.
//   %f and friends -- no FPU, and softfloat formatting is far larger still. Fixed point
//                  is the framework's numeric type; format the parts separately.

#pragma once

#include <stdarg.h>
#include <stddef.h>

// snprintf semantics: always NUL-terminates when size > 0, and returns the length the
// output WOULD have had, so truncation is detectable by comparing against size.
int pnx_vformat(char *buf, size_t size, const char *fmt, va_list ap);

int pnx_format(char *buf, size_t size, const char *fmt, ...);