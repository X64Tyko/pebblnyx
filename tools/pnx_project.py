#!/usr/bin/env python3
"""Projects: what one is, how to find it, how to make a new one.

Until now the editor was pointed at an `assets.toml` and everything else was implied by
where that file sat in *this* repository. A distributed editor cannot assume that. It is
handed an arbitrary folder and has to answer: is this a pebblnyx project, which framework
was it built against, and where is everything?

That is what `.pknproj` is for. It sits at the project root, it is small, and it is JSON
rather than TOML precisely because **nobody should hand-edit it** -- it records decisions
the editor made, not decisions the author made. Authored content lives in `assets.toml`,
which stays TOML and stays commented.

**The engine lives in the editor, and a project anywhere builds against it.** A project on
disk contains content and game code -- not a copy of the framework. That is the same
bargain Unity and Godot make, and it means updating the editor updates the engine
everywhere rather than leaving N stale copies scattered across a drive.

The wrinkle is waf: `pebble build` globs `src/c/**/*.c` relative to the project, and a
glob pointing outside `top` does not work. In this repo the examples get around it with a
symlink at `src/c/pnx`, which needs privileges on Windows and breaks the moment a project
is zipped and sent to someone.

So the engine is **synced into `src/c/pnx` immediately before each build** -- from the
editor's own copy, every time. It is a build artefact, not project source: the scaffold
gitignores it, and it is refreshed rather than trusted, so it cannot drift from the editor
that produced it. The project folder stays small and portable; the engine is always the
one in the editor doing the building.

`.pknproj` records the version last built against, so the editor can say "this was
authored against 0.1.0, you are now on 0.2.0" rather than changing behaviour silently.
"""

import json
import os
import shutil
import sys

# Bumped when the framework's source changes in a way a project should be told about.
# Separate from PNX_BLOB_VERSION, which is about the asset format rather than the code.
FRAMEWORK_VERSION = "0.1.0"

# What the SHIPPED EDITOR calls itself, and the thing the updater compares against a
# release tag. Separate from FRAMEWORK_VERSION because they answer different questions:
# the framework version tells a project which engine it was authored against, this one
# tells a running binary whether a newer binary exists.
#
# It must equal the git tag, minus the `v`. An updater that compares against a number
# nobody remembered to bump is worse than no updater -- it reports "up to date" forever --
# so the release workflow fails the build when a tag and this string disagree, rather
# than trusting the release checklist.
EDITOR_VERSION = "0.1.0-beta.8"

PROJECT_FILE = ".pknproj"
PROJECT_FORMAT = 1


def framework_dir():
    """The `pnx` C sources to copy into a new project.

    Frozen builds carry the framework as bundled data; a source checkout uses the tree it
    lives in. PyInstaller unpacks --add-data under sys._MEIPASS.
    """
    if getattr(sys, "frozen", False):
        return os.path.join(getattr(sys, "_MEIPASS", ""), "pnx")
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "src", "pnx")


def framework_available():
    return os.path.isdir(framework_dir()) and os.path.exists(
        os.path.join(framework_dir(), "pnx.h"))


# --------------------------------------------------------------------- the file

def project_path(folder):
    return os.path.join(folder, PROJECT_FILE)


def is_project(folder):
    return os.path.exists(project_path(folder))


def looks_like_project(folder):
    """True for a folder the editor can open, including ones predating `.pknproj`.

    An existing project is a folder with an `assets.toml` in it. Refusing those would
    mean the file format change orphaned every project made before it.
    """
    return is_project(folder) or os.path.exists(os.path.join(folder, "assets.toml"))


def load(folder):
    """Read `.pknproj`, or synthesise one for a folder that predates it."""
    path = project_path(folder)
    if os.path.exists(path):
        with open(path) as f:
            data = json.load(f)
        data.setdefault("manifest", "assets.toml")
        return data

    if not os.path.exists(os.path.join(folder, "assets.toml")):
        raise ValueError(f"{folder} is not a pebblnyx project "
                         f"(no {PROJECT_FILE} and no assets.toml)")

    # Synthesised, not written: opening a folder should not modify it. `save` runs when
    # the author does something that warrants it.
    return {"format": PROJECT_FORMAT, "name": os.path.basename(os.path.abspath(folder)),
            "manifest": "assets.toml", "framework": None, "adopted": False}


