#!/usr/bin/env python3
"""Per-module size report against the 64KB app ceiling.

Why this exists: a Pebble app's `virtual_size` field is a **uint16**, so text + rodata
+ data + bss must stay under 65,535 bytes. Overflowing it does not produce a useful
error -- the SDK fails inside inject_metadata.py with a bare

    struct.error: 'H' format requires 0 <= number <= 65535

naming nothing. By the time a build fails that way, the question "which module grew?"
is unanswerable without this tool. So the framework reports the breakdown on every
build, and a module that is switched off in pnx_config.h has to show up as zero here or
the compile-time selection is not actually working.

Usage:
    tools/size_report.py <path-to-app.elf> [--budget BYTES] [--json] [--fail-over PCT]
"""

import argparse
import collections
import json
import os
import re
import shutil
import subprocess
import sys

# Everything in the app image is copied into RAM at launch, so rodata counts against the
# ceiling exactly like code does. This surprises people who expect flash-resident
# constants; on this platform there is no such thing for app data.
SECTION_KINDS = {
    "t": "text",   # code
    "r": "rodata", # constants -- in RAM here, not flash
    "d": "data",   # initialised, mutable
    "b": "bss",    # zeroed, mutable
}

VIRTUAL_SIZE_LIMIT = 65535


def find_tool(name):
    """Locate an arm-none-eabi binutil, preferring whatever is on PATH."""
    found = shutil.which(name)
    if found:
        return found

    for root in (
        os.path.expanduser("~/.local/share/pebble-sdk/SDKs"),
        os.path.expanduser("~/.pebble-sdk/SDKs"),
    ):
        if not os.path.isdir(root):
            continue
        for dirpath, _dirnames, filenames in os.walk(root):
            if name in filenames:
                return os.path.join(dirpath, name)

    return None


def allocated_size(readelf_path, elf_path):
    """Sum of every SHF_ALLOC section -- the number that actually has to fit.

    Summing symbols undercounts, because .header, the build-id note, .init/.fini and
    inter-section padding occupy space while belonging to no symbol. On the empty
    example that gap is 760 bytes, which is more than enough to turn a passing budget
    check into a build that fails at inject_metadata. This matches what the SDK's own
    "Total footprint in RAM" reports.
    """
    result = subprocess.run([readelf_path, "-S", "-W", elf_path],
                            capture_output=True, text=True, check=True)

    total = 0
    sections = {}
    for line in result.stdout.splitlines():
        # "  [ 3] .text  PROGBITS  00000168 000198 001134 00  AX  0   0  4"
        match = re.match(
            r"\s*\[\s*\d+\]\s+(\S+)\s+\S+\s+([0-9a-fA-F]+)\s+[0-9a-fA-F]+\s+"
            r"([0-9a-fA-F]+)\s+[0-9a-fA-F]+\s+([A-Zx]*)",
            line,
        )
        if not match:
            continue
        name, _addr, size_hex, flags = match.groups()
        if "A" not in flags:
            continue          # debug info and comments ship in the ELF but not the app
        size = int(size_hex, 16)
        sections[name] = size
        total += size

    return total, sections


def classify(symbol_type):
    """Map an nm type letter to a size bucket, or None if it does not occupy space."""
    lower = symbol_type.lower()
    # 'V'/'W' are weak symbols, which occupy space like their strong equivalents.
    if lower == "v":
        return "data"
    if lower == "w":
        return "text"
    return SECTION_KINDS.get(lower)


