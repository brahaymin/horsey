/*!
 * SPYGammaFeed v1.2 — data adapter for the horsey SPY Gamma dashboard.
 * Modes: github_archive | rest | websocket | alpaca_iex
 * alpaca_iex: wss://stream.data.alpaca.markets/v2/iex (Paper keys in localStorage["horsey_alpaca_paper"])
 *   + keeps GitHub archive poll for GEX/briefings in parallel.
 * See docs/live_feed_spec.md.
 */
(function (w) {
  "use strict";
  const LS = "spyGammaFeedConfig";
  const LS_ALPACA = "horsey_alpaca_paper";
  const ALPACA_IEX_WS = "wss://stream.data.alpaca.markets/v2/iex";
  const DEFAULTS = { mode: "github_archive", restUrl: "", wsUrl: "", pollSec: 60, autoRefresh: true };
  const subs = { snapshot: [], tick: [], status: [], index: [], log: [], poll: [] };
  let cfg = Object.assign({}, DEFAULTS);
  try { Object.assign(cfg, JSON.parse(localStorage.getItem(LS) || "{}")); } catch (e) {}
  try {
    const q = new URLSearchParams(location.search);
    if (q.get("feed")) {
      cfg.mode = q.get("feed");
      if (q.get("url")) cfg[cfg.mode === "websocket" ? "wsUrl" : "restUrl"] = q.get("url");
    }
  } catch (e) {}
  let timer = null, archiveTimer = null, ws = null, lastIndexTop = null, state = "idle", lastUpdate = null;
  let lastPollAt = 0, wakeBound = false, priorClose = null, alpacaAuthOk = false;
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
  function alpacaKeys() {
    try {
      const o = JSON.parse(localStorage.getItem(LS_ALPACA) || "null");
      if (o && o.key && o.secret) return { key: String(o.key).trim(), secret: String(o.secret).trim() };
    } catch (e) {}
    return null;
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
  function startArchivePoll() {
    if (!cfg.autoRefresh) return;
    clearInterval(archiveTimer);
    archiveTimer = setInterval(() => { F.pollNow("timer"); }, Math.max(10, cfg.pollSec) * 1000);
  }
  function handleAlpacaMsg(raw) {
    let msgs; try { msgs = JSON.parse(raw); } catch (e) { return; }
    if (!Array.isArray(msgs)) msgs = [msgs];
    msgs.forEach(m => {
      if (!m || typeof m !== "object") return;
      if (m.T === "success" && /authenticated/i.test(String(m.msg || ""))) {
        alpacaAuthOk = true; status("live");
        try { ws.send(JSON.stringify({ action: "subscribe", trades: ["SPY"], quotes: ["SPY"] })); } catch (e) {}
        return;
      }
      if (m.T === "error") { console.warn("[SPYGammaFeed] Alpaca IEX error", m); status("error"); return; }
      if (m.T === "subscription") return;
      if ((m.T === "t" || m.T === "q") && (m.S === "SPY" || m.symbol === "SPY")) {
        let px = null;
        if (m.T === "t" && m.p != null) px = +m.p;
        else if (m.T === "q") {
          const bp = m.bp != null ? +m.bp : null, ap = m.ap != null ? +m.ap : null;
          if (bp != null && ap != null && bp > 0 && ap > 0) px = (bp + ap) / 2;
          else px = ap || bp;
        }
        if (px == null || !(px > 0)) return;
        const tick = { spy_price: px, ts: m.t || new Date().toISOString() };
        if (priorClose && priorClose > 0) tick.spy_change_pct = ((px - priorClose) / priorClose) * 100;
        F.applyLiveTick(tick);
      }
    });
  }
  function startAlpacaIex() {
    const creds = alpacaKeys();
    if (!creds) { status("error"); console.warn("[SPYGammaFeed] alpaca_iex mode needs localStorage horsey_alpaca_paper {key,secret}"); startArchivePoll(); return; }
    status("connecting");
    alpacaAuthOk = false;
    try {
      ws = new WebSocket(ALPACA_IEX_WS);
      ws.onopen = () => {
        try { ws.send(JSON.stringify({ action: "auth", key: creds.key, secret: creds.secret })); } catch (e) { status("error"); }
      };
      ws.onerror = () => status("error");
      ws.onclose = () => { if (state !== "idle") status("disconnected"); };
      ws.onmessage = (ev) => handleAlpacaMsg(ev.data);
    } catch (e) {
      console.warn("[SPYGammaFeed] Alpaca IEX WebSocket failed", e);
      status("error");
    }
    // Always keep GitHub archive poll for GEX snapshots + LiveHub briefings
    status("ok"); // interim until auth success flips to live
    startArchivePoll();
    // Kick one archive poll immediately
    F.pollNow("alpaca_iex_boot");
  }

  const F = {
    version: "1.2.0",
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
    setPriorClose(v) { priorClose = (v == null || isNaN(+v)) ? null : +v; return priorClose; },
    getPriorClose() { return priorClose; },
    status() {
      const m = {
        github_archive: "GitHub Archive",
        rest: "REST Poll",
        websocket: "Live WebSocket",
        alpaca_iex: "Alpaca Paper IEX + Archive"
      }[cfg.mode] || cfg.mode;
      const refresh = (cfg.mode === "websocket" || (cfg.mode === "alpaca_iex" && alpacaAuthOk))
        ? "streaming"
        : (cfg.autoRefresh ? `Auto-Refresh ${cfg.pollSec}s` : "Manual");
      return { mode: cfg.mode, state, label: `Source: ${m} · ${refresh}`, lastUpdate };
    },
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
      const s = await getJSON(meta.json_path);
      try {
        const sum = s && s.summary || {};
        const prev = sum.spy_previous_close || sum.spy_prev_close || (s.spy_detail && (s.spy_detail.previous_close || s.spy_detail.prev_close));
        const close = sum.spy_regular_close || sum.spy_price;
        F.setPriorClose(prev || close);
      } catch (e) {}
      lastUpdate = new Date(); emit("snapshot", s, meta); status(cfg.mode === "alpaca_iex" && alpacaAuthOk ? "live" : "ok"); return s;
    },
    applyLiveTick(tick) {
      if (!tick) return;
      lastUpdate = new Date();
      emit("tick", tick);
      if (cfg.mode !== "github_archive") status(cfg.mode === "alpaca_iex" ? (alpacaAuthOk ? "live" : state) : "live");
    },
    renderSnapshot(s, meta) {
      lastUpdate = new Date();
      emit("snapshot", s, meta || { id: "live", display_ct: "Live", json_path: cfg.restUrl || "live" });
    },
    stop() {
      clearInterval(timer); timer = null;
      clearInterval(archiveTimer); archiveTimer = null;
      if (ws) { try { ws.close(); } catch (e) {} ws = null; }
      alpacaAuthOk = false;
      status("idle");
    },
    restart() { F.stop(); F.start(); },
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
          lastUpdate = new Date();
        } else {
          // github_archive OR alpaca_iex archive leg
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
          if (!(cfg.mode === "alpaca_iex" && alpacaAuthOk)) status("ok");
        }
      } catch (e) {
        out.error = String(e && e.message || e);
        if (cfg.mode !== "alpaca_iex") status("error");
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
      } else if (cfg.mode === "alpaca_iex") {
        startAlpacaIex();
      } else {
        status("ok");
        startArchivePoll();
      }
    },
    _setIndexTop(id) { lastIndexTop = id; }
  };
  w.SPYGammaFeed = F;
})(window);
