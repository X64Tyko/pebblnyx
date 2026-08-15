# The editor

A visual tool for building levels, managing assets, and packaging a build.

---

**E3, the emulator panel, is built -- not the way this section originally planned it.**
The plan was noVNC embedded over pebble-tool's own websockify bridge, ruled out below
("The embedded emulator, revisited") because the SDK snapshot checked at the time had no
QEMU image for the platform this framework built for. Two things changed
since: M9 made every platform a real `pebble build` target, and the SDK installed for
this project now ships a QEMU image for all seven, not the four found before. What
shipped is simpler than the noVNC plan besides: `Emulator` in `tools/pnx_editor.py` shells
out to `pebble build` / `pebble install --emulator` / `pebble emu-button`, and reads the
screen by polling QEMU's own monitor `screendump` -- the same call pebble-tool's own
`screenshot` command uses for a headless capture -- rather than embedding a VNC client.
No websockify, no noVNC, no video: a still image refreshed a few times a second. The
noVNC/websockify sections below describe pebble-tool's OWN mechanism accurately and are
kept for that reason, not because the editor uses them.

## Why it belongs in the plan

The framework's stated goal is that someone can build a game without thinking about the
code underneath. **Code is only half of what stops people.** The other half is content:
slicing sheets, choosing tile ids, hand-authoring ASCII maps, wiring warps, keeping
under a 256KB budget. A framework that removes the C but leaves the content work in a
text editor has moved the barrier, not lowered it.

It also pays for itself immediately on this project. Several things already done by hand
are exactly what the editor automates: rendering tile previews to a PNG to pick ids,
tracing why a warp did not fire, checking the resource budget after each change.

## The stack question, and pebble-tool's own emulator mechanism

This section describes the noVNC/websockify plan and is accurate about what
`pebble-tool` itself offers -- it is just not what `tools/pnx_editor.py` ended up using
for the screen (see the status note at the top of this document). Kept because the port
table below is real and because `EMULATOR.frame`'s `screendump`-over-the-monitor-socket
approach is a direct, simpler descendant of the same "several tools attach to one state
file" architecture.

The Pebble emulator is **QEMU with a VNC display**, and `pebble-tool` ships
**websockify** — a WebSocket-to-TCP bridge whose entire purpose is putting VNC in a
browser. From `pebble_tool/sdk/emulator.py`, a running emulator exposes:

| Port | Purpose |
|---|---|
| QEMU VNC | the watch screen |
| QEMU monitor | machine control |
| QEMU gdb | debugging |
| pypkjs WebSocket | install, logs, screenshots (the pebble protocol) |

All of it recorded in a state file, so several tools can attach to one running emulator
rather than fighting over it.

**A local web app was the right call anyway, just not for the VNC-embeds-for-free
reason.** `screendump` needs no browser-side protocol client at all -- an `<img>` tag
polling a server route is enough -- so the noVNC argument for this stack never actually
got exercised. What did: the asset pipeline is Python, `pebble` is Python, so a Python
server plus a browser UI reuses the pipeline as a library rather than reimplementing it,
which is the same reason every other tab in this editor is built this way.

## Architecture

```
browser UI
  ├─ manifest editor      tilesets, sprites, maps, dialog, audio
  ├─ map canvas           paint tiles, place entities, wire warps
  ├─ device panel         platform picker, polled screen, buttons
  └─ build panel          validate, budget, package
        │
   local Python server
  ├─ pipeline (imported, not shelled out)
  ├─ pebble build / install --emulator / emu-button / kill  (shelled out)
  └─ QEMU monitor socket   screendump, polled on request
```

Everything in that diagram shipped: manifest editor, map canvas and build panel are
E1/E2/E4/E5's inspector, painting and packaging tabs; sprite and code tabs from E12; the
device panel is `Emulator` and the `device` tab, both in `tools/pnx_editor.py`, and E3
in `docs/ROADMAP.md`.

