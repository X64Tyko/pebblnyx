"""Generic top-level manifest keys ([project] table entries)."""

import os
import re


class ProjectKeysMixin:
    @property
    def force_screen_lock(self):
        return bool(self.project.get("force_screen_lock", False))

    @property
    def device_address(self):
        # host:port of a phone running the Pebble app's Developer Connection --
        # Emulator.install_device()'s `pebble install --phone` target. A project
        # setting, not a one-off form field, because the address is the same every
        # session until the phone's own IP changes.
        return self.project.get("device_address", "")

    def set_project(self, key, value):
        """Rewrite one key of [project], creating it if the table lacks it."""
        if key not in ("name", "budget_bytes", "resources", "header", "force_screen_lock",
                       "device_address"):
            raise ValueError(f"{key!r} is not a [project] key the editor sets")

        if key == "budget_bytes":
            value = int(value)
            # The device ceiling, not the appstore one: someone shipping outside the
            # appstore may legitimately want the larger cap, and the pipeline already
            # reports against both. Below a floor the number stops meaning anything.
            if not 1024 <= value <= 1048576:
                raise ValueError("the budget must be between 1 KB and the device's 1 MB "
                                 "ceiling")
            line = f"budget_bytes = {value}"
        elif key == "force_screen_lock":
            # A TOML bool, not a quoted string -- read back by Emulator.start() as
            # self.project.get("force_screen_lock", False), same as every other
            # [project] key, and turned into PNX_DEFINES=PNX_FORCE_SCREEN_LOCK=1 for
            # the emulator's own `pebble build` only (pnx_config.h's own comment says
            # why this is a build knob, not a manifest content decision like
            # orientation -- nothing about it belongs in a shipped binary).
            value = str(value).strip().lower() in ("1", "true", "yes", "on")
            line = f"force_screen_lock = {'true' if value else 'false'}"
        elif key == "device_address":
            # host:port of a phone's Developer Connection. Allowed empty, unlike the
            # other string keys below -- "not configured yet" and "just cleared it" are
            # both ordinary states, not errors.
            value = str(value).strip()
            line = f'device_address = "{value}"'
        else:
            value = str(value).strip()
            if not value:
                raise ValueError(f"{key} cannot be empty")
            if key == "name" and not re.fullmatch(r"[A-Za-z0-9 _.-]+", value):
                raise ValueError("a project name may hold letters, digits, spaces, "
                                 "underscores, dots and hyphens")
            if key in ("resources", "header") and os.path.isabs(value):
                raise ValueError(f"{key} is a path inside the project, not an absolute "
                                 f"one")
            line = f'{key} = "{value}"'

        lines = open(self.path).read().split("\n")
        start = next((i for i, l in enumerate(lines) if l.strip() == "[project]"), None)
        if start is None:
            raise ValueError("the manifest has no [project] table")
        end = next((j for j in range(start + 1, len(lines))
                    if lines[j].lstrip().startswith("[")), len(lines))

        at = next((j for j in range(start + 1, end)
                   if re.match(rf"\s*{key}\s*=", lines[j])), None)
        if at is not None:
            lines[at] = line
        else:
            # One past the LAST actual `key = value` line, not "walk back from the block's
            # end over blank lines" -- the block's end can include trailing comments that
            # belong to whatever table follows [project], since detection only looks for
            # the next `[`. A key that has never appeared before must not land among them.
            at = start + 1
            for j in range(start + 1, end):
                if re.match(r"\s*[A-Za-z_][A-Za-z0-9_]*\s*=", lines[j]):
                    at = j + 1
            lines[at:at] = [line]
        with open(self.path, "w") as f:
            f.write("\n".join(lines))
        self.reload()

