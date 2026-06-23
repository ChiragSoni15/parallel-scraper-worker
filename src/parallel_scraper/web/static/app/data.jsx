// Data layer — replaces the prototype's mock window.RUNS / LEADS / etc with real
// fetches against the FastAPI backend. Same window.* names so screens stay close
// to the prototype, but everything is now a hook returning live data.

const { useState: useStateD, useEffect: useEffectD, useCallback: useCallbackD, useRef: useRefD, useMemo: useMemoD } = React;

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "content-type": "application/json", ...(opts.headers || {}) },
    ...opts,
  });
  if (!res.ok) {
    const text = await res.text().catch(() => res.statusText);
    throw new Error(`${res.status}: ${text}`);
  }
  return res.json();
}

// ── helpers ─────────────────────────────────────────────────────────

function relativeTime(iso) {
  if (!iso) return "—";
  let d;
  try { d = new Date(iso); } catch { return iso; }
  if (isNaN(d.getTime())) return iso;
  const diffMs = Date.now() - d.getTime();
  const diffMin = diffMs / 60000;
  if (diffMin < 1) return "just now";
  if (diffMin < 60) return `${Math.floor(diffMin)}m ago`;
  const diffH = diffMin / 60;
  if (diffH < 24) return `${Math.floor(diffH)}h ago`;
  const diffD = diffH / 24;
  if (diffD < 2) return "yesterday";
  if (diffD < 7) return `${Math.floor(diffD)}d ago`;
  const m = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];
  return `${m[d.getMonth()]} ${d.getDate()}`;
}

function summaryToRunRow(s) {
  // /api/runs returns rich summaries; collapse to the shape liverun/welcome want.
  const status = s.active ? "running"
    : (s.exit_code === 0 ? "done"
       : (typeof s.exit_code === "number" ? "failed" : "done"));
  const queriesArr = Array.isArray(s.queries) ? s.queries : [];
  const name = s.city
    ? `${s.city}${queriesArr.length ? " · " + queriesArr[0] + (queriesArr.length > 1 ? " +" + (queriesArr.length - 1) : "") : ""}`
    : (s.run_id || "—");
  return {
    id: s.run_id,
    name,
    area: s.city || "—",
    status,
    started: relativeTime(s.started_at),
    started_iso: s.started_at,
    queries: queriesArr.length || 0,
    queries_arr: queriesArr,
    cells: s.cells_total || 0,
    found: s.discovered || 0,
    scraped: s.scraped || 0,
    eta: "—",  // computed only when we have rate sampling (phase 3)
    cost: s.cost_usd || 0,  // populated once cost tracker lands
    raw: s,
  };
}

function logToRow(l) {
  const lvl = (l.level || "info").toLowerCase();
  const lvlClass = lvl === "error" ? "err" : lvl === "warning" || lvl === "warn" ? "warn" : lvl === "info" ? "info" : "ok";
  const ts = (l.ts || "").includes("T") ? (l.ts.split("T")[1] || "").split(".")[0] : (l.ts || "");
  return { t: ts || "—", lvl: lvlClass, msg: l.msg || "" };
}

// ── hooks ───────────────────────────────────────────────────────────

function cellStatesFromGrid(g) {
  const feats = (g && g.features) || [];
  return feats.map((f) => {
    const p = (f && f.properties) || {};
    return { cell_id: p.cell_id, status: p.status, place_count: p.place_count || 0 };
  }).filter((c) => c.cell_id && c.status);
}

function appendLiveLogEvents(ref, events) {
  if (!events || events.length === 0) return ref.current || [];
  let rows = (ref.current || []).concat(events.map((e) => logToRow({
    ts: e.ts, level: e.level, msg: e.msg,
  })));
  while (rows.length > 500) rows.shift();
  while (rows.length > 1 && JSON.stringify(rows).length > 256 * 1024) rows.shift();
  ref.current = rows;
  return rows;
}

function useRuns(refreshMs = 10000) {
  const [runs, setRuns] = useStateD([]);
  const [loaded, setLoaded] = useStateD(false);
  const fetchRuns = useCallbackD(async () => {
    try {
      const data = await api("/api/runs");
      setRuns((data.runs || []).map(summaryToRunRow));
      setLoaded(true);
    } catch (e) { /* silent */ }
  }, []);
  useEffectD(() => {
    fetchRuns();
    const t = setInterval(fetchRuns, refreshMs);
    return () => clearInterval(t);
  }, [fetchRuns, refreshMs]);
  return { runs, loaded, refresh: fetchRuns };
}

