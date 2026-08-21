#include "pnx_hud_vars.h"

#if PNX_USE_HUD

#include <string.h>

static int32_t* s_ints;
static char (*s_text)[PNX_HUD_VAR_TEXT_LEN];
static uint8_t s_count;

void pnx_hud_vars_init(int32_t* ints, char (*text)[PNX_HUD_VAR_TEXT_LEN], uint8_t count)
{
	s_ints	= ints;
	s_text	= text;
	s_count = count;

	if (s_ints)
		memset(s_ints, 0, (size_t)count * sizeof(int32_t));
	if (s_text)
		memset(s_text, 0, (size_t)count * PNX_HUD_VAR_TEXT_LEN);
}

void pnx_hud_var_set_i32(uint8_t id, int32_t value)
{
	if (s_ints && id < s_count)
		s_ints[id] = value;
}

int32_t pnx_hud_var_get_i32(uint8_t id)
{
	if (s_ints && id < s_count)
		return s_ints[id];
	return 0;
}

void pnx_hud_var_set_text(uint8_t id, const char* value)
{
	if (!s_text || id >= s_count || !value)
		return;
	strncpy(s_text[id], value, PNX_HUD_VAR_TEXT_LEN - 1);
	s_text[id][PNX_HUD_VAR_TEXT_LEN - 1] = '\0';
}

const char* pnx_hud_var_get_text(uint8_t id)
{
	if (s_text && id < s_count)
		return s_text[id];
	return "";
}

#endif // PNX_USE_HUD
