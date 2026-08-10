#include "pnx_sprite.h"

#if PNX_USE_SPRITES

#include "../assets/pnx_assets.h"

void pnx_sprite_draw(const PnxSprite *sprite, PnxTarget *target,
                     const PnxCamera *camera, int32_t wx, int32_t wy,
                     uint8_t frame, const PnxPalette *palette, bool mirror) {
  if (!sprite || frame >= sprite->frame_count) return;

  if (!palette) palette = pnx_sprite_frame_palette(sprite, frame);

  // Feet anchor: y names the ground line, so the art extends upward from it.
  pnx_blit_4bpp(target, pnx_sprite_frame(sprite, frame), palette,
                wx - camera->x - sprite->w / 2,
                wy - camera->y - sprite->h,
                sprite->w, sprite->h, mirror);
}

void pnx_sprites_draw_sorted(const PnxSpriteInstance *instances, uint8_t count,
                             uint8_t *order, PnxTarget *target,
                             const PnxCamera *camera) {
  if (!instances || !order) return;

  uint8_t n = 0;
  for (uint8_t i = 0; i < count; i++) {
    if (!(instances[i].flags & PNX_SPRITE_HIDDEN)) order[n++] = i;
  }

  // Insertion sort: n is small (a screen holds a handful of characters) and the order
  // is nearly sorted frame to frame, which is the case insertion sort is best at and
  // quicksort is worst at.
  for (uint8_t i = 1; i < n; i++) {
    const uint8_t key = order[i];
    int16_t j = (int16_t)i - 1;
    while (j >= 0 && instances[order[j]].y > instances[key].y) {
      order[j + 1] = order[j];
      j--;
    }
    order[j + 1] = key;
  }

  for (uint8_t k = 0; k < n; k++) {
    const PnxSpriteInstance *s = &instances[order[k]];
    const PnxSprite *asset = pnx_scene_sprite(s->sprite);
    if (!asset) continue;

    const PnxPalette *pal = (s->palette == PNX_SPRITE_PALETTE_DEFAULT)
                            ? NULL : pnx_palette(s->palette);

    pnx_sprite_draw(asset, target, camera, s->x, s->y, s->frame, pal,
                    (s->flags & PNX_SPRITE_MIRROR) != 0);
  }
}

#endif  // PNX_USE_SPRITES