**The editor is a GUI over the manifest.** That framing keeps it small: the manifest is
already the single source of truth, the pipeline already validates and packs, and the
generated header is already the contract with the C. The editor reads and writes one
file and calls existing code. It should never become a second, parallel definition of
what the content is.

## Staging

Ordered so each stage is independently useful, rather than a big-bang.

**E1 — Inspector (read-only).** Load a manifest, render every tileset with ids and
semantic tags, render each map, show the resource budget by category against the 256KB
cap, and list validation errors. No editing. This is the stage that replaces things
currently done by hand, and it is worth building even if nothing after it happens.

**E2 — Map editor.** Paint tiles, set flags, place entities, wire warps by clicking
source and destination. Validation runs live — the reachability flood fill highlights
unreachable doors *as you draw*, rather than at build time.

**E3 — Emulator panel. DONE, not the way this was originally planned.** Build → install
→ run → buttons, against `pebble`'s own QEMU emulator rather than the noVNC embed the
plan called for -- see the status note at the top of this document for why, and
`Emulator` in `tools/pnx_editor.py` for the mechanism. No log streaming yet: the panel
shows each step's own output as it runs, not the app's ongoing `pebble logs`.
`tools/host_harness.c` plus `tools/preview.py`'s PNG contact sheet -- real game logic,
real pixels, no hardware timing -- remain the faster, no-SDK-required check for "does
this map work" that this panel was never meant to replace.

**E4 — Asset import.** Drop a sheet in; slice it, pick a colour key, preview
quantisation to the 64-colour palette, preview dedup savings, choose frames. Currently
the fiddliest manual work by a distance.

**E5 — Package.** One button: validate, build, enforce the budget, produce the `.pbw`,
report size breakdown against every cap.

**E6 — Music editor.** A tracker view over the sequencer's data model. Only meaningful
after M4 exists, and probably the largest single piece.

**E7 — Font import. DONE.** Pick a TTF, choose a size and depth, and watch it rasterise.

The reason this needs a UI at all is that a font is the one asset that can pass every
check the pipeline makes and still be unusable. "Legible at 12px" is not a property bytes
have. So the tab is built around one interaction — drag the threshold, watch the text —
and everything else supports it:

- **The preview is the real thing.** The candidate is packed into an actual `PF` blob and
  parsed straight back, then drawn with the same metrics the runtime uses. Previewing from
  the rasteriser's intermediate output, or compositing in the browser, would mean two
  implementations of the same thing — and the moment they disagree the preview is worse
  than useless, because it still looks authoritative.
- **The background is editable, because text fails differently over different things.** A
  HUD sits over gameplay and has to survive whatever tile scrolls under it; dialogue sits
  on a flat panel and only has to be comfortable to read. The canvas is 200x228 at 1:1 to
  4x, and the background is a flat colour or a real map from the manifest at any scroll
  offset, with an optional text box. The text itself can be any `[dialog.*]` page.
- **It counts glyphs that rasterised blank.** That is how an imported font is usually
  quietly broken: the threshold eats the thin strokes, the build succeeds, and the watch
  shows gaps. The count goes red rather than waiting to be noticed.
- **A system typeface is copied into `art/fonts/`, not referenced in place.** A manifest
  pointing at `/usr/share/fonts` builds on one machine.

Deliberately not in this stage: **per-glyph pixel editing.** Rasterising at 12px does break
the occasional glyph and it will be wanted eventually, but it is a small editor of its own
and the threshold slider recovers most cases.

**E8 — One executable. DONE.** `tools/build_editor.py` freezes the editor into a single
~20 MB file that opens its own window. Verified running with no Python and no Pillow in the
environment: it serves the UI, rasterises a TTF, and runs the pipeline.

**Native window via pywebview, not Electron.** Both put the HTML in a real desktop window.
Electron does it by bundling its own Chromium — identical rendering everywhere, ~200 MB,
and a Node toolchain to maintain next to the Python one. pywebview drives the webview the
OS already ships (WebKitGTK, WebView2, WKWebView): ~1 MB, no second toolchain, and a binary
a tenth the size. The cost is depending on something the OS provides, so it is a soft
dependency — no webview means a browser tab, not a failure, and `--browser` forces that
path.