# Source file -> the pnx_config.h switch that compiles it in.
#
# The report used to group by DIRECTORY, on the stated premise that a directory was the
# granularity pnx_config.h switches at. That stopped being true: src/pnx/audio holds three
# independently switchable modules (PNX_USE_AUDIO, PNX_USE_SEQUENCER, PNX_USE_SYNTH) and
# src/pnx/gfx holds four. Grouping them together means the report cannot answer the one
# question it exists to answer -- "does turning this off reclaim the bytes?" -- because
# the answer is buried inside a line covering two other modules.
#
# So attribution follows the SWITCH, not the folder. Every row is now something a project
# can turn off, which is what makes the number actionable.
MODULE_OF_FILE = {
    "pnx_audio":    "pnx/audio",          # PNX_USE_AUDIO
    "pnx_music":    "pnx/sequencer",      # PNX_USE_SEQUENCER
    "pnx_synth":    "pnx/synth",          # PNX_USE_SYNTH
    "pnx_tilemap":  "pnx/tilemap",        # PNX_USE_TILEMAP
    "pnx_sprite":   "pnx/sprites",        # PNX_USE_SPRITES
    "pnx_text":     "pnx/text",           # PNX_USE_TEXT
    "pnx_diag":     "pnx/diagnostics",    # PNX_USE_DIAGNOSTICS
    "pnx_assets":   "pnx/assets",         # PNX_USE_ASSETS
    "pnx_input":    "pnx/input",          # PNX_USE_INPUT
    "pnx_gfx":      "pnx/gfx",
}


def module_of(source_path, elf_dir):
    """Attribute a source file to the module its pnx_config.h switch names.

    Falls back to the containing directory for anything not in the table, so a source
    file added tomorrow lands somewhere sensible instead of vanishing into a bucket --
    and shows up under a directory name, which is the hint that it wants an entry here.
    """
    if source_path is None:
        return "(unattributed)"

    normalised = source_path.replace("\\", "/")

    # Matches both the framework tree (src/pnx/core/...) and a game that reaches it
    # through a symlink (src/c/pnx/core/...), which is how examples/ is laid out.
    stem = re.search(r"/pnx/(?:[^/]+/)?([^/]+)\.c$", normalised)
    if stem and stem.group(1) in MODULE_OF_FILE:
        return MODULE_OF_FILE[stem.group(1)]

    match = re.search(r"/pnx/([^/]+)/", normalised)
    if match:
        return "pnx/" + match.group(1)
    if re.search(r"/pnx/[^/]+\.c$", normalised):
        return "pnx/(root)"

    if "/src/c/" in normalised:
        return "game"

    # SDK and libc land here. Kept visible rather than hidden: it is a real part of the
    # budget and people are routinely surprised by how much of it a single printf drags
    # in.
    return "(sdk/libc)"


def parse_symbols(nm_path, elf_path):
    """Return (module, kind, size, name) tuples for every space-occupying symbol."""
    # -S gives sizes, -l resolves symbols to source files via DWARF, --defined-only
    # skips imports.
    result = subprocess.run(
        [nm_path, "-S", "-l", "--defined-only", elf_path],
        capture_output=True, text=True, check=True,
    )

    elf_dir = os.path.dirname(os.path.abspath(elf_path))
    rows = []
    seen = set()

    for line in result.stdout.splitlines():
        # "<addr> <size> <type> <name>\t<file>:<line>"; the size and file are optional.
        match = re.match(
            r"^([0-9a-fA-F]+)\s+([0-9a-fA-F]+)\s+(\S)\s+(\S+)(?:\s+(.*?):(\d+))?\s*$",
            line,
        )
        if not match:
            continue

        addr, size_hex, sym_type, name, source, _line_no = match.groups()
        kind = classify(sym_type)
        if kind is None:
            continue

        size = int(size_hex, 16)
        if size == 0:
            continue

        # Aliases at the same address would otherwise be counted twice.
        key = (addr, kind)
        if key in seen:
            continue
        seen.add(key)

        rows.append((module_of(source, elf_dir), kind, size, name))

    return rows


def build_report(rows, budget, alloc_total=None, sections=None):
    modules = collections.defaultdict(lambda: collections.Counter())
    for module, kind, size, _name in rows:
        modules[module][kind] += size

    report = {"modules": {}, "budget": budget, "limit": VIRTUAL_SIZE_LIMIT}
    totals = collections.Counter()

    for module, counts in modules.items():
        entry = {k: counts.get(k, 0) for k in ("text", "rodata", "data", "bss")}
        entry["total"] = sum(entry.values())
        entry["ram"] = entry["data"] + entry["bss"]
        report["modules"][module] = entry
        totals.update(counts)

    report["total"] = {k: totals.get(k, 0) for k in ("text", "rodata", "data", "bss")}
    report["total"]["total"] = sum(report["total"].values())
    report["total"]["ram"] = report["total"]["data"] + report["total"]["bss"]

    # The budget is judged on allocated sections, not on the symbol sum. The remainder
    # is real and unavoidable -- headers, build-id, init/fini, alignment padding -- so it
    # is shown rather than quietly dropped.
    report["sections"] = sections or {}
    report["allocated"] = alloc_total if alloc_total is not None else report["total"]["total"]
    report["unaccounted"] = report["allocated"] - report["total"]["total"]
    report["largest_symbols"] = [
        {"name": n, "module": m, "kind": k, "size": s}
        for m, k, s, n in sorted(rows, key=lambda r: -r[2])[:10]
    ]
    return report