def save(folder, data):
    data = dict(data)
    data["format"] = PROJECT_FORMAT
    with open(project_path(folder), "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    return data


def framework_state(folder, data=None):
    """What engine version this project last built against, and what the editor has."""
    data = data or load(folder)
    staged = os.path.join(folder, "src", "c", "pnx")
    have = data.get("framework")
    owned = bool(data.get("engine_owned"))
    return {
        "staged": os.path.exists(os.path.join(staged, "pnx.h")),
        # A symlink is how the in-repo examples reach the live tree. Legitimate there,
        # so it is reported rather than treated as damage -- and never overwritten.
        "linked": os.path.islink(staged),
        "built_against": have,
        "editor": FRAMEWORK_VERSION,
        "available": framework_available(),
        "changed": bool(have and have != FRAMEWORK_VERSION and not owned),
        # The project has taken ownership of its engine copy: it is no longer restaged,
        # and it no longer tracks the editor.
        "owned": owned,
        "owned_at": data.get("engine_owned_at"),
        "owned_from": data.get("engine_owned_from"),
    }


def take_engine_ownership(folder, on=True):
    """Detach this project's engine from the editor's, or reattach it.

    Editing the staged engine is meaningless while it is restaged before every build --
    the edit would vanish at the next build, which is the worst way to lose work. So
    unlocking is not merely permission to write: it stops the sync, and the project owns
    that copy from then on. Recorded in `.pknproj` with the version it forked from, so
    "why does this project not get engine fixes" has an answer sitting next to it.
    """
    data = load(folder)
    if on:
        import datetime
        data["engine_owned"] = True
        data["engine_owned_from"] = FRAMEWORK_VERSION
        data["engine_owned_at"] = datetime.datetime.now().isoformat(timespec="seconds")
    else:
        for k in ("engine_owned", "engine_owned_from", "engine_owned_at"):
            data.pop(k, None)
    return save(folder, data)


# ------------------------------------------------------------------ the sync

_SKIP_DIRS = {"__pycache__", ".git"}


def sync_framework(folder):
    """Stage the editor's engine into <project>/src/c/pnx for a build.

    Called before every build, not once at creation. The engine a project compiles
    against is therefore always the one inside the editor doing the compiling -- which is
    the whole point, and it also means a project folder copied between machines picks up
    whatever engine is there rather than carrying a stale one.

    Left alone if it is a symlink: this repository's examples deliberately point at the
    live tree so an engine edit is picked up by the next build, and replacing that with a
    frozen snapshot mid-development would be a nasty surprise.
    """
    dest = os.path.join(folder, "src", "c", "pnx")
    if os.path.islink(dest):
        return dest                      # a live tree; the author meant it

    # The project owns its engine copy. Restaging would silently destroy deliberate
    # modifications, which is precisely what taking ownership was meant to prevent.
    if is_project(folder) and load(folder).get("engine_owned"):
        return dest

    if not framework_available():
        raise ValueError("this build of the editor has no copy of the pnx engine")

    os.makedirs(os.path.dirname(dest), exist_ok=True)
    if os.path.exists(dest):
        shutil.rmtree(dest)
    shutil.copytree(framework_dir(), dest,
                    ignore=shutil.ignore_patterns(*_SKIP_DIRS, "*.pyc"))

    # Record what was actually staged, so the project can report which engine produced
    # its last build rather than guessing.
    if is_project(folder):
        data = load(folder)
        if data.get("framework") != FRAMEWORK_VERSION:
            data["framework"] = FRAMEWORK_VERSION
            save(folder, data)
    return dest


# -------------------------------------------------------------------- scaffold

GITIGNORE = '''# Build output
build/
*.pbw
.lock-waf_*

# The pebblnyx engine, staged here from the editor before each build. A build artefact,
# not project source -- committing it would pin a copy that the editor then silently
# disagrees with.
src/c/pnx/

# Generated from assets.toml by the pipeline. Derived, so not committed: a stale blob
# outliving the manifest that describes it is exactly what the blob version field exists
# to catch.
resources/*.bin
src/c/assets_gen.h
'''

WSCRIPT = '''# Build rules for a pebblnyx game. Generated by the editor; safe to edit.
#
# The source glob reaches into src/c/pnx, where the editor stages its copy of the engine
# immediately before each build. That directory is a build artefact rather than project
# source -- the engine lives in the editor, so a project built on another machine uses
# the engine there rather than carrying its own.

import os

top = '.'
out = 'build'


def options(ctx):
    ctx.load('pebble_sdk')


def configure(ctx):
    ctx.load('pebble_sdk')


def build(ctx):
    ctx.load('pebble_sdk')

    # Lets a build override pnx_config.h without editing it:
    #     PNX_DEFINES=PNX_USE_DIAGNOSTICS=0 pebble build
    extra = [d for d in os.environ.get('PNX_DEFINES', '').split() if d]

    binaries = []
    cached_env = ctx.env
    for platform in ctx.env.TARGET_PLATFORMS:
        ctx.env = ctx.all_envs[platform]
        ctx.set_group(ctx.env.PLATFORM_NAME)
        if extra:
            ctx.env.append_value('DEFINES', extra)
        app_elf = '{}/pebble-app.elf'.format(ctx.env.BUILD_DIR)

        # The host platform is excluded here as well as being #ifdef'd out, so a device
        # build never compiles it at all.
        sources = ctx.path.ant_glob('src/c/**/*.c',
                                    excl=['**/pnx_platform_host.c'],
                                    follow_symlinks=True)
        ctx.pbl_build(source=sources, target=app_elf, bin_type='app')
        binaries.append({'platform': platform, 'app_elf': app_elf})
    ctx.env = cached_env

    ctx.set_group('bundle')
    ctx.pbl_bundle(binaries=binaries, js=[])
'''

MAIN_C = '''// {name}
//
// The fixed-timestep loop below is the shape every pebblnyx game takes. Frames arrive at
// ~37.33ms at best and carry seconds while the app is covered, so real time is
// accumulated and consumed in fixed ticks rather than trusted directly.

#include "pnx/pnx.h"
#include "assets_gen.h"

#include <string.h>

#define ARENA_BYTES (24 * 1024)

typedef struct {{
  PnxArena arena;
  uint32_t accumulator_ms;
  uint32_t ticks;
}} Game;

static void frame(void *ctx, uint32_t elapsed_ms, PnxTarget *target) {{
  Game *g = (Game *)ctx;

  PnxEvent ev;
  while (pnx_platform_poll_event(&ev)) {{
    if (ev.type == PNX_EVENT_BUTTON_DOWN && ev.button == PNX_BUTTON_BACK) {{
      pnx_platform_quit();
    }}
  }}

  // Clamped: a frame arriving after a notification can carry several seconds, and
  // without this the sim fast-forwards through all of it.
  g->accumulator_ms += elapsed_ms;
  const uint32_t max_ms = PNX_TICK_MS * PNX_MAX_CATCHUP_TICKS;
  if (g->accumulator_ms > max_ms) g->accumulator_ms = max_ms;

  while (g->accumulator_ms >= PNX_TICK_MS) {{
    g->accumulator_ms -= PNX_TICK_MS;
    g->ticks++;
  }}

  pnx_gfx_clear(target, 0xC0);
}}

int main(void) {{
  static Game g;
  memset(&g, 0, sizeof(g));

  if (!pnx_arena_init(&g.arena, "scene", ARENA_BYTES, 4)) {{
    pnx_platform_log("arena init failed");
    return 1;
  }}

  pnx_platform_run(frame, &g);
  pnx_arena_destroy(&g.arena);
  return 0;
}}
'''

ASSETS_TOML = '''# Asset manifest for {name}.
#
# This is the single source of truth for content. No C file should name a PNG, a pixel
# offset, a sheet layout or a magic tile id -- everything reaches game code through the
# header the pipeline generates from this file.
#
# TOML rather than JSON so the reasoning behind the content can live beside it.

[project]
name = "{name}"
resources = "resources"
header = "src/c/assets_gen.h"
# The appstore rejects bundles over 256KB of resources. The device's hard cap is 1MB, but
# shipping is the constraint that actually binds.
budget_bytes = 262144

# Add content with the editor's Import and Fonts tabs, or by hand:
#
#   [[atlas]]   a tileset carved from a sheet
#   [[sprite]]  animation frames
#   [[font]]    a rasterised typeface  (a licence is required)
#   [[map]]     a grid of legend characters
#   [scene.*]   what loads together -- the only load point the framework has
'''

PACKAGE_JSON = {
    "name": None,
    "author": "",
    "version": "1.0.0",
    "keywords": ["pebble-app"],
    "private": True,
    "dependencies": {},
    "pebble": {
        "displayName": None,
        "uuid": None,
        "sdkVersion": "3",
        "enableMultiJS": False,
        "targetPlatforms": ["emery"],
        "watchapp": {"watchface": False},
        "messageKeys": [],
        "resources": {"media": []},
    },
}


def create(folder, name, author=""):
    """Scaffold a complete, buildable project in `folder`.

    Complete meaning it compiles as-is: the framework is vendored, the manifest is valid,
    and `pebble build` produces a .pbw. A scaffold that needs three more manual steps
    before it builds is a scaffold that teaches the author those steps are normal.
    """
    import re
    import uuid as _uuid

    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 _-]*", name or ""):
        raise ValueError("a project name needs letters, digits, spaces, - or _")

    slug = re.sub(r"[^a-z0-9_]+", "_", name.lower()).strip("_") or "game"
    os.makedirs(folder, exist_ok=True)

    if os.listdir(folder) and looks_like_project(folder):
        raise ValueError(f"{folder} already contains a project")

    os.makedirs(os.path.join(folder, "src", "c"), exist_ok=True)
    os.makedirs(os.path.join(folder, "resources"), exist_ok=True)
    os.makedirs(os.path.join(folder, "art"), exist_ok=True)

    with open(os.path.join(folder, "assets.toml"), "w") as f:
        f.write(ASSETS_TOML.format(name=slug))
    with open(os.path.join(folder, "wscript"), "w") as f:
        f.write(WSCRIPT)
    with open(os.path.join(folder, ".gitignore"), "w") as f:
        f.write(GITIGNORE)
    with open(os.path.join(folder, "src", "c", "main.c"), "w") as f:
        f.write(MAIN_C.format(name=name))

    pkg = json.loads(json.dumps(PACKAGE_JSON))
    pkg["name"] = slug
    pkg["author"] = author
    pkg["pebble"]["displayName"] = name
    pkg["pebble"]["uuid"] = str(_uuid.uuid4())
    with open(os.path.join(folder, "package.json"), "w") as f:
        json.dump(pkg, f, indent=2)
        f.write("\n")

    # An empty generated header, so the project compiles before the first build of the
    # assets rather than failing on a missing include.
    gen = os.path.join(folder, "src", "c", "assets_gen.h")
    if not os.path.exists(gen):
        with open(gen, "w") as f:
            f.write("// GENERATED by the asset pipeline -- do not edit.\n"
                    "// Empty until the first build.\n#pragma once\n")

    data = save(folder, {"name": name, "manifest": "assets.toml",
                         "framework": None, "author": author})
    # Staged now as well as before each build, so a freshly created project compiles
    # from a terminal without the editor having to run first.
    sync_framework(folder)
    return load(folder) if is_project(folder) else data