**This required one real fix.** `Project.build()` shelled out to
`sys.executable pnx_assets.py`, which is correct from a checkout and wrong from a frozen
binary, where `sys.executable` is the editor — pressing Build would have relaunched the
editor. It now imports the pipeline and calls it in-process, which is what this document
specified in the first place.

**E10 — Projects anywhere, and the engine in the editor. DONE.** The editor opens an
arbitrary folder, creates new projects, and — the part that makes it a tool rather than a
viewer — **carries the pnx engine inside it**.

A project on disk holds content and game code, not a copy of the framework. Before every
build the editor stages its own engine into `<project>/src/c/pnx`, so a game anywhere
compiles against the engine in the editor doing the compiling. That directory is a build
artefact: the scaffold gitignores it, and it is refreshed rather than trusted, so it
cannot drift from the editor that produced it.

The wrinkle this works around is waf: `pebble build` globs `src/c/**/*.c` relative to the
project and a glob pointing outside `top` does not work. The examples in this repository
use a symlink, which needs privileges on Windows and breaks the moment a project is zipped
and sent to someone. Staging sidesteps both. A symlinked `src/c/pnx` is detected and left
alone, so the in-repo examples keep picking up live engine edits.

**`.pknproj`** sits at each project root. JSON rather than TOML precisely because nobody
should hand-edit it — it records what the editor decided (name, manifest, the engine
version last built against), while `assets.toml` stays TOML and stays commented because it
records what the *author* decided. A folder with an `assets.toml` and no `.pknproj` still
opens: the format change was not allowed to orphan existing projects, and adopting one is a
button.

Verified end to end: a frozen binary in a stripped environment created a project in a
temporary directory, staged the engine out of its own bundle, ran the pipeline, and
produced a `.pbw`.

**E11 — Release CI. DONE.** `.github/workflows/editor.yml` tests, then builds and packages
for Linux, Windows, macOS Intel and macOS Apple silicon, and drafts a GitHub Release on a
`v*` tag.

- **Tests gate the builds.** Producing installers from a failing tree is worse than
  producing none, because the artefact looks legitimate.
- **The pipeline runs before the host tests**, because the generated header and the asset
  blobs are derived and therefore gitignored — and `tests/test_assets.c` includes that
  header. In the other order a fresh checkout does not compile.
- **CI fails if the font tests would skip.** `test_assets.py` skips when it cannot find a
  TTF, and a suite that quietly stops testing a feature is worse than one that fails.
- **The smoke test asserts the engine is in the bundle**, which is the failure this design
  is exposed to: PyInstaller silently omitting the added data would ship an editor that
  opens projects and cannot build one.
- **Pinned to the oldest GA runner images**, not `-latest`: a binary built against a newer
  OS will not run on an older one. It is also how a retired label bites — a job asking for
  a removed image is never scheduled, so it does not fail, it queues until GitHub's
  24-hour timeout, and `release` (which needs it) never runs at all.

**The window, and a crash worth recording.** Windows and macOS get a real webview through
pywebview. Linux gets neither — PyGObject through PyInstaller is distro-specific, and Qt's
WebEngine is Chromium again at ~200MB against a 20MB binary — so it opens a Chromium in
`--app` mode instead: a window with no browser furniture, from something already installed.

The first version of that passed `--user-data-dir` to get a private profile, because a
Chromium that is already running hands the URL over and exits, and the editor wanted a
process of its own to wait on. **It crashed a compositor.** A fresh profile has no GPU
preferences, so Chromium re-probes and can settle on a different device than the browser
that user normally runs; on a hybrid Intel + NVIDIA machine the compositor then could not
import its buffers, and Hyprland aborted inside Mesa's `dri_create_fence_fd` immediately
after `eglCreateImageKHR ... EGL_BAD_MATCH: createImageFromDmaBufs failed`, taking the
session with it.

