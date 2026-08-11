#include "pnx_tilemap.h"

#if PNX_USE_TILEMAP

void pnx_tilemap_draw(const PnxMap *map, const PnxAtlas *atlas,
                      PnxTarget *target, const PnxCamera *camera) {
  if (!map || !atlas) return;

  const int32_t T = atlas->tile_px;
  const bool metatiled = pnx_atlas_is_metatiled(atlas);

  // Floor division, not truncation: at a negative camera x, truncation rounds toward
  // zero and drops the column that should be partly visible at the left edge.
  const int32_t first_tx = pnx_floor_div(camera->x, T);
  const int32_t first_ty = pnx_floor_div(camera->y, T);

  // One extra row and column, because the first is usually partly off screen.
  const int32_t cols = camera->view_w / T + 2;
  const int32_t rows = camera->view_h / T + 2;

  for (int32_t ty = first_ty; ty < first_ty + rows; ty++) {
    if (ty < 0 || ty >= map->h) continue;
    for (int32_t tx = first_tx; tx < first_tx + cols; tx++) {
      if (tx < 0 || tx >= map->w) continue;

      const uint16_t id = pnx_map_tile(map, tx, ty);
      const uint8_t flip = pnx_map_flip(map, tx, ty);
      const int32_t sx = tx * T - camera->x;
      const int32_t sy = ty * T - camera->y;

      // Hoisted out of the loop body by the branch predictor, not by us: an atlas is
      // one layout or the other for its whole life.
      if (metatiled) {
        // Flips are not applied to metatiles: mirroring a composed tile means mirroring
        // the quadrant ORDER as well as each quadrant, and the pipeline does not emit
        // flips for metatiled atlases for that reason.
        pnx_blit_metatile(target, atlas, id, sx, sy);
      } else {
        pnx_blit_4bpp(target, pnx_atlas_tile(atlas, (uint8_t)id),
                      pnx_atlas_tile_palette(atlas, (uint8_t)id),
                      sx, sy, (int16_t)T, (int16_t)T, flip);
      }
    }
  }
}

#endif  // PNX_USE_TILEMAP
