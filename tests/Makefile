# Host test build. Deliberately plain: the point is that `make` in this directory
# compiles and runs the platform-independent parts of the framework with a normal
# compiler, so a logic bug is caught in a second instead of over Bluetooth.
#
# Only core and the host platform build here. Anything that includes <pebble.h> cannot,
# and that is the invariant being enforced -- if a module starts failing to compile
# under this Makefile, something above the platform layer reached for the SDK.

CC      ?= cc
CFLAGS  ?= -std=c11 -Wall -Wextra -Werror -Wno-unused-parameter -DPNX_PLATFORM_HOST -DPNX_USE_SYNTH=1 -g -O1
SRC      = test_core.c test_assets.c test_gfx.c test_text.c test_audio.c test_input.c test_stream.c \
           test_synth.c test_save.c test_app.c test_physics.c test_layer.c \
           ../src/pnx/core/pnx_arena.c ../src/pnx/core/pnx_fmt.c ../src/pnx/core/pnx_diag.c \
           ../src/pnx/app/pnx_app.c \
           ../src/pnx/assets/pnx_assets.c \
           ../src/pnx/gfx/pnx_gfx.c ../src/pnx/gfx/pnx_tilemap.c \
           ../src/pnx/gfx/pnx_sprite.c ../src/pnx/gfx/pnx_text.c \
           ../src/pnx/gfx/pnx_nineslice.c ../src/pnx/gfx/pnx_layer.c \
           ../src/pnx/audio/pnx_audio.c ../src/pnx/audio/pnx_music.c \
           ../src/pnx/audio/pnx_synth.c \
           ../src/pnx/input/pnx_input.c \
           ../src/pnx/save/pnx_save.c \
           ../src/pnx/physics/pnx_physics.c \
           ../src/pnx/platform/pnx_platform_host.c
OUT      = build/test_core

# A second binary, deliberately separate from $(OUT) rather than a mode of it: it needs
# -DPNX_HOST_BW=1, which the main binary must NOT carry, since the point of the main one
# is that ordinary colour behaviour stays the default. Only pnx_assets.c is linked in --
# this checks the loader's format auto-detection against real pipeline output, not the
# whole framework -- so it does not need every other module's .c file.
BW_SRC   = test_pack2bit_bw.c \
           ../src/pnx/core/pnx_arena.c ../src/pnx/core/pnx_fmt.c ../src/pnx/core/pnx_diag.c \
           ../src/pnx/assets/pnx_assets.c \
           ../src/pnx/platform/pnx_platform_host.c
BW_OUT   = build/test_pack2bit_bw

# A third binary, square (PNX_HOST_WIDTH == PNX_HOST_HEIGHT) so pnx_target_row's round
# mask applies at all -- see PNX_DISPLAY_WIDTH/HEIGHT's own guard in test_round.c. gabbro
# and chalk are the two round platforms; both are colour, so this links the same modules
# test_pack2bit_bw.c does not need to, but not audio/save/app, which have nothing to do
# with round clipping.
ROUND_SRC = test_round.c \
            ../src/pnx/core/pnx_arena.c ../src/pnx/core/pnx_fmt.c ../src/pnx/core/pnx_diag.c \
            ../src/pnx/assets/pnx_assets.c ../src/pnx/gfx/pnx_gfx.c \
            ../src/pnx/platform/pnx_platform_host.c
ROUND_OUT = build/test_round

.PHONY: all test test-c test-assets test-bw test-round clean
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
       ../examples/worldtiles/src/c/assets_gen.h

$(OUT): $(SRC) $(HDRS)
	@mkdir -p build
	$(CC) $(CFLAGS) -DPNX_USE_PHYSICS=1 $(SRC) -o $(OUT)

$(BW_OUT): $(BW_SRC) $(HDRS)
	@mkdir -p build
	$(CC) $(CFLAGS) -DPNX_HOST_BW=1 $(BW_SRC) -o $(BW_OUT)

$(ROUND_OUT): $(ROUND_SRC) $(HDRS)
	@mkdir -p build
	$(CC) $(CFLAGS) -DPNX_HOST_WIDTH=200 -DPNX_HOST_HEIGHT=200 $(ROUND_SRC) -o $(ROUND_OUT)

test: test-c test-assets test-bw test-round

test-c: $(OUT)
	@./$(OUT)

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

# Proves the blitter honours a round display's per-row bounds (docs/ROADMAP.md's M9 round-
# display item) rather than merely being designed to: poisons the whole framebuffer, draws
# through pnx_host_set_round(true), and checks every pixel against the SAME per-row
# [min_x, max_x] the blitter itself was given. See test_round.c.
test-round: $(ROUND_OUT)
	@./$(ROUND_OUT)

clean:
	rm -rf build
