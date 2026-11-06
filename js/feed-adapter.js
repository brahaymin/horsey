/*!
 * SPYGammaFeed: plug-and-play data adapter for the horsey SPY Gamma dashboard.
 * Keeps data ingestion separate from UI rendering. The UI registers:
 *   SPYGammaFeed.onSnapshot(fn)  -> fn(snapshotJson, meta)   full re-render (renderSnapshot)
 *   SPYGammaFeed.onTick(fn)      -> fn(tick)                 lightweight live update (applyLiveTick)
 *   SPYGammaFeed.onStatus(fn)    -> fn({mode,state,label,lastUpdate})
 *   SPYGammaFeed.onPoll(fn)      -> fn({reason, indexChanged, topId, idx})  after each pollNow
 * Modes:
 *   "github_archive" (default): ./snapshots/index.json + snapshot JSON + ./history/timeseries.jsonl, optional N-second poll
 *   "rest":      polls config.restUrl every config.pollSec seconds. A response that has `gex_scopes` is treated as a full
 *                snapshot (same schema as latest.json); otherwise it is treated as a tick. Override with
 *                window.SPYGammaFeedMapper = (json) => ({snapshot?, tick?})
 *   "websocket": connects to config.wsUrl (wss://). Each message is JSON: either {type:"snapshot", data:{...}}
 *                or a tick {spy_price, spy_change_pct, spy_volume, vix, es_price, nq_price, gex_45d, gex_0dte, ts}
 * Config is saved in localStorage ("spyGammaFeedConfig") and can be set with the in-page Data Source modal or
 * the URL params ?feed=rest&url=... / ?feed=websocket&url=...
 * pollNow() / checkUpdates() also fire on visibilitychange (visible), window focus, and online — throttled to ≥5s —
 * so unlocking a phone catches hourly snapshots that landed while the screen was asleep.
 * Live polls append ?_cb=<bucket> and use {cache:"no-store"} to defeat GitHub Pages max-age=600.
 * See docs/live_feed_spec.md for the full schema.
 */
(function (w) {
  "use strict";
  const LS = "spyGammaFeedConfig";
  const DEFAULTS = { mode: "github_archive", restUrl: "", wsUrl: "", pollSec: 60, autoRefresh: true };
  const subs = { snapshot: [], tick: [], status: [], index: [], log: [], poll: [] };
  let cfg = Object.assign({}, DEFAULTS);
  try { Object.assign(cfg, JSON.parse(localStorage.getItem(LS) || "{}")); } catch (e) {}
  try {
    const q = new URLSearchParams(location.search);
    if (q.get("feed")) { cfg.mode = q.get("feed"); if (q.get("url")) cfg[cfg.mode === "websocket" ? "wsUrl" : "restUrl"] = q.get("url"); }
  } catch (e) {}
  let timer = null, ws = null, lastIndexTop = null, state = "idle", lastUpdate = null;
  let lastPollAt = 0, wakeBound = false;
  const POLL_THROTTLE_MS = 5000;
  const emit = (k, ...a) => subs[k].forEach(f => { try { f(...a); } catch (e) { console.error(e); } });
  const status = (s) => { state = s; emit("status", F.status()); };

  function bust(u) {
    const sep = String(u).indexOf("?") >= 0 ? "&" : "?";
    return u + sep + "_cb=" + Math.floor(Date.now() / 15000);
  }
  async function getJSON(u) {
    const r = await fetch(bust(u), { cache: "no-store" });
    if (!r.ok) throw new Error(u + " → HTTP " + r.status);
    return r.json();
  }
  async function getText(u) {
    const r = await fetch(bust(u), { cache: "no-store" });
    if (!r.ok) throw new Error(u + " → HTTP " + r.status);
    return r.text();
  }

  function bindWake() {
    if (wakeBound) return;
    wakeBound = true;
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "visible") F.pollNow("visibility");
    });
    w.addEventListener("focus", () => F.pollNow("focus"));
    w.addEventListener("online", () => F.pollNow("online"));
  }

  const F = {
    version: "1.1.0",
    get config() { return Object.assign({}, cfg); },
    configure(c) { cfg = Object.assign({}, cfg, c || {}); try { localStorage.setItem(LS, JSON.stringify(cfg)); } catch (e) {} F.restart(); return F.config; },
    reset() { try { localStorage.removeItem(LS); } catch (e) {} cfg = Object.assign({}, DEFAULTS); F.restart(); },
    onSnapshot(f) { subs.snapshot.push(f); return F; },
    onTick(f) { subs.tick.push(f); return F; },
    onStatus(f) { subs.status.push(f); return F; },
    onIndex(f) { subs.index.push(f); return F; },
    onLog(f) { subs.log.push(f); return F; },
    onPoll(f) { subs.poll.push(f); return F; },
    bustUrl: bust,
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
      try { const t = await getText("history/timeseries.jsonl"); rows = t.trim().split("\n").filter(Boolean).map(JSON.parse); } catch (e) {}
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
    /**
     * Unified live check. Fires on the interval timer and on phone unlock / tab focus / online.
     * Throttled to ≥5s between checks (pass reason "force" to bypass).
     * Alias: checkUpdates().
     */
    async pollNow(reason) {
      reason = reason || "manual";
      const now = Date.now();
      if (reason !== "force" && lastPollAt && (now - lastPollAt) < POLL_THROTTLE_MS) {
        return { throttled: true, reason, waitedMs: now - lastPollAt };
      }
      lastPollAt = now;
      const out = { reason, mode: cfg.mode, indexChanged: false, topId: lastIndexTop, idx: null, error: null };
      try {
        if (cfg.mode === "rest" && cfg.restUrl) {
          const j = await getJSON(cfg.restUrl);
          const mapped = w.SPYGammaFeedMapper ? w.SPYGammaFeedMapper(j) : (j && j.gex_scopes ? { snapshot: j } : { tick: j });
          if (mapped.snapshot) F.renderSnapshot(mapped.snapshot);
          if (mapped.tick) F.applyLiveTick(mapped.tick);
          status("live");
          lastUpdate = new Date();
        } else if (cfg.mode === "websocket") {
          /* streaming — wake just notifies listeners; no REST poll */
          lastUpdate = new Date();
        } else {
          const idx = await getJSON("snapshots/index.json");
          const top = idx.snapshots && idx.snapshots[0] && idx.snapshots[0].id;
          out.idx = idx; out.topId = top;
          if (lastIndexTop && top && top !== lastIndexTop) {
            out.indexChanged = true;
            out.prevTopId = lastIndexTop;
            emit("index", idx);
            try { await F.loadLog(); } catch (e) {}
          } else if (!lastIndexTop && top) {
            lastIndexTop = top;
          }
          if (top) lastIndexTop = top;
          lastUpdate = new Date();
          status("ok");
        }
      } catch (e) {
        out.error = String(e && e.message || e);
        status("error");
      }
      emit("poll", out);
      return out;
    },
    checkUpdates(reason) { return F.pollNow(reason); },
    start() {
      bindWake();
      if (cfg.mode === "rest" && cfg.restUrl) {
        const poll = () => F.pollNow("timer");
        poll();
        timer = setInterval(poll, Math.max(1, cfg.pollSec) * 1000);
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
        if (cfg.autoRefresh) {
          timer = setInterval(() => { F.pollNow("timer"); }, Math.max(10, cfg.pollSec) * 1000);
        }
      }
    },
    _setIndexTop(id) { lastIndexTop = id; }
  };
  w.SPYGammaFeed = F;
})(window);
