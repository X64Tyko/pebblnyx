#include "pnx_text.h"

#if PNX_USE_TEXT

// ------------------------------------------------------------------------ blending
//
// Only depth 2 needs this. ARGB2222 gives each channel two bits, so a blend is a table
// indexed by (ink, dst) -- 16 entries per ratio, 32 bytes for both, shared across R, G
// and B. That is the whole cost of antialiased text, and it is why the alternative
// (blending in a wider space and quantising back) was not worth considering.
//
// Entry [s][d] is round((s * k + d * (3 - k)) / 3) for k = 1 and k = 2. The tables are
// written out rather than computed so they cost no runtime and no bss; test_gfx.c checks
// them against the formula, which is what keeps a typo here from becoming a colour bug.

#define BLEND(s, d) ((uint8_t)(s) * 4u + (uint8_t)(d))

// k = 1: one third ink, two thirds destination.
static const uint8_t s_blend_13[16] = {
  0, 1, 1, 2,
  0, 1, 2, 2,
  1, 1, 2, 3,
  1, 2, 2, 3,
};

// k = 2: two thirds ink.
static const uint8_t s_blend_23[16] = {
  0, 0, 1, 1,
  1, 1, 1, 2,
  1, 2, 2, 2,
  2, 2, 3, 3,
};

// Alpha comes from the ink, not from the blend: text is opaque, and the coverage level
// has already decided how much of it lands.
static inline uint8_t blend_px(uint8_t ink, uint8_t dst, const uint8_t *lut) {
  const uint8_t r = lut[BLEND((ink >> 4) & 3, (dst >> 4) & 3)];
  const uint8_t g = lut[BLEND((ink >> 2) & 3, (dst >> 2) & 3)];
  const uint8_t b = lut[BLEND(ink & 3, dst & 3)];
  return (uint8_t)((ink & 0xC0) | (uint8_t)(r << 4) | (uint8_t)(g << 2) | b);
}

// --------------------------------------------------------------------------- spans
//
// One glyph row into one framebuffer row, clipped against the row's own reported span
// rather than the target width -- a round display narrows [min_x, max_x] per row, and
// assuming a rectangle is what breaks there. Same contract as span_4bpp in pnx_gfx.c.

static void span_1bpp(uint8_t *row_base, int32_t x, const uint8_t *line,
                      uint8_t colour, int32_t w, int16_t min_x, int16_t max_x) {
  int32_t i0 = 0, i1 = w;
  if (x + i0 < min_x) i0 = min_x - x;
  if (x + i1 > max_x + 1) i1 = max_x + 1 - x;
  if (i1 <= i0) return;

  uint8_t *dst = row_base + x;
  for (int32_t i = i0; i < i1; i++) {
    // MSB first: pixel i is bit 7 - (i & 7) of byte i >> 3.
    if (line[i >> 3] & (uint8_t)(0x80u >> (i & 7))) dst[i] = colour;
  }
}

static void span_2bpp(uint8_t *row_base, int32_t x, const uint8_t *line,
                      uint8_t colour, int32_t w, int16_t min_x, int16_t max_x) {
  int32_t i0 = 0, i1 = w;
  if (x + i0 < min_x) i0 = min_x - x;
  if (x + i1 > max_x + 1) i1 = max_x + 1 - x;
  if (i1 <= i0) return;

  uint8_t *dst = row_base + x;
  for (int32_t i = i0; i < i1; i++) {
    const uint8_t level = (uint8_t)((line[i >> 2] >> (6 - 2 * (i & 3))) & 3u);
    if (level == 0) continue;                       // transparent, the common case
    if (level == 3) { dst[i] = colour; continue; }  // full ink, no read needed
    dst[i] = blend_px(colour, dst[i], level == 1 ? s_blend_13 : s_blend_23);
  }
}

// One glyph at a pen position. `y` is the baseline; the bitmap's top row sits
// bearing_y above it.
static void draw_glyph(PnxTarget *t, const PnxFont *f, const PnxGlyph *g,
                       int32_t pen_x, int32_t baseline_y, uint8_t colour) {
  if (!g->bits) return;                             // a space: advance only

  const int32_t x = pen_x + g->bearing_x;
  const int32_t y = baseline_y - g->bearing_y;
  const int16_t th = pnx_target_height(t);
  const uint8_t stride = pnx_font_row_bytes(f, g->w);

  int32_t j0 = 0, j1 = g->h;
  if (y < 0) j0 = -y;
  if (y + g->h > th) j1 = th - y;

  for (int32_t j = j0; j < j1; j++) {
    PnxRow row = pnx_target_row(t, (int16_t)(y + j));
    if (!row.data) continue;

    const uint8_t *line = g->bits + (uint32_t)j * stride;
    if (f->depth == 1) {
      span_1bpp(row.data, x, line, colour, g->w, row.min_x, row.max_x);
    } else {
      span_2bpp(row.data, x, line, colour, g->w, row.min_x, row.max_x);
    }
  }
}

