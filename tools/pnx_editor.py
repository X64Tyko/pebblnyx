#!/usr/bin/env python3
"""pebblnyx editor: a local app for building maps, managing assets and packaging.

Runs a small server on localhost and opens a browser. Nothing leaves the machine and
there is nothing to install beyond what the pipeline already needs.

The design rule throughout: **the manifest is the source of truth, and the editor is
just a hand on it.** Saving performs a surgical edit of `assets.toml` -- the block being
changed and nothing else -- so comments, ordering and hand-written sections survive.
An editor that rewrites the whole file would quietly delete the reasoning people leave
in their manifests, which is most of what makes them readable.

Maps are painted in **legend characters**, not tile indices, because that is what a map
actually stores. Painting indices would let you place a tile the legend cannot express,
and the file could then no longer round-trip.

This file is a thin entry point. The editor itself lives in tools/editor/ -- a Python
backend package (tools/editor/{updater,toolchain,emulator,server,project/,routes/}.py)
plus its frontend as real HTML/CSS/JS files (tools/editor/static/), not the single
12.8k-line file with everything embedded in one `PAGE` string this used to be. Kept as a
module in its own right, rather than folded away, because tests/test_assets.py imports
it directly (`import pnx_editor`) and tools/build_editor.py launches it as a script --
both by path, so the names below have to keep resolving from here.

Usage:
    tools/pnx_editor.py <manifest.toml> [--port 8765] [--no-browser]
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from editor.emulator import Emulator, EMULATOR                        # noqa: E402,F401
from editor.liveness import Liveness, LIVE                            # noqa: E402,F401
from editor.project import Project                                    # noqa: E402,F401
from editor.routes.project import INDEX_HTML as PAGE                  # noqa: E402,F401
from editor.server import (                                           # noqa: E402,F401
    Session, EditorServer, make_handler, main,
    REQUEST_LOCK, LOCK_FREE_PATHS,
    find_manifest, claim_port, already_ours, open_app_window, open_window,
    selftest, TOOLS,
)
from editor.toolchain import Toolchain, TOOLCHAIN                     # noqa: E402,F401
from editor.updater import (                                          # noqa: E402,F401
    Updater, UPDATER, parse_version, newer, https_context, restart_later,
)

if __name__ == "__main__":
    sys.exit(main())
