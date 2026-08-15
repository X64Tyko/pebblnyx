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

sys.path.insert(0, TOOLS)
from pnx_project import FRAMEWORK_VERSION as VERSION            # noqa: E402

# What a person sees in a Start menu, a Finder window or an applications menu. The
# executable keeps its hyphenated name; this is the human one, and the two differ on
# purpose -- `pebblnyx-editor` is what you type, "pebblnyx editor" is what you read.
APP_TITLE = "pebblnyx editor"

# Identity for the Windows uninstall entry, bare so the .iss can add its own braces.
# Fixed forever: change it and an upgrade installs alongside the old version rather than
# replacing it.
APP_GUID = "9E5A5C4E-6D3B-4E51-9B7C-3F2A1D0C4B87"

# Pillow is a hard requirement here, unlike in the pipeline where it is an optional
# import: an editor that cannot rasterise a font or slice a sheet has no reason to exist.
# certifi is required, not optional: the frozen binary carries its own OpenSSL, whose
# compiled-in CA paths are the build machine's. Without a bundled root store the
# updater cannot verify github.com and reports the whole internet as unreachable.
REQUIRED = ["PyInstaller", "PIL", "certifi"]

# pywebview is not required to BUILD. Without it the editor opens a browser tab instead
# of a window, which is the documented fallback -- and on Linux there is often no system
# webview binding to bundle anyway. Missing it is a warning, not a failure.
OPTIONAL = ["webview"]

PIP_NAMES = {"PIL": "pillow", "webview": "pywebview", "PyInstaller": "pyinstaller",
             "certifi": "certifi"}


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


def find_iscc():
    """The Inno Setup compiler. Preinstalled on GitHub's Windows runners."""
    found = shutil.which("ISCC") or shutil.which("iscc")
    if found:
        return found
    for base in (os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
                 os.environ.get("ProgramFiles", r"C:\Program Files")):
        for version in ("6", "5"):
            path = os.path.join(base, f"Inno Setup {version}", "ISCC.exe")
            if os.path.exists(path):
                return path
    return None


def package_windows(name, base):
    """An installer, plus the bare binary in a zip for anyone who wants it portable.

    Both, because they answer different questions. The installer is for the person who
    wants the thing on their Start menu and does not want to think about where a
    downloaded .exe should live; the zip is for the person who keeps tools in a folder
    and does not want an uninstaller entry.

    Installed per-user under LOCALAPPDATA rather than into Program Files. That is what
    `PrivilegesRequired=lowest` buys: no UAC prompt at all. An unsigned installer asking
    for administrator rights is exactly the shape of the thing people are told not to run.
    """
    exe = os.path.join(DIST, name + ".exe")
    if not os.path.exists(exe):
        print(f"no {exe} to package", file=sys.stderr)
        return []

    out = [os.path.join(DIST, base + ".zip")]
    with zipfile.ZipFile(out[0], "w", zipfile.ZIP_DEFLATED) as z:
        z.write(exe, os.path.basename(exe))

    iscc = find_iscc()
    if not iscc:
        # Loud rather than quietly shipping only the zip: "the installer silently stopped
        # being built" is the kind of thing nobody notices until a release day.
        print("Inno Setup (ISCC.exe) not found -- cannot build the installer.\n"
              "    Install it from https://jrsoftware.org/isdl.php, or "
              "`choco install innosetup`.", file=sys.stderr)
        return []

    script = os.path.join(WORK, f"{name}.iss")
    os.makedirs(WORK, exist_ok=True)

    # Inno escapes a literal opening brace by doubling it, so an AppId GUID is written
    # `{{GUID}` -- built here rather than inside the f-string below, where Python's brace
    # doubling and Inno's would be fighting over the same characters.
    #
    # Every directive in that script also has to sit on ONE line: .iss has no
    # continuation character, so a wrapped parameter list reads as a new and invalid
    # directive. Hence the long lines below.
    app_id = "{{" + APP_GUID + "}"

    with open(script, "w") as f:
        f.write(f'''; GENERATED by tools/build_editor.py -- do not edit.
[Setup]
AppId={app_id}
AppName={APP_TITLE}
AppVersion={VERSION}
AppPublisher=pebblnyx
DefaultDirName={{autopf}}\\{name}
DefaultGroupName={APP_TITLE}
DisableProgramGroupPage=yes
DisableDirPage=auto
PrivilegesRequired=lowest
OutputDir={DIST}
OutputBaseFilename={base}-setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayName={APP_TITLE}

[Files]
Source: "{exe}"; DestDir: "{{app}}"; Flags: ignoreversion

[Icons]
Name: "{{autoprograms}}\\{APP_TITLE}"; Filename: "{{app}}\\{name}.exe"
Name: "{{autodesktop}}\\{APP_TITLE}"; Filename: "{{app}}\\{name}.exe"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional shortcuts:"; Flags: unchecked

[Run]
Filename: "{{app}}\\{name}.exe"; Description: "Open {APP_TITLE}"; Flags: nowait postinstall skipifsilent
''')

    r = subprocess.run([iscc, script])
    if r.returncode != 0:
        return []
    out.append(os.path.join(DIST, base + "-setup.exe"))
    return out


