// parallel-scraper dashboard — vanilla JS, hash routing, Leaflet maps.

(() => {
  const $ = (s, root = document) => root.querySelector(s);
  const $$ = (s, root = document) => Array.from(root.querySelectorAll(s));

  const TILE_URL = "https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png";
  const TILE_DARK_URL = "https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png";

  const state = {
    // routing
    route: "/",
    selectedRunId: null,

    // runs
    runs: [],

    // new-run wizard
    newrunMap: null,
    boundaryLayer: null,
    gridLayer: null,
    drawnItems: null,
    drawControl: null,
    selectedArea: null,    // {source, osm_id, osm_type, custom_polygon, area_name, total_cells}
    activeTab: "city",
    columns: null,         // {catalog, profiles, lean}
    selectedCols: [],      // current checkbox state
    queries: [{ q: "shops", label: "shops" }],
    presets: {},           // local fallback

    // monitor
    monitorMap: null,
    monitorBoundaryLayer: null,
    monitorCellLayer: null,
    cellById: {},          // cell_id -> Leaflet layer
    cellStateById: {},     // cell_id -> last applied class
    es: null,
  };

  // ─── api ──────────────────────────────────────

  async function api(path, opts = {}) {
    const res = await fetch(path, {
      headers: { "content-type": "application/json" },
      ...opts,
    });
    if (!res.ok) {
      const text = await res.text().catch(() => res.statusText);
      throw new Error(`${res.status}: ${text}`);
    }
    return res.json();
  }

  // ─── helpers ──────────────────────────────────

  function escHtml(s) {
    return String(s ?? "")
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }
  function pad(n, w) { return String(n ?? 0).padStart(w, " "); }
  function formatElapsed(s) {
    s = Math.max(0, Math.floor(s));
    const h = String(Math.floor(s / 3600)).padStart(2, "0");
    const m = String(Math.floor((s % 3600) / 60)).padStart(2, "0");
    const sec = String(s % 60).padStart(2, "0");
    if (s >= 3600) return `${h}:${m}:${sec}`;
    return `${m}:${sec}`;
  }

  function showView(name) {
    $$('.view').forEach(v => { v.hidden = (v.dataset.view !== name); });
    $$('.nav-item').forEach(n => {
      const r = n.dataset.route;
      n.classList.toggle("active",
        (r === "/" && name === "welcome") ||
        (r === "/" && name === "monitor") ||
        (r === "/new" && name === "newrun"));
    });
  }

  // ─── routing ──────────────────────────────────

  function parseHash() {
    const h = (location.hash || "").replace(/^#/, "");
    if (!h || h === "/") return { name: "welcome" };
    if (h === "/new")      return { name: "newrun" };
    const m = h.match(/^\/run\/([^/]+)$/);
    if (m) return { name: "monitor", runId: decodeURIComponent(m[1]) };
    return { name: "welcome" };
  }

  function applyRoute() {
    const r = parseHash();
    if (r.name === "welcome") {
      // If there are runs, jump to the latest one's monitor by default.
      if (state.runs.length === 0) showView("welcome");
      else { location.hash = `#/run/${encodeURIComponent(state.runs[0].run_id)}`; return; }
    } else if (r.name === "newrun") {
      showView("newrun");
      requestAnimationFrame(() => requestAnimationFrame(() => ensureNewrunMap()));
    } else if (r.name === "monitor") {
      state.selectedRunId = r.runId;
      showView("monitor");
      loadMonitor(r.runId);
    }
    $$('#run-list li').forEach(el => el.toggleAttribute("aria-selected",
      el.dataset.runId === state.selectedRunId));
  }

  // ─── runs list ────────────────────────────────

  async function refreshRuns({ silent = true } = {}) {
    try {
      const data = await api("/api/runs");
      state.runs = data.runs || [];
      renderRunList();
      // If we're on welcome and now have runs, route to first.
      if (state.runs.length > 0 && !location.hash || location.hash === "#" || location.hash === "#/") {
        if (parseHash().name === "welcome" && state.runs.length > 0) {
          // Don't auto-redirect on initial empty welcome — only if user lands on welcome with runs.
          // Keep welcome screen as the explicit landing; runs are accessible from sidebar.
        }
      }
      // If a previously-selected run vanished, reset.
      if (state.selectedRunId && !state.runs.find(r => r.run_id === state.selectedRunId)) {
        state.selectedRunId = null;
      }
    } catch (err) {
      if (!silent) console.error("refreshRuns failed:", err);
    }
  }

  function renderRunList() {
    const list = $("#run-list");
    list.innerHTML = "";
    if (state.runs.length === 0) {
      list.dataset.empty = "no runs yet";
      return;
    }
    for (const r of state.runs) {
      const li = document.createElement("li");
      li.dataset.runId = r.run_id;
      if (r.run_id === state.selectedRunId) li.setAttribute("aria-selected", "true");
      const dotClass = r.active ? "active" : (r.exit_code && r.exit_code !== 0 ? "failed" : "done");
      const queries = (r.queries || []).join(", ") || "—";
      li.innerHTML = `
        <span class="run-id">${escHtml(r.run_id)}</span>
        <span class="run-meta">
          <span class="dot ${dotClass}"></span>
          <span>${escHtml(r.city || "?")}</span>
          <span>·</span>
          <span>${r.scraped}/${r.discovered}</span>
        </span>`;
      li.addEventListener("click", () => {
        location.hash = `#/run/${encodeURIComponent(r.run_id)}`;
      });
      list.appendChild(li);
    }
  }

  // ─── new-run wizard ───────────────────────────

  function ensureNewrunMap() {
    if (state.newrunMap) { state.newrunMap.invalidateSize(); return state.newrunMap; }
    const map = L.map("newrun-map", { zoomControl: true, attributionControl: true })
      .setView([20.5937, 78.9629], 4);
    L.tileLayer(TILE_URL, { maxZoom: 19, attribution: "© OpenStreetMap" }).addTo(map);
    state.drawnItems = new L.FeatureGroup().addTo(map);
    state.newrunMap = map;
    setTimeout(() => map.invalidateSize(), 200);
    return map;
  }

  function clearNewrunLayers() {
    if (state.boundaryLayer) { state.newrunMap.removeLayer(state.boundaryLayer); state.boundaryLayer = null; }
    if (state.gridLayer)     { state.newrunMap.removeLayer(state.gridLayer); state.gridLayer = null; }
    if (state.drawnItems)    state.drawnItems.clearLayers();
  }

  function selectTab(tab) {
    state.activeTab = tab;
    $$('.tabs .tab').forEach(t => t.setAttribute("aria-selected", t.dataset.tab === tab ? "true" : "false"));
    $$('.tab-panel').forEach(p => p.hidden = p.dataset.panel !== tab);
  }

  function setStep1Status(text) { $("#step1-status").textContent = text; }
  function setStage(num) {
    $$('.config-stage').forEach(s => s.hidden = (s.dataset.stage !== String(num)));
    $("#newrun-stage-label").textContent = num === 1
      ? "stage 1 · area selection"
      : "stage 2 · search & run config";
  }

  async function searchCity() {
    const q = $("#city-query").value.trim();
    if (!q) return;
    const list = $("#city-candidates");
    list.hidden = false;
    list.innerHTML = `<li class="muted small">searching…</li>`;
    try {
      const data = await api("/api/grid-scraper/search-city", {
        method: "POST", body: JSON.stringify({ query: q }),
      });
      const cands = data.candidates || [];
      if (cands.length === 0) { list.innerHTML = `<li class="muted small">no matches</li>`; return; }
      list.innerHTML = "";
      for (const c of cands) {
        const li = document.createElement("li");
        li.innerHTML = `
          <span class="display-name">${escHtml(c.display_name)}</span>
          <span class="meta">OSM ${escHtml(c.osm_type)} #${c.osm_id} · ${escHtml(c.category)}</span>`;
        li.addEventListener("click", () => {
          $$('#city-candidates li').forEach(el => el.removeAttribute("aria-selected"));
          li.setAttribute("aria-selected", "true");
          fetchAndRenderGrid({ source: "city", osm_id: c.osm_id, osm_type: c.osm_type, area_name: c.display_name });
        });
        list.appendChild(li);
      }
    } catch (err) {
      list.innerHTML = `<li class="muted small">error: ${escHtml(err.message)}</li>`;
    }
  }

  async function loadByOsmId() {
    const idStr = $("#osm-id-input").value.trim();
    if (!idStr) return;
    const osm_id = parseInt(idStr, 10);
    const osm_type = $("#osm-type-input").value;
    fetchAndRenderGrid({ source: "osm", osm_id, osm_type, area_name: "" });
  }

  function startDrawMode() {
    ensureNewrunMap();
    clearNewrunLayers();
    state.selectedArea = null;
    setStep1Status("draw a polygon on the map");
    $("#next-stage-btn").disabled = true;
    $("#polygon-clear-btn").hidden = false;
    if (!state.drawControl) {
      state.drawControl = new L.Control.Draw({
        position: "topright",
        draw: {
          polygon: { allowIntersection: false, showArea: true,
                     shapeOptions: { color: "#4a90ff" } },
          rectangle: { shapeOptions: { color: "#4a90ff" } },
          polyline: false, circle: false, marker: false, circlemarker: false,
        },
        edit: { featureGroup: state.drawnItems, edit: false, remove: true },
      });
      state.newrunMap.addControl(state.drawControl);
      state.newrunMap.on(L.Draw.Event.CREATED, (ev) => {
        state.drawnItems.clearLayers();
        state.drawnItems.addLayer(ev.layer);
        const latlngs = ev.layer.getLatLngs()[0];
        const coords = latlngs.map(p => [p.lat, p.lng]);
        const name = $("#polygon-name").value.trim() || "custom polygon";
        fetchAndRenderGrid({ source: "polygon", custom_polygon: coords, area_name: name });
      });
    }
  }

  function clearDraw() {
    if (state.drawnItems) state.drawnItems.clearLayers();
    clearNewrunLayers();
    $("#polygon-clear-btn").hidden = true;
    state.selectedArea = null;
    setStep1Status("no area selected");
    $("#next-stage-btn").disabled = true;
    $("#grid-info").textContent = "";
  }

  async function fetchAndRenderGrid(area) {
    const map = ensureNewrunMap();
    setStep1Status("loading boundary + grid…");
    $("#next-stage-btn").disabled = true;
    $("#grid-info").textContent = "";

    const grid_size_meters = parseInt($("#grid-size-num").value, 10) || 1000;
    const body = { grid_size_meters, grid_type: "square", area_name: area.area_name || "" };
    if (area.source === "polygon") body.custom_polygon = area.custom_polygon;
    else { body.osm_id = area.osm_id; body.osm_type = area.osm_type; }

    try {
      const result = await api("/api/grid-scraper/generate-grid", {
        method: "POST", body: JSON.stringify(body),
      });
      clearNewrunLayers();
      state.boundaryLayer = L.geoJSON(result.boundary_geojson, {
        style: { color: "#4a90ff", weight: 2, fillOpacity: 0.05 },
      }).addTo(map);
      state.gridLayer = L.geoJSON(result.grid_geojson, {
        style: { color: "#7e6cff", weight: 0.4, fillOpacity: 0.10 },
      }).addTo(map);
      try { map.fitBounds(state.boundaryLayer.getBounds(), { padding: [20, 20] }); } catch (e) {}

      const display = result.city_display_name || area.area_name || "(area)";
      setStep1Status(`✓ ${display}`);
      $("#grid-info").textContent = `${result.total_cells} cells`;
      $("#next-stage-btn").disabled = false;

      state.selectedArea = {
        source: area.source,
        osm_id: area.osm_id, osm_type: area.osm_type,
        custom_polygon: area.custom_polygon,
        area_name: display, total_cells: result.total_cells,
      };
    } catch (err) {
      setStep1Status(`error: ${err.message}`);
    }
  }

  // ─── stage 2: queries + columns ───────────────

  function normalizeQuery(item) {
    if (typeof item === "string") {
      const q = item.trim();
      return q ? { q, label: q } : null;
    }
    if (!item || typeof item !== "object") return null;
    const query = (item.q ?? item.query ?? "").toString().trim();
    if (!query) return null;
    const label = (item.label ?? item.name ?? query).toString().trim() || query;
    return { q: query, label };
  }

  function normalizeQueries(items) {
    const out = (items || []).map(normalizeQuery).filter(Boolean);
    return out.length ? out : [{ q: "", label: "" }];
  }

  function renderQueries() {
    const box = $("#queries-list");
    box.innerHTML = "";
    state.queries = normalizeQueries(state.queries);
    state.queries.forEach((q, i) => {
      const row = document.createElement("div");
      row.className = "query-row";
      row.innerHTML = `
        <input type="text" class="query-input" placeholder="search query" title="${escHtml(q.q)}" value="${escHtml(q.q)}">
        <input type="text" class="label-input" placeholder="label" title="${escHtml(q.label)}" value="${escHtml(q.label)}">
        <button type="button" class="rm-q" title="remove">✕</button>`;
      const [qIn, lblIn, rmBtn] = row.children;
      qIn.addEventListener("input", () => {
        state.queries[i].q = qIn.value;
        qIn.title = qIn.value;
        if (!state.queries[i].label || state.queries[i].label === q.label) {
          lblIn.value = qIn.value;
          lblIn.title = qIn.value;
          state.queries[i].label = qIn.value;
        }
      });
      lblIn.addEventListener("input", () => {
        state.queries[i].label = lblIn.value;
        lblIn.title = lblIn.value;
      });
      rmBtn.addEventListener("click", () => {
        if (state.queries.length === 1) return;
        state.queries.splice(i, 1);
        renderQueries();
      });
      box.appendChild(row);
    });
  }

  async function loadColumnCatalog() {
    if (state.columns) return state.columns;
    state.columns = await api("/api/columns");
    renderColumnCheckboxes(state.columns.lean.slice());
    return state.columns;
  }

  function renderColumnCheckboxes(initialChecked) {
    const box = $("#column-checkboxes");
    box.innerHTML = "";
    const checked = new Set(initialChecked);
    state.selectedCols = [...initialChecked];
    for (const [key, label] of Object.entries(state.columns.catalog)) {
      const lbl = document.createElement("label");
      lbl.innerHTML = `<input type="checkbox" data-col="${escHtml(key)}" ${checked.has(key) ? "checked" : ""}>
                       <code>${escHtml(key)}</code> · ${escHtml(label)}`;
      lbl.querySelector("input").addEventListener("change", onCheckboxChanged);
      box.appendChild(lbl);
    }
    updateColsSummary();
  }

  function getCheckedCols() {
    return $$('#column-checkboxes input[type=checkbox]')
      .filter(cb => cb.checked).map(cb => cb.dataset.col);
  }
  function setCheckedCols(cols) {
    const want = new Set(cols);
    $$('#column-checkboxes input[type=checkbox]').forEach(cb => {
      cb.checked = want.has(cb.dataset.col);
    });
    state.selectedCols = [...cols];
    updateColsSummary();
  }
  function findMatchingProfile(cols) {
    if (!state.columns) return null;
    const target = JSON.stringify([...cols].sort());
    for (const [name, list] of Object.entries(state.columns.profiles)) {
      if (JSON.stringify([...list].sort()) === target) return name;
    }
    return null;
  }
  function onProfileChanged() {
    const sel = $("#profile-select");
    if (!sel.value || !state.columns) return;
    const cols = state.columns.profiles[sel.value] || [];
    setCheckedCols(cols);
  }
  function onCheckboxChanged() {
    const cols = getCheckedCols();
    state.selectedCols = cols;
    const match = findMatchingProfile(cols);
    $("#profile-select").value = match || "";
    updateColsSummary();
  }
  function updateColsSummary() {
    const n = state.selectedCols.length;
    $("#cols-summary").textContent = `${n} column${n === 1 ? "" : "s"}`;
  }

  // ─── presets (server-backed JSON store) ───────

  async function loadPresets() {
    try {
      const data = await api("/api/presets/queries");
      state.presets = {};
      for (const [name, queries] of Object.entries(data.presets || {})) {
        state.presets[name] = normalizeQueries(queries);
      }
    } catch (err) {
      console.warn("loadPresets failed:", err);
      state.presets = {};
    }
    renderPresetSelect();
  }

  function renderPresetSelect() {
    const sel = $("#preset-select");
    const cur = sel.value;
    sel.innerHTML = `<option value="">— select a preset —</option>`;
    for (const name of Object.keys(state.presets)) {
      const opt = document.createElement("option");
      opt.value = name; opt.textContent = name;
      sel.appendChild(opt);
    }
    if (state.presets[cur]) sel.value = cur;
    $("#preset-load-btn").disabled = !sel.value;
    $("#preset-delete-btn").disabled = !sel.value;
  }

  function presetLoad() {
    const name = $("#preset-select").value;
    if (!name || !state.presets[name]) return;
    state.queries = normalizeQueries(JSON.parse(JSON.stringify(state.presets[name])));
    renderQueries();
  }

  async function presetSaveAs() {
    const name = $("#preset-name").value.trim();
    if (!name) {
      alert("Preset name required.");
      return;
    }
    const queries = normalizeQueries(state.queries).filter(q => q.q.trim());
    if (!queries.length) {
      alert("Add at least one non-empty query before saving a preset.");
      return;
    }
    try {
      await api("/api/presets/queries", {
        method: "POST",
        body: JSON.stringify({ name, queries }),
      });
      await loadPresets();
      $("#preset-select").value = name;
      $("#preset-load-btn").disabled = false;
      $("#preset-delete-btn").disabled = false;
      $("#preset-name").value = "";
    } catch (err) {
      alert(`save failed: ${err.message}`);
    }
  }

  async function presetDelete() {
    const name = $("#preset-select").value;
    if (!name) return;
    if (!confirm(`Delete preset "${name}"?`)) return;
    try {
      await api(`/api/presets/queries/${encodeURIComponent(name)}`, { method: "DELETE" });
      await loadPresets();
    } catch (err) {
      alert(`delete failed: ${err.message}`);
    }
  }

  // ─── start run ────────────────────────────────

  async function startRun() {
    if (!state.selectedArea) { alert("Pick an area first."); return; }
    const cols = getCheckedCols();
    const matched = findMatchingProfile(cols);
    const queries = normalizeQueries(state.queries).filter(q => q.q.trim());
    state.queries = queries;
    const queriesStr = queries.map(q => q.q).filter(Boolean).join(",");
    const a = state.selectedArea;

    const body = {
      osm_id: a.osm_id || 0,
      osm_type: a.osm_type || "relation",
      city: a.area_name || "area",
      grid_size_meters: parseInt($("#grid-size-num").value, 10) || 1000,
      queries: queriesStr || "shops",
      phase1_workers: parseInt($("#phase1-num").value, 10) || 3,
      phase2_workers: parseInt($("#phase2-num").value, 10) || 5,
      discovery_rps: 2.0,
      consumer_rps: 2.0,
      consumer_delay_ms: 500,
      master_dedup: "inputs/known_placeids.csv",
      dry_run: $("#dry-run").checked,
      profile: matched,
      columns: matched ? null : (cols.length ? cols : null),
    };
    if (a.source === "polygon") body.custom_polygon = a.custom_polygon;
    const maxP = parseInt($("#max-places").value, 10);
    if (!isNaN(maxP)) body.max_places = maxP;

    const btn = $("#start-run-btn");
    btn.disabled = true; btn.textContent = "starting…";
    try {
      const result = await api("/api/runs", { method: "POST", body: JSON.stringify(body) });
      await refreshRuns();
      location.hash = `#/run/${encodeURIComponent(result.run_id)}`;
    } catch (err) {
      alert(`failed to start run: ${err.message}`);
    } finally {
      btn.disabled = false; btn.textContent = "▷ start run";
    }
  }

  // ─── monitor view ─────────────────────────────

  function ensureMonitorMap() {
    if (state.monitorMap) { state.monitorMap.invalidateSize(); return state.monitorMap; }
    const map = L.map("monitor-map", { zoomControl: true, attributionControl: true })
      .setView([20.5937, 78.9629], 5);
    L.tileLayer(TILE_DARK_URL, {
      maxZoom: 19, subdomains: "abcd", r: "@2x",
      attribution: "© OpenStreetMap, © CARTO",
    }).addTo(map);
    state.monitorMap = map;
    setTimeout(() => map.invalidateSize(), 200);
    return map;
  }

  function clearMonitorLayers() {
    if (state.monitorBoundaryLayer) {
      state.monitorMap.removeLayer(state.monitorBoundaryLayer);
      state.monitorBoundaryLayer = null;
    }
    if (state.monitorCellLayer) {
      state.monitorMap.removeLayer(state.monitorCellLayer);
      state.monitorCellLayer = null;
    }
    state.cellById = {};
    state.cellStateById = {};
  }

  async function loadMonitor(runId) {
    const map = ensureMonitorMap();
    clearMonitorLayers();

    let summary;
    try { summary = await api(`/api/runs/${encodeURIComponent(runId)}`); }
    catch (err) { alert(`run not found: ${err.message}`); location.hash = "#/"; return; }

    renderMonitorHeader(summary);
    renderMonitorStats(summary);
    renderMonitorFiles(summary);

    // Try to load grid (may not exist yet for fresh runs).
    try {
      const grid = await api(`/api/runs/${encodeURIComponent(runId)}/grid`);
      renderGridOnMonitor(grid);
      applyCellStates(summary.cells || []);
    } catch (err) {
      // Producer hasn't persisted grid yet — try again shortly.
      setTimeout(async () => {
        try {
          const grid = await api(`/api/runs/${encodeURIComponent(runId)}/grid`);
          renderGridOnMonitor(grid);
          applyCellStates(summary.cells || []);
        } catch {}
      }, 3000);
    }

    // Initial log fetch + start polling.
    refreshLogs(runId);

    // SSE stream for live updates.
    if (state.es) { state.es.close(); state.es = null; }
    const es = new EventSource(`/api/runs/${encodeURIComponent(runId)}/stream`);
    es.addEventListener("snapshot", (ev) => {
      try {
        const snap = JSON.parse(ev.data);
        renderMonitorHeader(snap);
        renderMonitorStats(snap);
        renderMonitorFiles(snap);
        if (snap.cells) applyCellStates(snap.cells);
        // Pull grid if we don't have it yet (run might have started after page load).
        if (!state.monitorCellLayer) {
          api(`/api/runs/${encodeURIComponent(runId)}/grid`).then(g => {
            renderGridOnMonitor(g);
            applyCellStates(snap.cells || []);
          }).catch(() => {});
        }
      } catch (e) { console.warn(e); }
    });
    state.es = es;

    // Poll logs on a timer.
    if (state.logTimer) clearInterval(state.logTimer);
    state.logTimer = setInterval(() => refreshLogs(runId), 3000);
  }

  function renderMonitorHeader(snap) {
    $("#m-runid").textContent = snap.run_id || "—";
    $("#m-area").textContent = snap.city || "—";
    $("#m-subtitle").textContent =
      `queries: ${(snap.queries || []).join(", ") || "—"}` +
      (snap.started_at ? ` · started ${snap.started_at.replace("T", " ")}` : "");

    const pill = $("#m-status");
    let stateName = "idle", label = "idle";
    if (snap.active) { stateName = "running"; label = "running"; }
    else if (snap.exit_code === 0) { stateName = "done"; label = "done"; }
    else if (typeof snap.exit_code === "number") { stateName = "failed"; label = `exited (${snap.exit_code})`; }
    pill.dataset.state = stateName;
    pill.querySelector(".status-label").textContent = label;

    $("#m-stop-btn").hidden = !snap.active;
    $("#m-resume-btn").hidden = snap.active;
  }

  function renderMonitorStats(snap) {
    $("#s-discovered").textContent = (snap.discovered ?? 0).toLocaleString();
    $("#s-scraped").textContent = (snap.scraped ?? 0).toLocaleString();
    $("#s-errors").textContent = (snap.errors ?? 0).toLocaleString();
    $("#s-queue").textContent = (snap.queue_depth ?? 0).toLocaleString();
    $("#s-rps").textContent = (snap.rps_inst ?? 0).toFixed(2);
    $("#s-elapsed").textContent = formatElapsed(snap.elapsed_s ?? 0);

    const cellsTotal = snap.cells_total || 0;
    const cellsDone = snap.cells_done || 0;
    $("#p-cells-text").textContent = `${cellsDone} / ${cellsTotal} cells`;
    $("#p-cells-fill").style.width = cellsTotal > 0 ? `${(cellsDone / cellsTotal * 100).toFixed(1)}%` : "0%";

    const pidsDone = snap.placeids_done || 0;
    const pidsTotal = (snap.placeids_done || 0) + (snap.placeids_pending || 0) + (snap.placeids_failed || 0) || (snap.discovered || 0);
    $("#p-pids-text").textContent = `${pidsDone} / ${pidsTotal} places`;
    $("#p-pids-fill").style.width = pidsTotal > 0 ? `${(pidsDone / pidsTotal * 100).toFixed(1)}%` : "0%";
  }

  function renderMonitorFiles(snap) {
    const ul = $("#m-files");
    ul.innerHTML = "";
    const files = [
      ["phase1_discovered.csv", snap.phase1_csv_rows],
      ["phase2_data.csv", snap.phase2_csv_rows],
      ["phase2_failures.csv", snap.failures_rows],
      ["phase2_data.full.csv.gz", null],
      ["state_cells.csv", snap.cells_total],
      ["state_placeids.csv", null],
      ["heartbeat.jsonl", null],
      ["run.log", null],
      ["grid.geojson", null],
    ];
    for (const [name, count] of files) {
      const li = document.createElement("li");
      li.innerHTML = `<span>${escHtml(name)}</span>${count != null ? `<span class="count">${count} rows</span>` : ""}`;
      ul.appendChild(li);
    }
  }

  function renderGridOnMonitor(geojson) {
    if (!geojson || !geojson.features) return;
    const map = ensureMonitorMap();
    if (state.monitorCellLayer) {
      map.removeLayer(state.monitorCellLayer);
      state.monitorCellLayer = null;
      state.cellById = {};
      state.cellStateById = {};
    }
    const layer = L.geoJSON(geojson, {
      style: () => ({ className: "cell-pending", fillOpacity: 1, weight: 0 }),
      onEachFeature: (feature, lyr) => {
        const cid = feature.properties?.cell_id;
        if (cid) {
          state.cellById[cid] = lyr;
          lyr.bindTooltip(cid, { direction: "top", className: "cell-tip" });
        }
      },
    }).addTo(map);
    state.monitorCellLayer = layer;
    try { map.fitBounds(layer.getBounds(), { padding: [20, 20] }); } catch (e) {}
  }

  function applyCellStates(cells) {
    for (const c of cells) {
      const lyr = state.cellById[c.cell_id];
      if (!lyr) continue;
      const cls = `cell-${stateToClass(c.status)}`;
      if (state.cellStateById[c.cell_id] === cls) continue;
      const path = lyr.getElement?.();
      if (path) {
        path.classList.remove("cell-pending", "cell-progress", "cell-done", "cell-failed");
        path.classList.add(cls);
      }
      state.cellStateById[c.cell_id] = cls;
    }
  }

  function stateToClass(status) {
    if (status === "done") return "done";
    if (status === "failed") return "failed";
    if (status === "in_progress") return "progress";
    return "pending";
  }

  async function refreshLogs(runId) {
    try {
      const data = await api(`/api/runs/${encodeURIComponent(runId)}/logs?n=80`);
      renderLogTable(data.lines || []);
    } catch (err) {
      // silent — logs may not be ready
    }
  }

  function renderLogTable(lines) {
    const tbody = $("#log-tbody");
    if (!lines.length) {
      tbody.innerHTML = `<tr><td colspan="4" class="muted">(waiting…)</td></tr>`;
      return;
    }
    tbody.innerHTML = lines.slice(-50).reverse().map(l => `
      <tr>
        <td>${escHtml(l.ts || "")}</td>
        <td class="lvl-${escHtml(l.level || "INFO")}">${escHtml(l.level || "INFO")}</td>
        <td title="${escHtml(l.msg)}">${escHtml(l.msg || "")}</td>
        <td class="cell-id">${escHtml(l.cell_id || "")}</td>
      </tr>`).join("");
  }

  // ─── monitor actions ──────────────────────────

  async function stopRun() {
    if (!state.selectedRunId) return;
    if (!confirm(`Stop ${state.selectedRunId}?`)) return;
    try {
      await api(`/api/runs/${encodeURIComponent(state.selectedRunId)}/stop`, { method: "POST" });
      await refreshRuns();
    } catch (err) { alert(`stop failed: ${err.message}`); }
  }

  async function resumeRun() {
    if (!state.selectedRunId) return;
    try {
      await api("/api/runs", {
        method: "POST",
        body: JSON.stringify({ run_id: state.selectedRunId, profile: "lean" }),
      });
      await refreshRuns();
      loadMonitor(state.selectedRunId);
    } catch (err) { alert(`resume failed: ${err.message}`); }
  }

  // ─── wiring ───────────────────────────────────

  document.addEventListener("DOMContentLoaded", async () => {
    await refreshRuns({ silent: true });
    applyRoute();

    window.addEventListener("hashchange", applyRoute);
    $("#refresh-btn").addEventListener("click", () => refreshRuns({ silent: false }));

    // tabs
    $$('.tabs .tab').forEach(t => t.addEventListener("click", () => selectTab(t.dataset.tab)));

    // city / osm / draw
    $("#city-search-btn").addEventListener("click", searchCity);
    $("#city-query").addEventListener("keydown", e => {
      if (e.key === "Enter") { e.preventDefault(); searchCity(); }
    });
    $("#osm-load-btn").addEventListener("click", loadByOsmId);
    $("#osm-id-input").addEventListener("keydown", e => {
      if (e.key === "Enter") { e.preventDefault(); loadByOsmId(); }
    });
    $("#polygon-draw-btn").addEventListener("click", startDrawMode);
    $("#polygon-clear-btn").addEventListener("click", clearDraw);

    // grid size two-way bind
    const gsRange = $("#grid-size-range"), gsNum = $("#grid-size-num");
    gsRange.addEventListener("input", () => { gsNum.value = gsRange.value; });
    gsNum.addEventListener("change", () => { gsRange.value = gsNum.value; });
    // workers two-way binds (phase 1 + phase 2)
    const p1r = $("#phase1-range"), p1n = $("#phase1-num");
    p1r.addEventListener("input", () => { p1n.value = p1r.value; });
    p1n.addEventListener("change", () => { p1r.value = Math.min(p1r.max, Math.max(p1r.min, p1n.value)); });
    const p2r = $("#phase2-range"), p2n = $("#phase2-num");
    p2r.addEventListener("input", () => { p2n.value = p2r.value; });
    p2n.addEventListener("change", () => { p2r.value = Math.min(p2r.max, Math.max(p2r.min, p2n.value)); });

    // grid type seg
    $$('.seg-btn').forEach(b => b.addEventListener("click", () => {
      if (b.disabled) return;
      $$('.seg-btn').forEach(x => x.classList.toggle("active", x === b));
    }));

    // stage navigation
    $("#next-stage-btn").addEventListener("click", async () => {
      await loadColumnCatalog();
      renderQueries();
      loadPresets();
      setStage(2);
    });
    $("#back-stage-btn").addEventListener("click", () => setStage(1));

    // queries + presets + columns
    $("#add-query-btn").addEventListener("click", () => {
      state.queries.push({ q: "", label: "" });
      renderQueries();
    });
    $("#preset-select").addEventListener("change", () => {
      $("#preset-load-btn").disabled = !$("#preset-select").value;
      $("#preset-delete-btn").disabled = !$("#preset-select").value;
    });
    $("#preset-load-btn").addEventListener("click", presetLoad);
    $("#preset-save-btn").addEventListener("click", presetSaveAs);
    $("#preset-delete-btn").addEventListener("click", presetDelete);
    $("#profile-select").addEventListener("change", onProfileChanged);

    // start run
    $("#start-run-btn").addEventListener("click", startRun);

    // monitor actions
    $("#m-stop-btn").addEventListener("click", stopRun);
    $("#m-resume-btn").addEventListener("click", resumeRun);

    // periodic light refresh of runs list
    setInterval(() => refreshRuns({ silent: true }), 10000);

    // keyboard shortcuts
    document.addEventListener("keydown", (e) => {
      if ((e.ctrlKey || e.metaKey) && e.key === "k") {
        e.preventDefault();
        if (parseHash().name === "newrun") $("#city-query")?.focus();
      }
      if (e.key === "Escape") {
        const r = parseHash();
        if (r.name === "newrun") location.hash = "#/";
      }
    });
  });
})();
