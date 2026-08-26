# Host test build. Deliberately plain: the point is that `make` in this directory
# compiles and runs the platform-independent parts of the framework with a normal
# compiler, so a logic bug is caught in a second instead of over Bluetooth.
#
# Only core and the host platform build here. Anything that includes <pebble.h> cannot,
# and that is the invariant being enforced -- if a module starts failing to compile
# under this Makefile, something above the platform layer reached for the SDK.
#
# PNX_COMPRESS_MODE (pnx_config.h) is a project-wide, mutually exclusive choice -- a
# single binary can only be built in ONE mode, so mode-specific behaviour needs its own
# binary rather than a shared one exercising all three the way the old, additive
# PNX_USE_*_COMPRESS flags let one binary do. Three binaries below, one per mode:
# $(OUT) (PNX_COMPRESS_NONE, the general framework suite -- most tests want plain
# resident-pixel semantics and do not care about compression at all), $(LZSS_OUT)
# (PNX_COMPRESS_LZSS), $(BITPLANE_OUT) (PNX_COMPRESS_BITPLANE, also the default a project
# gets if it names no mode).

CC      ?= cc
CFLAGS  ?= -std=c11 -Wall -Wextra -Werror -Wno-unused-parameter -DPNX_PLATFORM_HOST -DPNX_USE_SYNTH=1 -g -O1
SRC      = test_core.c test_assets.c test_gfx.c test_text.c test_audio.c test_input.c test_stream.c \
           test_synth.c test_save.c test_app.c test_physics.c test_sprite.c test_layer.c test_map_compress.c \
           test_tween.c test_hud_vars.c test_hud_window.c test_blit_scaled.c \
           ../src/pnx/core/pnx_arena.c ../src/pnx/core/pnx_fmt.c ../src/pnx/core/pnx_diag.c \
           ../src/pnx/core/pnx_anim.c \
           ../src/pnx/app/pnx_app.c \
           ../src/pnx/assets/pnx_assets.c ../src/pnx/assets/pnx_lzss.c \
           ../src/pnx/gfx/pnx_gfx.c ../src/pnx/gfx/pnx_tilemap.c \
           ../src/pnx/gfx/pnx_sprite.c ../src/pnx/gfx/pnx_text.c \
           ../src/pnx/gfx/pnx_nineslice.c ../src/pnx/gfx/pnx_layer.c \
           ../src/pnx/gfx/pnx_hud.c ../src/pnx/gfx/pnx_hud_vars.c ../src/pnx/gfx/pnx_hud_window.c \
           ../src/pnx/audio/pnx_audio.c ../src/pnx/audio/pnx_music.c \
           ../src/pnx/audio/pnx_synth.c \
           ../src/pnx/input/pnx_input.c \
           ../src/pnx/save/pnx_save.c \
           ../src/pnx/physics/pnx_physics.c \
           ../src/pnx/platform/pnx_platform_host.c
OUT      = build/test_core

# LZSS-specific behaviour: sprite/atlas compression under PNX_COMPRESS_LZSS.
LZSS_SRC = test_main_lzss.c test_sprite_compress.c test_atlas_compress.c \
           ../src/pnx/core/pnx_arena.c ../src/pnx/core/pnx_fmt.c ../src/pnx/core/pnx_diag.c \
           ../src/pnx/assets/pnx_assets.c ../src/pnx/assets/pnx_lzss.c \
           ../src/pnx/gfx/pnx_gfx.c ../src/pnx/gfx/pnx_sprite.c \
           ../src/pnx/platform/pnx_platform_host.c
LZSS_OUT = build/test_lzss

# Bitplane-specific behaviour: the codec itself, the two decoded-unit LRU caches, and
# sprite/atlas loading under PNX_COMPRESS_BITPLANE. pnx_lzss.c is linked in for exactly
# one thing -- test_bitplane_compress.c's own relative decode-cost comparison against
# LZSS on equivalent bytes -- not because a bitplane build ever needs it at runtime.
BITPLANE_SRC = test_main_bitplane.c test_bitplane_compress.c test_tile_cache.c test_bitplane_atlas.c \
               test_bitplane_sprite.c test_sprite_cache.c \
               ../src/pnx/core/pnx_arena.c ../src/pnx/core/pnx_fmt.c ../src/pnx/core/pnx_diag.c \
               ../src/pnx/assets/pnx_assets.c ../src/pnx/assets/pnx_bitplane.c \
               ../src/pnx/assets/pnx_lzss.c \
               ../src/pnx/assets/pnx_tile_cache.c ../src/pnx/assets/pnx_sprite_cache.c \
               ../src/pnx/gfx/pnx_gfx.c ../src/pnx/gfx/pnx_sprite.c \
               ../src/pnx/platform/pnx_platform_host.c
