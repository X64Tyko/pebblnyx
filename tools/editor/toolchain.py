"""Finds, installs and tracks the Pebble SDK (`Toolchain`)."""

import os
import shutil
import subprocess
import sys
import threading

from editor.updater import _config_dir

# **We do not ship the Pebble SDK and we do not fetch it ourselves.** The Pebble
# Developer License grants the licence to the USER -- "limited, non-transferable,
# non-sublicensable" -- and section 5(f) prohibits distributing the SDK. So the editor
# drives Pebble's own first-party tool: it shows the terms, takes a real acceptance, and
# then runs `pebble sdk install`. The bytes go from Pebble's server to the user's disk
# and we never hold a copy, which is the same thing the user would do by hand.
SDK_TERMS = [
    ("Pebble Terms of Use",
     "https://developer.repebble.com/legal/terms-of-use/index.html"),
    ("Pebble Developer License",
     "https://developer.repebble.com/legal/sdk-license/index.html"),
]


class Toolchain:
    """Finds, installs and keeps track of the Pebble SDK."""

    def __init__(self):
        self.log = []
        self.busy = False
        self._lock = threading.Lock()

    # ------------------------------------------------------------- detection

    @staticmethod
    def pebble_path():
        return shutil.which("pebble")

    @staticmethod
    def installer():
        """How pebble-tool can be installed on this machine.

        A frozen editor has no pip of its own, so this looks for a package manager the
        user already has rather than assuming a Python environment we control.
        """
        for name, cmd in (("uv", ["uv", "tool", "install", "pebble-tool"]),
                          ("pipx", ["pipx", "install", "pebble-tool"]),
                          ("pip", [sys.executable, "-m", "pip", "install", "--user",
                                   "pebble-tool"])):
            if name == "pip":
                if getattr(sys, "frozen", False):
                    continue          # sys.executable is the editor, not an interpreter
                try:
                    subprocess.run([sys.executable, "-m", "pip", "--version"],
                                   capture_output=True, timeout=10)
                except Exception:     # noqa: BLE001
                    continue
                return name, cmd
            if shutil.which(name):
                return name, cmd
        return None, None

    def accepted(self):
        return os.path.exists(os.path.join(_config_dir(), "sdk-license-accepted"))

    def accept(self):
        """Record that the user accepted, with what and when.

        Written as a file rather than held in memory because the grant is to the person
        and persists across runs -- and because a record of what was agreed to, and when,
        is worth having if it is ever asked about.
        """
        import datetime
        with open(os.path.join(_config_dir(), "sdk-license-accepted"), "w") as f:
            f.write(f"accepted {datetime.datetime.now().isoformat(timespec='seconds')}\n")
            for title, url in SDK_TERMS:
                f.write(f"{title}: {url}\n")

    def status(self, remote=False):
        """What is installed, and whether a build can run.

        `remote` is opt-in because listing available versions hits Pebble's server, and
        doing that on every page load would be both slow and rude.
        """
        pebble = self.pebble_path()
        out = {"pebble": pebble, "installed": [], "active": None, "available": [],
               "accepted": self.accepted(), "busy": self.busy,
               "log": "".join(self.log[-400:]),
               "installer": self.installer()[0], "terms": SDK_TERMS}

        if pebble:
            try:
                r = subprocess.run([pebble, "sdk", "list"], capture_output=True,
                                   text=True, timeout=60 if remote else 15)
                section = None
                for line in (r.stdout or "").splitlines():
                    low = line.strip().lower()
                    if low.startswith("installed"):
                        section = "installed"
                    elif low.startswith("available"):
                        section = "available"
                    elif line.strip() and section:
                        v = line.strip()
                        if v.endswith("(active)"):
                            v = v.replace("(active)", "").strip()
                            out["active"] = v
                        out[section].append(v)
            except Exception as e:                       # noqa: BLE001
                out["error"] = str(e)

        out["can_build"] = bool(pebble and out["active"])
        # Newer SDKs are reported rather than installed silently: a toolchain that
        # changes under a project between builds is its own class of confusing bug.
        newer = [v for v in out["available"] if v not in out["installed"]]
        out["newer"] = newer[-1] if newer else None
        return out

    # -------------------------------------------------------------- installing

    def _run(self, cmd, label):
        self.log.append(f"\n$ {' '.join(cmd)}\n")
        try:
            p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 text=True, bufsize=1)
            for line in p.stdout:
                self.log.append(line)
                del self.log[:-400]
            return p.wait() == 0
        except FileNotFoundError:
            self.log.append(f"{label}: {cmd[0]} not found\n")
            return False
        except Exception as e:                           # noqa: BLE001
            self.log.append(f"{label} failed: {e}\n")
            return False

    def install(self, version="latest"):
        """Install pebble-tool if needed, then the SDK. Runs on a worker thread.

        Refuses without a recorded acceptance. The licence is granted to the user and
        cannot be accepted on their behalf, so this is a real gate rather than a notice --
        which is a little stricter than the official CLI, and the safer side to err on.
        """
        if not self.accepted():
            raise ValueError("the Pebble SDK licence has to be accepted first")
        with self._lock:
            if self.busy:
                raise ValueError("an install is already running")
            self.busy = True

        def work():
            try:
                if not self.pebble_path():
                    name, cmd = self.installer()
                    if not cmd:
                        self.log.append(
                            "No uv, pipx or pip found to install pebble-tool with.\n"
                            "Install one, or install pebble-tool yourself:\n"
                            "    pip install pebble-tool\n")
                        return
                    self.log.append(f"Installing pebble-tool with {name}...\n")
                    if not self._run(cmd, "pebble-tool"):
                        return

                pebble = self.pebble_path()
                if not pebble:
                    self.log.append(
                        "pebble-tool installed but `pebble` is not on PATH -- "
                        "you may need to open a new terminal or add its bin directory.\n")
                    return

                self.log.append(f"\nInstalling Pebble SDK {version} "
                                f"(~767MB, includes the ARM toolchain)...\n")
                if self._run([pebble, "sdk", "install", version], "sdk"):
                    self.log.append("\nDone. The Build button can produce a .pbw now.\n")
            finally:
                self.busy = False

        threading.Thread(target=work, daemon=True).start()



TOOLCHAIN = Toolchain()
