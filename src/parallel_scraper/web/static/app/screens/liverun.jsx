// Live Run mission control — stats / map heatmap / detail / log / workers
const { useMemo: useMemoLR, useEffect: useEffectLR, useState: useStateLR, useRef: useRefLR } = React;

function fmtElapsed(s) {
  s = Math.max(0, Math.floor(s || 0));
  const h = String(Math.floor(s / 3600)).padStart(2, "0");
  const m = String(Math.floor((s % 3600) / 60)).padStart(2, "0");
  const sec = String(s % 60).padStart(2, "0");
  return s >= 3600 ? `${h}:${m}:${sec}` : `${m}:${sec}`;
}

function fmtETA(seconds) {
  if (!isFinite(seconds) || seconds < 0) return "—";
  if (seconds < 60) return `${Math.ceil(seconds)}s`;
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  if (h >= 24) {
    const d = Math.floor(h / 24);
    return `${d}d ${h % 24}h`;
  }
  if (h > 0) return `${h}h ${m}m`;
  return `${m}m`;
}

function fmtK(n) {
  n = n || 0;
  if (n >= 1000) return (n / 1000).toFixed(n >= 10000 ? 0 : 1) + "K";
  return String(n);
}

// 1-second client-side ticker — keeps elapsed counters live between SSE
// snapshot pushes (which arrive every ~2s). Returns Date.now() integer that
// changes every second so consumers re-render naturally.
function useTickLR(intervalMs = 1000) {
  const [tick, setTick] = useStateLR(0);
  useEffectLR(() => {
    const id = setInterval(() => setTick((t) => t + 1), intervalMs);
    return () => clearInterval(id);
  }, [intervalMs]);
  return tick;
}

// Sliding-window rate calculator. Each call records (now, value); returns
// the slope (Δvalue / Δseconds) over the last `windowMs` of samples.
//
// Two failure modes to avoid:
//   (a) Component mounts with placeholder value=0, then the first SSE
//       snapshot bumps it to e.g. 387,349 (resumed run). A naive sliding
//       window treats that 0→387K jump as a per-second rate (gives a
//       bogus 150,000/s). Fix: skip leading-zero samples until we see
//       the first non-zero value.
//   (b) Two snapshots arrive a few ms apart and the difference between
//       them gets divided by a tiny dt — same bogus inflation. Fix:
//       require minSeconds of data before reporting a rate.
function useRateLR(value, windowMs = 15000, minSeconds = 3) {
  const samplesRef = useRefLR([]);
  const now = Date.now();
  const samples = samplesRef.current;

  // Skip everything until we have a real value to anchor on.
  if (samples.length === 0 && (!value || value <= 0)) return 0;

  if (samples.length === 0 || samples[samples.length - 1].v !== value) {
    samples.push({ t: now, v: value || 0 });
  }
  while (samples.length > 2 && now - samples[0].t > windowMs) samples.shift();
  if (samples.length < 2) return 0;

  const dt = (samples[samples.length - 1].t - samples[0].t) / 1000;
  if (dt < minSeconds) return 0;  // not enough data yet — don't extrapolate

  const dv = samples[samples.length - 1].v - samples[0].v;
  if (dv <= 0) return 0;
  return dv / dt;
}