def package_macos(name, base):
    """A .dmg the user drags into Applications, which is what installing means here.

    The window contains the .app and a symlink to /Applications, so the gesture is
    obvious without instructions. Previously it held the bare .app, which people drag to
    the desktop or run from the mounted volume -- and running from a read-only mounted
    volume produces failures that look like bugs in the app.
    """
    app = os.path.join(DIST, name + ".app")
    if not os.path.exists(app):
        print(f"no {app} to package", file=sys.stderr)
        return []

    stage = os.path.join(WORK, "dmg")
    shutil.rmtree(stage, ignore_errors=True)
    os.makedirs(stage)
    shutil.copytree(app, os.path.join(stage, os.path.basename(app)), symlinks=True)
    os.symlink("/Applications", os.path.join(stage, "Applications"))

    dmg = os.path.join(DIST, base + ".dmg")
    if os.path.exists(dmg):
        os.remove(dmg)
    # hdiutil is in the base system, so this needs nothing installed.
    r = subprocess.run(["hdiutil", "create", "-volname", APP_TITLE,
                        "-srcfolder", stage, "-ov", "-format", "UDZO", dmg])
    return [dmg] if r.returncode == 0 else []


DESKTOP_ENTRY = """[Desktop Entry]
Type=Application
Name={title}
Comment=Author, preview and build Pebble games
Exec={exec_path}
Terminal=false
Categories=Development;IDE;
Keywords=pebble;game;editor;
"""

INSTALL_SH = """#!/bin/sh
# Puts the editor on your PATH and in your applications menu, for this user only --
# nothing here needs root, and nothing is written outside your home directory.
#
# You do not have to run this. The binary beside it works as-is; this only makes it
# launchable by name and from a desktop menu.
set -e

here=$(cd "$(dirname "$0")" && pwd)
bin="$HOME/.local/bin"
apps="$HOME/.local/share/applications"

mkdir -p "$bin" "$apps"
cp "$here/{name}" "$bin/{name}"
chmod +x "$bin/{name}"

sed "s|^Exec=.*|Exec=$bin/{name}|" "$here/{name}.desktop" > "$apps/{name}.desktop"

echo "installed: $bin/{name}"
case ":$PATH:" in
  *":$bin:"*) ;;
  *) echo "note: $bin is not on your PATH -- add it, or run the binary directly." ;;
esac

# Ask the thing we just installed whether it is complete. Python, Pillow and the engine
# travel inside the binary -- nothing is installed from the system and nothing needs to
# be -- but "it is all in there" is a claim worth checking on the machine that will run
# it, rather than trusting the build that produced it.
echo
if "$bin/{name}" --selftest; then
  :
else
  echo "WARNING: this build is missing something listed above. It may still start," >&2
  echo "         but expect failures. Please report it with the lines above." >&2
fi
"""

UNINSTALL_SH = """#!/bin/sh
# Undoes install.sh. Two files, both in your home directory.
set -e
rm -f "$HOME/.local/bin/{name}" "$HOME/.local/share/applications/{name}.desktop"
echo "removed {name}"
"""


