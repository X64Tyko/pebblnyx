#!/usr/bin/env python3
"""Generates tests/compile_commands.json for clang-tidy (and any clangd-based editor)
from tests/Makefile's own CFLAGS and SRC -- not a hand-maintained duplicate of them.

Why derive rather than restate: tests/Makefile already IS the definition of "the
portable, host-buildable slice of pebblnyx" (see its own header comment -- anything that
includes <pebble.h> cannot build there, and that is the invariant it enforces). A second,
hand-written list of the same files with the same flags would drift the first time
someone edited one and not the other, and drift here means clang-tidy silently checking
the wrong flags or missing a new source file -- a quiet failure, not a loud one.

Only CC-invoked, single-TU-per-entry semantics are needed for clang-tidy (it runs one
file at a time regardless of how the real build batches them), so each SRC file gets its
own compile_commands.json entry reusing the Makefile's one shared CFLAGS line -- which
is accurate, since the Makefile compiles every file in SRC with that same one line.

pnx_platform_pebble.c and example/game code are deliberately NOT included -- they need
<pebble.h> and the ARM SDK's semantic environment (the exact cross flags a real `pebble
build -v` uses), which is a different, heavier thing to model correctly and is out of
scope here. See tools/lint.sh's header comment for why that boundary is where it is.

    python3 tools/gen_compile_commands.py

Regenerate after editing tests/Makefile's CFLAGS or SRC; not run automatically, the same
as the asset pipeline -- see assets.toml's own footgun comments for the pattern.
"""

import json
import os
import re
import shlex
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TESTS_DIR = os.path.join(ROOT, "tests")
MAKEFILE = os.path.join(TESTS_DIR, "Makefile")
OUT = os.path.join(TESTS_DIR, "compile_commands.json")


def _extract_make_var(text, name):
    # `NAME ?= ...` or `NAME = ...`, followed by any number of `\`-continued lines --
    # exactly the two forms tests/Makefile uses for CFLAGS and SRC. Walked line by line
    # rather than as one regex: a single `.*(?:\\\n.*)*$` looked right but a greedy `.*`
    # swallows the trailing backslash on the first line, so `$` (MULTILINE) is satisfied
    # right there and the continuation is never reached -- silently returning just the
    # first line. Caught by SRC coming back with 7 files instead of 19.
    lines = text.splitlines()
    pattern = re.compile(r"^" + re.escape(name) + r"\s*\??=\s*(.*)$")
    for i, line in enumerate(lines):
        m = pattern.match(line)
        if not m:
            continue
        parts = [m.group(1)]
        while parts[-1].endswith("\\"):
            parts[-1] = parts[-1][:-1]
            i += 1
            parts.append(lines[i])
        return " ".join(parts)
    raise SystemExit(f"gen_compile_commands: could not find {name} in {MAKEFILE}")


def _extract_recipe_extra_flags(text):
    """Flags the $(OUT) recipe adds between $(CFLAGS) and $(SRC) -- e.g.
    -DPNX_USE_PHYSICS=1, turned on for the main test binary only (pnx_physics.c is in
    SRC, but PNX_USE_PHYSICS defaults off, so without this clang-tidy would see an empty
    pnx_physics.h and every symbol from it as undeclared).

    Parsed from the recipe line itself rather than hardcoded, for the same reason
    CFLAGS/SRC are: a flag added to the recipe and not here would otherwise drift
    silently the way this one briefly did.

    Backslash-continued first: the recipe wrapped onto a second physical line (to keep
    the flag list readable) after this regex was written, which `.` -- no re.DOTALL --
    cannot see across. That silently dropped back to `extra = []` with no error, the
    exact silent-drift failure this function's own docstring above warns about; caught
    when clang-tidy flagged pnx_physics.h's own symbols as undeclared despite this
    function existing to prevent exactly that.
    """
    joined = re.sub(r"\\\n[ \t]*", " ", text)
    m = re.search(r"^\$\(OUT\):.*\n(?:.*\n)*?\t\$\(CC\)\s+\$\(CFLAGS\)\s*(.*?)\s*\$\(SRC\)",
                 joined, re.M)
    return shlex.split(m.group(1)) if m and m.group(1).strip() else []


def main():
    with open(MAKEFILE) as f:
        text = f.read()

    cflags = shlex.split(_extract_make_var(text, "CFLAGS"))
    extra = _extract_recipe_extra_flags(text)
    src = shlex.split(_extract_make_var(text, "SRC"))
    if not src:
        raise SystemExit("gen_compile_commands: SRC parsed empty -- Makefile format changed?")

    cc = os.environ.get("CC", "cc")
    entries = []
    for rel in src:
        abs_path = os.path.normpath(os.path.join(TESTS_DIR, rel))
        entries.append(
            {
                "directory": TESTS_DIR,
                "file": abs_path,
                "arguments": [cc, *cflags, *extra, "-c", rel],
            }
        )

    with open(OUT, "w") as f:
        json.dump(entries, f, indent=2)
        f.write("\n")

    print(f"wrote {len(entries)} entries to {OUT}")


if __name__ == "__main__":
    main()