// ── Resume override dialog ─────────────────────────────────
// Lets the user tune Phase 2 browsers / Phase 1 workers / recycle-after
// before resuming. Pre-filled from the saved run_config.json values that
// _summarize_run now exposes on snap.
function ResumeDialog({ runId, snap, onClose }) {
  const I = window.Icons;
  const initialP2 = Number(snap?.num_consumer_threads || 8);
  const [p2, setP2] = React.useState(initialP2);
  const [p1, setP1] = React.useState(0);             // 0 = auto from active keys
  const [recycle, setRecycle] = React.useState(50);
  const [submitting, setSubmitting] = React.useState(false);
  const [err, setErr] = React.useState(null);

  // The scraper's _maybe_load_or_write_run_config rejects the resume if
  // selected_columns drifts. We MUST send the original column list — no
  // profile, no defaults — so we read it from snap and surface it as
  // read-only context for the user.
  const cols = snap?.selected_columns || [];
  const queries = snap?.queries || [];

  async function submit() {
    setSubmitting(true);
    setErr(null);
    try {
      // Guard: the resume needs config fields that _summarize_run only
      // exposes if the dashboard server has been restarted with the new
      // code. If they're missing, abort with a clear message rather than
      // silently submitting osm_id=0 (which would crash with a boundary
      // fetch error from OSM).
      if (!snap?.osm_relation_id && !snap?.custom_polygon) {
        throw new Error(
          "this run's config (osm_relation_id) isn't visible to the " +
          "dashboard yet — restart the dashboard server once so the " +
          "extra config fields are exposed, then retry."
        );
      }
      if (!cols || cols.length === 0) {
        throw new Error("no selected_columns found on this run — cannot safely resume.");
      }
      // NOTE: `Number(x) || default` would silently swallow legitimate 0
      // values (e.g. phase2=0 means "sequential mode, Phase 1 only"). Use
      // explicit isNaN checks instead.
      const p2Val = Number(p2);
      const p1Val = Number(p1);
      const recVal = Number(recycle);
      const body = {
        run_id: runId,
        osm_id: snap?.osm_relation_id || 0,
        osm_type: snap?.osm_type || "relation",
        city: snap?.city || "area",
        grid_size_meters: snap?.grid_size_meters || 250,
        queries: queries.join(","),
        discovery_backend: snap?.discovery_backend || "places_api",
        places_tier: snap?.places_tier || "ESSENTIALS",
        phase1_workers: Number.isFinite(p1Val) && p1Val >= 0 ? p1Val : 0,
        phase2_workers: Number.isFinite(p2Val) && p2Val >= 0 ? p2Val : 8,
        worker_browser_recycle_after: Number.isFinite(recVal) && recVal >= 1 ? recVal : 50,
        // Must exactly match the saved column list, or scraper refuses
        // to resume with a "columns mismatch" SystemExit.
        columns: cols,
      };
      await window.startRun(body);
      onClose();
    } catch (e) {
      setErr(String(e.message || e));
    } finally {
      setSubmitting(false);
    }
  }

  // Modal overlay. Uses inline styles so we don't depend on theme CSS
  // shipping a .modal-overlay class.
  return (
    <div
      style={{
        position: "fixed", inset: 0, background: "rgba(0,0,0,0.6)",
        zIndex: 1000, display: "flex", alignItems: "center", justifyContent: "center",
      }}
      onClick={onClose}
    >
      <div
        style={{
          background: "var(--bg-1)", border: "1px solid var(--border-1)",
          borderRadius: 8, padding: 20, width: 480, maxWidth: "92vw",
          boxShadow: "0 10px 40px rgba(0,0,0,0.5)",
        }}
        onClick={(e) => e.stopPropagation()}
      >
        <div style={{display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 14}}>
          <h3 style={{margin: 0, fontSize: 14, fontWeight: 600}}>resume run · adjust workers</h3>
          <button className="btn ghost sm" onClick={onClose}>✕</button>
        </div>
        <div style={{fontSize: 11, color: "var(--fg-2)", marginBottom: 14, lineHeight: 1.5}}>
          resuming <code>{runId}</code> · {cols.length} columns · {queries.length} queries · area: <code>{snap?.city || "—"}</code>
        </div>
        <div style={{display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12, marginBottom: 14}}>
          <label style={{fontSize: 11}}>
            <div style={{marginBottom: 4, color: "var(--fg-2)"}}>phase 2 browsers</div>
            <input
              type="number" min="0" max="40" value={p2}
              onChange={(e) => setP2(e.target.value)}
              style={{width: "100%", padding: 6, background: "var(--bg-0)", border: "1px solid var(--border-1)", borderRadius: 4, color: "var(--fg-0)"}}
            />
            <div style={{fontSize: 10, color: Number(p2) === 0 ? "var(--accent)" : "var(--fg-3)", marginTop: 4}}>
              {Number(p2) === 0
                ? "→ sequential mode: phase 1 only, place_ids saved for later"
                : `was ${initialP2}; try 8 on 16-core CPU`}
            </div>
          </label>
          <label style={{fontSize: 11}}>
            <div style={{marginBottom: 4, color: "var(--fg-2)"}}>phase 1 workers</div>
            <input
              type="number" min="0" max="500" value={p1}
              onChange={(e) => setP1(e.target.value)}
              style={{width: "100%", padding: 6, background: "var(--bg-0)", border: "1px solid var(--border-1)", borderRadius: 4, color: "var(--fg-0)"}}
            />
            <div style={{fontSize: 10, color: "var(--fg-3)", marginTop: 4}}>0 = auto from active keys</div>
          </label>
          <label style={{fontSize: 11, gridColumn: "1 / 3"}}>
            <div style={{marginBottom: 4, color: "var(--fg-2)"}}>recycle browser every N places</div>
            <input
              type="number" min="1" max="500" value={recycle}
              onChange={(e) => setRecycle(e.target.value)}
              style={{width: "100%", padding: 6, background: "var(--bg-0)", border: "1px solid var(--border-1)", borderRadius: 4, color: "var(--fg-0)"}}
            />
          </label>
        </div>
        {err && (
          <div style={{padding: 8, background: "rgba(255,80,80,0.1)", border: "1px solid rgba(255,80,80,0.3)", borderRadius: 4, color: "#ff8080", fontSize: 11, marginBottom: 10}}>
            {err}
          </div>
        )}
        <div style={{display: "flex", gap: 8, justifyContent: "flex-end"}}>
          <button className="btn ghost" onClick={onClose} disabled={submitting}>cancel</button>
          <button className="btn primary" onClick={submit} disabled={submitting}>
            {submitting ? "submitting…" : "resume"}
          </button>
        </div>
      </div>
    </div>
  );
}