function useColumns() {
  const [cols, setCols] = useStateD(null);
  useEffectD(() => { api("/api/columns").then(setCols).catch(() => {}); }, []);
  return cols;
}

function usePresets() {
  const [presets, setPresets] = useStateD({});
  const [loading, setLoading] = useStateD(false);
  const refresh = useCallbackD(async () => {
    setLoading(true);
    try { const d = await api("/api/presets/queries"); setPresets(d.presets || {}); }
    catch { setPresets({}); }
    finally { setLoading(false); }
  }, []);
  useEffectD(() => { refresh(); }, [refresh]);
  return { presets, refresh, loading };
}

async function searchCity(q) {
  const data = await api("/api/grid-scraper/search-city", {
    method: "POST", body: JSON.stringify({ query: q }),
  });
  return (data.candidates || []).map(c => ({
    id: `${c.osm_type}-${c.osm_id}`,
    name: c.display_name,
    sub: `OSM ${c.osm_type} #${c.osm_id} · ${c.category}`,
    osm_id: c.osm_id, osm_type: c.osm_type, lat: c.lat, lon: c.lon,
    bbox: c.bbox || null,
  }));
}

async function generateGrid({ osm_id, osm_type, custom_polygon, area_name, grid_size_meters, grid_type }) {
  return api("/api/grid-scraper/generate-grid", {
    method: "POST",
    body: JSON.stringify({
      osm_id, osm_type, custom_polygon: custom_polygon || null,
      area_name: area_name || "", grid_size_meters, grid_type: grid_type || "square",
    }),
  });
}

// SSE snapshot stream for a run. Returns the latest snapshot + cell-state list.
//
// Grid loading strategy:
//   Small grid  (<5000 cells, e.g. a single city at 1km): fetch ?mode=full
//                  once at boot. ~few hundred KB, parses instantly.
//   Large grid (>=5000 cells, e.g. MMR at 250m = 70K cells / 22 MB): NEVER
//                  fetch the full grid. Instead poll ?mode=touched on a
//                  modest cadence (every ~6 s) so only the touched-cell
//                  subset comes over the wire. Map renders only those.
function useRunStream(runId) {
  const [snap, setSnap] = useStateD(null);
  const [grid, setGrid] = useStateD(null);
  const [boundary, setBoundary] = useStateD(null);
  const [cellStates, setCellStates] = useStateD([]);
  const [liveLogs, setLiveLogs] = useStateD([]);
  const esRef = useRefD(null);
  const liveLogsRef = useRefD([]);
  useEffectD(() => {
    if (!runId) {
      setSnap(null); setGrid(null); setBoundary(null); setCellStates([]); setLiveLogs([]);
      liveLogsRef.current = [];
      return;
    }
    let cancelled = false;
    let mode = "full";              // optimistic; revised after first snap
    let touchedTimer = null;
    let fetchedInitialGrid = false;
    liveLogsRef.current = [];
    setLiveLogs([]);

    // Boundary is small (a single polygon — typically <1 MB even for MMR)
    // and never changes during a run, so fetch once.
    api(`/api/runs/${encodeURIComponent(runId)}/boundary`)
      .then(b => { if (!cancelled) setBoundary(b); })
      .catch(() => {});

    function fetchGrid(modeArg) {
      api(`/api/runs/${encodeURIComponent(runId)}/grid?mode=${modeArg}`)
        .then(g => {
          if (cancelled) return;
          if (modeArg === "touched") {
            setCellStates(cellStatesFromGrid(g));
            if (mode === "touched") setGrid(g);
          } else {
            setGrid(g);
          }
        })
        .catch(() => {});
    }

    function startTouchedPolling() {
      if (touchedTimer) return;
      // 6s cadence keeps the heatmap fresh; the server caches by CSV mtime.
      touchedTimer = setInterval(() => {
        if (cancelled) return;
        fetchGrid("touched");
      }, 6000);
    }

    async function chooseGridModeAndFetch(s) {
      let totalCells = (s && s.grid_cells_total) || 0;
      if (!totalCells) {
        try {
          const meta = await api(`/api/runs/${encodeURIComponent(runId)}/grid?mode=meta`);
          totalCells = meta.feature_count || 0;
          if (!cancelled && totalCells) {
            setSnap((prev) => ({
              ...(prev || s || {}),
              grid_cells_total: totalCells,
              cells_total: (prev && prev.cells_total) || (s && s.cells_total) || totalCells * (((s && s.queries) || []).length || 1),
            }));
          }
        } catch {}
      }
      const huge = totalCells > 5000;
      mode = huge ? "touched" : "full";
      fetchGrid(mode);
      fetchGrid("touched");
      fetchedInitialGrid = true;
      startTouchedPolling();
    }

    // bootstrap with summary; defer grid fetch until we know the size.
    api(`/api/runs/${encodeURIComponent(runId)}`).then(s => {
      if (cancelled) return;
      setSnap(s);
      chooseGridModeAndFetch(s);
    }).catch(() => {});

    // SSE: initial snapshot, then heartbeat deltas only.
    const es = new EventSource(`/api/runs/${encodeURIComponent(runId)}/stream`);
    esRef.current = es;
    const onSnap = (ev) => {
      try {
        const s = JSON.parse(ev.data);
        if (!cancelled) setSnap((prev) => ({ ...(prev || {}), ...s }));
        // Lazy first fetch if the summary call hasn't come back yet.
        if (!fetchedInitialGrid) {
          chooseGridModeAndFetch(s);
        }
      } catch {}
    };
    const onHeartbeat = (ev) => {
      try {
        const h = JSON.parse(ev.data);
        const logEvents = h.log_events || [];
        if (logEvents.length) {
          const rows = appendLiveLogEvents(liveLogsRef, logEvents);
          if (!cancelled) setLiveLogs(rows.slice());
        }
        const { log_events, ...rest } = h;
        if (!cancelled) setSnap((prev) => ({ ...(prev || {}), ...rest }));
      } catch {}
    };
    es.addEventListener("snapshot", onSnap);
    es.addEventListener("heartbeat", onHeartbeat);
    return () => {
      cancelled = true;
      es.close();
      esRef.current = null;
      if (touchedTimer) { clearInterval(touchedTimer); touchedTimer = null; }
    };
  }, [runId]);
  return { snap, grid, boundary, cellStates, logs: liveLogs };
}

