#include "pnx_layer.h"

#if PNX_USE_LAYERS

// Scales the camera's own offset by parallax_pct_x/y / 255, independently per axis. A
// layer with BOTH at PNX_LAYER_PARALLAX_WORLD is handed the real camera untouched -- not
// a 255/255 multiply that happens to be exact -- so an existing single-layer game's one
// layer costs nothing extra to draw through this. Plain int32_t, not int64_t: camera.x/y
// times parallax_pct (<=254 here, 255 already handed off above) only risks overflowing
// int32_t past roughly +-8.4M world pixels, not a world any pebblnyx project's flash/
// resource budget could hold -- and this runs every frame a parallax layer is drawn, so
// the __aeabi_ldivmod/__udivmoddi4 a 64-bit division pulls out of libgcc was paid on
// every platform, not just the ones tight on space.
static PnxCamera scaled_camera(const PnxCamera* camera, uint8_t parallax_pct_x,
							   uint8_t parallax_pct_y)
{
	if (parallax_pct_x == PNX_LAYER_PARALLAX_WORLD && parallax_pct_y == PNX_LAYER_PARALLAX_WORLD)
		return *camera;

	PnxCamera c = *camera;
	c.x			= (camera->x * (int32_t)parallax_pct_x) / 255;
	c.y			= (camera->y * (int32_t)parallax_pct_y) / 255;
	return c;
}

void pnx_layers_draw(void* ctx, const PnxLayer* layers, uint8_t layer_count,
					 const PnxSpriteInstance* instances, uint8_t instance_count, uint8_t* order,
					 PnxTarget* target, const PnxCamera* camera)
{
	if (!layers || !camera)
		return;

	for (uint8_t i = 0; i < layer_count; i++)
	{
		const PnxLayer* layer = &layers[i];
		const PnxCamera eff	  = scaled_camera(camera, layer->parallax_pct_x, layer->parallax_pct_y);

		if (layer->kind == PNX_LAYER_CALLBACK)
		{
			if (layer->as.draw)
				layer->as.draw(ctx, target, &eff);
		}
		else // PNX_LAYER_SPRITES
		{
			pnx_sprites_draw_layer(instances, instance_count, order, target, &eff,
								   layer->as.sprite_layer);
		}
	}
}

#endif // PNX_USE_LAYERS