The abort is a driver-and-compositor bug rather than the editor's. The *trigger* was the
editor's, and a content tool has no business being able to end someone's session — so it
reuses the user's own browser profile now, rendering the way their browser already renders.
Waiting on the process is what that costs, and it costs nothing, because the page's
heartbeat already reports when the UI is gone. `--browser` remains for anyone who wants a
plain tab.

**What each platform actually receives.** The binary is self-contained either way; this is
about the gesture after the download, which is where "run a Python script" used to be:

| Windows | `-setup.exe` from Inno Setup — Start menu entry, optional desktop shortcut, uninstaller. `PrivilegesRequired=lowest`, so it installs under `%LOCALAPPDATA%` with no UAC prompt at all: an unsigned installer demanding administrator rights is the exact shape of the thing people are told not to run. A `.zip` of the bare `.exe` ships alongside for anyone who keeps tools in a folder. |
|---|---|
| macOS | `.dmg` whose window holds the `.app` beside a symlink to `/Applications`, so the drag is obvious. It used to contain the bare `.app`, which invites running it from the read-only mounted volume — and those failures look like bugs in the app. |
| Linux | `.tar.gz` extracting to a folder: the binary, a `.desktop` entry, `install.sh` and `uninstall.sh`. No one Linux installer format fits — a `.deb` excludes Arch, an AppImage wants FUSE — so what ships is the binary plus a script that writes two files under `$HOME` and one that removes them. Root is never needed. |

Not signed or notarised — that needs an Apple Developer account and a Windows
code-signing certificate. Until then macOS wants one right-click → Open and Windows may
show SmartScreen.

**E12 — Sprites and Code. STUBS, on purpose.** Two tabs that exist so the shape is right
and the external detour is gone, not because they are finished.

*Sprites* is a pixel editor that **paints in ARGB2222** — the device's own 64 colours.
Same principle as the font preview: what is on the canvas is what ships, so two colours
chosen to contrast cannot quietly collapse on import. Pencil, fill, pick, erase, undo, a
grid, frames stacked vertically to match what `[[sprite]] frames` already expects, and an
actual-size view beside the zoomed one. It saves an ordinary PNG into the project, so the
result goes through the same importer as art made anywhere else and nothing downstream
knows where it came from.

*Code* is a file tree over the project plus an editor. Deliberately a tree over the
**project**, not something C-specific: when M8's Alloy scripting lands, `.js` joins the
same tree through the same endpoints. The staged engine is listed and readable — looking
up what `pnx_text_draw` takes should not mean going to find the framework — but is
read-only, because it is overwritten from the editor's copy before every build and an edit
there would vanish at the worst moment. `assets_gen.h` is marked generated for the same
reason. Paths are checked lexically against the project root, so a stray `..` cannot read
or write outside it.

Not in these yet: syntax highlighting, multiple open documents, selection tools,
onion-skinning, undo beyond 40 steps.

**E13 — The shell, modelled on VS Code / Rider. DONE.** Six top tabs was already crowded
and every capability added a seventh — the same crowding that made Toolchain a bad peer to
Maps. So the layout is now the one those tools converged on: an **activity rail** down the
left, a **contextual toolbar** carrying only what the current activity needs, the
**sidebar and document** area, one **shared output panel**, and a **status bar** showing
project, engine version, budget and SDK state at a glance.

The output panel is shared rather than per-tab because a build result is not a property of
whichever tab you were on when you pressed Build — it used to be a box inside the Maps
sidebar, invisible from everywhere else.

**E14 — Four things that make it usable rather than demonstrable. DONE.**

