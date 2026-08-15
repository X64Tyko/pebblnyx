"""Builds, installs into and controls a QEMU emulator via `pebble` (`Emulator`)."""

import contextlib
import io
import json
import os
import platform
import socket
import subprocess
import tempfile
import threading
import time

import pnx_assets as pa                                     # noqa: E402

from editor import TOOLS
from editor.toolchain import Toolchain


# ---------------------------------------------------------------------- emulator
#
# E3 in docs/EDITOR.md ("emulator panel") was built as a plan and then marked
# SUPERSEDED: it assumed a QEMU target for the platform this framework built for, and at
# the time nothing shipped one. M9 made every platform in Project.PLATFORMS a real
# `pebble build` target, which reopens the question per platform -- an author's
# installed SDK either ships a QEMU image for it or it does not, and that is a property
# of their disk, not of this codebase (EDITOR.md's own "not currently possible" note was
# checked against one SDK snapshot, and this one's `sdk-core/pebble/*/qemu/` shows all
# seven now). So there is no hardcoded list here: every platform is offered, and
# `pebble install --emulator` either works or reports why not, the same way a build
# failure already does everywhere else in this editor.
#
# Runs entirely through the `pebble` CLI -- not libpebble2 directly, and not a
# hand-rolled VNC/RFB client. The one exception is the live frame, which talks to
# QEMU's own monitor socket with `screendump`: that is the mechanism pebble-tool's OWN
# `screenshot`/GIF commands use for a headless capture, so this is a second caller of an
# already-real mechanism instead of a second protocol implementation.
class Emulator:
    """Builds, installs into and controls a QEMU emulator via `pebble`.

    One instance for the whole process, like TOOLCHAIN -- pebble-tool's own emulator
    state (see INFO_PATH below) is a single machine-wide file, so per-project
    bookkeeping here would just be a second, potentially-stale opinion about whose
    emulator is actually running.
    """

    # pebble_tool.sdk.emulator.get_emulator_info_path(): tempfile.gettempdir()/pb-emulator.json.
    # Read-only here -- `pebble install --emulator` and `pebble kill` own writing it. See
    # docs/EDITOR.md's "recorded in a state file, so several tools can attach to one
    # running emulator" -- this editor is simply one more of those tools.
    INFO_PATH = os.path.join(tempfile.gettempdir(), "pb-emulator.json")

    def __init__(self):
        self.log = []
        self.busy = False
        self._lock = threading.Lock()
        # One lock per platform, not one global lock: a slow emery screendump has no
        # reason to make a concurrent basalt one wait, and nothing here runs more than
        # one platform's emulator meaningfully at once anyway (see stop()'s own note).
        self._frame_locks = {}
        # platform -> {"proc": Popen, "lock": Lock} -- see _bridge() and
        # tools/pnx_emu_bridge.py for why this exists at all: `pebble emu-button` on
        # its own measured at ~670-680ms per press, consistently, because it is a
        # fresh CLI subprocess (and a fresh 0.5s-before-first-attempt connection)
        # every single call. This keeps one connection open per platform instead.
        self._bridges = {}
        self._bridges_lock = threading.Lock()
        # Separate from the emulator's own log/busy: packaging and running the emulator
        # are two different things someone might want the state of independently, and
        # sharing one log would interleave them.
        self.package_log = []
        self.package_busy = False
        self.package_result = None
        # Building straight onto a real watch over the phone app's Developer
        # Connection -- its own log/busy/result for the same reason package's are
        # separate from the emulator's.
        self.install_log = []
        self.install_busy = False
        self.install_result = None
        # `pebble logs --phone <address>`: a long-lived process, not a one-shot build,
        # so it gets its own start/stop rather than living inside install_device --
        # attaching to logs and installing a new build are independent actions.
        self._logs_proc = None
        self._logs_buf = []

    @classmethod
    def _all_info(cls):
        try:
            with open(cls.INFO_PATH) as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    @staticmethod
    def _pid_alive(pid):
        # Mirrors pebble-tool's own check (pebble_tool/sdk/emulator.py's
        # _is_pid_running), which documents itself as unreliable on Windows (PBL-21228)
        # -- there is no more portable answer available without a dependency this editor
        # does not otherwise need. Diverges from upstream in one way: an unexpected
        # errno there is re-raised; here it is just "not confirmed alive", because a
        # wrong "not running" is a stale-looking panel and a crash in a status poll is
        # worse.
        if not pid:
            return False
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True

    def info(self, platform):
        """pebble-tool's live record for `platform`, if a process from it is still alive."""
        for version_info in self._all_info().get(platform, {}).values():
            if self._pid_alive(version_info.get("qemu", {}).get("pid")):
                return version_info
        return None

    def status(self, platform):
        return {"running": self.info(platform) is not None,
                "busy": self.busy, "log": "".join(self.log[-400:])}

    # ------------------------------------------------------------- lifecycle
    #
    # `pebble sdk install` DOES install QEMU -- it lands at
    # <sdk>/toolchain/bin/qemu-pebble, confirmed by running it directly. It just does not
    # put that directory on PATH, and pebble-tool's own emulator spawn resolves
    # `qemu-pebble` as a bare command via subprocess's normal PATH lookup (`PEBBLE_QEMU_
    # PATH`, pebble_tool/sdk/emulator.py) rather than through the SDK-aware path
    # resolution `pebble build` uses internally -- which is why a build can succeed while
    # every emulator command silently fails to find it. So "installing the SDK doesn't
    # install the emulator" was the right symptom read the wrong way: the binary is
    # there, `pebble` just cannot see it without help.
    #
    # The fix mirrors pebble-tool's own layout (pebble_tool/util/get_persist_dir,
    # pebble_tool/sdk/manager.py's `SDKs/current` symlink) rather than asking the user to
    # edit their PATH: this editor already knows how to find the active SDK, so every
    # `pebble` call below gets a PATH with that toolchain's `bin` prepended.
    @staticmethod
    def _sdk_persist_dir():
        if platform.system() == "Darwin":
            return os.path.expanduser("~/Library/Application Support/Pebble SDK")
        legacy = os.path.expanduser("~/.pebble-sdk")
        if os.path.isdir(legacy):
            return legacy
        data_home = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
        return os.path.join(data_home, "pebble-sdk")

    @classmethod
    def _pebble_env(cls):
        """`os.environ`, with the active SDK's toolchain (qemu-pebble, arm-none-eabi-*)
        on PATH -- see the lifecycle comment above for why this is needed at all."""
        env = dict(os.environ)
        toolchain_bin = os.path.join(cls._sdk_persist_dir(), "SDKs", "current",
                                     "toolchain", "bin")
        if os.path.isdir(toolchain_bin):
            env["PATH"] = toolchain_bin + os.pathsep + env.get("PATH", "")
        return env

    # ---------------------------------------------------------- button bridge
    #
    # See tools/pnx_emu_bridge.py's own docstring for the full story. Short version:
    # `pebble emu-button` is a fresh CLI subprocess per call, and its connection setup
    # sleeps 0.5s before even attempting to connect -- every time, since the CLI has
    # no way to hold a connection open between commands. Measured consistently at
    # ~670-680ms per press. This spawns ONE long-lived helper (under pebble-tool's own
    # Python, since libpebble2 lives in that venv, not this editor's) that opens the
    # same websocket once and reuses it for every press against a given platform.

    @staticmethod
    def _bridge_interpreter():
        """The Python interpreter pebble-tool itself runs under.

        Read off `pebble`'s own shebang line rather than guessing a venv layout --
        pip, pipx, uv and a plain venv each produce a different one, and the shebang
        is the one thing every POSIX install already gets right, for the same reason
        `pebble` itself runs at all. Windows console-script .exe wrappers have no
        shebang to read; their interpreter is normally right next to them in the same
        Scripts/bin directory, which is the fallback below.
        """
        pebble = Toolchain.pebble_path()
        if not pebble:
            return None
        try:
            with open(pebble, errors="ignore") as f:
                first = f.readline()
        except OSError:
            first = ""
        if first.startswith("#!"):
            return first[2:].strip()
        for name in ("python.exe", "python3.exe", "python", "python3"):
            candidate = os.path.join(os.path.dirname(pebble), name)
            if os.path.exists(candidate):
                return candidate
        return None

    def _spawn_bridge(self, platform):
        interp = self._bridge_interpreter()
        info = self.info(platform)
        port = info and info.get("pypkjs", {}).get("port")
        if not interp or not port:
            return None
        script = os.path.join(TOOLS, "pnx_emu_bridge.py")
        try:
            proc = subprocess.Popen([interp, script, "--port", str(port)],
                                    stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL, text=True, bufsize=1)
            ready = proc.stdout.readline()
            if ready.strip() != "ready":
                proc.kill()
                return None
        except OSError:
            return None
        return proc

    def _bridge(self, platform):
        """A live bridge for `platform`, spawning one if needed. None if it could not
        be started for any reason -- callers fall back to the slower CLI path rather
        than failing outright; a slow button is better than no button."""
        with self._bridges_lock:
            entry = self._bridges.get(platform)
            if entry and entry["proc"].poll() is None:
                return entry
            proc = self._spawn_bridge(platform)
            if not proc:
                self._bridges.pop(platform, None)
                return None
            entry = {"proc": proc, "lock": threading.Lock()}
            self._bridges[platform] = entry
            return entry

    def _bridge_send(self, platform, line):
        """One command exchange with `platform`'s bridge, or None if it is
        unavailable -- distinct from "ok"/"err ...", which mean the bridge itself
        answered. None is the signal for button() to fall back to the CLI.
        """
        entry = self._bridge(platform)
        if not entry:
            return None
        with entry["lock"]:
            try:
                entry["proc"].stdin.write(line + "\n")
                entry["proc"].stdin.flush()
                resp = entry["proc"].stdout.readline()
            except (OSError, ValueError):
                resp = ""
        if not resp:
            # The bridge died mid-exchange -- drop it so the NEXT call respawns one
            # against whatever is actually running now, rather than writing into a
            # closed pipe forever.
            with self._bridges_lock:
                if self._bridges.get(platform) is entry:
                    del self._bridges[platform]
            return None
        return resp.strip()

    def _kill_bridge(self, platform):
        with self._bridges_lock:
            entry = self._bridges.pop(platform, None)
        if not entry:
            return
        with contextlib.suppress(Exception):
            entry["proc"].stdin.close()
        with contextlib.suppress(Exception):
            entry["proc"].terminate()

    def _run(self, cmd, cwd=None, extra_env=None, log=None):
        log = self.log if log is None else log
        log.append(f"\n$ {' '.join(cmd)}\n")
        env = self._pebble_env()
        if extra_env:
            env.update(extra_env)
        try:
            p = subprocess.Popen(cmd, cwd=cwd, env=env, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, text=True, bufsize=1)
        except FileNotFoundError:
            log.append(f"{cmd[0]} not found -- install the Pebble SDK in Settings first\n")
            return False
        for line in p.stdout:
            log.append(line)
            del log[:-400]
        return p.wait() == 0

    def start(self, project, platform):
        """Bakes the project's CURRENT orientation, compiles it, and boots `platform`.

        Runs on a worker thread: an ARM compile plus a cold QEMU boot is tens of
        seconds, and the panel polls status() rather than blocking the request on it.

        Baking first is what makes this "respect the orientation when testing" rather
        than install whatever the last build happened to leave behind:
        `project.build()` runs the real pipeline against `project.orientation` every
        time -- the exact call the ordinary Build button makes -- so there is no
        separate "emulator build" path that could fall out of sync with the project's
        actual orientation setting.
        """
        pebble = Toolchain.pebble_path()
        if not pebble:
            return False, "pebble-tool is not installed -- see Settings"
        with self._lock:
            if self.busy:
                return False, "an emulator build is already running"
            self.busy = True

        def work():
            try:
                # A rebuild can respawn pypkjs on a different port (a version change,
                # or a VNC-state mismatch elsewhere killing and restarting QEMU); a
                # bridge holding the OLD port would fail every press until it noticed
                # and respawned itself anyway, so just start that over now.
                self._kill_bridge(platform)
                self.log.append(f"\n=== {platform}: baking resources for "
                                f"{pa.ORIENT_NAMES[project.orientation]} ===\n")
                result = project.build()
                self.log.append(result["output"])
                if not result["ok"]:
                    self.log.append("\nasset build failed -- see above\n")
                    return
                # PNX_DEFINES is pnx_config.h's own escape hatch (any example's
                # wscript reads it from the environment) -- not a wscript edit, so a
                # project that never opted into PNX_FORCE_SCREEN_LOCK in its own
                # source stays that way; this only ever affects the emulator's build,
                # never a shipped one, because nothing outside this call sets it.
                build_env = ({"PNX_DEFINES": "PNX_FORCE_SCREEN_LOCK=1"}
                             if project.force_screen_lock else None)
                if not self._run([pebble, "build"], cwd=project.root, extra_env=build_env):
                    self.log.append("\n`pebble build` failed -- see above\n")
                    return
                # --vnc, even though nothing here speaks VNC: it is the flag that keeps
                # QEMU headless. Without it, pebble_tool/sdk/emulator.py's own
                # _spawn_qemu adds `-display sdl,show-cursor=on` and a real, visible
                # window opens on whatever machine is running this editor -- which is
                # also, evidently, where "6fps on emery" comes from: that is the SDL
                # window's own software-rendered compositing bottlenecking, not the
                # emulated hardware. screendump reads the framebuffer over the monitor
                # socket either way, so headless costs this panel nothing.
                self._run([pebble, "install", "--emulator", platform, "--vnc"], cwd=project.root)
            finally:
                self.busy = False

        threading.Thread(target=work, daemon=True).start()
        return True, None

    @staticmethod
    def _find_pbw(root):
        """The most recently built `.pbw` in `<project>/build/` -- pebble-tool's own
        output location. Newest by mtime rather than a name the editor guesses at,
        because the name comes from `package.json`/`appinfo.json` and this has no
        business re-deriving it."""
        build_dir = os.path.join(root, "build")
        if not os.path.isdir(build_dir):
            return None
        pbws = [os.path.join(build_dir, f) for f in os.listdir(build_dir)
                if f.endswith(".pbw")]
        return max(pbws, key=os.path.getmtime) if pbws else None

    def package(self, project):
        """Runs the SAME `pebble build` `start()` does, minus the emulator-only
        PNX_FORCE_SCREEN_LOCK override that call adds (see its own comment) -- a
        package is meant to be exactly what ships, not tuned for testing convenience.

        Separate from `start()`/`busy` rather than reusing them: someone packaging a
        release and someone testing in the emulator are two different things to be
        doing, and blocking one on the other (or interleaving their logs) would be a
        made-up conflict, not a real one.
        """
        pebble = Toolchain.pebble_path()
        if not pebble:
            return False, "pebble-tool is not installed -- see Settings"
        with self._lock:
            if self.package_busy:
                return False, "a package build is already running"
            self.package_busy = True
        self.package_log = []
        self.package_result = None

        def work():
            try:
                self.package_log.append("=== building assets ===\n")
                result = project.build()
                self.package_log.append(result["output"])
                if not result["ok"]:
                    self.package_log.append("\nasset build failed -- see above\n")
                    self.package_result = {"ok": False, "error": "asset build failed"}
                    return
                self.package_log.append("\n=== pebble build ===\n")
                if not self._run([pebble, "build"], cwd=project.root, log=self.package_log):
                    self.package_result = {"ok": False,
                                           "error": "`pebble build` failed -- see output"}
                    return
                pbw = self._find_pbw(project.root)
                if not pbw:
                    self.package_result = {"ok": False,
                                           "error": "pebble build succeeded but no .pbw "
                                                    "turned up in build/"}
                    return
                self.package_log.append(f"\n{pbw}\n")
                self.package_result = {"ok": True, "path": pbw,
                                       "size": os.path.getsize(pbw)}
            finally:
                self.package_busy = False

        threading.Thread(target=work, daemon=True).start()
        return True, None

    def package_status(self):
        return {"busy": self.package_busy, "log": "".join(self.package_log[-400:]),
                "result": self.package_result}

    def install_device(self, project, address):
        """`pebble build` + `pebble install --phone <address>` -- straight onto a real
        watch over the phone app's Developer Connection, the same way `start()` installs
        into QEMU. Applies PNX_FORCE_SCREEN_LOCK the same way `start()` does: a watch on
        a desk being tested has the same "no wrist, no ambient light, a real timeout
        looks like a hang" problem the emulator does, so this is a testing build, not a
        shipping one -- `package()` is that.
        """
        pebble = Toolchain.pebble_path()
        if not pebble:
            return False, "pebble-tool is not installed -- see Settings"
        if not address:
            return False, "no device address set -- see Settings"
        with self._lock:
            if self.install_busy:
                return False, "an install is already running"
            self.install_busy = True
        self.install_log = []
        self.install_result = None

        def work():
            try:
                self.install_log.append(f"=== building for {address} ===\n")
                result = project.build()
                self.install_log.append(result["output"])
                if not result["ok"]:
                    self.install_log.append("\nasset build failed -- see above\n")
                    self.install_result = {"ok": False, "error": "asset build failed"}
                    return
                build_env = ({"PNX_DEFINES": "PNX_FORCE_SCREEN_LOCK=1"}
                             if project.force_screen_lock else None)
                if not self._run([pebble, "build"], cwd=project.root, extra_env=build_env,
                                 log=self.install_log):
                    self.install_result = {"ok": False,
                                           "error": "`pebble build` failed -- see output"}
                    return
                self.install_log.append(f"\n=== installing to {address} ===\n")
                if not self._run([pebble, "install", "--phone", address],
                                 cwd=project.root, log=self.install_log):
                    self.install_result = {"ok": False,
                                           "error": "`pebble install` failed -- see output "
                                                    "(is the Pebble app's Developer "
                                                    "Connection on and pointed at this "
                                                    "address?)"}
                    return
                self.install_result = {"ok": True}
            finally:
                self.install_busy = False

        threading.Thread(target=work, daemon=True).start()
        return True, None

    def install_status(self):
        return {"busy": self.install_busy, "log": "".join(self.install_log[-400:]),
                "result": self.install_result}

    def start_logs(self, address):
        """Attaches to `pebble logs --phone <address>`, buffering lines for
        `logs_status()` to poll -- the log streaming docs/EDITOR.md's E3 flagged as
        missing even for the emulator panel ("shows each step's own output as it runs,
        not the app's ongoing `pebble logs`"). One stream at a time: attaching to a new
        address detaches whatever was running first, the same "only one thing is ever
        usefully in front" reasoning `stop()` uses for the emulator itself.
        """
        pebble = Toolchain.pebble_path()
        if not pebble:
            return False, "pebble-tool is not installed -- see Settings"
        if not address:
            return False, "no device address set -- see Settings"
        self.stop_logs()
        try:
            proc = subprocess.Popen([pebble, "logs", "--phone", address],
                                    env=self._pebble_env(), stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True, bufsize=1)
        except FileNotFoundError:
            return False, f"{pebble} not found"
        self._logs_proc = proc
        self._logs_buf = []

        def read():
            for line in proc.stdout:
                self._logs_buf.append(line)
                del self._logs_buf[:-500]
            # The loop ends when pebble-tool's own process exits -- detached, crashed,
            # or the phone dropped the connection. logs_status()'s "attached" reflects
            # that on the next poll; nothing here needs to notice it happening.

        threading.Thread(target=read, daemon=True).start()
        return True, None

    def stop_logs(self):
        if self._logs_proc is not None:
            with contextlib.suppress(Exception):
                self._logs_proc.terminate()
            self._logs_proc = None

    def logs_status(self):
        alive = self._logs_proc is not None and self._logs_proc.poll() is None
        return {"attached": alive, "lines": "".join(self._logs_buf[-500:])}

    def stop(self):
        """`pebble kill` stops every running emulator, not just one platform's --
        pebble-tool has no narrower command, and the fixed VNC/websockify ports it would
        allocate under `--vnc` (docs/EDITOR.md) mean only one is usefully in front at a
        time anyway. Every bridge goes with it: each one holds a connection into an
        emulator this is about to kill, and a bridge whose emulator is gone is not
        "slow", it is dead weight the next button press would just have to notice and
        replace anyway.
        """
        with self._bridges_lock:
            platforms = list(self._bridges)
        for p in platforms:
            self._kill_bridge(p)
        pebble = Toolchain.pebble_path()
        if not pebble:
            return False
        try:
            subprocess.run([pebble, "kill"], env=self._pebble_env(),
                           capture_output=True, timeout=15)
        except (OSError, subprocess.TimeoutExpired):
            return False
        return True

    def button(self, platform, buttons, action="push"):
        """`push` sets the FULL set of buttons currently held -- not additive, a real
        Pebble reports one bitmask -- so a caller tracking held state client-side (a
        keydown/keyup set, a mousedown/mouseup pair) always resends the complete set
        rather than one name at a time. `release` takes no buttons; it means "nothing is
        held any more", the same state a real hand lifted off every button reaches.
        """
        if action not in ("push", "release"):
            return False
        if action == "push" and (not buttons
                                 or any(b not in ("up", "select", "down", "back")
                                        for b in buttons)):
            return False
        # Checked here rather than left to `pebble emu-button` itself: with nothing
        # alive for `platform` the command can still exit 0 (it spawns a fresh QEMU
        # rather than refusing), which would silently boot an emulator no button on
        # this panel ever asked for.
        if self.info(platform) is None:
            return False

        line = "push " + " ".join(buttons) if action == "push" else "release"
        resp = self._bridge_send(platform, line)
        if resp is not None:
            return resp == "ok"

        # Bridge unavailable for some reason (interpreter not found, spawn failed,
        # died mid-session) -- fall back to the CLI. Slow (~670ms/call, see
        # pnx_emu_bridge.py), but a slow button beats a missing one.
        pebble = Toolchain.pebble_path()
        if not pebble:
            return False
        # --vnc here too, and not just in start(): pebble-tool's own connection logic
        # (ManagedEmulatorTransport._find_ports) treats a VNC-state mismatch against
        # the running QEMU as a reason to KILL IT AND RESPAWN A FRESH ONE -- headless,
        # started with --vnc, vs. this call without it would silently kill the emulator
        # on the first button press and boot a new, visible, un-installed one in its
        # place. That is not hypothetical: it is exactly what "buttons don't work, then
        # a new emulator window opens" turned out to be.
        cmd = [pebble, "emu-button", action] + (list(buttons) if action == "push" else []) \
            + ["--emulator", platform, "--vnc"]
        try:
            r = subprocess.run(cmd, env=self._pebble_env(), capture_output=True,
                               text=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            return False
        return r.returncode == 0

    # ------------------------------------------------------------ live frame

    def frame(self, platform):
        """One PNG frame off the running emulator's screen, or None.

        `screendump` over the QEMU monitor's plain-text protocol -- not VNC. Polled by
        the panel on an interval rather than pushed, which trades video smoothness for
        needing no persistent connection and no client-side decoder beyond an <img> tag.

        Two guards earned by a real bug, not written defensively up front: every call
        used to write to the SAME fixed path (`pnx-emu-{platform}.ppm`) and the panel's
        old poll timer fired on a fixed clock regardless of whether the previous request
        had finished. Once a single call took longer than the poll interval -- one slow
        screendump was enough -- two requests landed in two different server threads
        (ThreadingTCPServer, one thread per connection) both reading, writing and
        deleting that one file, each usually losing the race and timing out near the
        full 1.5s wait. That is what "2.6fps and massive lag" actually was: not the
        emulator running slowly, this editor fighting itself over a shared path. A
        per-call unique filename removes the collision; the per-platform lock below
        removes the wasted, doomed-to-lose-anyway concurrent attempts that caused it,
        for whatever client-side timing this is ever called under next.
        """
        info = self.info(platform)
        port = info and info.get("qemu", {}).get("monitor")
        if not port or pa.Image is None:
            return None
        lock = self._frame_locks.setdefault(platform, threading.Lock())
        if not lock.acquire(blocking=False):
            return None
        ppm = os.path.join(tempfile.gettempdir(),
                           f"pnx-emu-{platform}-{threading.get_ident()}-{time.time_ns()}.ppm")
        try:
            try:
                with socket.create_connection(("127.0.0.1", int(port)), timeout=1.0) as s:
                    s.settimeout(1.0)
                    with contextlib.suppress(OSError):
                        s.recv(4096)                    # the monitor's own banner
                    s.sendall(f"screendump {ppm}\n".encode())
                    with contextlib.suppress(OSError):
                        s.recv(4096)
            except OSError:
                return None

            deadline = time.time() + 1.5
            while time.time() < deadline:
                if os.path.exists(ppm) and os.path.getsize(ppm) > 0:
                    break
                time.sleep(0.02)
            else:
                return None

            try:
                with pa.Image.open(ppm) as img:
                    buf = io.BytesIO()
                    img.convert("RGB").save(buf, format="PNG")
                    return buf.getvalue()
            except OSError:
                return None
        finally:
            lock.release()
            with contextlib.suppress(OSError):
                os.remove(ppm)



EMULATOR = Emulator()