def print_report(report, show_symbols):
    modules = report["modules"]
    total = report["total"]
    budget = report["budget"]

    # The padding label is wider than most module names; excluding it from the width
    # calculation pushes its number out of the total column.
    name_width = max([len(n) for n in modules] + [len("module"), len("(headers/padding)")])

    header = (f"{'module'.ljust(name_width)}  {'text':>8} {'rodata':>8} "
              f"{'data':>7} {'bss':>8} {'total':>8}")
    print(header)
    print("-" * len(header))

    for name in sorted(modules, key=lambda n: -modules[n]["total"]):
        e = modules[name]
        print(f"{name.ljust(name_width)}  {e['text']:>8} {e['rodata']:>8} "
              f"{e['data']:>7} {e['bss']:>8} {e['total']:>8}")

    print("-" * len(header))
    print(f"{'TOTAL'.ljust(name_width)}  {total['text']:>8} {total['rodata']:>8} "
          f"{total['data']:>7} {total['bss']:>8} {total['total']:>8}")

    if report["unaccounted"]:
        print(f"{'(headers/padding)'.ljust(name_width)}  {'':>8} {'':>8} "
              f"{'':>7} {'':>8} {report['unaccounted']:>8}")

    used = report["allocated"]
    pct = 100.0 * used / budget if budget else 0.0
    bar_width = 40
    filled = min(bar_width, int(bar_width * used / budget)) if budget else 0
    print()
    print(f"[{'#' * filled}{'.' * (bar_width - filled)}] "
          f"{used} / {budget} bytes ({pct:.1f}%)")
    print(f"{budget - used} bytes remaining before the uint16 virtual_size overflows.")

    if show_symbols:
        print("\nlargest symbols")
        for s in report["largest_symbols"]:
            print(f"  {s['size']:>7}  {s['kind']:<6} {s['module']:<16} {s['name']}")


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("elf", help="path to pebble-app.elf")
    parser.add_argument("--budget", type=int, default=VIRTUAL_SIZE_LIMIT,
                        help="byte ceiling to measure against (default: 65535)")
    parser.add_argument("--fail-over", type=float, default=None, metavar="PCT",
                        help="exit non-zero if usage exceeds this percentage of budget")
    parser.add_argument("--symbols", action="store_true",
                        help="also list the ten largest symbols")
    parser.add_argument("--json", action="store_true", help="emit JSON instead of a table")
    args = parser.parse_args()

    if not os.path.exists(args.elf):
        print(f"error: no such file: {args.elf}", file=sys.stderr)
        return 2

    nm_path = find_tool("arm-none-eabi-nm")
    readelf_path = find_tool("arm-none-eabi-readelf")
    if nm_path is None or readelf_path is None:
        print("error: arm-none-eabi binutils not found on PATH or under the Pebble SDK",
              file=sys.stderr)
        return 2

    rows = parse_symbols(nm_path, args.elf)
    if not rows:
        print("error: no sized symbols found -- was the ELF built with debug info?",
              file=sys.stderr)
        return 2

    alloc_total, sections = allocated_size(readelf_path, args.elf)
    report = build_report(rows, args.budget, alloc_total, sections)

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print_report(report, args.symbols)

    used = report["allocated"]
    if used > args.budget:
        print(f"\nFAIL: over budget by {used - args.budget} bytes.", file=sys.stderr)
        return 1
    if args.fail_over is not None and 100.0 * used / args.budget > args.fail_over:
        print(f"\nFAIL: usage exceeds the {args.fail_over}% threshold.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())