function useRunLogs(runId, intervalMs = 3000, n = 80, enabled = true) {
  const [lines, setLines] = useStateD([]);
  useEffectD(() => {
    if (!runId || !enabled) { setLines([]); return; }
    let alive = true;
    const tick = async () => {
      try {
        const d = await api(`/api/runs/${encodeURIComponent(runId)}/logs?n=${n}`);
        if (alive) setLines((d.lines || []).map(logToRow));
      } catch {}
    };
    tick();
    const t = setInterval(tick, intervalMs);
    return () => { alive = false; clearInterval(t); };
  }, [runId, intervalMs, n, enabled]);
  return lines;
}

function useRunLeads(runId, q = "", page = 0, limit = 200, enabled = true) {
  // `enabled=false` defers the (potentially slow) /leads call. The server has to
  // parse phase2_data.csv + phase1_discovered.csv on every request — for a 175K
  // lead run on OneDrive that's 1-3 s. Results screen uses this to show summary
  // first, then load the leads only when the user clicks "load outlets".
  const [data, setData] = useStateD({ leads: [], total: 0, loaded: false, loading: false });
  useEffectD(() => {
    if (!runId || !enabled) {
      setData({ leads: [], total: 0, loaded: false, loading: false });
      return;
    }
    let alive = true;
    setData(d => ({ ...d, loading: true }));
    const params = new URLSearchParams({ page: String(page), limit: String(limit) });
    if (q) params.set("q", q);
    api(`/api/runs/${encodeURIComponent(runId)}/leads?${params}`).then(d => {
      if (alive) setData({ leads: d.leads || [], total: d.total || 0, loaded: true, loading: false });
    }).catch(() => { if (alive) setData({ leads: [], total: 0, loaded: true, loading: false }); });
    return () => { alive = false; };
  }, [runId, q, page, limit, enabled]);
  return data;
}

function useRunBoundary(runId) {
  const [boundary, setBoundary] = useStateD(null);
  useEffectD(() => {
    if (!runId) { setBoundary(null); return; }
    let alive = true;
    api(`/api/runs/${encodeURIComponent(runId)}/boundary`)
      .then(b => { if (alive) setBoundary(b); })
      .catch(() => { if (alive) setBoundary(null); });
    return () => { alive = false; };
  }, [runId]);
  return boundary;
}