BITPLANE_OUT = build/test_bitplane

# A fourth binary, deliberately separate from $(OUT) rather than a mode of it: it needs
# -DPNX_HOST_BW=1, which the main binary must NOT carry, since the point of the main one
# is that ordinary colour behaviour stays the default. Only pnx_assets.c is linked in --
# this checks the loader's format auto-detection against real pipeline output, not the
# whole framework -- so it does not need every other module's .c file. Built
# PNX_COMPRESS_NONE: examples/overworld's assets.toml sets `compress = "none"` (the same
# build test_gfx.c/test_assets.c/$(OUT) need), and this loads overworld's real pipeline
# output, so the mode here has to match what built it. The depth-mismatch refusal this
# test exists to prove (a BW-compiled reader's own 2bpp size formula never coincidentally
# matches a colour blob's real byte count) does not depend on which compression mode is
# in play -- it is a resident-size arithmetic check, unaffected by LZSS/bitplane.
BW_SRC   = test_pack2bit_bw.c \
           ../src/pnx/core/pnx_arena.c ../src/pnx/core/pnx_fmt.c ../src/pnx/core/pnx_diag.c \
           ../src/pnx/assets/pnx_assets.c \
           ../src/pnx/platform/pnx_platform_host.c
BW_OUT   = build/test_pack2bit_bw

# A fifth binary: PNX_COMPRESS_BITPLANE x PNX_DISPLAY_BW together, the one combination
# none of the other four binaries above cover (BITPLANE_OUT is colour-only, BW_OUT is
# PNX_COMPRESS_NONE) -- exercises bp_pack/pnx_bitplane_decode's PNX_DISPLAY_BW branch
# (pnx_bitplane.c) and the pipeline's bitplane_encode_bw/compress_sprite_pixels_bitplane_bw
# (tools/pnx_assets.py) against a real 2bpp bitplane-compressed atlas.
BWBP_SRC = test_main_bitplane_bw.c test_bitplane_bw.c \
           ../src/pnx/core/pnx_arena.c ../src/pnx/core/pnx_fmt.c ../src/pnx/core/pnx_diag.c \
           ../src/pnx/assets/pnx_assets.c ../src/pnx/assets/pnx_bitplane.c \
           ../src/pnx/assets/pnx_tile_cache.c ../src/pnx/assets/pnx_sprite_cache.c \
           ../src/pnx/platform/pnx_platform_host.c
BWBP_OUT = build/test_bitplane_bw

# A sixth binary, square (PNX_HOST_WIDTH == PNX_HOST_HEIGHT) so pnx_target_row's round
# mask applies at all -- see PNX_DISPLAY_WIDTH/HEIGHT's own guard in test_round.c. gabbro
# and chalk are the two round platforms; both are colour, so this links the same modules
# test_pack2bit_bw.c does not need to, but not audio/save/app, which have nothing to do
# with round clipping. Built PNX_COMPRESS_NONE: it touches no compressed content at all.
ROUND_SRC = test_round.c \
            ../src/pnx/core/pnx_arena.c ../src/pnx/core/pnx_fmt.c ../src/pnx/core/pnx_diag.c \
            ../src/pnx/assets/pnx_assets.c ../src/pnx/gfx/pnx_gfx.c \
            ../src/pnx/platform/pnx_platform_host.c
ROUND_OUT = build/test_round

.PHONY: all test test-c test-lzss test-bitplane test-assets test-bw test-bitplane-bw test-round clean
all: test

