#!/usr/bin/env python3
"""Generates the raw resources examples/flashbench reads at controlled offsets, at
several different total sizes, to separate two competing explanations for a surprising
result: does read cost scale with OFFSET within a resource (the original WorldTile
streaming finding), with the resource's own TOTAL SIZE (flashbench v1's flat ~40ms
regardless of offset, in one 220KB resource), or both?

Not run through tools/pnx_assets.py -- the manifest has no generic raw-blob asset type,
and these files' whole point is to be read at arbitrary offsets via
pnx_platform_resource_read() directly, which the higher-level pnx_assets API does not
expose. See main.c's header comment.

Five sizes, chosen to bracket the two real data points this is trying to reconcile:
8KB matches a WorldTile bank (M4d's post-fix streaming unit), 75KB matches the
pre-banking map resource the original O(offset) finding was measured on, 220KB matches
flashbench v1's resource. 32KB and 150KB fill in the curve between them.

Content is a repeating byte-index pattern (byte i = i & 0xFF), not random or zeroed, so a
read can be sanity-checked against its expected byte -- catching a resource that built
stale or a read landing at the wrong offset, which reading actual content never would.

    python3 gen_flashdata.py

Deterministic and reproducible from nothing, the same as resonant's art/gen_art.py and
the framework's own tools/pnx_placeholder.py -- resources/ is gitignored like every other
pipeline output, so this has to be re-run after a fresh checkout, not committed.
"""

import os

SIZES_KB = [8, 32, 75, 150, 220]

OUT_DIR = os.path.join(os.path.dirname(__file__), "resources")


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    for kb in SIZES_KB:
        size = kb * 1024
        path = os.path.join(OUT_DIR, f"flashdata_{kb}k.bin")
        with open(path, "wb") as f:
            f.write(bytes(i & 0xFF for i in range(size)))
        print(f"wrote {size} bytes to {path}")


if __name__ == "__main__":
    main()
