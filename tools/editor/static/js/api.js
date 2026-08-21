// The transport shim that lets the hosted (GitHub Pages) editor run against either a
// local companion process or an in-page Pyodide runtime, while every existing UI file
// (app.js, atlas.js, sprites.js, hud_window.js) keeps calling plain `fetch('/api/...')`
// or the shared `post()` helper exactly as it always has.
//
// Rather than rewrite the ~55 call sites spread across those files -- all of which
// already funnel through `fetch()` one way or another, `post()` included -- this
// replaces `window.fetch` itself for `/api/...` paths and leaves everything else
// (loading /static/js/*.js, images, etc.) untouched. Loaded first, before app.js, so
// the override is in place before anything else runs.
//
// Three transports, chosen per request:
//   'same-origin' -- this page IS being served by the native companion (today's
//                    behaviour, running from a checkout or an installed build). Real
//                    fetch, unchanged.
//   'pyodide'     -- this page was loaded from a static host with no companion serving
//                    it. Asset-editing routes run in-page via Pyodide
//                    (editor.webruntime.dispatch), against a folder the user picked
//                    with the File System Access API.
//   'companion'   -- routes that only exist server-side (build a real .pbw, flash a
//                    device, run the emulator, install the SDK) go over CORS to a
//                    local companion at 127.0.0.1, if one was detected. If none was
//                    detected, these fail with a clear "install the companion" message
//                    rather than a bare 404 -- normally unreachable, since the UI
//                    disables those controls first (see webruntime.js), but a request
//                    that gets here anyway should say why it didn't work.
(function () {
  const REAL_FETCH = window.fetch.bind(window);

  // Routes that only exist on the native companion -- never shipped into the Pyodide
  // payload (see tools/build_web_payload.py) because they need a real subprocess/QEMU.
  const COMPANION_ONLY_PREFIXES = [
    "/api/device/", "/api/sdk/", "/api/emulator/", "/api/package",
  ];

  let assetMode = "same-origin";     // 'same-origin' | 'pyodide'
  let companionOrigin = null;        // e.g. 'http://127.0.0.1:8765', or null
  let pyodideBridge = null;          // the PyProxy for editor.webruntime, once loaded
  let ready = Promise.resolve();     // resolves once the chosen transport can serve a request

  function isCompanionOnly(path) {
    return COMPANION_ONLY_PREFIXES.some((p) => path.startsWith(p));
  }

  function bytesToResponse(status, ctype, body) {
    const bytes = body instanceof Uint8Array ? body
      : new TextEncoder().encode(typeof body === "string" ? body : "");
    return {
      status,
      ok: status >= 200 && status < 300,
      headers: { get: (h) => (h.toLowerCase() === "content-type" ? ctype : null) },
      json: async () => JSON.parse(new TextDecoder().decode(bytes)),
      text: async () => new TextDecoder().decode(bytes),
      blob: async () => new Blob([bytes], { type: ctype || "application/octet-stream" }),
    };
  }

  async function viaPyodide(method, path, init) {
    if (!pyodideBridge) {
      return bytesToResponse(503, "application/json",
        JSON.stringify({ ok: false, error: "the in-browser editor runtime is still starting" }));
    }
    const bodyText = init && init.body ? init.body : null;
    const raw = bodyText != null ? new TextEncoder().encode(bodyText) : null;
    const result = pyodideBridge.dispatch(method, path, raw);
    const plain = result.toJs
      ? result.toJs({ dict_converter: Object.fromEntries })
      : result;
    if (result.destroy) result.destroy();
    return bytesToResponse(plain.status, plain.ctype, plain.body);
  }

  function viaCompanion(method, path, init) {
    if (!companionOrigin) {
      return Promise.resolve(bytesToResponse(503, "application/json", JSON.stringify({
        ok: false,
        error: "this needs the local companion -- install it to build, flash a device, "
             + "or run the emulator",
      })));
    }
    return REAL_FETCH(companionOrigin + path, { ...init, mode: "cors", credentials: "omit" });
  }

  window.fetch = async function (input, init) {
    // `input` can be a string, a URL, or a Request -- fetch() accepts all three, and
    // Pyodide's own WASM loader is exactly the caller that showed this: it passes a
    // URL object, which has no `.url` property (that's Request-only), so treating
    // "not a string" as "must be a Request" broke on the very first real page load.
    const url = input instanceof Request ? input.url : String(input);
    if (!url.startsWith("/api/")) return REAL_FETCH(input, init);

    await ready;
    if (assetMode === "same-origin" && !isCompanionOnly(url)) return REAL_FETCH(input, init);

    const method = (init && init.method) || "GET";
    if (isCompanionOnly(url)) return viaCompanion(method, url, init);
    if (assetMode === "pyodide") return viaPyodide(method, url, init);
    return REAL_FETCH(input, init);
  };

  window.API = {
    // The pre-override fetch, for webruntime.js's own bootstrap probes. Those decide
    // what `ready` resolves to -- routing them through the intercepted `window.fetch`
    // above, which awaits `ready` for any /api/ path, would deadlock a same-origin
    // /api/ping check against the very promise it's supposed to resolve.
    realFetch: REAL_FETCH,

    // Called from webruntime.js once it knows which transport(s) are available.
    // `readyPromise`, if given, is awaited before the FIRST request of either kind is
    // let through -- covers the time Pyodide/Pillow/the project payload take to load,
    // so app.js's normal startup fetch('/api/state') just waits instead of racing it.
    configure({ mode, companionOrigin: origin, pyodideBridge: bridge, readyPromise }) {
      if (mode) assetMode = mode;
      if (origin !== undefined) companionOrigin = origin;
      if (bridge !== undefined) pyodideBridge = bridge;
      if (readyPromise) ready = readyPromise;
    },
    get mode() { return assetMode; },
    get companionAvailable() { return !!companionOrigin; },
  };
})();
