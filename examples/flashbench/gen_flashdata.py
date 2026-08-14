#!/usr/bin/env python3
"""Generates the raw resources examples/flashbench reads, v3: does read cost scale with
a resource's OWN size, or with how much OTHER data precedes it in the .pbpack?

v2 (5 differently-sized resources, each read near its own start and two-thirds deep)
found offset-within-a-resource does not matter and cost scales with resource size --
but declared the five in ascending order, which `pbpack.py`'s own `finalize()` (offsets
assigned by walking `table_entries` in declaration order, each one placed right after the
last) means the biggest resource was ALSO always the one with the most other content
before it. Size and pack position were never actually separated, so v2 cannot tell "cost
tracks this resource's own size" apart from "cost tracks how much precedes it" -- they
were the same variable wearing two names.

This version fixes that: five identical 8KB PROBEs (the real WorldTile bank size),
interleaved with ~42KB PADDING resources so each probe sits behind a different amount of
other content -- 0, 50, 100, 150, 200 KB. Same size, five positions. If read cost still
grows across the five probes, it is position, not size, that was driving v2's result
(and v1's, and very possibly the original WorldTile finding, which read maps declared
after their atlases). If the five come back flat, size is confirmed and this is settled.

Every file's content must be BYTE-DISTINCT from every other, probes and padding alike --
first version of this script filled every padding file with the same 0xAA and every probe
with the same `i & 0xFF` pattern, and `pbpack.py`'s `add_resource` deduplicates identical
content (`self.contents.index(content)` reuses an existing entry rather than adding a
new one -- a real feature, see MEASUREMENTS.md's WorldTile section on `field`/`plain`
sharing banks). That collapsed all five probes onto one physical copy and all four
padding files onto another, verified after the fact by deserializing the built .pbpack
directly: every probe entry pointed at the same offset. Each file here is salted by its
own index so no two are ever identical. Probes keep a checkable pattern so a read can be
verified against its expected content, catching a stale build or a read landing at the
wrong offset.

    python3 gen_flashdata.py

Deterministic and reproducible from nothing, the same as resonant's art/gen_art.py and
the framework's own tools/pnx_placeholder.py -- resources/ is gitignored like every other
pipeline output, so this has to be re-run after a fresh checkout, not committed.
"""

import os

PROBE_KB = 8
PRECEDING_KB = [0, 50, 100, 150, 200]  # target bytes preceding each probe in the pack

OUT_DIR = os.path.join(os.path.dirname(__file__), "resources")


def write_probe(index, size):
    path = os.path.join(OUT_DIR, f"probe_{index}.bin")
    # Salted by `index` so probe 0's bytes never collide with probe 1's, etc. -- see the
    # module docstring for why identical content across files is a real bug here, not a
    # cosmetic one. main.c checks byte 0 against `index & 0xFF` to confirm it read the
    # probe it meant to.
    with open(path, "wb") as f:
        f.write(bytes((i + index) & 0xFF for i in range(size)))
    print(f"wrote {size} bytes to {path}  (probe {index})")


def write_padding(index, size):
    path = os.path.join(OUT_DIR, f"padding_{index}.bin")
    # Salted the same way as probes -- a padding file identical to another padding file
    # (or, worse, to a probe) would silently deduplicate and collapse the pack layout
    # this script exists to control.
    with open(path, "wb") as f:
        f.write(bytes((i * 7 + index * 31) & 0xFF for i in range(size)))
    print(f"wrote {size} bytes to {path}  (padding {index}, never read directly)")


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    probe_bytes = PROBE_KB * 1024
    cumulative = 0
    for i, target_kb in enumerate(PRECEDING_KB):
        target = target_kb * 1024
        if target > cumulative:
            write_padding(i - 1, target - cumulative)  # only reachable for i > 0
            cumulative = target
        elif i > 0:
            raise SystemExit(
                f"PRECEDING_KB must strictly increase by more than {PROBE_KB}KB per "
                f"step -- probe {i} wants {target_kb}KB preceding, already at "
                f"{cumulative // 1024}KB")
        write_probe(i, probe_bytes)
        cumulative += probe_bytes
    print(f"total pack content from this script: {cumulative} bytes")


if __name__ == "__main__":
    main()
