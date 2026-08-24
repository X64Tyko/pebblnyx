// Decides how this page reaches the editor's backend, then makes it so. Runs before
// app.js -- see index.html's script order -- because app.js calls `load()`
// unconditionally at the bottom of its own file the instant it's parsed, and that
// immediately does `fetch('/api/state')`. api.js's fetch override already blocks any
// /api/ request behind a readiness promise; this file's only job at parse time is to
// hand that promise to API.configure() synchronously, then spend as long as it needs
// resolving it.
//
// Three outcomes:
//   - This page IS the native companion's own served page (checked first, via a
//     same-origin /api/ping) -> nothing to do, same-origin fetch already works.
//   - Otherwise, this is a static-hosted page (GitHub Pages, or a local `python -m
//     http.server` for testing) -> boot Pyodide in-page, so a user can pick a real
//     local folder and edit it with zero install. A companion MAY also be running
//     locally for build/device/emulator/SDK work; that's detected independently and
//     doesn't block the above.
(function () {
  const PYODIDE_CDN = "https://cdn.jsdelivr.net/pyodide/v314.0.5/full/";
  const COMPANION_PORT = 8765;           // matches server.py's --port default
  const PING_TIMEOUT_MS = 800;
  const DB_NAME = "pebblnyx-web";
  const STORE_NAME = "recent-projects";

  // Both probes use API.realFetch, not the (possibly-overridden) global fetch: a
  // same-origin /api/ping through the shim would await `ready` -- the very promise
  // this function's result decides -- which is a deadlock, not just a redundant hop.
  async function pingSameOrigin() {
    try {
      const ctrl = new AbortController();
      const timer = setTimeout(() => ctrl.abort(), PING_TIMEOUT_MS);
      const r = await window.API.realFetch("/api/ping", { cache: "no-store", signal: ctrl.signal });
      clearTimeout(timer);
      if (!r.ok) return false;
      const d = await r.json();
      return d && d.app === "pebblnyx-editor";
    } catch (_) {
      return false;
    }
  }

  async function pingCompanion() {
    const origin = `http://127.0.0.1:${COMPANION_PORT}`;
    try {
      const ctrl = new AbortController();
      const timer = setTimeout(() => ctrl.abort(), PING_TIMEOUT_MS);
      const r = await window.API.realFetch(origin + "/api/ping", {
        cache: "no-store", mode: "cors", credentials: "omit", signal: ctrl.signal,
      });
      clearTimeout(timer);
      if (!r.ok) return null;
      const d = await r.json();
      return d && d.app === "pebblnyx-editor" ? origin : null;
    } catch (_) {
      return null;
    }
  }

  // ---------------------------------------------------------------- IndexedDB
  function openDb() {
    return new Promise((resolve, reject) => {
      const req = indexedDB.open(DB_NAME, 1);
      req.onupgradeneeded = () => {
        req.result.createObjectStore(STORE_NAME, { keyPath: "id" });
      };
      req.onsuccess = () => resolve(req.result);
      req.onerror = () => reject(req.error);
    });
  }

  async function dbGetAll() {
    const db = await openDb();
    return new Promise((resolve, reject) => {
      const tx = db.transaction(STORE_NAME, "readonly");
      const req = tx.objectStore(STORE_NAME).getAll();
      req.onsuccess = () => resolve(req.result || []);
      req.onerror = () => reject(req.error);
    });
  }

  async function dbPut(entry) {
    const db = await openDb();
    return new Promise((resolve, reject) => {
      const tx = db.transaction(STORE_NAME, "readwrite");
      tx.objectStore(STORE_NAME).put(entry);
      tx.oncomplete = () => resolve();
      tx.onerror = () => reject(tx.error);
    });
  }

  // ------------------------------------------------------------ project UI (pyodide mode)
  // The existing #picker panel browses server-side paths by typing them (drawPicker() in
  // app.js, via /api/project/browse) -- there is no server here, so it's replaced with a
  // real folder picker. openProject()/load() in app.js are reused completely unchanged;
  // once a folder is mounted, opening it is the same /api/project/open POST app.js
  // already makes.
  let mountedFs = null;
  const MOUNT_PATH = "/mnt/proj";

  async function syncAfterWrite() {
    if (mountedFs) await mountedFs.syncfs();
  }

  async function pickAndMount(pyodide) {
    // Requesting "readwrite" here, rather than a separate handle.requestPermission()
    // call after the picker resolves, is deliberate: that second call needs its own
    // fresh user activation, and Chrome doesn't reliably treat the activation from
    // opening the picker as still current once its promise resolves -- surfaced as
    // "Failed to execute 'requestPermission' ... User activation is required" even
    // though the user just picked a folder. Asking for the mode up front grants it as
    // part of the same gesture the picker itself already consumed.
    const handle = await window.showDirectoryPicker({ mode: "readwrite" });
    if (mountedFs) {
      try { pyodide.FS.unmount(MOUNT_PATH); } catch (_) { /* nothing was mounted yet */ }
    }
    mountedFs = await pyodide.mountNativeFS(MOUNT_PATH, handle);
    await dbPut({
      id: handle.name + ":" + Date.now(),
      name: handle.name,
      handle,
      lastOpened: Date.now(),
    });
    return handle;
  }

  function renderPyodideProjectUI(pyodide) {
    const openBtn = document.getElementById("projopen");
    const newBtn = document.getElementById("projnew");
    const picker = document.getElementById("picker");
    if (!openBtn) return; // this build of index.html doesn't have the project panel

    openBtn.textContent = "Open a folder on this device…";
    openBtn.onclick = async () => {
      try {
        await pickAndMount(pyodide);
        await window.openProject(MOUNT_PATH);
      } catch (e) {
        alert("Couldn't open that folder: " + e.message);
      }
    };

    newBtn.textContent = "New project in a folder on this device…";
    newBtn.onclick = async () => {
      try {
        // showDirectoryPicker (inside pickAndMount) has to run first, before any
        // prompt() -- prompt() blocks on the user actually reading and typing, and by
        // the time it returns, Chrome's transient user-activation from this very click
        // has typically already expired, so the picker call after it fails outright
        // ("Must be handling a user gesture to show a file picker"). Name/author don't
        // touch the filesystem, so asking for them after the folder is picked is safe.
        const handle = await pickAndMount(pyodide);
        const name = prompt("Project name?");
        if (!name) return;
        const author = prompt("Author (optional)?") || "";
        const r = await (await fetch("/api/project/create", {
          method: "POST", headers: { "content-type": "application/json" },
          body: JSON.stringify({ parent: "/mnt", folder: "proj", name, author }),
        })).json();
        if (!r.ok) { alert(r.error); return; }
        location.reload();
      } catch (e) {
        alert("Couldn't create a project there: " + e.message);
      }
    };

    // The server-path-browsing picker panel (#picker) has no meaning without a server;
    // it never opens in this mode since openBtn/newBtn no longer call showPicker().
    if (picker) picker.style.display = "none";

    renderRecent(pyodide);
  }

  async function renderRecent(pyodide) {
    const wrap = document.getElementById("recent");
    if (!wrap) return;
    const entries = (await dbGetAll()).sort((a, b) => b.lastOpened - a.lastOpened);
    if (!entries.length) { wrap.innerHTML = "<small>—</small>"; return; }
    wrap.innerHTML = entries.map((e, i) =>
      `<button data-i="${i}">${e.name}</button>`).join("");
    for (const btn of wrap.querySelectorAll("button")) {
      btn.onclick = async () => {
        const entry = entries[btn.dataset.i];
        try {
          const state = await entry.handle.queryPermission({ mode: "readwrite" });
          const granted = state === "granted"
            || (await entry.handle.requestPermission({ mode: "readwrite" })) === "granted";
          if (!granted) { alert("Permission to that folder was not granted."); return; }
          if (mountedFs) {
            try { pyodide.FS.unmount(MOUNT_PATH); } catch (_) { /* ignore */ }
          }
          mountedFs = await pyodide.mountNativeFS(MOUNT_PATH, entry.handle);
          entry.lastOpened = Date.now();
          await dbPut(entry);
          await window.openProject(MOUNT_PATH);
        } catch (e) {
          alert("Couldn't reopen that project: " + e.message
            + " -- pick it again with “Open a folder”.");
        }
      };
    }
  }

  // ---------------------------------------------------------------- Pyodide bootstrap
  function showLoading(text) {
    let el = document.getElementById("pnx-web-loading");
    if (!el) {
      el = document.createElement("div");
      el.id = "pnx-web-loading";
      el.style.cssText = "position:fixed;inset:0;display:flex;align-items:center;"
        + "justify-content:center;background:#111;color:#ddd;font:14px sans-serif;z-index:9999";
      document.body.appendChild(el);
    }
    el.textContent = text;
    el.style.display = "flex";
  }

  function hideLoading() {
    const el = document.getElementById("pnx-web-loading");
    if (el) el.style.display = "none";
  }

  function loadScript(src) {
    return new Promise((resolve, reject) => {
      const s = document.createElement("script");
      s.src = src;
      s.onload = resolve;
      s.onerror = () => reject(new Error("failed to load " + src));
      document.head.appendChild(s);
    });
  }

  async function bootPyodide() {
    showLoading("Loading the pebblnyx editor runtime (first visit only, ~30s)…");
    await loadScript(PYODIDE_CDN + "pyodide.js");
    const pyodide = await loadPyodide({ indexURL: PYODIDE_CDN });
    await pyodide.loadPackage(["pillow"]);

    showLoading("Fetching the editor…");
    const zipResp = await fetch("web-payload.zip");
    const zipBuf = new Uint8Array(await zipResp.arrayBuffer());
    pyodide.unpackArchive(zipBuf, "zip", { extractDir: "/pebblnyx" });

    await pyodide.runPythonAsync(
      "import sys\n"
      + "if '/pebblnyx/tools' not in sys.path:\n"
      + "    sys.path.insert(0, '/pebblnyx/tools')\n"
      + "import editor.webruntime as wr\n"
    );
    const bridge = pyodide.globals.get("wr");

    // Seed recent-projects from IndexedDB before anything calls into the bridge.
    const entries = await dbGetAll();
    bridge.seed_recent(entries.map((e) => e.name));
    bridge.on_recent_change((paths) => {
      const jsPaths = paths.toJs ? paths.toJs() : paths;
      // Actual persistence already happened via dbPut() in pickAndMount()/renderRecent()
      // -- this callback exists for parity with Session.remember() and is intentionally
      // a no-op here; the path list Python tracks is only ever used to answer
      // /api/project/recent while a project is open, which the recent-projects UI
      // above doesn't depend on (it reads IndexedDB directly).
      void jsPaths;
    });

    hideLoading();
    if (navigator.storage && navigator.storage.persist) {
      navigator.storage.persist().catch(() => {});
    }
    renderPyodideProjectUI(pyodide);

    // Every mutating call needs its write flushed from Pyodide's IDBFS-backed mount
    // back to the real folder on disk -- the mount is not continuously synced. Simplest
    // correct rule: sync after every POST once a folder is mounted, at the cost of some
    // per-save latency (see the design write-up's Risks section for why this wasn't
    // debounced instead).
    const origFetch = window.fetch;
    window.fetch = async function (input, init) {
      const r = await origFetch(input, init);
      // See api.js's own fetch override for why this isn't `input.url` -- input can be
      // a plain string or a URL object here too, neither of which has that property.
      const url = input instanceof Request ? input.url : String(input);
      if (mountedFs && url.startsWith("/api/") && init && init.method === "POST") {
        await syncAfterWrite();
      }
      return r;
    };

    return bridge;
  }

  // ---------------------------------------------------------------------- companion badge
  // Package / device-install / device-logs / emulator-start all end up at api.js's
  // viaCompanion(), which already fails softly with a "needs the companion" JSON body
  // instead of crashing (see sdkStatus() and emuStatus()/etc.'s existing s.error
  // checks) -- this is the proactive half of that: disable the controls up front and
  // say why, rather than let someone click "Build & run" only to be told afterwards.
  const COMPANION_ONLY_CONTROLS = ["package", "emustart", "devinstall", "devlogstart"];

  function applyCompanionGating(available) {
    const banner = document.getElementById("companionbanner");
    if (banner) banner.style.display = available ? "none" : "";
    for (const id of COMPANION_ONLY_CONTROLS) {
      const el = document.getElementById(id);
      if (!el) continue;
      el.disabled = !available;
      el.title = available ? "" : "install the local companion to use this";
    }
  }

  async function watchCompanion() {
    const origin = await pingCompanion();
    window.API.configure({ companionOrigin: origin });
    applyCompanionGating(!!origin);
    const badge = document.getElementById("stproject");
    if (badge && origin) {
      badge.title = "local companion detected -- build/device/emulator/SDK are available";
    }
  }

  // The companion is a process someone starts after the tab is already open just as
  // often as before -- polled rather than checked once, so the gating above catches up
  // without needing its own "recheck" button.
  const COMPANION_POLL_MS = 20000;
  function pollCompanion() {
    watchCompanion();
    setInterval(watchCompanion, COMPANION_POLL_MS);
  }

  // -------------------------------------------------------------------------------- boot
  async function detectAndBoot() {
    if (await pingSameOrigin()) {
      return; // this page IS the companion; api.js's default same-origin mode is correct
    }
    pollCompanion(); // independent of asset-editing readiness; runs in the background
    const bridge = await bootPyodide();
    window.API.configure({ mode: "pyodide", pyodideBridge: bridge });
  }

  window.API.configure({ readyPromise: detectAndBoot() });
})();