const LiveRun = ({ run, mapStyle, setRoute }) => {
  const I = window.Icons;
  const runId = run && run.id;
  const { snap, grid, boundary, cellStates, logs: liveLogs } = window.useRunStream(runId);
  const fileLogs = window.useRunLogs(runId, 3000, 80, !snap?.active && (!liveLogs || liveLogs.length === 0));
  const logs = (liveLogs && liveLogs.length) ? liveLogs : fileLogs;

  // 1-second client tick — drives live elapsed counter + rate windows.
  useTickLR(1000);

  const cells = cellStates || [];
  const cellCounts = useMemoLR(() => {
    const out = { queued: 0, running: 0, done: 0, failed: 0 };
    for (const c of cells) {
      if (c.status === "done") out.done++;
      else if (c.status === "failed") out.failed++;
      else if (c.status === "in_progress") out.running++;
      else out.queued++;
    }
    return out;
  }, [cells]);
  // Total work = (grid cells × queries). _summarize_run exposes this as
  // grid_cells_total × queries_total when available; falls back to whatever
  // state_cells.csv has so the bar shows SOMETHING even before grid loads.
  const totalCells = snap?.cells_total || cells.length || 0;
  const cellsDone = snap?.cells_done || cellCounts.done || 0;
  const cellsInFlight = snap?.cells_in_flight ?? cellCounts.running;
  const cellsRemaining = Math.max(0, totalCells - cellsDone - cellsInFlight);
  const donePct = totalCells > 0 ? ((cellsDone / totalCells) * 100).toFixed(1) : "0.0";

  // Phase 2 progress denominator: prefer discovered (cumulative places found
  // by Phase 1). Previous denominator (placeids_done+pending+failed) under-
  // counted because Phase 1 discovers faster than Phase 2 can ingest, and
  // briefly went >100% during catch-up bursts.
  const discoveredTotal = snap?.discovered || 0;
  const scraped = snap?.scraped || 0;
  const queueDepth = snap?.queue_depth || 0;
  const placeIdsFailed = snap?.placeids_failed || 0;
  const scrapedPct = discoveredTotal > 0
    ? Math.min(100, (scraped / discoveredTotal) * 100).toFixed(1)
    : "0.0";

  const errorPct = (snap && (snap.placeids_done + snap.placeids_failed) > 0)
    ? ((snap.placeids_failed / (snap.placeids_done + snap.placeids_failed)) * 100).toFixed(1)
    : "0.0";

  const cost = snap && typeof snap.cost_usd === "number" ? snap.cost_usd : 0;
  const apiCalls = snap && typeof snap.api_calls === "number" ? snap.api_calls : 0;
  const costEst = cost > 0 ? Math.max(cost * 1.5, 0.01) : 0;

  // Live elapsed: server's elapsed_s is the anchor; we add wall-clock seconds
  // since the snapshot arrived to keep the counter moving between SSEs.
  // We measure the local arrival time of each new elapsed_s value.
  const elapsedAnchorRef = useRefLR({ snapAt: Date.now(), snapValue: snap?.elapsed_s || 0 });
  if (snap && elapsedAnchorRef.current.snapValue !== (snap.elapsed_s || 0)) {
    elapsedAnchorRef.current = { snapAt: Date.now(), snapValue: snap.elapsed_s || 0 };
  }
  const elapsedLive = snap?.active
    ? elapsedAnchorRef.current.snapValue + (Date.now() - elapsedAnchorRef.current.snapAt) / 1000
    : (snap?.elapsed_s || 0);

  // Smoothed rates over a 15s window of (cellsDone, scraped) samples — these
  // drive both the dashboard number AND the ETA. Anchored to live values so
  // the user sees a moving average, not whatever rps_inst happened to be at
  // the last heartbeat (which was often 0.0 even when work was flowing).
  const cellRate = useRateLR(cellsDone, 15000);
  const scrapeRate = useRateLR(scraped, 15000);

  const phase1ETA = cellRate > 0 ? (cellsRemaining / cellRate) : Infinity;
  // Phase 2's "remaining" is the still-undiscovered Phase 1 work projected as
  // additional places, PLUS the queue. Rough heuristic: assume final
  // discovered ≈ discovered × (totalCells / cellsDone) when cellsDone > 0.
  const projectedDiscovered = cellsDone > 0
    ? Math.round(discoveredTotal * (totalCells / cellsDone))
    : discoveredTotal;
  const phase2Remaining = Math.max(0, projectedDiscovered - scraped);
  const phase2ETA = scrapeRate > 0 ? (phase2Remaining / scrapeRate) : Infinity;

  const workers = window.workersFromSnapshot(snap);
  const workersBusy = {
    p1: workers.p1.filter((w) => w.state === "busy").length,
    p2: workers.p2.filter((w) => w.state === "busy").length,
  };
  const apiKeys = (snap && snap.api_keys) || [];
  const keysUsable = apiKeys.filter(k => k.available !== false && !k.exhausted && !k.cooling_down).length;
  const callsThisRun = apiKeys.reduce((a, k) => a + (k.run_count || 0), 0);
  const coolingCount = apiKeys.filter(k => k.cooling_down).length;
  const discoveryRps = snap && snap.discovery_rps ? snap.discovery_rps : 0;

  // Log filter — default to "events" (warnings + errors + producer
  // anomalies) so the feed shows signal not noise. Toggle to "all" for raw.
  const [logFilter, setLogFilter] = useStateLR("events");
  const [workersExpanded, setWorkersExpanded] = useStateLR(false);

  const filteredLogs = useMemoLR(() => {
    if (logFilter === "all") return logs;
    if (logFilter === "errors") {
      return logs.filter((l) => {
        const lvl = String(l.lvl || "").toLowerCase();
        return lvl === "error" || lvl === "err" || lvl === "critical" || lvl === "fatal";
      });
    }
    // "events": warnings, errors, and anomaly-like INFO lines. Drops the
    // high-volume per-cell completion log so the feed is readable.
    return logs.filter((l) => {
      const lvl = String(l.lvl || "").toLowerCase();
      if (lvl === "warning" || lvl === "warn" || lvl === "error" || lvl === "err" || lvl === "critical" || lvl === "fatal") return true;
      const msg = String(l.msg || "");
      // Highlight interesting INFOs even if they're not warnings.
      if (/\b429\b|rate.?limit|ratelimit|cooldown|retir|failed|crash|exception|exhausted|fallback|phase2_disabled|threadpool_start/i.test(msg)) return true;
      return false;
    });
  }, [logs, logFilter]);

  const status = snap?.active ? "running"
    : (snap?.exit_code === 0 ? "done"
       : (typeof snap?.exit_code === "number" ? "failed" : "idle"));

  async function doStop() {
    if (!runId) return;
    if (!confirm(`Stop ${runId}?`)) return;
    try { await window.stopRun(runId); } catch (e) { alert(`stop failed: ${e.message}`); }
  }

  async function doClearLogs() {
    if (!runId) return;
    if (!confirm("Clear run.log, debug logs, and subprocess.log? run.errors.log is preserved.")) return;
    try {
      await window.api(`/api/runs/${encodeURIComponent(runId)}/logs/truncate`, { method: "POST" });
    } catch (e) {
      alert(`clear logs failed: ${e.message}`);
    }
  }

  const [resumeOpen, setResumeOpen] = React.useState(false);

  async function doResume() {
    // Open the override dialog instead of submitting a hard-coded body.
    // The previous behavior (profile="lean", default workers) lost the
    // original column selection AND offered no way to scale workers down
    // when the machine was thrashing. The dialog pre-fills from the saved
    // run_config (exposed via _summarize_run) so the user only edits what
    // they want to change.
    if (!runId) return;
    setResumeOpen(true);
  }

  if (!runId) {
    return (
      <div className="welcome-grid">
        <section className="welcome-hero">
          <h1 className="welcome-h1">no active run</h1>
          <p className="welcome-sub">start a new run from the sidebar, or pick one from the recent runs list.</p>
          <div style={{display:"flex", gap:8, marginTop:4}}>
            <button className="btn primary" onClick={() => setRoute("new")}><I.Plus size={13}/> new run</button>
          </div>
        </section>
      </div>
    );
  }

  const queriesArr = (snap?.queries) || (run && run.queries_arr) || [];
  const startedAt = snap?.started_at;

  return (
    <div className="mc-grid">
      {resumeOpen && (
        <ResumeDialog
          runId={runId}
          snap={snap}
          onClose={() => setResumeOpen(false)}
        />
      )}
      {/* stats bar — every value is live (elapsed ticks every 1s, rates
          from sliding window) */}
      <div className="mc-stats">
        <div className="stat"><div className="lbl">progress</div><div className="val">{donePct}<span style={{fontSize:12, color:"var(--fg-2)"}}>%</span></div></div>
        <div className="stat"><div className="lbl">places found</div><div className="val">{discoveredTotal.toLocaleString()}</div></div>
        <div className="stat"><div className="lbl">deep scraped</div><div className="val">{scraped.toLocaleString()} <span className="delta">/ {discoveredTotal.toLocaleString()}</span></div></div>
        <div className="stat"><div className="lbl">errors</div><div className="val">{(snap?.errors || 0).toLocaleString()} <span className={`delta ${(snap?.errors || 0) > 0 ? "down" : ""}`}>{errorPct}%</span></div></div>
        <div className="stat"><div className="lbl">elapsed</div><div className="val">{fmtElapsed(elapsedLive)}</div></div>
        <div className="stat"><div className="lbl">api calls</div><div className="val">{apiCalls.toLocaleString()}</div></div>
      </div>

      {/* map with heatmap */}
      <div className="mc-map">
        <window.MapView
          mapStyle={mapStyle}
          boundary={boundary}
          grid={grid}
          cellStates={cells}
          fitTo="boundary"
          overlayLabel={
            <>
              <span className={`chip ${status} dot`}>{status}</span>
              <span className="num" style={{color:"var(--fg-1)"}}>{snap?.city || run?.area || "—"}</span>
              <span className="faint num" style={{fontSize:10.5}}>{runId}</span>
            </>
          }
        />

        {/* legend */}
        <div style={{position:"absolute", bottom:32, left:12, zIndex:5,
                     background:"color-mix(in oklch, var(--bg-1) 92%, transparent)",
                     border:"1px solid var(--line)", borderRadius:6, padding:"8px 12px",
                     display:"flex", gap:14, fontSize:11, fontFamily:"var(--font-mono)",
                     backdropFilter:"blur(6px)"}}>
          <span style={{display:"flex", alignItems:"center", gap:6}}>
            <span style={{width:10, height:10, background:"oklch(0.55 0.005 240 / 0.4)", border:"1px solid oklch(0.55 0.005 240 / 0.7)"}}/> queued <span className="faint">{cellCounts.queued}</span>
          </span>
          <span style={{display:"flex", alignItems:"center", gap:6}}>
            <span style={{width:10, height:10, background:"oklch(0.78 0.16 145 / 0.5)", border:"1px solid var(--st-running)"}}/> running <span className="faint">{cellCounts.running}</span>
          </span>
          <span style={{display:"flex", alignItems:"center", gap:6}}>
            <span style={{width:10, height:10, background:"oklch(0.70 0.10 195 / 0.4)", border:"1px solid var(--st-done)"}}/> done <span className="faint">{cellCounts.done}</span>
          </span>
          <span style={{display:"flex", alignItems:"center", gap:6}}>
            <span style={{width:10, height:10, background:"oklch(0.68 0.16 30 / 0.5)", border:"1px solid var(--st-failed)"}}/> failed <span className="faint">{cellCounts.failed}</span>
          </span>
        </div>
      </div>

      {/* detail / phase breakdown */}
      <div className="mc-detail">
        <div className="mc-detail-head">
          <I.Activity size={13} style={{color:"var(--accent)"}}/>
          <h3>run detail</h3>
          <span style={{marginLeft:"auto", display:"flex", gap:6}}>
            {status === "running" ? (
              <button className="btn ghost sm danger" onClick={doStop}><I.Stop size={11}/> stop</button>
            ) : (
              <button className="btn ghost sm" onClick={doResume}><I.Refresh size={11}/> resume</button>
            )}
            <button className="btn ghost sm" onClick={() => setRoute("results", runId)}>
              <I.ArrowRight size={11}/> results
            </button>
          </span>
        </div>
        <div className="mc-detail-body">
          <div className="phase">
            <div className="phase-head">
              <span className="name" style={{color:"var(--accent)"}}>phase 1 · grid discovery</span>
              <span className="pct">{donePct}% · eta {fmtETA(phase1ETA)}</span>
            </div>
            <div className="bar"><span style={{width:`${donePct}%`}}/></div>
            <div className="phase-stats">
              <div><span className="v num">{cellsDone.toLocaleString()}</span>done</div>
              <div><span className="v num">{cellsInFlight.toLocaleString()}</span>in flight</div>
              <div><span className="v num">{cellsRemaining.toLocaleString()}</span>remaining</div>
              <div><span className="v num">{cellRate.toFixed(1)}<span style={{fontSize:10.5}}>/s</span></span>rate</div>
            </div>
          </div>

          <div className="phase">
            <div className="phase-head">
              <span className="name">phase 2 · deep scrape</span>
              <span className="pct">{scrapedPct}% · eta {fmtETA(phase2ETA)}</span>
            </div>
            <div className="bar"><span style={{width:`${scrapedPct}%`}}/></div>
            <div className="phase-stats">
              <div><span className="v num">{scraped.toLocaleString()}</span>scraped</div>
              <div><span className="v num">{workersBusy.p2}</span>browsers busy</div>
              <div><span className="v num">{queueDepth.toLocaleString()}</span>in queue</div>
              <div><span className="v num">{scrapeRate.toFixed(2)}<span style={{fontSize:10.5}}>/s</span></span>rate</div>
            </div>
          </div>

          {cost > 0 && (
            <div className="phase">
              <div className="phase-head">
                <span className="name">cost meter</span>
                <span className="pct">${cost.toFixed(2)} / ~${costEst.toFixed(2)}</span>
              </div>
              <div className="bar"><span style={{width:`${Math.min(100, (cost/costEst*100) || 0)}%`, background: cost/costEst > 0.8 ? "var(--st-warn)" : undefined}}/></div>
              <div className="phase-stats">
                <div><span className="v num">{apiCalls.toLocaleString()}</span>API calls</div>
                <div><span className="v num">${apiCalls > 0 ? (cost/apiCalls).toFixed(4) : "0.0000"}</span>per call</div>
                <div><span className="v num">${(snap?.discovered || 0) > 0 ? (cost/snap.discovered*1000).toFixed(2) : "0.00"}</span>per 1k places</div>
              </div>
            </div>
          )}

          <div className="kv" style={{marginTop:4}}>
            <dt>area</dt>      <dd>{snap?.city || "—"}</dd>
            <dt>queries</dt>   <dd className="mono">{queriesArr.length} ({queriesArr.slice(0,3).join(", ")}{queriesArr.length > 3 ? "…" : ""})</dd>
            <dt>workers</dt>   <dd className="mono">P1: {workers.p1.length} · P2: {workers.p2.length}</dd>
            <dt>output</dt>    <dd className="mono">runs/{runId}/</dd>
            <dt>started</dt>   <dd className="mono">{startedAt ? startedAt.replace("T", " ") : "—"}</dd>
            <dt>queue</dt>     <dd className="mono">{(snap?.queue_depth || 0).toLocaleString()} pending</dd>
          </div>
        </div>
      </div>

      {/* log stream — filtered. Default = "events" (errors + warnings +
          anomaly-tagged INFOs). The per-cell completion log is dropped from
          this view; switch to "all" for the raw feed. */}
      <div className="mc-log">
        <div style={{display:"flex", gap:6, padding:"4px 6px", borderBottom:"1px solid var(--line-soft)",
                     fontSize:10, fontFamily:"var(--font-mono)", textTransform:"uppercase",
                     letterSpacing:0.06, color:"var(--fg-2)"}}>
          <span style={{marginRight:"auto"}}>log</span>
          <button
            onClick={doClearLogs}
            title="clear rotated info/debug/subprocess logs; keep run.errors.log"
            style={{
              background: "transparent",
              color: "var(--fg-2)",
              border: "1px solid var(--line)",
              padding: "1px 6px",
              borderRadius: 3,
              fontSize: 10,
              fontFamily: "var(--font-mono)",
              cursor: "pointer",
              textTransform: "uppercase",
            }}
          >
            clear
          </button>
          {["events", "errors", "all"].map((mode) => (
            <button
              key={mode}
              onClick={() => setLogFilter(mode)}
              style={{
                background: logFilter === mode ? "var(--accent)" : "transparent",
                color: logFilter === mode ? "var(--bg-0)" : "var(--fg-2)",
                border: "1px solid var(--line)",
                padding: "1px 6px",
                borderRadius: 3,
                fontSize: 10,
                fontFamily: "var(--font-mono)",
                cursor: "pointer",
                textTransform: "uppercase",
              }}
            >
              {mode}
            </button>
          ))}
          <span className="faint num" style={{fontSize:10, marginLeft:6}}>
            {filteredLogs.length}/{logs.length}
          </span>
        </div>
        <div className="log">
          {filteredLogs.length === 0 ? (
            <div className="row">
              <span className="msg muted">
                {logFilter === "all" ? "(waiting for logs…)"
                  : logFilter === "errors" ? "no errors — nice."
                  : "no anomalies — workers humming along."}
              </span>
            </div>
          ) : filteredLogs.slice(-50).reverse().map((l, i) => (
            <div key={i} className="row">
              <span className="t">{l.t}</span>
              <span className={`lvl ${l.lvl}`}>{l.lvl}</span>
              <span className="msg">{l.msg}</span>
            </div>
          ))}
        </div>
      </div>

      {/* workers — collapsed by default. Expand to see per-worker pills.
          At 139 P1 workers the pill grid was visual noise; the digest tells
          you whether the fleet is healthy at a glance. */}
      <div className="mc-workers">
        <div
          style={{display:"flex", alignItems:"center", justifyContent:"space-between",
                  padding:"2px 4px 6px", cursor:"pointer"}}
          onClick={() => setWorkersExpanded((x) => !x)}
          title={workersExpanded ? "click to collapse" : "click to expand per-worker pills"}
        >
          <span style={{fontFamily:"var(--font-mono)", fontSize:10, textTransform:"uppercase", letterSpacing:0.06, color:"var(--fg-2)"}}>
            workers {workersExpanded ? "▾" : "▸"}
          </span>
          <span className="faint num" style={{fontSize:10}}>
            P1 {workersBusy.p1}/{workers.p1.length} busy · P2 {workersBusy.p2}/{workers.p2.length} busy
          </span>
        </div>
        {!workersExpanded && (workers.p1.length > 0 || workers.p2.length > 0) && (
          <div style={{padding:"4px 4px 8px", fontSize:11, color:"var(--fg-2)", lineHeight:1.6}}>
            <div>phase 1 — <span className="num" style={{color:"var(--fg-1)"}}>{workersBusy.p1}</span> hitting the api · <span className="num">{workers.p1.length - workersBusy.p1}</span> idle</div>
            <div>phase 2 — <span className="num" style={{color:"var(--fg-1)"}}>{workersBusy.p2}</span> scraping · <span className="num">{workers.p2.length - workersBusy.p2}</span> idle</div>
          </div>
        )}
        {workersExpanded && workers.p1.length === 0 && workers.p2.length === 0 && (
          <div className="muted" style={{fontSize:11.5, padding:"6px 4px"}}>
            (per-worker activity unavailable — backend hasn't published <code>workers[]</code> yet)
          </div>
        )}
        {workersExpanded && workers.p1.map(w => (
          <div key={"p1-" + w.id} className={`worker ${w.state}`}>
            <span className="id">{w.id}</span>
            <span className="task">
              <span style={{color:"var(--accent)"}}>P1</span> · {w.task} {w.q && <span className="q">· {w.q}</span>}
            </span>
            <span className="dur">{w.dur}</span>
          </div>
        ))}
        {workersExpanded && workers.p2.map(w => (
          <div key={"p2-" + w.id} className={`worker ${w.state}`}>
            <span className="id">{w.id}</span>
            <span className="task">
              <span style={{color:"var(--accent)"}}>P2</span> · <span className="num" style={{fontFamily:"var(--font-mono)", fontSize:10.5}}>{w.task}</span> {w.q && <span className="q">· {w.q}</span>}
            </span>
            <span className="dur">{w.dur}</span>
          </div>
        ))}

        {/* api-key usage — calls per key against the daily quota */}
        <div style={{display:"flex", alignItems:"center", justifyContent:"space-between",
                     padding:"8px 4px 6px", marginTop:8, borderTop:"1px solid var(--line-soft)"}}>
          <span style={{fontFamily:"var(--font-mono)", fontSize:10, textTransform:"uppercase", letterSpacing:0.06, color:"var(--fg-2)"}}>api keys</span>
          <span className="faint num" style={{fontSize:10}}>
            {keysUsable}/{apiKeys.length} usable · {discoveryRps}/s
            {coolingCount > 0 ? ` · ${coolingCount} cooling` : ""}
          </span>
        </div>
        {apiKeys.length === 0 ? (
          <div className="muted" style={{fontSize:11.5, padding:"4px 4px"}}>
            (no Places API key usage yet)
          </div>
        ) : apiKeys.map(k => {
          const unlimited = !k.quota;       // quota 0 / null = no cap
          const cooling = !!k.cooling_down; // parked after repeated 429s
          const pct = unlimited ? 100 : Math.min(100, k.day_count / k.quota * 100);
          const cls = cooling ? "cooling"
            : unlimited ? "unlimited"
            : (k.exhausted ? "full" : (pct >= 80 ? "warn" : ""));
          return (
            <div key={k.alias} className={`keyrow ${k.exhausted ? "exhausted" : ""} ${cooling ? "cooling" : ""}`}
                 title={`${k.alias}: ${(k.day_count||0).toLocaleString()} calls today`
                        + (unlimited ? " · unlimited (no quota cap)" : ` / ${k.quota.toLocaleString()} daily quota`)
                        + (k.run_count ? ` · ${k.run_count.toLocaleString()} this run` : "")
                        + (k.rate_limit_hits ? ` · ${k.rate_limit_hits} rate-limit hits` : "")
                        + (cooling ? ` · COOLING DOWN ${k.cooldown_remaining_s}s after 429s` : "")
                        + (k.exhausted ? " · EXHAUSTED — skipped by rotator" : "")}>
              <span className="ka">{k.alias}</span>
              <span className={`keybar ${cls}`}><span style={{width: (cooling ? 100 : pct) + "%"}}/></span>
              <span className="kn">{cooling ? `cool ${k.cooldown_remaining_s}s`
                : unlimited ? fmtK(k.day_count)
                : fmtK(k.day_count) + "/" + fmtK(k.quota)}</span>
            </div>
          );
        })}
      </div>
    </div>
  );
};

window.LiveRun = LiveRun;