# Every header under src/, not a hand-listed subset. The list used to name only core and
# platform, so editing pnx_assets.h left a stale binary that segfaulted on blobs the new
# reader had already been taught to parse -- a failure that looks like a code bug and is not.
#
# The example's GENERATED header counts too, and for the same reason: test_assets.c takes
# its asset ids from it rather than restating them, so adding an atlas to the example's
# manifest renumbers every id under a binary that still holds the old ones. That presents
# as twelve scene failures with nothing wrong in any source file.
HDRS = $(shell find ../src -name '*.h') \
       ../examples/overworld/src/c/assets_gen.h \
       ../examples/worldtiles/src/c/assets_gen.h \
       fixtures/lzss/gen.h \
       fixtures/lzss_pixels/gen.h \
       fixtures/bitplane/bpeg_fixtures.h \
       fixtures/bitplane_atlas/gen.h \
       fixtures/bitplane_sprite/gen.h

$(OUT): $(SRC) $(HDRS)
	@mkdir -p build
	$(CC) $(CFLAGS) -DPNX_COMPRESS_MODE=PNX_COMPRESS_NONE -DPNX_USE_PHYSICS=1 -DPNX_USE_MAP_COMPRESS=1 \
		$(SRC) -o $(OUT)

$(LZSS_OUT): $(LZSS_SRC) $(HDRS)
	@mkdir -p build
	$(CC) $(CFLAGS) -DPNX_COMPRESS_MODE=PNX_COMPRESS_LZSS $(LZSS_SRC) -o $(LZSS_OUT)

$(BITPLANE_OUT): $(BITPLANE_SRC) $(HDRS)
	@mkdir -p build
	$(CC) $(CFLAGS) -DPNX_COMPRESS_MODE=PNX_COMPRESS_BITPLANE -DPNX_USE_MAP_COMPRESS=1 \
		$(BITPLANE_SRC) -o $(BITPLANE_OUT)

$(BW_OUT): $(BW_SRC) $(HDRS)
	@mkdir -p build
	$(CC) $(CFLAGS) -DPNX_HOST_BW=1 -DPNX_COMPRESS_MODE=PNX_COMPRESS_NONE $(BW_SRC) -o $(BW_OUT)

$(BWBP_OUT): $(BWBP_SRC) $(HDRS)
	@mkdir -p build
	$(CC) $(CFLAGS) -DPNX_HOST_BW=1 -DPNX_COMPRESS_MODE=PNX_COMPRESS_BITPLANE \
		$(BWBP_SRC) -o $(BWBP_OUT)

$(ROUND_OUT): $(ROUND_SRC) $(HDRS)
	@mkdir -p build
	$(CC) $(CFLAGS) -DPNX_COMPRESS_MODE=PNX_COMPRESS_NONE -DPNX_HOST_WIDTH=200 -DPNX_HOST_HEIGHT=200 $(ROUND_SRC) -o $(ROUND_OUT)

test: test-c test-lzss test-bitplane test-assets test-bw test-bitplane-bw test-round

test-c: $(OUT)
	@./$(OUT)

test-lzss: $(LZSS_OUT)
	@./$(LZSS_OUT)

test-bitplane: $(BITPLANE_OUT)
	@./$(BITPLANE_OUT)

# The asset pipeline's validation is Python, and its value is that it FAILS builds --
# a claim that needs testing as much as any C function does.
test-assets:
	@python3 test_assets.py

# Proves the fix for a real bug: a build shipped 2bpp ~bw resources while compiled to
# expect 4bpp ones, and `pebble build` never caught it because it only proves the C
# compiles. This loads the pipeline's actual overworld output -- both the ~bw file and
# the untagged one -- through a real PNX_HOST_BW=1 build and checks the loader tells
# them apart correctly. See test_pack2bit_bw.c.
test-bw: $(BW_OUT)
	@./$(BW_OUT)

# The BITPLANE x PNX_DISPLAY_BW combination -- see BWBP_SRC's own comment. See
# test_bitplane_bw.c.
test-bitplane-bw: $(BWBP_OUT)
	@./$(BWBP_OUT)

# Proves the blitter honours a round display's per-row bounds (docs/ROADMAP.md's M9 round-
# display item) rather than merely being designed to: poisons the whole framebuffer, draws
# through pnx_host_set_round(true), and checks every pixel against the SAME per-row
# [min_x, max_x] the blitter itself was given. See test_round.c.
test-round: $(ROUND_OUT)
	@./$(ROUND_OUT)

clean:
	rm -rf build
