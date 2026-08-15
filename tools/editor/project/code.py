"""The code tab: file tree, read/write, lint, and the autocomplete symbol table."""

import os
import re
import shutil
import subprocess

import pnx_project as pp                                    # noqa: E402


class CodeMixin:
    def code_symbols(self):
        """Every `pnx_*` / `PNX_*` name the engine and the generated header declare.

        This exists because of a mistake made writing the project scaffold: it called
        `pnx_platform_exit`, the real name is `pnx_platform_quit`, and nothing said so
        until a full ARM compile failed. On a watch that round trip is slow enough to
        matter, and the check that prevents it is a set membership test.
        """
        names = set()
        roots = [os.path.join(self.root, "src", "c", "pnx")]
        gen = os.path.join(self.root,
                           self.project.get("header", "src/c/assets_gen.h"))
        files = []
        for r in roots:
            if os.path.isdir(r):
                for dp, _dn, fs in os.walk(r, followlinks=True):
                    files += [os.path.join(dp, f) for f in fs if f.endswith((".h", ".c"))]
        if os.path.exists(gen):
            files.append(gen)

        ident = re.compile(r"\b((?:pnx|Pnx|PNX)[A-Za-z0-9_]*)")
        for path in files:
            try:
                with open(path, encoding="utf-8", errors="replace") as f:
                    for m in ident.finditer(f.read()):
                        names.add(m.group(1))
            except OSError:
                continue

        # Names the generated header produces that do not carry a pnx prefix -- tile
        # roles, sprite dimensions, scene and dialog ids.
        if os.path.exists(gen):
            with open(gen, encoding="utf-8", errors="replace") as f:
                for m in re.finditer(r"^#define\s+([A-Z][A-Z0-9_]*)", f.read(), re.M):
                    names.add(m.group(1))
        return sorted(names)

    def _safe(self, rel):
        """Resolve a project-relative path, refusing anything outside the project.

        The editor serves on localhost, but "only local" is not the same as "only this
        project" -- a stray `../../../..` in a request should not be able to read or
        write the user's home directory.

        Checked LEXICALLY, with abspath rather than realpath. abspath collapses `..`, so
        traversal is still blocked, but it does not resolve symlinks -- and it must not,
        because this repository's own examples reach the engine through a symlinked
        `src/c/pnx`. Resolving would put those files outside the project and make the
        engine unreadable in exactly the tree where reading it is most useful.
        """
        full = os.path.abspath(os.path.join(self.root, rel))
        root = os.path.abspath(self.root)
        if full != root and not full.startswith(root + os.sep):
            raise ValueError(f"{rel!r} is outside the project")
        return full

    def code_tree(self):
        """Every source file in the project, engine last and marked read-only."""
        out = []
        for base in ("src", "."):
            start = os.path.join(self.root, base)
            if not os.path.isdir(start):
                continue
            # followlinks, because the engine reaches a project either as a staged copy
            # or -- in this repository -- as a symlink, and both should list.
            for dirpath, dirnames, files in os.walk(start, followlinks=True):
                dirnames[:] = [d for d in dirnames
                               if d not in ("build", ".git", "__pycache__", "resources",
                                            "art", "node_modules")]
                if base == ".":
                    dirnames[:] = []          # top level only, for assets.toml et al
                for fn in sorted(files):
                    if not fn.endswith(self.CODE_EXTS):
                        continue
                    full = os.path.join(dirpath, fn)
                    rel = os.path.relpath(full, self.root)
                    if rel in [e["path"] for e in out]:
                        continue
                    engine = rel.replace("\\", "/").startswith("src/c/pnx/")
                    out.append({
                        "path": rel, "name": fn,
                        "dir": os.path.dirname(rel) or ".",
                        "bytes": os.path.getsize(full),
                        "engine": engine,
                        "editable": not engine,
                        "generated": fn == "assets_gen.h",
                    })
        out.sort(key=lambda e: (e["engine"], e["dir"], e["name"]))
        return out

    @staticmethod
    def _compiler():
        for name in ("cc", "clang", "gcc"):
            found = shutil.which(name)
            if found:
                return found
        return None

    def code_lint(self, rel):
        """Syntax-check one file against the engine, and return diagnostics."""
        try:
            path = self._safe(rel)
        except ValueError as e:
            return {"ok": False, "why": str(e)}
        if not path.endswith((".c", ".h")):
            return {"ok": False, "why": "only C sources are checked"}

        cc = self._compiler()
        if not cc:
            return {"ok": False,
                    "why": "no C compiler on PATH -- install one to get real diagnostics"}

        cmd = [cc, "-fsyntax-only", "-std=c11", "-Wall", "-Wextra",
               "-Wno-unused-parameter",
               # The host half of the platform seam. Without it the SDK headers would be
               # wanted, and they are not here.
               "-DPNX_PLATFORM_HOST",
               # The generated header only defines this when it can include the SDK's
               # resource ids, which do not exist on a host. A stub keeps the file
               # compiling so the diagnostics are about the code someone wrote.
               "-DPNX_ASSET_RESOURCE_TABLE={0}",
               "-I", os.path.join(self.root, "src", "c"),
               "-I", self.root]
        if path.endswith(".h"):
            cmd += ["-x", "c"]                   # a header is not a translation unit
        cmd.append(path)

        try:
            r = subprocess.run(cmd, capture_output=True, text=True,
                               timeout=self.LINT_TIMEOUT)
        except subprocess.TimeoutExpired:
            return {"ok": False, "why": f"the compiler did not finish in "
                                        f"{self.LINT_TIMEOUT}s"}
        except Exception as e:                           # noqa: BLE001
            return {"ok": False, "why": f"could not run {os.path.basename(cc)}: {e}"}

        # `file:line:col: level: message`, and only for THIS file: an error inside an
        # engine header is a real diagnostic but it is not something the author can act
        # on here, so it is reported without a line to jump to.
        out = []
        pattern = re.compile(r"^(.*?):(\d+):(?:(\d+):)?\s*(error|warning|note):\s*(.*)$")
        for line in (r.stderr or "").splitlines():
            m = pattern.match(line)
            if not m:
                continue
            where, ln, col, level, msg = m.groups()
            if level == "note":
                # Notes belong to the diagnostic above them -- "did you mean X" is the
                # useful half of an implicit-declaration error.
                if out:
                    out[-1]["note"] = msg
                continue
            try:
                here = os.path.samefile(where, path)
            except OSError:
                here = False                     # a path the compiler invented, or gone
            out.append({"line": int(ln) if here else 0,
                        "col": int(col or 0), "level": level, "msg": msg,
                        "file": os.path.relpath(where, self.root)
                        if os.path.exists(where) else where})

        return {"ok": True, "compiler": os.path.basename(cc), "diags": out,
                "clean": not out}

    def _engine_owned(self):
        try:
            return bool(pp.load(self.root).get("engine_owned"))
        except Exception:                                # noqa: BLE001
            return False

    def code_read(self, rel):
        full = self._safe(rel)
        with open(full, encoding="utf-8", errors="replace") as f:
            text = f.read()
        engine = rel.replace("\\", "/").startswith("src/c/pnx/")
        owned = engine and self._engine_owned()

        if engine and owned:
            note = ("This project owns its engine copy — edits are kept, and it no "
                    "longer tracks the editor.")
        elif engine:
            note = ("The engine is restaged from the editor before every build, so edits "
                    "here would be overwritten. Unlock it in Settings to take ownership.")
        elif rel.endswith("assets_gen.h"):
            note = "Generated from assets.toml — edits are overwritten by the next build."
        else:
            note = ""

        return {"path": rel, "text": text, "editable": (not engine) or owned,
                "engine": engine, "note": note}

    def code_write(self, rel, text):
        if rel.replace("\\", "/").startswith("src/c/pnx/") and not self._engine_owned():
            raise ValueError("the staged engine is restaged on every build, so edits "
                             "would be lost. Take ownership of it in Settings first.")
        full = self._safe(rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        return {"ok": True, "bytes": len(text.encode("utf-8"))}

