#!/usr/bin/env python3
"""Freeze the editor into one double-clickable executable.

The barrier this removes is the whole install: Python, Pillow, and knowing to run a
script from a terminal. What comes out is one file that opens a window.

**Native window, not a bundled browser.** pywebview drives the webview the OS already
ships -- WebKitGTK, WebView2, WKWebView -- so this stays a Python-only project and the
binary lands around 40 MB. Electron would bundle Chromium instead: identical rendering
everywhere, ~200 MB, and a Node toolchain to maintain alongside this one. The editor
falls back to a browser tab when no system webview is present, so the trade is a
degraded path rather than a broken one.

**What this does NOT bundle: the Pebble SDK.** Everything except producing a `.pbw`
works with no external dependency -- authoring maps and fonts, previewing at device
size, validating, budgeting, and emitting the `.bin` resources. The SDK and its ARM
toolchain are hundreds of megabytes and are handled separately; see docs/EDITOR.md.

Usage:
    tools/build_editor.py            # build for the current platform
    tools/build_editor.py --clean    # remove build artefacts first
"""

import argparse
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import zipfile

TOOLS = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(TOOLS)
DIST = os.path.join(ROOT, "dist")
WORK = os.path.join(ROOT, "build", "editor")
FRAMEWORK = os.path.join(ROOT, "src", "pnx")

# Pillow is a hard requirement here, unlike in the pipeline where it is an optional
# import: an editor that cannot rasterise a font or slice a sheet has no reason to exist.
REQUIRED = ["PyInstaller", "PIL"]

# pywebview is not required to BUILD. Without it the editor opens a browser tab instead
# of a window, which is the documented fallback -- and on Linux there is often no system
# webview binding to bundle anyway. Missing it is a warning, not a failure.
OPTIONAL = ["webview"]

PIP_NAMES = {"PIL": "pillow", "webview": "pywebview", "PyInstaller": "pyinstaller"}


def check_deps():
    missing = [PIP_NAMES[m] for m in REQUIRED if not _importable(m)]
    if missing:
        print("missing build dependencies:\n"
              f"    pip install {' '.join(missing)}", file=sys.stderr)
        return False
    for mod in OPTIONAL:
        if not _importable(mod):
            print(f"note: {PIP_NAMES[mod]} not installed -- this build will open a "
                  f"browser tab rather than a native window")
    return True


def _importable(mod):
    try:
        __import__(mod)
        return True
    except ImportError:
        return False


def target_tag():
    """`<os>-<arch>`, for naming an artefact someone has to pick from a release page."""
    system = {"Darwin": "macos", "Windows": "windows", "Linux": "linux"}.get(
        platform.system(), platform.system().lower())
    arch = {"AMD64": "x86_64", "x86_64": "x86_64",
            "arm64": "arm64", "aarch64": "arm64"}.get(platform.machine(),
                                                      platform.machine())
    return f"{system}-{arch}"


def package(name):
    """Wrap the built binary in whatever that platform expects to receive.

    Not an installer in the MSI/pkg sense, deliberately: the editor is a single
    self-contained file that needs no install step, and adding one would mean code
    signing infrastructure on two platforms to solve a problem that does not exist. What
    it does need is the idiomatic *container* -- a .dmg on macOS because that is how a
    .app is delivered, a .zip on Windows because a bare .exe download is treated as
    hostile, a .tar.gz on Linux because it preserves the executable bit.
    """
    tag = target_tag()
    base = f"{name}-{tag}"
    system = platform.system()

    if system == "Darwin":
        app = os.path.join(DIST, name + ".app")
        if not os.path.exists(app):
            print(f"no {app} to package", file=sys.stderr)
            return None
        dmg = os.path.join(DIST, base + ".dmg")
        if os.path.exists(dmg):
            os.remove(dmg)
        # hdiutil is in the base system, so this needs nothing installed.
        r = subprocess.run(["hdiutil", "create", "-volname", name,
                            "-srcfolder", app, "-ov", "-format", "UDZO", dmg])
        return dmg if r.returncode == 0 else None

    exe = os.path.join(DIST, name + (".exe" if system == "Windows" else ""))
    if not os.path.exists(exe):
        print(f"no {exe} to package", file=sys.stderr)
        return None

    if system == "Windows":
        out = os.path.join(DIST, base + ".zip")
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
            z.write(exe, os.path.basename(exe))
        return out

    out = os.path.join(DIST, base + ".tar.gz")
    with tarfile.open(out, "w:gz") as t:
        # arcname without the path so it extracts as one file, not a tree.
        t.add(exe, arcname=os.path.basename(exe))
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--clean", action="store_true", help="remove build artefacts first")
    ap.add_argument("--package", action="store_true",
                    help="also wrap the result as .tar.gz / .zip / .dmg")
    ap.add_argument("--name", default="pebblnyx-editor")
    args = ap.parse_args()

    if not check_deps():
        return 1

    # A binary without the engine can open projects and never build one, which is a
    # worse failure than not building at all because it only surfaces at the end.
    if not os.path.exists(os.path.join(FRAMEWORK, "pnx.h")):
        print(f"no engine sources at {FRAMEWORK}", file=sys.stderr)
        return 1

    if args.clean:
        for path in (DIST, WORK):
            shutil.rmtree(path, ignore_errors=True)

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--onefile",
        # No console window on Windows/macOS: this opens its own window, and a stray
        # terminal behind it looks like something went wrong.
        "--windowed",
        f"--name={args.name}",
        f"--distpath={DIST}",
        f"--workpath={WORK}",
        f"--specpath={WORK}",
        # The pipeline and the preview renderer are imported by the editor, not shelled
        # out to -- which is exactly why Project.build() had to stop invoking
        # sys.executable. Named explicitly so PyInstaller's analysis cannot miss them.
        "--hidden-import=pnx_assets",
        "--hidden-import=pnx_preview",
        "--hidden-import=pnx_project",
        # **The engine ships inside the editor.** A project anywhere on disk builds
        # against this copy -- pnx_project stages it into <project>/src/c/pnx before each
        # build -- so the editor is not merely a content tool, it is what a game is
        # compiled against. Without this a distributed editor could open a project and
        # never build one.
        f"--add-data={FRAMEWORK}{os.pathsep}pnx",
        f"--paths={TOOLS}",
    ]
    if platform.system() == "Darwin":
        cmd.append("--osx-bundle-identifier=dev.pebblnyx.editor")
    cmd.append(os.path.join(TOOLS, "pnx_editor.py"))

    print(" ".join(cmd))
    r = subprocess.run(cmd)
    if r.returncode != 0:
        return r.returncode

    out = os.path.join(DIST, args.name + (".exe" if os.name == "nt" else ""))
    if os.path.exists(out):
        mb = os.path.getsize(out) / (1024 * 1024)
        print(f"\n{out}  ({mb:.0f} MB)")

    if args.package:
        archive = package(args.name)
        if not archive:
            return 1
        mb = os.path.getsize(archive) / (1024 * 1024)
        print(f"{archive}  ({mb:.0f} MB)")

    print("Run it with a project folder, or with no arguments to pick one.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
