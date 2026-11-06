/*!
 * SPYGammaFeed: plug-and-play data adapter for the horsey SPY Gamma dashboard.
 * Keeps data ingestion separate from UI rendering. The UI registers:
 *   SPYGammaFeed.onSnapshot(fn)  -> fn(snapshotJson, meta)   full re-render (renderSnapshot)
 *   SPYGammaFeed.onTick(fn)      -> fn(tick)                 lightweight live update (applyLiveTick)
 *   SPYGammaFeed.onStatus(fn)    -> fn({mode,state,label,lastUpdate})
 * Modes:
 *   "github_archive" (default): ./snapshots/index.json + snapshot JSON + ./history/timeseries.jsonl, optional N-second poll
 *   "rest":      polls config.restUrl every config.pollSec seconds. A response that has `gex_scopes` is treated as a full
 *                snapshot (same schema as latest.json); otherwise it is treated as a tick. Override with
 *                window.SPYGammaFeedMapper = (json) => ({snapshot?, tick?})
 *   "websocket": connects to config.wsUrl (wss://). Each message is JSON: either {type:"snapshot", data:{...}}
 *                or a tick {spy_price, spy_change_pct, spy_volume, vix, es_price, nq_price, gex_45d, gex_0dte, ts}
 * Config is saved in localStorage ("spyGammaFeedConfig") and can be set with the in-page Data Source modal or
 * the URL params ?feed=rest&url=... / ?feed=websocket&url=...
 * See docs/live_feed_spec.md for the full schema.
 */
(function (w) {
  "use strict";
  const LS = "spyGammaFeedConfig";
  const DEFAULTS = { mode: "github_archive", restUrl: "", wsUrl: "", pollSec: 60, autoRefresh: true };
  const subs = { snapshot: [], tick: [], status: [], index: [], log: [] };
  let cfg = Object.assign({}, DEFAULTS);
  try { Object.assign(cfg, JSON.parse(localStorage.getItem(LS) || "{}")); } catch (e) {}
  try {
    const q = new URLSearchParams(location.search);
    if (q.get("feed")) { cfg.mode = q.get("feed"); if (q.get("url")) cfg[cfg.mode === "websocket" ? "wsUrl" : "restUrl"] = q.get("url"); }
  } catch (e) {}
  let timer = null, ws = null, lastIndexTop = null, state = "idle", lastUpdate = null;
  const emit = (k, ...a) => subs[k].forEach(f => { try { f(...a); } catch (e) { console.error(e); } });
  const status = (s) => { state = s; emit("status", F.status()); };
  async function getJSON(u) { const r = await fetch(u, { cache: "no-cache" }); if (!r.ok) throw new Error(u + " → HTTP " + r.status); return r.json(); }

  const F = {
    version: "1.0.0",
    get config() { return Object.assign({}, cfg); },
    configure(c) { cfg = Object.assign({}, cfg, c || {}); try { localStorage.setItem(LS, JSON.stringify(cfg)); } catch (e) {} F.restart(); return F.config; },
    reset() { try { localStorage.removeItem(LS); } catch (e) {} cfg = Object.assign({}, DEFAULTS); F.restart(); },
    onSnapshot(f) { subs.snapshot.push(f); return F; },
    onTick(f) { subs.tick.push(f); return F; },
    onStatus(f) { subs.status.push(f); return F; },
    onIndex(f) { subs.index.push(f); return F; },
    onLog(f) { subs.log.push(f); return F; },
    status() {
      const m = { github_archive: "GitHub Archive", rest: "REST Poll", websocket: "Live WebSocket" }[cfg.mode] || cfg.mode;
      const refresh = cfg.mode === "websocket" ? "streaming" : (cfg.autoRefresh ? `Auto-Refresh ${cfg.pollSec}s` : "Manual");
      return { mode: cfg.mode, state, label: `Source: ${m} · ${refresh}`, lastUpdate };
    },
    /* Archive helpers (also used by the snapshot selector in every mode) */
    async loadIndex() {
      let idx;
      try { idx = await getJSON("snapshots/index.json"); }
      catch (e) { idx = { snapshots: [{ id: "latest", display_ct: "Latest", json_path: "latest.json", md_path: "latest.md" }] }; }
      emit("index", idx); return idx;
    },
    async loadLog() {
      let rows = [];
      try { const t = await (await fetch("history/timeseries.jsonl", { cache: "no-cache" })).text(); rows = t.trim().split("\n").filter(Boolean).map(JSON.parse); } catch (e) {}
      emit("log", rows); return rows;
    },
    async loadSnapshot(meta) {
      const s = await getJSON(meta.json_path); lastUpdate = new Date(); emit("snapshot", s, meta); status("ok"); return s;
    },
    /* Public entry point for any live source */
    applyLiveTick(tick) { if (!tick) return; lastUpdate = new Date(); emit("tick", tick); if (cfg.mode !== "github_archive") status("live"); },
    renderSnapshot(s, meta) { lastUpdate = new Date(); emit("snapshot", s, meta || { id: "live", display_ct: "Live", json_path: cfg.restUrl || "live" }); },
    stop() { clearInterval(timer); timer = null; if (ws) { try { ws.close(); } catch (e) {} ws = null; } status("idle"); },
    restart() { F.stop(); F.start(); },
    start() {
      if (cfg.mode === "rest" && cfg.restUrl) {
        const poll = async () => {
          try {
            const j = await getJSON(cfg.restUrl);
            const out = w.SPYGammaFeedMapper ? w.SPYGammaFeedMapper(j) : (j && j.gex_scopes ? { snapshot: j } : { tick: j });
            if (out.snapshot) F.renderSnapshot(out.snapshot);
            if (out.tick) F.applyLiveTick(out.tick);
            status("live");
          } catch (e) { console.warn("[SPYGammaFeed] REST poll failed", e); status("error"); }
        };
        poll(); timer = setInterval(poll, Math.max(1, cfg.pollSec) * 1000);
      } else if (cfg.mode === "websocket" && cfg.wsUrl) {
        status("connecting");
        try {
          ws = new WebSocket(cfg.wsUrl);
          ws.onopen = () => status("live");
          ws.onerror = () => status("error");
          ws.onclose = () => { if (state !== "idle") status("disconnected"); };
          ws.onmessage = (ev) => {
            let m; try { m = JSON.parse(ev.data); } catch (e) { return; }
            const out = w.SPYGammaFeedMapper ? w.SPYGammaFeedMapper(m) : (m.type === "snapshot" ? { snapshot: m.data } : { tick: m.data || m });
            if (out.snapshot) F.renderSnapshot(out.snapshot);
            if (out.tick) F.applyLiveTick(out.tick);
          };
        } catch (e) { console.warn("[SPYGammaFeed] WebSocket failed", e); status("error"); }
      } else {
        // github_archive: poll the index; notify the UI when a newer snapshot lands
        status("ok");
        if (cfg.autoRefresh) timer = setInterval(async () => {
          try {
            const idx = await getJSON("snapshots/index.json");
            const top = idx.snapshots && idx.snapshots[0] && idx.snapshots[0].id;
            if (lastIndexTop && top && top !== lastIndexTop) { emit("index", idx); F.loadLog(); }
            lastIndexTop = top; lastUpdate = new Date(); status("ok");
          } catch (e) { status("error"); }
        }, Math.max(10, cfg.pollSec) * 1000);
      }
    },
    _setIndexTop(id) { lastIndexTop = id; }
  };
  w.SPYGammaFeed = F;
})(window);