// ------------------------------------------------------------------------ measuring

static inline int16_t glyph_advance(const PnxFont *f, char c) {
  PnxGlyph g;
  pnx_font_glyph(f, pnx_font_glyph_index(f, c), &g);
  return g.advance;
}

// Width of [s, end). Used by both the measure calls and by alignment, so a centred line
// can never be centred against a width the draw does not produce.
static int16_t width_range(const PnxFont *f, const char *s, const char *end) {
  int32_t w = 0;
  for (const char *p = s; p < end && *p; p++) w += glyph_advance(f, *p);
  return (int16_t)w;
}

int16_t pnx_text_width(const PnxFont *f, const char *s) {
  if (!f || !s) return 0;
  int32_t w = 0;
  for (const char *p = s; *p && *p != '\n'; p++) w += glyph_advance(f, *p);
  return (int16_t)w;
}

// --------------------------------------------------------------------------- wrap

typedef struct {
  const char *end;   // one past the last character to DRAW on this line
  const char *next;  // where the following line starts
} PnxLineBreak;

// Finds the next line break at width `w`.
//
// Three cases, in priority order: an explicit '\n'; the last space before the overrun;
// and -- when a single word is wider than the box -- a hard break at the character that
// would have overflowed. The hard break matters more than it looks: without it a long
// word runs out of a dialogue box and over the art, which reads as a rendering bug.
//
// Always consumes at least one character, so a glyph wider than the whole box cannot
// spin the caller forever.
static PnxLineBreak next_line(const PnxFont *f, const char *s, int16_t w) {
  const char *last_space = NULL;
  int32_t width = 0;

  for (const char *p = s; *p; p++) {
    if (*p == '\n') {
      PnxLineBreak br = { p, p + 1 };
      return br;
    }

    const int32_t adv = glyph_advance(f, *p);
    if (w > 0 && width + adv > w && p != s) {
      const char *end = last_space ? last_space : p;
      const char *next = last_space ? last_space + 1 : p;
      // Collapse the run of spaces at the break, so a wrapped line does not start
      // indented by whatever happened to follow the break point.
      while (*next == ' ') next++;
      PnxLineBreak br = { end, next };
      return br;
    }

    if (*p == ' ') last_space = p;
    width += adv;
  }

  const char *end = s;
  while (*end) end++;
  PnxLineBreak br = { end, end };
  return br;
}

int16_t pnx_text_lines_wrapped(const PnxFont *f, const char *s, int16_t w) {
  if (!f || !s || !*s) return 0;

  int16_t lines = 0;
  for (const char *p = s; *p; ) {
    const PnxLineBreak br = next_line(f, p, w);
    lines++;
    if (br.next == p) break;      // cannot happen, but never loop on a bad font
    p = br.next;
  }
  return lines;
}

int16_t pnx_text_height_wrapped(const PnxFont *f, const char *s, int16_t w) {
  if (!f) return 0;
  return (int16_t)(pnx_text_lines_wrapped(f, s, w) * f->line_height);
}

// ------------------------------------------------------------------------ drawing

int16_t pnx_text_draw(PnxTarget *t, const PnxFont *f, const char *s,
                      int32_t x, int32_t y, uint8_t colour) {
  if (!t || !f || !s) return 0;

  int32_t pen = x;
  for (const char *p = s; *p && *p != '\n'; p++) {
    PnxGlyph g;
    pnx_font_glyph(f, pnx_font_glyph_index(f, *p), &g);
    draw_glyph(t, f, &g, pen, y, colour);
    pen += g.advance;
  }
  return (int16_t)(pen - x);
}

int16_t pnx_text_draw_wrapped(PnxTarget *t, const PnxFont *f, const char *s,
                              int32_t x, int32_t y, int16_t w, int16_t h,
                              uint8_t colour, PnxTextAlign align) {
  if (!t || !f || !s) return 0;

  int16_t drawn = 0;
  int32_t baseline = y;

  for (const char *p = s; *p; ) {
    const PnxLineBreak br = next_line(f, p, w);

    // `h` bounds the box from the first baseline, so a caller sizing a dialogue box by
    // pnx_text_height_wrapped gets exactly the lines it measured.
    if (h > 0 && baseline - y >= h) break;

    int32_t lx = x;
    if (align != PNX_ALIGN_LEFT) {
      const int16_t lw = width_range(f, p, br.end);
      lx += (align == PNX_ALIGN_CENTER) ? (w - lw) / 2 : (w - lw);
    }

    int32_t pen = lx;
    for (const char *c = p; c < br.end; c++) {
      PnxGlyph g;
      pnx_font_glyph(f, pnx_font_glyph_index(f, *c), &g);
      draw_glyph(t, f, &g, pen, baseline, colour);
      pen += g.advance;
    }

    drawn++;
    baseline += f->line_height;
    if (br.next == p) break;
    p = br.next;
  }
  return drawn;
}

#endif  // PNX_USE_TEXT
