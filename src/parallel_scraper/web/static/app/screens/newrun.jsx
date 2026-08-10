// New Run wizard — compact left config rail + always-on Leaflet map preview.
// Two stages: area selection, queries+workers. Stage progress at top.

const { useState: useStateNR, useEffect: useEffectNR, useMemo: useMemoNR, useRef: useRefNR } = React;

const NewRun = ({ setRoute, mapStyle }) => {
  const I = window.Icons;
  const [stage, setStage] = useStateNR(1);
  const [areaMode, setAreaMode] = useStateNR("city");

  // city search
  const [searchQ, setSearchQ] = useStateNR("");
  const [searchResults, setSearchResults] = useStateNR([]);
  const [searching, setSearching] = useStateNR(false);
  const [selectedAreaKey, setSelectedAreaKey] = useStateNR(null);

  // osm
  const [osmId, setOsmId] = useStateNR("");
  const [osmType, setOsmType] = useStateNR("relation");

  // draw
  const [drawCustomPolygon, setDrawCustomPolygon] = useStateNR(null);
  const [drawAreaName, setDrawAreaName] = useStateNR("");

  // upload (GeoJSON file)
  const [uploadAreaName, setUploadAreaName] = useStateNR("");
  const [uploadFileName, setUploadFileName] = useStateNR("");

  // grid config
  const [gridSize, setGridSize] = useStateNR(1000);
  const [gridShape, setGridShape] = useStateNR("square");

  // resolved area + grid GeoJSON
  const [areaResolved, setAreaResolved] = useStateNR(null);  // { name, total_cells, source, osm_id, osm_type, custom_polygon }
  const [boundaryGJ, setBoundaryGJ] = useStateNR(null);
  const [gridGJ, setGridGJ] = useStateNR(null);
  const [gridLoading, setGridLoading] = useStateNR(false);
  const [gridError, setGridError] = useStateNR(null);

  // stage 2 — queries
  const seededRef = useRefNR(false);
  const [queries, setQueries] = useStateNR(() => {
    if (window.__starterQueries && window.__starterQueries.length) {
      const q = [...window.__starterQueries]; window.__starterQueries = null; return q;
    }
    return ["shops"];
  });
  useEffectNR(() => {
    if (seededRef.current) return; seededRef.current = true;
    if (window.__starterQueries && window.__starterQueries.length) {
      setQueries([...window.__starterQueries]); window.__starterQueries = null;
    }
  }, []);

  const [workers1, setWorkers1] = useStateNR(3);
  const [workers2, setWorkers2] = useStateNR(5);
  const [recycleAfter, setRecycleAfter] = useStateNR(50);
  const [columnsProfile, setColumnsProfile] = useStateNR("lean");
  const [customCols, setCustomCols] = useStateNR([]);
  const [showColumnsPicker, setShowColumnsPicker] = useStateNR(false);
  const [dryRun, setDryRun] = useStateNR(false);
  const [maxPlaces, setMaxPlaces] = useStateNR("");
  const [noImages, setNoImages] = useStateNR(false);
  const [captureShots, setCaptureShots] = useStateNR(false);
  const [phase1Only, setPhase1Only] = useStateNR(false);
  const [starting, setStarting] = useStateNR(false);
  // Custom run name. Empty -> server generates par_YYYYMMDD_HHMMSS_xxxxxx.
  // Otherwise user-supplied; sanitized to filesystem-safe chars on send.
  const [customName, setCustomName] = useStateNR("");

  const cols = window.useColumns();
  // Live API-key stats — used to display the auto-scaled Phase-1 rate / worker
  // count that the backend will actually pick at run start.
  const apiStatus = window.useApiKeys ? window.useApiKeys(15000) : null;
  // Seed customCols from the chosen profile once the catalog loads, and refresh
  // them when the user switches profile from the dropdown. The "custom" profile
  // is what the per-column checkboxes flip into — don't overwrite it here.
  useEffectNR(() => {
    if (!cols || columnsProfile === "custom") return;
    const profKey = columnsProfile === "standard" ? "default"
                  : columnsProfile === "full"     ? "rich"
                  : columnsProfile;
    const preset = cols.profiles?.[profKey] || cols.lean || [];
    setCustomCols([...preset]);
  }, [cols, columnsProfile]);
  const { presets, refresh: refreshPresets, loading: presetsLoading } = window.usePresets();
  const [presetSel, setPresetSel] = useStateNR("");
  const [presetName, setPresetName] = useStateNR("");

  // Leaflet draw integration — only enabled when areaMode === "draw"
  const mapRef = useRefNR(null);
  const drawCtrlRef = useRefNR(null);
  const drawnRef = useRefNR(null);
  const onMapReady = (m) => { mapRef.current = m; };

  useEffectNR(() => {
    const map = mapRef.current; if (!map || !window.L) return;
    // Tear down previous draw bits
    if (drawCtrlRef.current) { map.removeControl(drawCtrlRef.current); drawCtrlRef.current = null; }
    if (drawnRef.current)    { map.removeLayer(drawnRef.current);  drawnRef.current = null; }
    if (areaMode !== "draw") return;
    const drawn = new L.FeatureGroup().addTo(map);
    drawnRef.current = drawn;
    const ctrl = new L.Control.Draw({
      position: "topright",
      draw: {
        polygon: { allowIntersection: false, showArea: true, shapeOptions: { color: "#7be060" } },
        rectangle: { shapeOptions: { color: "#7be060" } },
        polyline: false, circle: false, marker: false, circlemarker: false,
      },
      edit: { featureGroup: drawn, edit: false, remove: true },
    });
    map.addControl(ctrl);
    drawCtrlRef.current = ctrl;
    const onCreated = (ev) => {
      drawn.clearLayers(); drawn.addLayer(ev.layer);
      const latlngs = ev.layer.getLatLngs()[0];
      const coords = latlngs.map(p => [p.lat, p.lng]);
      setDrawCustomPolygon(coords);
      void resolveAreaFromPolygon(coords, drawAreaName || "custom polygon");
    };
    map.on(L.Draw.Event.CREATED, onCreated);
    return () => {
      map.off(L.Draw.Event.CREATED, onCreated);
    };
  }, [areaMode]);

  // ── area resolution helpers ────────────────────────────────────

  function bboxToPolygon(bbox) {
    if (!Array.isArray(bbox) || bbox.length !== 4) return null;
    const [south, north, west, east] = bbox.map(Number);
    if (![south, north, west, east].every(Number.isFinite)) return null;
    if (north <= south || east <= west) return null;
    return [[south, west], [south, east], [north, east], [north, west], [south, west]];
  }

  async function resolveCity() {
    if (!searchQ.trim()) return;
    setSearching(true);
    try {
      const r = await window.searchCity(searchQ.trim());
      setSearchResults(r);
      if (r.length) {
        const preferred = r.find(x => x.osm_type === "relation")
          || r.find(x => x.osm_type === "way")
          || r[0];
        setSelectedAreaKey(preferred.id);
        void resolveAreaFromCandidate(preferred);
      }
    } catch (e) {
      setSearchResults([]); setGridError(e.message);
    } finally { setSearching(false); }
  }

  async function resolveAreaFromCandidate(c) {
    setSelectedAreaKey(c.id);
    setGridLoading(true); setGridError(null);
    try {
      const bboxPolygon = c.osm_type === "node" ? bboxToPolygon(c.bbox) : null;
      const gridRequest = bboxPolygon
        ? { custom_polygon: bboxPolygon, area_name: c.name, grid_size_meters: gridSize, grid_type: gridShape }
        : { osm_id: c.osm_id, osm_type: c.osm_type, area_name: c.name, grid_size_meters: gridSize, grid_type: gridShape };
      const g = await window.generateGrid(gridRequest);
      setBoundaryGJ(g.boundary_geojson); setGridGJ(g.grid_geojson);
      if ((g.total_cells || 0) <= 0) {
        setAreaResolved(null);
        setGridError("Selected area produced 0 grid cells. Choose a boundary result, use an OSM relation/way, or draw a larger area.");
        return;
      }
      setAreaResolved({ name: g.city_display_name || c.name, total_cells: g.total_cells,
                        source: bboxPolygon ? "bbox" : "osm", osm_id: c.osm_id,
                        osm_type: c.osm_type, custom_polygon: bboxPolygon });
    } catch (e) {
      setGridError(e.message); setAreaResolved(null);
    } finally { setGridLoading(false); }
  }

  async function resolveAreaFromOsm() {
    const id = parseInt(osmId, 10);
    if (!id) { setGridError("OSM id required"); return; }
    setGridLoading(true); setGridError(null);
    try {
      const g = await window.generateGrid({ osm_id: id, osm_type: osmType, area_name: "",
                                            grid_size_meters: gridSize, grid_type: gridShape });
      setBoundaryGJ(g.boundary_geojson); setGridGJ(g.grid_geojson);
      if ((g.total_cells || 0) <= 0) {
        setAreaResolved(null);
        setGridError("Selected OSM object produced 0 grid cells. Use a relation/way boundary or draw a larger area.");
        return;
      }
      setAreaResolved({ name: g.city_display_name || `OSM ${osmType} #${id}`, total_cells: g.total_cells,
                        source: "osm", osm_id: id, osm_type: osmType });
    } catch (e) { setGridError(e.message); setAreaResolved(null); }
    finally { setGridLoading(false); }
  }

  // GeoJSON -> [[lat,lng],...] rings (JS mirror of boundary.polygon_rings_from_geojson).
  // Accepts Polygon / MultiPolygon / Feature / FeatureCollection; outer rings only.
  // GeoJSON coords are (lon, lat) — swapped here to the wizard's (lat, lng).
  function ringsFromGeoJSON(data) {
    const swap = ring => ring.map(([lng, lat]) => [Number(lat), Number(lng)]);
    if (!data || typeof data !== "object") throw new Error("not a GeoJSON object");
    if (data.type === "FeatureCollection") {
      const rings = (data.features || []).flatMap(f => ringsFromGeoJSON(f));
      if (!rings.length) throw new Error("no Polygon/MultiPolygon features found");
      return rings;
    }
    if (data.type === "Feature") return ringsFromGeoJSON(data.geometry || {});
    if (data.type === "Polygon") return [swap(data.coordinates[0])];
    if (data.type === "MultiPolygon") return data.coordinates.map(poly => swap(poly[0]));
    throw new Error(`unsupported GeoJSON type: ${data.type}`);
  }

  function onGeojsonFile(file) {
    if (!file) return;
    setUploadFileName(file.name);
    const reader = new FileReader();
    reader.onload = () => {
      try {
        const rings = ringsFromGeoJSON(JSON.parse(reader.result));
        // Single ring goes flat (legacy shape); multiple rings nest (MultiPolygon).
        const coords = rings.length === 1 ? rings[0] : rings;
        const name = uploadAreaName || file.name.replace(/\.(geo)?json$/i, "");
        void resolveAreaFromPolygon(coords, name);
      } catch (e) {
        setGridError(`could not parse ${file.name}: ${e.message}`);
        setAreaResolved(null);
      }
    };
    reader.onerror = () => setGridError(`could not read ${file.name}`);
    reader.readAsText(file);
  }

  async function resolveAreaFromPolygon(coords, name, source = "polygon", extra = {}) {
    setGridLoading(true); setGridError(null);
    try {
      const g = await window.generateGrid({ custom_polygon: coords, area_name: name,
                                            grid_size_meters: gridSize, grid_type: gridShape });
      setBoundaryGJ(g.boundary_geojson); setGridGJ(g.grid_geojson);
      if ((g.total_cells || 0) <= 0) {
        setAreaResolved(null);
        setGridError("Selected polygon produced 0 grid cells. Draw a larger area or reduce grid size.");
        return;
      }
      setAreaResolved({ name, total_cells: g.total_cells, source, custom_polygon: coords, ...extra });
    } catch (e) { setGridError(e.message); setAreaResolved(null); }
    finally { setGridLoading(false); }
  }

  // Re-resolve grid when size or shape changes (only if area already picked).
  useEffectNR(() => {
    if (!areaResolved) return;
    const t = setTimeout(() => {
      if (areaResolved.source === "osm") {
        void resolveAreaFromCandidate({ id: selectedAreaKey || `${areaResolved.osm_type}-${areaResolved.osm_id}`,
                                        osm_id: areaResolved.osm_id, osm_type: areaResolved.osm_type, name: areaResolved.name });
      } else if (areaResolved.source === "polygon" || areaResolved.source === "bbox") {
        void resolveAreaFromPolygon(areaResolved.custom_polygon, areaResolved.name, areaResolved.source,
                                    { osm_id: areaResolved.osm_id, osm_type: areaResolved.osm_type });
      }
    }, 250);
    return () => clearTimeout(t);
    // eslint-disable-next-line
  }, [gridSize, gridShape]);

  const cells = areaResolved ? areaResolved.total_cells : 0;
  const validQueries = queries.map(q => (q || "").trim()).filter(Boolean);
  const callsEstimate = cells * validQueries.length;
  // Auto-scaled Phase-1: rate = active_keys × per_key_qpm/60 × utilization.
  // Workers are backlog capacity; per-key request slots do the actual pacing.
  const activeKeys = apiStatus?.active || 0;
  const PER_KEY_QPM = 600, UTIL = 0.85;
  const autoRps = activeKeys * (PER_KEY_QPM / 60) * UTIL;
  const autoWorkers = activeKeys
    ? Math.min(400, Math.max(workers1, Math.max(32, activeKeys * 10)))
    : Math.max(1, workers1);
  // ETA from the auto-scaled rate (was using a bogus `workers1 * 40` constant).
  const etaSeconds = validQueries.length && autoRps > 0
    ? Math.ceil(callsEstimate / autoRps)
    : 0;
  const etaText = etaSeconds <= 0 ? "—"
    : etaSeconds < 60 ? `${etaSeconds}s`
    : etaSeconds < 3600 ? `${Math.ceil(etaSeconds / 60)}m`
    : `${(etaSeconds / 3600).toFixed(1)}h`;

  // ── start run ────────────────────────────────────────────────

  // Run names become filesystem directory names. Allow [A-Za-z0-9_-] only;
  // replace others with _, strip leading dots so the dir isn't hidden.
  function sanitizeRunName(s) {
    return (s || "").trim().replace(/[^A-Za-z0-9_-]/g, "_").replace(/^\.+/, "");
  }
  const sanitizedName = sanitizeRunName(customName);

  async function doStartRun() {
    if (!areaResolved) return;
    if (cells <= 0) {
      alert("Pick an area that creates at least one grid cell.");
      return;
    }
    if (validQueries.length === 0) {
      alert("Add at least one search query.");
      return;
    }
    const queriesStr = validQueries.join(",");
    const body = {
      osm_id: areaResolved.osm_id || 0,
      osm_type: areaResolved.osm_type || "relation",
      city: areaResolved.name || "area",
      grid_size_meters: gridSize,
      queries: queriesStr,
      phase1_workers: workers1,
      phase2_workers: phase1Only ? 0 : workers2,
      worker_browser_recycle_after: recycleAfter,
      dry_run: dryRun,
      no_image_download: noImages,
      capture_screenshots: captureShots,
    };
    if (sanitizedName) {
      body.run_id = sanitizedName;
    }
    if (columnsProfile === "custom") {
      if (!customCols.length) { alert("Pick at least one column."); setStarting(false); return; }
      body.columns = [...customCols];
    } else {
      body.profile = columnsProfile === "standard" ? "default"
                   : columnsProfile === "full" ? "rich"
                   : columnsProfile;
    }
    const mp = parseInt(maxPlaces, 10);
    if (!isNaN(mp)) body.max_places = mp;
    if (areaResolved.source === "polygon" || areaResolved.source === "bbox") {
      body.custom_polygon = areaResolved.custom_polygon;
    }
    setStarting(true);
    try {
      const r = await window.startRun(body);
      setRoute("live", r.run_id);
    } catch (e) {
      alert(`failed to start run: ${e.message}`);
    } finally { setStarting(false); }
  }

  async function doSavePreset() {
    const name = presetName.trim();
    if (!name) { alert("preset name required"); return; }
    const qs = queries.map(q => ({ q: (q || "").trim(), label: (q || "").trim() })).filter(x => x.q);
    if (!qs.length) { alert("add at least one query first"); return; }
    try { await window.savePreset(name, qs); setPresetName(""); refreshPresets(); }
    catch (e) { alert(`save failed: ${e.message}`); }
  }
  function doLoadPreset() {
    if (!presetSel || !presets[presetSel]) return;
    const p = presets[presetSel];
    setQueries(p.map(x => typeof x === "string" ? x : (x.q || x.query || "")).filter(Boolean));
  }
  async function doDeletePreset() {
    if (!presetSel) return;
    if (!confirm(`Delete preset "${presetSel}"?`)) return;
    try { await window.deletePreset(presetSel); setPresetSel(""); refreshPresets(); }
    catch (e) { alert(`delete failed: ${e.message}`); }
  }

  return (
    <div className="run-layout">
      <section className="run-config">
        <header className="run-config-head">
          <h2>new run</h2>
          <div className="muted" style={{fontSize:12}}>
            stage {stage} · {stage===1 ? "define area & grid" : "queries & workers"}
          </div>
          <div className="stage-progress">
            <span className={`stage-pill ${stage>=1 ? "active" : ""}`}/>
            <span className={`stage-pill ${stage>=2 ? "active" : ""}`}/>
            <span className="stage-num">{stage}/2</span>
          </div>
        </header>

        <div className="run-config-body">
          {stage === 1 && (
            <>
              <div className="field">
                <div className="field-label">area · how to define it</div>
                <div className="seg" style={{width:"100%"}}>
                  <button aria-pressed={areaMode==="city"}    onClick={()=>setAreaMode("city")} style={{flex:1, justifyContent:"center"}}>
                    <I.Search size={11}/> city
                  </button>
                  <button aria-pressed={areaMode==="osm"}     onClick={()=>setAreaMode("osm")} style={{flex:1, justifyContent:"center"}}>
                    <I.Boundary size={11}/> OSM id
                  </button>
                  <button aria-pressed={areaMode==="draw"}    onClick={()=>setAreaMode("draw")} style={{flex:1, justifyContent:"center"}}>
                    <I.Pen size={11}/> draw
                  </button>
                  <button aria-pressed={areaMode==="upload"}  onClick={()=>setAreaMode("upload")} style={{flex:1, justifyContent:"center"}}>
                    <I.Upload size={11}/> upload
                  </button>
                </div>
              </div>

              {areaMode === "city" && (
                <div className="field">
                  <div className="field-row">
                    <input className="input" placeholder="city, region, country" value={searchQ}
                           onChange={e=>setSearchQ(e.target.value)}
                           onKeyDown={e=>{ if (e.key==="Enter") { e.preventDefault(); resolveCity(); } }}/>
                    <button className="btn" onClick={resolveCity} disabled={searching || !searchQ.trim()}>
                      <I.Search size={11}/> {searching ? "…" : "search"}
                    </button>
                  </div>
                  <div className="search-results">
                    {searchResults.map(s => (
                      <div key={s.id} className="search-result"
                           aria-selected={s.id === selectedAreaKey}
                           onClick={()=>resolveAreaFromCandidate(s)}>
                        <div className="nm">{s.name}</div>
                        <div className="sub">{s.sub}</div>
                      </div>
                    ))}
                    {searchResults.length === 0 && !searching && (
                      <div className="muted" style={{fontSize:11.5, padding:"4px 8px"}}>type a city name and hit search.</div>
                    )}
                  </div>
                </div>
              )}

              {areaMode === "osm" && (
                <div className="field">
                  <div className="field-row">
                    <input className="input mono" placeholder="13312356" value={osmId}
                           onChange={e=>setOsmId(e.target.value)}
                           onKeyDown={e=>{ if (e.key==="Enter") { e.preventDefault(); resolveAreaFromOsm(); } }}/>
                    <select className="input" style={{width:110}} value={osmType} onChange={e=>setOsmType(e.target.value)}>
                      <option value="relation">relation</option>
                      <option value="way">way</option>
                      <option value="node">node</option>
                    </select>
                    <button className="btn" onClick={resolveAreaFromOsm}><I.Download size={11}/> load</button>
                  </div>
                  <p className="help">paste an OSM relation/way ID — boundary is fetched from Nominatim and grid-tiled.</p>
                </div>
              )}

              {areaMode === "draw" && (
                <div className="field">
                  <p className="help">use the toolbar on the map (top-right) to draw a polygon or rectangle.</p>
                  <div className="field-row">
                    <input className="input" placeholder="area name (e.g. Bandra West)"
                           value={drawAreaName} onChange={e=>setDrawAreaName(e.target.value)}/>
                  </div>
                </div>
              )}

              {areaMode === "upload" && (
                <div className="field">
                  <p className="help">
                    upload a GeoJSON boundary — Polygon, MultiPolygon, Feature or
                    FeatureCollection (e.g. an H3-derived coverage boundary). MultiPolygons
                    grid every part.
                  </p>
                  <div className="field-row">
                    <input className="input" placeholder="area name (e.g. Orkla Bangalore)"
                           value={uploadAreaName} onChange={e=>setUploadAreaName(e.target.value)}/>
                  </div>
                  <div className="field-row">
                    <label className="btn" style={{cursor:"pointer"}}>
                      <I.Upload size={11}/> choose .geojson
                      <input type="file" accept=".geojson,.json,application/geo+json,application/json"
                             style={{display:"none"}}
                             onChange={e=>{ onGeojsonFile(e.target.files && e.target.files[0]); e.target.value = ""; }}/>
                    </label>
                    {uploadFileName && <span className="muted" style={{fontSize:11.5}}>{uploadFileName}</span>}
                  </div>
                </div>
              )}

              <hr className="hr"/>

              <div className="field">
                <div className="field-label">grid size · cell edge in meters</div>
                <div className="field-row">
                  <input type="range" className="slider" min={250} max={5000} step={50}
                         value={gridSize} onChange={e=>setGridSize(+e.target.value)}/>
                  <input className="input mono" style={{width:90, textAlign:"right"}}
                         value={gridSize} onChange={e=>setGridSize(+e.target.value || 1000)}/>
                  <span className="muted" style={{fontSize:11}}>m</span>
                </div>
                <p className="help">smaller cells = more API calls but better completeness near dense urban cores.</p>
              </div>

              <div className="field">
                <div className="field-label">grid shape</div>
                <div className="seg" style={{width:"100%"}}>
                  <button aria-pressed={gridShape==="square"} onClick={()=>setGridShape("square")} style={{flex:1, justifyContent:"center"}}>
                    <I.Square size={11}/> square
                  </button>
                  <button aria-pressed={gridShape==="hex"} onClick={()=>setGridShape("hex")} style={{flex:1, justifyContent:"center"}}>
                    <I.Hex size={11}/> hexagon
                  </button>
                </div>
              </div>

              {gridLoading && (
                <div className="panel" style={{padding:"8px 12px", fontSize:12}}>
                  <span className="muted">loading boundary + grid…</span>
                </div>
              )}
              {gridError && (
                <div className="panel" style={{padding:"8px 12px", fontSize:12, color:"var(--st-failed)"}}>
                  error: {gridError}
                </div>
              )}
              {areaResolved && !gridLoading && (
                <div className="panel" style={{padding:"10px 12px", display:"flex", alignItems:"center", gap:10}}>
                  <I.Check size={13} style={{color:"var(--accent)"}}/>
                  <div style={{flex:1, fontSize:12.5}}>
                    <div style={{fontWeight:500}}>{areaResolved.name}</div>
                    <div className="muted num" style={{fontSize:10.5}}>
                      {areaResolved.source === "osm"
                        ? `OSM ${areaResolved.osm_type} #${areaResolved.osm_id}`
                        : areaResolved.source === "bbox"
                          ? `bounding box from OSM ${areaResolved.osm_type} #${areaResolved.osm_id}`
                          : "custom polygon"}
                    </div>
                  </div>
                  <span className="chip num">{(areaResolved.total_cells || 0).toLocaleString()} cells</span>
                </div>
              )}
            </>
          )}

          {stage === 2 && (
            <>
              <div className="field">
                <div className="field-label">
                  run name · {customName ? (
                    sanitizedName ? <span className="num">{sanitizedName}</span> :
                      <span style={{color:"var(--st-failed)"}}>invalid</span>
                  ) : <span className="muted">auto-generated if empty</span>}
                </div>
                <input className="input" style={{width:"100%"}}
                       placeholder="e.g. Mumbai_Parle (leave empty for auto)"
                       value={customName}
                       onChange={e=>setCustomName(e.target.value)}
                       maxLength={64}/>
                <p className="help" style={{marginTop:4}}>
                  becomes the output folder name. Only A-Z, a-z, 0-9, _ and - are kept; other characters become _.
                  Leave empty to get the default <span className="num">par_YYYYMMDD_HHMMSS_xxxxxx</span> format.
                </p>
              </div>

              <hr className="hr"/>

              <div className="field">
                <div className="field-label">search queries · {validQueries.length}</div>
                <div className="field-row">
                  <select className="input" style={{flex:1}} value={presetSel} onChange={e=>setPresetSel(e.target.value)}>
                    <option value="">— saved presets —</option>
                    {Object.keys(presets).map(name => <option key={name} value={name}>{name}</option>)}
                  </select>
                  <button className="btn sm" onClick={refreshPresets} disabled={presetsLoading}
                          title="reload saved presets from disk">
                    <I.Refresh size={10}/> {presetsLoading ? "…" : "reload"}
                  </button>
                  <button className="btn sm" onClick={doLoadPreset} disabled={!presetSel}><I.Download size={10}/> load</button>
                  <button className="btn sm danger" onClick={doDeletePreset} disabled={!presetSel} title="delete preset"><I.Trash size={10}/></button>
                </div>
                <div className="field-row" style={{marginTop:6}}>
                  <input className="input" style={{flex:1}} placeholder="preset name"
                         value={presetName} onChange={e=>setPresetName(e.target.value)}/>
                  <button className="btn sm" onClick={doSavePreset} disabled={!presetName.trim()}><I.Save size={10}/> save as</button>
                </div>
                <div className="query-list" style={{marginTop:6}}>
                  {queries.map((q, i) => (
                    <div key={i} className="query-row">
                      <input value={q} onChange={e=>{
                        const next = queries.slice(); next[i] = e.target.value; setQueries(next);
                      }}/>
                      <button className="x" onClick={()=>setQueries(queries.filter((_,j)=>j!==i))} title="remove">
                        <I.X size={10}/>
                      </button>
                    </div>
                  ))}
                  <button className="btn ghost sm" style={{justifyContent:"flex-start"}}
                          onClick={()=>setQueries([...queries, ""])}>
                    <I.Plus size={10}/> add query
                  </button>
                </div>
              </div>

              <hr className="hr"/>

              <div className="field">
                <div className="field-label">workers</div>
                <div className="worker-grid">
                  <div>
                    <div style={{fontSize:11, color:"var(--accent)", fontFamily:"var(--font-mono)", marginBottom:6, textTransform:"uppercase", letterSpacing:0.06}}>
                      phase 1 · discovery
                    </div>
                    <div className="auto-readout">
                      <span className="auto-badge">AUTO</span>
                      <div className="auto-vals">
                        <span className="num">~{autoWorkers}</span><span className="muted"> workers</span>
                        <span className="num" style={{marginLeft:10}}>~{autoRps.toFixed(0)}</span><span className="muted">/s</span>
                      </div>
                    </div>
                    <p className="help" style={{marginTop:4}}>auto-paced from active API keys
                      {apiStatus ? `: ${activeKeys} x ${PER_KEY_QPM}/min x ${Math.round(UTIL*100)}%` : ": loading keys"}.
                      Keys hitting 429 are auto-parked 30s and the rate rebalances.</p>
                  </div>
                  <div style={{opacity: phase1Only ? 0.45 : 1, pointerEvents: phase1Only ? "none" : "auto"}}>
                    <div style={{fontSize:11, color:"var(--accent)", fontFamily:"var(--font-mono)", marginBottom:6, textTransform:"uppercase", letterSpacing:0.06}}>
                      phase 2 · deep scrape {phase1Only && <span style={{color:"var(--fg-mute)", textTransform:"none", letterSpacing:0}}>· disabled (phase 1 only)</span>}
                    </div>
                    <div className="field-row">
                      <input type="range" className="slider" min={1} max={50} value={workers2} onChange={e=>setWorkers2(+e.target.value)}/>
                      <input className="input mono" style={{width:50, textAlign:"center"}} value={workers2} onChange={e=>setWorkers2(+e.target.value || 1)}/>
                    </div>
                    <p className="help" style={{marginTop:4}}>parallel deep-scrape Chromium browsers (~100 MB each); RAM-bound, not key-bound.</p>
                    <div className="field-row" style={{marginTop:6, alignItems:"center"}}>
                      <span className="muted" style={{fontSize:11, whiteSpace:"nowrap"}}>recycle browser every</span>
                      <input className="input mono" type="number" min={1} max={500}
                             value={recycleAfter}
                             onChange={e=>setRecycleAfter(Math.max(1, +e.target.value || 50))}
                             style={{width:64, textAlign:"center"}}/>
                      <span className="muted" style={{fontSize:11}}>places</span>
                    </div>
                    <p className="help" style={{marginTop:4}}>each browser scrapes this many places, then closes + relaunches — bounds memory creep over long runs. Default 50.</p>
                  </div>
                </div>
              </div>

              <hr className="hr"/>

              <div className="field">
                <div className="field-label">columns to save · {columnsProfile === "custom" ? customCols.length : (cols?.profiles?.[columnsProfile === "standard" ? "default" : columnsProfile === "full" ? "rich" : columnsProfile]?.length || 0)} selected</div>
                <div className="field-row">
                  <select className="input" style={{flex:1}} value={columnsProfile} onChange={e=>setColumnsProfile(e.target.value)}>
                    <option value="lean">lean · {cols ? (cols.profiles?.lean?.length || 10) : 10} columns</option>
                    <option value="standard">default · {cols ? (cols.profiles?.default?.length || 14) : 14} columns</option>
                    <option value="full">rich · {cols ? (cols.profiles?.rich?.length || 22) : 22} columns</option>
                    <option value="reviews-only">reviews-only · {cols ? (cols.profiles?.["reviews-only"]?.length || 6) : 6} columns</option>
                    <option value="custom">custom · {customCols.length} selected</option>
                  </select>
                  <button className="btn sm ghost" onClick={()=>setShowColumnsPicker(v => !v)}>
                    <I.Sliders size={10}/> {showColumnsPicker ? "hide" : "pick"}
                  </button>
                </div>
                {showColumnsPicker && (
                  cols ? (
                    <div className="column-picker">
                      {Object.entries(cols.catalog).map(([key, label]) => {
                        const checked = customCols.includes(key);
                        return (
                          <label key={key} className="column-picker-row">
                            <input type="checkbox" checked={checked} onChange={e=>{
                              const on = e.target.checked;
                              setColumnsProfile("custom");
                              setCustomCols(prev => on
                                ? Array.from(new Set([...prev, key]))
                                : prev.filter(c => c !== key));
                            }}/>
                            <code>{key}</code>
                            <span className="muted">{label}</span>
                          </label>
                        );
                      })}
                    </div>
                  ) : (
                    <div className="muted" style={{fontSize:11.5, padding:"6px 4px"}}>loading column catalogue…</div>
                  )
                )}
                <p className="help">presets map to <code>--profile</code>; ticking any box switches to <code>custom</code> and sends an exact <code>--columns</code> list.</p>
              </div>

              <div className="field">
                <div className="field-label">other</div>
                <label className="check">
                  <input type="checkbox" checked={dryRun} onChange={e=>setDryRun(e.target.checked)}/>
                  dry run · mock both phases · no live network
                </label>
                <label className="check">
                  <input type="checkbox" checked={noImages} onChange={e=>setNoImages(e.target.checked)}/>
                  metadata only · skip image downloads · image_urls still saved
                </label>
                <label className="check">
                  <input type="checkbox" checked={captureShots} onChange={e=>setCaptureShots(e.target.checked)}/>
                  capture review screenshots · overview + ratings panel · for LLM review
                </label>
                <label className="check">
                  <input type="checkbox" checked={phase1Only} onChange={e=>setPhase1Only(e.target.checked)}/>
                  phase 1 only · discovery only · skip deep scrape (resumable later)
                </label>
                <div className="field-row" style={{marginTop:6}}>
                  <span className="muted" style={{width:90, fontSize:11.5}}>max places</span>
                  <input className="input mono" placeholder="(no cap)" style={{flex:1}}
                         value={maxPlaces} onChange={e=>setMaxPlaces(e.target.value)}/>
                </div>
              </div>
            </>
          )}
        </div>

        <footer className="run-config-foot">
          {stage === 1 ? (
            <>
              <div className="summary">
                <span><span className="num">{cells.toLocaleString()}</span> cells · ~<span className="num">{(cells*0.4).toFixed(0)}s</span> phase-1</span>
                <span className="faint">choose queries next to estimate discovery calls</span>
              </div>
              <button className="btn primary" disabled={!areaResolved || cells <= 0 || gridLoading} onClick={()=>setStage(2)}>
                next <I.ArrowRight size={11}/>
              </button>
            </>
          ) : (
            <>
              <div className="summary">
                <span><span className="num">{cells.toLocaleString()}</span> x <span className="num">{validQueries.length}</span> = <span className="num">{callsEstimate.toLocaleString()}</span> calls</span>
                <span className="faint">{validQueries.length
                  ? `Phase 1 ~${etaText} @ ~${autoRps.toFixed(0)}/s (${activeKeys} keys) - IDs-only $0`
                  : "add a query to start"}</span>
              </div>
              <button className="btn ghost" onClick={()=>setStage(1)}>
                <I.ChevronLeft size={11}/> back
              </button>
              <button className="btn primary" onClick={doStartRun} disabled={starting || validQueries.length === 0 || cells <= 0}>
                <I.Play size={11}/> {starting ? "starting…" : "start run"}
              </button>
            </>
          )}
        </footer>
      </section>

      <window.MapView
        mapStyle={mapStyle}
        boundary={boundaryGJ}
        grid={gridGJ}
        onMapReady={onMapReady}
        fitTo="auto"
        overlayLabel={
          <>
            {areaResolved
              ? <><span className="chip num">{(areaResolved.total_cells || 0).toLocaleString()} cells</span>
                  <span style={{color:"var(--fg-2)"}}>{gridSize}m {gridShape}</span></>
              : <span className="faint">pick an area to preview the grid</span>}
          </>
        }
      />
    </div>
  );
};

window.NewRun = NewRun;
