"""The pebblnyx editor, as a package.

Split out of the original single-file tools/pnx_editor.py (12.8k lines: Python backend
plus the entire frontend embedded as one big HTML/CSS/JS string). tools/pnx_editor.py is
now a thin compatibility shim re-exporting the names below, kept because
tests/test_assets.py imports it directly and tools/build_editor.py launches it as a
script.

Layout:
    updater.py     -- Updater, UPDATER, restart_later, version-compare helpers
    liveness.py     -- Liveness, LIVE
    toolchain.py    -- Toolchain, TOOLCHAIN (finds/installs the Pebble SDK)
    emulator.py      -- Emulator, EMULATOR (QEMU via `pebble`)
    project/         -- Project, assembled from mixins by domain (atlas, maps, ...)
    routes/          -- the HTTP route table, split by domain to match project/
    server.py        -- Session, EditorServer, make_handler, main(), and everything
                         else that used to sit after PAGE in the monolith
    static/          -- index.html, css/, js/ -- what used to be the PAGE r-string

This file's only job is to make the sibling flat `pnx_*` modules (pnx_assets,
pnx_project, pnx_mapfile, pnx_preview, size_report, pnx_emu_bridge) resolve the same way
from inside this package as they always did from tools/pnx_editor.py itself: those
modules aren't part of this package and aren't installed anywhere, they're just other
files in tools/, found by putting tools/ on sys.path. Every submodule here does
`import pnx_assets as pa` etc. as a flat import, relying on this having already run --
so it runs at package-import time, before any submodule is imported, regardless of
whether the entry point is tools/pnx_editor.py or something inside this package.
"""

import os
import sys

TOOLS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if TOOLS not in sys.path:
    sys.path.insert(0, TOOLS)