function useStats() {
  const [stats, setStats] = useStateD(null);
  useEffectD(() => {
    api("/api/stats").then(setStats).catch(() => setStats({
      // fallback: zeros so the UI still renders before the endpoint exists
      total_runs: 0, total_places: 0, total_spend: 0,
      api_calls_today: 0, throughput_per_min: 0, disk_usage_mb: 0, archived_count: 0,
    }));
  }, []);
  return stats;
}

function useApiKeys(refreshMs = 10000) {
  const [keys, setKeys] = useStateD(null);
  const refresh = useCallbackD(async () => {
    try { setKeys(await api("/api/api-keys")); }
    catch { setKeys(null); }
  }, []);
  useEffectD(() => {
    refresh();
    const t = setInterval(refresh, refreshMs);
    return () => clearInterval(t);
  }, [refresh, refreshMs]);
  return keys;
}

// Workers list pulled from snapshot.workers (added in phase 3).
function workersFromSnapshot(snap) {
  const ws = (snap && snap.workers) || [];
  const p1 = ws.filter(w => w.phase === 1 || w.phase === "1" || w.phase === "phase1");
  const p2 = ws.filter(w => w.phase === 2 || w.phase === "2" || w.phase === "phase2");
  const fmtDur = (started_at) => {
    if (!started_at) return "—";
    try {
      const s = Math.max(0, Math.floor((Date.now() - new Date(started_at).getTime()) / 1000));
      return `${String(Math.floor(s / 60)).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;
    } catch { return "—"; }
  };
  const norm = (w) => ({ id: w.id, state: w.state || "idle", task: w.task || "—", q: w.query || "", dur: fmtDur(w.started_at) });
  return { p1: p1.map(norm), p2: p2.map(norm) };
}

// run actions
async function startRun(body) {
  return api("/api/runs", { method: "POST", body: JSON.stringify(body) });
}
async function stopRun(runId) {
  return api(`/api/runs/${encodeURIComponent(runId)}/stop`, { method: "POST" });
}
async function savePreset(name, queries) {
  return api("/api/presets/queries", { method: "POST", body: JSON.stringify({ name, queries }) });
}
async function deletePreset(name) {
  return api(`/api/presets/queries/${encodeURIComponent(name)}`, { method: "DELETE" });
}

// Built-in starter presets shown on the Welcome screen (frontend-only — no
// backend round-trip; clicking one routes to /new with the queries pre-loaded).
const STARTER_PRESETS = [
  { key: "retail_fnb", title: "retail · F&B sweep", sub: "14 queries · grocery, beverages, snacks",
    queries: ["grocery store","supermarket","convenience store","beverage distributor","aerated drinks supplier",
              "bottled water supplier","soft drinks shop","juice shop","candy store","confectionery store",
              "hot dog restaurant","takeout restaurant","delivery service food","hawker stall"] },
  { key: "hospitality", title: "hospitality leads", sub: "9 queries · restaurants, cafés, hotels",
    queries: ["restaurant","cafe","coffee shop","hotel","bed and breakfast","bar","pub","bakery","dessert shop"] },
  { key: "auto", title: "services · automotive", sub: "11 queries · garages, parts, EV",
    queries: ["car repair","mechanic","auto parts","tyre shop","car wash","gas station","ev charging station",
              "motorcycle repair","car dealer","auto body shop","towing service"] },
  { key: "blank", title: "custom (blank)", sub: "start with no queries pre-loaded", queries: [] },
];

// Expose to screen modules.
window.api = api;
window.relativeTime = relativeTime;
window.useRuns = useRuns;
window.useColumns = useColumns;
window.usePresets = usePresets;
window.searchCity = searchCity;
window.generateGrid = generateGrid;
window.useRunStream = useRunStream;
window.useRunLogs = useRunLogs;
window.useRunLeads = useRunLeads;
window.useRunBoundary = useRunBoundary;
window.useStats = useStats;
window.useApiKeys = useApiKeys;
window.workersFromSnapshot = workersFromSnapshot;
window.startRun = startRun;
window.stopRun = stopRun;
window.savePreset = savePreset;
window.deletePreset = deletePreset;
window.STARTER_PRESETS = STARTER_PRESETS;
window.summaryToRunRow = summaryToRunRow;