*The budget is live.* It used to reflect the last build, which means a map could be grown
past the appstore cap and only say so hours later, after the work. Now maps are priced
**exactly** from the manifest — the blob layout is arithmetic on width, height, warps and
flag overrides — and everything else reuses the blob already on disk, because
re-quantising a tileset on every keystroke would cost seconds and change nothing. It
matches the pipeline to the byte on the example. Overrides are read from the previous
build rather than assumed zero, because assuming zero *under*-estimates and
underestimating is the one direction that matters: it is what lets someone sail past the
cap believing they are inside it. The status bar goes red at the moment it is crossed,
from any tab.

*Tiles are chosen, not accepted.* The Import tab renders the whole slice with each cell
marked as it would be packed — kept, a free duplicate, empty — and any cell can be
dropped by clicking it, with the price updating as you go. Exclusions are recorded in the
manifest as region-relative indices, and are applied **before** dedup, because a grid of
sheet positions is what the author is looking at; dropping a deduplicated tile instead
would silently take every other position sharing its pixels.

*Engine editing is opt-in and honest about the trade.* Unlocking is not just write
permission — it stops the editor restaging the engine, so edits survive, and the project
stops receiving engine fixes. Both halves are stated, because stating only the first
would be a pleasant surprise followed by an unpleasant one. `.pknproj` records the version
it forked from and when.

*The code editor highlights and checks.* Highlighting is a `<pre>` overlay behind a
transparent textarea, so caret, selection, undo and IME remain the browser's job. Three
checks: bracket balance, unterminated literals, and unknown `pnx_*`/`PNX_*` identifiers
matched against symbols parsed from the engine headers and the generated header — with
an edit-distance suggestion. That last one exists because of a real mistake: the project
scaffold called `pnx_platform_exit`, the name is `pnx_platform_quit`, and nothing said so
until a full ARM compile failed.

**Three bugs this work surfaced**, all of the same family — silent, and invisible to
Python:

- `PAGE` is an r-string, so `'\\n'` in the inline JavaScript reached the browser as a
  backslash and an `n` rather than a newline, and `\\'` terminated a string early.
- The code editor's `analyse()` shadowed the importer's. The later definition won and the
  Import tab's statistics panel silently went blank.
- Three leftover `className=` assignments from the old tab bar wiped the `act` class off
  half the activity rail the first time Sprites was opened.

`tests/test_assets.py` now checks the inline page for all three: doubled escapes,
duplicate top-level names, and unbalanced tags. It was worth the twenty lines — each of
these cost more than that to find by hand.

## Honest scope warning

**A full editor can easily exceed the engine in effort.** E1–E3 are modest because they
mostly surface work the pipeline already does. E4 and E6 are real applications in their
own right — sheet slicing with a good UX, and a tracker, are each weeks.

Two sequencing risks worth naming:

1. **The editor must not delay the game.** The market research was unambiguous that
   adoption follows a shipped game, not tooling. Tooling that speeds *our* content work
   earns its place immediately; tooling built for hypothetical other developers should
   wait until there is evidence they exist.
2. **Do not build the editor before the content model is proven.** E2's map editor
   encodes assumptions about what a map *is*. If the game later needs layered maps,
   scripted events or per-tile metadata, an editor built too early gets rewritten. E1 is
   safe now because inspection makes no such commitment.

Recommended: **E1 alongside M2, E3 after M3, E2 once the first real maps exist, and
E4–E6 only when the pain justifies them.**

## The toolchain: what the licence allows, and what the editor does

Freezing the editor removed the Python install as a barrier. The larger one is that
**producing a `.pbw` needs the Pebble SDK**. Two useful facts, both checked rather than
assumed:

- **It is one install, not two.** The SDK download carries its own ARM toolchain at
  `<sdk>/toolchain/arm-none-eabi`. Nothing separate has to be sourced. It is ~767MB.
