# Host test build. Deliberately plain: the point is that `make` in this directory
# compiles and runs the platform-independent parts of the framework with a normal
# compiler, so a logic bug is caught in a second instead of over Bluetooth.
#
# Only core and the host platform build here. Anything that includes <pebble.h> cannot,
# and that is the invariant being enforced -- if a module starts failing to compile
# under this Makefile, something above the platform layer reached for the SDK.

CC      ?= cc
CFLAGS  ?= -std=c11 -Wall -Wextra -Werror -Wno-unused-parameter -DPNX_PLATFORM_HOST -g -O1
SRC      = test_core.c test_assets.c test_gfx.c test_audio.c \
           ../src/pnx/core/pnx_arena.c ../src/pnx/core/pnx_fmt.c ../src/pnx/core/pnx_diag.c \
           ../src/pnx/assets/pnx_assets.c \
           ../src/pnx/gfx/pnx_gfx.c ../src/pnx/gfx/pnx_tilemap.c \
           ../src/pnx/gfx/pnx_sprite.c ../src/pnx/audio/pnx_audio.c \
           ../src/pnx/platform/pnx_platform_host.c
OUT      = build/test_core

.PHONY: all test test-c test-assets clean
all: test

$(OUT): $(SRC) $(wildcard ../src/pnx/core/*.h) $(wildcard ../src/pnx/platform/*.h)
	@mkdir -p build
	$(CC) $(CFLAGS) $(SRC) -o $(OUT)

test: test-c test-assets

test-c: $(OUT)
	@./$(OUT)

# The asset pipeline's validation is Python, and its value is that it FAILS builds --
# a claim that needs testing as much as any C function does.
test-assets:
	@python3 test_assets.py

clean:
	rm -rf build
