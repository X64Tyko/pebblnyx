#include "pnx_layer.h"

#if PNX_USE_LAYERS

// Scales the camera's own offset by parallax_pct/255. A layer at PNX_LAYER_PARALLAX_WORLD
// is handed the real camera untouched -- not a 255/255 multiply that happens to be exact
// -- so an existing single-layer game's one layer costs nothing extra to draw through
// this. int64_t only for the multiply: PnxCamera.x/y are world pixels and parallax_pct
// tops out at 255, comfortably past int32_t range together on nothing this engine targets.
static PnxCamera scaled_camera(const PnxCamera* camera, uint8_t parallax_pct)
{
	if (parallax_pct == PNX_LAYER_PARALLAX_WORLD)
		return *camera;

	PnxCamera c = *camera;
	c.x			= (int32_t)(((int64_t)camera->x * parallax_pct) / 255);
	c.y			= (int32_t)(((int64_t)camera->y * parallax_pct) / 255);
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
		const PnxCamera eff	  = scaled_camera(camera, layer->parallax_pct);

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
