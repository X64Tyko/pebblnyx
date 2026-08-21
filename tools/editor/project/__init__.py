"""`Project`, assembled from the domain mixins in this package.

The original tools/pnx_editor.py had this as one 4,566-line class with 136 methods --
everything the editor can do to a manifest, in one body. Split here by the section
comments that were already in the source (rendering, importing, tile roles, map
lifecycle, ...), regrouped where a comment's own methods didn't match its heading (the
old "samples" section, for instance, held both the actual sample methods and every
dialog method).

Every mixin operates on the same instance -- `self.man`, `self.root`, `self.res`, etc.,
all set by `CoreMixin.__init__`/`reload` -- and freely calls methods defined in the
other mixins through `self`, exactly as the methods of one class always could. Splitting
the *file* changes nothing about how the *class* behaves; `Project(...)` below is the
same object callers always got.
"""

from editor.project.core import CoreMixin
from editor.project.atlas import AtlasMixin
from editor.project.maps import MapsMixin
from editor.project.sprites import SpritesMixin
from editor.project.nine_slice import NineSliceMixin
from editor.project.fonts import FontsMixin
from editor.project.code import CodeMixin
from editor.project.build import BuildMixin
from editor.project.project_keys import ProjectKeysMixin
from editor.project.music import MusicMixin
from editor.project.dialog import DialogMixin
from editor.project.scenes import ScenesMixin
from editor.project.hud import HudMixin
from editor.project.hud_window import HudWindowMixin


class Project(CoreMixin, AtlasMixin, MapsMixin, SpritesMixin, NineSliceMixin, FontsMixin,
              CodeMixin, BuildMixin, ProjectKeysMixin, MusicMixin, DialogMixin,
              ScenesMixin, HudMixin, HudWindowMixin):
    """Everything the editor knows, reloaded from disk on demand."""

    PLATFORMS = {
        "emery":   {"w": 200, "h": 228, "bw": False, "round": False,
                    "ram": 131072, "resources": 262144, "speaker": True},
        "gabbro":  {"w": 260, "h": 260, "bw": False, "round": True,
                    "ram": 131072, "resources": 262144, "speaker": False},
        "flint":   {"w": 144, "h": 168, "bw": True,  "round": False,
                    "ram": 65536,  "resources": 262144, "speaker": True},
        "basalt":  {"w": 144, "h": 168, "bw": False, "round": False,
                    "ram": 65536,  "resources": 262144, "speaker": False},
        "chalk":   {"w": 180, "h": 180, "bw": False, "round": True,
                    "ram": 65536,  "resources": 262144, "speaker": False},
        "diorite": {"w": 144, "h": 168, "bw": True,  "round": False,
                    "ram": 65536,  "resources": 262144, "speaker": False},
        "aplite":  {"w": 144, "h": 168, "bw": True,  "round": False,
                    "ram": 24576,  "resources": 131072, "speaker": False},
    }
    CODE_EXTS = (".c", ".h", ".js", ".json", ".toml")
    LINT_TIMEOUT = 8
    FONT_DIRS = [
        # Linux
        "/usr/share/fonts", "/usr/local/share/fonts", "~/.fonts", "~/.local/share/fonts",
        # macOS
        "/System/Library/Fonts", "/Library/Fonts", "~/Library/Fonts",
        # Windows
        "C:/Windows/Fonts",
    ]
    DISPLAY_W, DISPLAY_H = 200, 228
    ATLAS_KEYS = ("sheet", "tile", "region", "max_tiles", "colorkey", "exclude", "offset")