def package_linux(name, base):
    """A .tar.gz that extracts to a folder with an installer script beside the binary.

    Linux has no one installer format -- a .deb excludes Arch, an .rpm excludes Debian,
    an AppImage needs FUSE on the host -- so what ships is the binary plus the two things
    that make it feel installed: a .desktop entry and a script that puts both in the
    right places under $HOME. No root, no package manager, and an uninstall script that
    genuinely removes everything it wrote.
    """
    exe = os.path.join(DIST, name)
    if not os.path.exists(exe):
        print(f"no {exe} to package", file=sys.stderr)
        return []

    stage = os.path.join(WORK, "tar", base)
    shutil.rmtree(os.path.dirname(stage), ignore_errors=True)
    os.makedirs(stage)

    shutil.copy2(exe, os.path.join(stage, name))
    write(os.path.join(stage, f"{name}.desktop"),
          DESKTOP_ENTRY.format(title=APP_TITLE, exec_path=name))
    write(os.path.join(stage, "install.sh"), INSTALL_SH.format(name=name), executable=True)
    write(os.path.join(stage, "uninstall.sh"), UNINSTALL_SH.format(name=name),
          executable=True)
    write(os.path.join(stage, "README.txt"),
          f"{APP_TITLE} {VERSION}\n\n"
          f"Run it now:        ./{name}\n"
          f"Add to your menu:  ./install.sh\n"
          f"Remove it again:   ./uninstall.sh\n\n"
          "One self-contained file: no Python, no pip, no install required. The pnx\n"
          "engine ships inside it, so a project created anywhere builds against the\n"
          "engine in this editor.\n\n"
          "The Pebble SDK is NOT included and cannot be -- its terms forbid\n"
          "redistribution. Everything except producing a .pbw works without it; the\n"
          "Settings tab installs it for you on request.\n")

    out = os.path.join(DIST, base + ".tar.gz")
    with tarfile.open(out, "w:gz") as t:
        t.add(stage, arcname=base)
    return [out]


def write(path, text, executable=False):
    with open(path, "w", newline="\n") as f:
        f.write(text)
    if executable:
        os.chmod(path, 0o755)


def package(name):
    """Wrap the built binary in what that platform expects to be INSTALLED from.

    Still not an MSI or a .pkg: those want code signing to be anything but frightening,
    and neither certificate exists yet. What each platform gets is the unsigned form of
    the same gesture -- a wizard on Windows, a drag into Applications on macOS, a script
    that writes two files under $HOME on Linux -- so that "downloaded it, now what" has
    an answer everywhere.
    """
    tag = target_tag()
    base = f"{name}-{tag}"
    system = platform.system()

    if system == "Darwin":
        return package_macos(name, base)
    if system == "Windows":
        return package_windows(name, base)
    return package_linux(name, base)


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
        # Imported inside a function, so the analysis cannot see it; its data file is
        # the actual payload.
        "--hidden-import=certifi",
        "--collect-data=certifi",
        "--hidden-import=pnx_assets",
        "--hidden-import=pnx_preview",
        "--hidden-import=pnx_project",
        # Same sibling-module-found-via-sys.path pattern as the three above (every
        # tools/editor/*.py does `sys.path.insert(0, TOOLS)` then a plain `import`, which
        # is exactly the shape PyInstaller's static analysis can fail to trace) -- these
        # two were never listed, which worked only as long as nothing forced the point.
        # selftest()'s own dynamic `__import__("size_report", ...)` does.
        "--hidden-import=pnx_mapfile",
        "--hidden-import=size_report",
        # **The engine ships inside the editor.** A project anywhere on disk builds
        # against this copy -- pnx_project stages it into <project>/src/c/pnx before each
        # build -- so the editor is not merely a content tool, it is what a game is
        # compiled against. Without this a distributed editor could open a project and
        # never build one.
        f"--add-data={FRAMEWORK}{os.pathsep}pnx",
        # The editor's frontend, since tools/editor/split (0.1.0-beta.24): index.html,
        # css/, js/ are real files on disk now, not a PAGE string baked into
        # pnx_editor.py itself -- so they need to travel as data, the same as the engine
        # sources above, or editor/routes/project.py's own `open(...index.html)` at
        # import time fails inside the frozen bundle with nothing on screen to explain
        # why. (Exactly what shipped as v0.1.0-beta.26: CI built and froze the binary
        # fine, then it failed to even start.)
        f"--add-data={os.path.join(TOOLS, 'editor', 'static')}{os.pathsep}editor/static",
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

        # Ask the artefact whether it is complete, rather than assuming PyInstaller
        # collected everything. A missed hidden import produces a binary that runs and
        # then fails the first time someone rasterises a font, which is a bug report
        # instead of a build error. Packaging a known-broken binary is worse than failing.
        # Flushed, or this heading lands after the child's output: the parent's stdout is
        # block-buffered when the build is piped to a log, the child's is not.
        print("\nverifying the bundle carries its dependencies:", flush=True)
        check = subprocess.run([out, "--selftest"])
        if check.returncode != 0:
            print("the packaged editor is missing something -- not packaging it",
                  file=sys.stderr)
            return 1

    if args.package:
        produced = package(args.name)
        if not produced:
            return 1
        for path in produced:
            mb = os.path.getsize(path) / (1024 * 1024)
            print(f"{path}  ({mb:.0f} MB)")

    print("Run it with a project folder, or with no arguments to pick one.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