- **`pebble-tool` and the SDK have completely different licences.** The tool is MIT
  ([coredevices/pebble-tool](https://github.com/coredevices/pebble-tool)). The SDK is not.

### What the Pebble Developer License actually says

The clauses that decide the design, quoted from
[the licence](https://developer.repebble.com/legal/sdk-license/index.html):

> **§3.** Pebble grants to you a limited, **non-transferable, non-sublicensable**,
> non-exclusive, worldwide, license to use the Pebble SDK solely to develop, test and
> operate applications that will run on the Pebble Platform. **You will have no right to
> license, distribute or otherwise transfer the Pebble SDK** or any rights therein.

> **§5(f).** You will not: … **distribute the Pebble SDK** (other than the incorporation
> of distributable elements of the Pebble SDK in your application …)

Two conclusions follow directly:

1. **Bundling the SDK into the editor is out.** Not a grey area — §3 and §5(f) both
   prohibit it. Any plan to ship a single binary containing the toolchain is dead.
2. **The licence is granted to the person, not to the tool.** It is non-transferable and
   non-sublicensable, so the editor cannot hold a licence on a user's behalf or accept on
   their behalf. Each user's acceptance has to be genuinely their own.

Worth noting §4: open-source components inside the SDK are governed by their own licences
"and not this Agreement", so the GPL'd ARM toolchain is not itself restricted by the above.
Separating it out would mean taking on the GPL's own distribution obligations for no gain,
given the approach below works.

### So the editor drives Pebble's own tool

The Settings tab shows both documents, requires an explicit acceptance, and then runs
`pebble sdk install`. The bytes travel from Pebble's server to the user's disk; the editor
never holds or transfers a copy. This is the user doing what they would have done by hand,
with the typing removed — which is the barrier worth removing, and the only part of it that
was ever ours to remove.

It also means the editor never touches `sdk.repebble.com` itself, which sidesteps the
Terms of Use clause prohibiting "any robot, spider, scraper or other automated means to
access the Site(s)". That clause sits in a list about forum conduct and uploads, so it
probably was not aimed at build tooling — but the list is explicitly "not complete or
exclusive", and there is no reason to test it when the first-party tool will do the
download for us.

`pebble-tool` being MIT is what makes the first step safe: when it is absent the editor
installs it via whichever of `uv`, `pipx` or `pip` the machine has, and says so plainly
when it has none.

**The acceptance gate is deliberately stricter than the official CLI's.** `pebble sdk
install` only *prints* the licence links and proceeds. The editor requires a real click and
records what was accepted and when, in `~/.config/pebblnyx/`. Given the grant is personal
and non-transferable, a tool acting on the user's behalf should be able to show that the
user actually agreed.

### What it does not do

**It does not auto-update.** A newer SDK is reported in the status panel and updating is a
button; it never happens on its own. A toolchain that changes under a project between two
builds is its own category of confusing bug, and the roadmap already depends on `4.17`
behaviour in measured ways.

## Open questions

- **Does the editor own the manifest file, or edit it in place?** RESOLVED: in place.
  `assets.toml` stays the file an author reads and diffs; the editor writes into it
  rather than owning a parallel representation, which is also what made E16's audit able
  to find manifests the editor itself had made unbuildable -- there was only one file to
  be wrong.

- **Sharing one emulator instance with the CLI.** RESOLVED, by construction rather than
  by writing anything: `Emulator` only ever reads pebble-tool's own state file
  (`get_emulator_info_path()`, a single machine-wide JSON) and shells out to `pebble`
  itself for every mutation. An emulator started from a terminal with `pebble install
  --emulator basalt` shows up in the panel as running; `pebble kill` from either place
  stops what the other started. There was never a second copy of this state to keep in
  sync.

- **How much a QEMU run can actually be trusted for anything measured.** Answered, and
  the answer is "logic yes, performance no." Functional correctness checks out in
  full: a real `pebble build` / `install --emulator` / `screendump` round trip against
  both `examples/stressbench` and `resonant` boots, installs, navigates and renders
  correctly, buttons work, and `PNX_FORCE_SCREEN_LOCK` compiles and behaves as coded.

  Frame rate does not check out. `resonant`'s own in-app FPS overlay read a stable
  **8.1fps on `emery`** over 10+ seconds -- not editor polling, a number the firmware
  itself computed -- and `stressbench`'s diagnostics frame counter agreed independently
  at **~10.3fps** (93 frames over 9 wall-clock seconds), against pnx's own measured
  real-hardware ceiling of ~26.8fps (`docs/MEASUREMENTS.md`). The SAME `stressbench`
  build installed on `basalt` (cortex-m4, QEMU's mature board) visibly covered far more
  of the map in the same 9 seconds -- qualitatively, dramatically faster than `emery`
  (cortex-m33) under identical content and identical host hardware. That points at
  QEMU's own cortex-m33 emulation being the bottleneck, not this editor's plumbing
  (already the case that every other fix in this section addressed): cortex-m33 support
  in upstream QEMU is far newer than the cortex-m3/m4 TCG path basalt/aplite ride on,
  and Core Devices' own public work on a browser build of this same firmware
  independently documents "slow TCI execution" as a real, acknowledged constraint (a
  1-second minimum key-hold was added to compensate) -- a different QEMU backend
  (TCI vs TCG) but the same underlying fact, a new board target that has not had
  emulation speed as a design goal yet. `coredevices/PebbleOS#1542` (open, unrelated to
  speed -- gabbro's own QEMU screen has missing pixels) is separate evidence the newer
  boards' QEMU models are not considered fully settled even by their own maintainers.

  So: this panel is real signal for "does the app do the right thing", not for "does it
  hit its frame budget" -- that claim still needs real `emery` hardware, the same way
  M0's latency numbers did (`docs/ROADMAP.md`). Nothing here should be read as a
  regression in the app being tested if it looks slow specifically on `emery`.

## Installed but not found: `qemu-pebble` and PATH

`pebble sdk install` does install the emulator -- `<sdk>/toolchain/bin/qemu-pebble`
runs fine invoked directly -- it just does not put that directory on `PATH`, and
pebble-tool resolves `qemu-pebble` as a bare command via ordinary PATH lookup
(`PEBBLE_QEMU_PATH`, `pebble_tool/sdk/emulator.py`) rather than through the same
SDK-aware path resolution `pebble build` uses internally. So a build succeeds while
every emulator command silently fails to find the binary -- the reported symptom
("installing the SDK doesn't install the emulator") was a real, reproducible bug, just
not the one its own description suggested.

Fixed in `Emulator._pebble_env` by mirroring pebble-tool's own layout convention
(`pebble_tool.util.get_persist_dir`, and the `SDKs/current` symlink `sdk/manager.py`
maintains) rather than asking an author to edit their PATH: every `pebble` subprocess
this editor spawns gets that toolchain's `bin` prepended. Confirmed against the actual
installed SDK, not inferred -- see the "How much a QEMU run can be trusted" note above.

## The embedded emulator, revisited

Checked here, while building the panel, against `sdk-core/pebble/*/qemu/
qemu_micro_flash.bin` on the SDK this project has installed: every one of the seven
platforms in `PLATFORMS` (`tools/pnx_editor.py`) has one, not the four (**aplite,
basalt, chalk, diorite**) an earlier SDK snapshot had when this section first concluded
"not currently possible" -- `emery`, `gabbro` and `flint` have since gained real
emulator images too. Which is
exactly why `Emulator` never hardcodes a supported-platform list: that list is a fact
about whichever SDK is installed on the machine running the editor, not about this
codebase, and it has already changed once since this document last checked.

So "test without pushing to device" IS deliverable by embedding the Pebble emulator now,
for whichever platforms an author's own SDK covers -- see the status note at the top of
this document for what actually shipped. The host-platform-seam option this section used
to recommend instead (`pnx_platform_host.c`'s flat buffer, no firmware, no 26.8fps
ceiling, no flash timing) is not obsoleted by that: it is still the faster, no-SDK-needed
answer to "is this map walkable, does this warp fire", and the emulator panel is for the
question the host seam cannot answer at all -- what does this actually look and feel
like running.
