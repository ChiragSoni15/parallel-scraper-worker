// Real Leaflet map component, drop-in replacement for the prototype's FauxMap.
// Single <MapView> takes every layer as a prop. Screens pass GeoJSON / leads /
// cell-state arrays from the API; this component handles all Leaflet wiring.
//
// Props:
//   mapStyle       "dark" | "light" | "satellite"
//   overlayLabel   ReactNode rendered top-left over the tiles
//   boundary       GeoJSON FeatureCollection — area outline
//   grid           GeoJSON FeatureCollection — cells with feature.properties.cell_id
//   cellStates     [{cell_id, status}]  status ∈ pending/in_progress/done/failed
//   pins           [{id, lat, lng, ...}] — circle markers (results screen)
//   selectedPinId  pin id to highlight
//   onPinClick     (id) => void
//   onMapReady     (mapInstance) => void  (used by newrun draw mode)

const { useState: useStateMap, useEffect: useEffectMap, useRef: useRefMap } = React;

const TILE_PROVIDERS = {
  // CARTO basemaps now need an API key (tiles come back as a watermark).
  // Dark = plain OSM tiles inverted in CSS (see [data-mapstyle="dark"] in
  // styles.css) — keyless and full zoom range.
  dark: {
    url: "https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png",
    options: { maxZoom: 19, attribution: "© OpenStreetMap" },
  },
  light: {
    url: "https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png",
    options: { maxZoom: 19, attribution: "© OpenStreetMap" },
  },
  satellite: {
    url: "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
    options: { maxZoom: 19, attribution: "© Esri · World Imagery" },
  },
};

function statusToCellClass(status) {
  if (status === "done") return "done";
  if (status === "failed") return "failed";
  if (status === "in_progress" || status === "running") return "running";
  return "queued";
}

// fitBounds while the container is still collapsed (first paint: the map
// pane measures ~260x1 px for over a second while the grid layout settles)
// yields a NaN/max zoom and the view sticks at z19. Park the fit and let the
// ResizeObserver below apply it once the container has a real size.
function safeFit(map, bounds, opts) {
  map.invalidateSize();
  const sz = map.getSize();
  if (sz.x < 50 || sz.y < 50) { map._pendingFit = { bounds, opts }; return; }
  map._pendingFit = null;
  // animate:false — programmatic fits go straight to _resetView instead of a
  // requestAnimationFrame-scheduled zoom, which browsers pause for background
  // tabs (the fit is then silently dropped and the old run's view stays).
  map.fitBounds(bounds, { ...opts, animate: false });
}

function readAccent() {
  try {
    return getComputedStyle(document.documentElement).getPropertyValue("--accent").trim() || "#7be060";
  } catch { return "#7be060"; }
}

const MapView = ({
  mapStyle = "dark", overlayLabel,
  boundary, grid, cellStates, pins, selectedPinId, onPinClick,
  onMapReady,
  fitTo = "auto",  // "boundary" | "grid" | "pins" | "auto" | "none"
}) => {
  const containerRef = useRefMap(null);
  const mapRef = useRefMap(null);
  const tileRef = useRefMap(null);
  const boundaryRef = useRefMap(null);
  const gridRef = useRefMap(null);
  const pinGroupRef = useRefMap(null);
  const cellByIdRef = useRefMap({});

  // create map once
  useEffectMap(() => {
    if (!containerRef.current || mapRef.current) return;
    const map = L.map(containerRef.current, {
      zoomControl: true,
      attributionControl: false,  // we render our own .map-attr
      preferCanvas: false,
    }).setView([20.5937, 78.9629], 4);
    mapRef.current = map;
    onMapReady && onMapReady(map);
    setTimeout(() => map.invalidateSize(), 200);
    const ro = new ResizeObserver(() => {
      map.invalidateSize();
      if (map._pendingFit) safeFit(map, map._pendingFit.bounds, map._pendingFit.opts);
    });
    ro.observe(containerRef.current);
    return () => { ro.disconnect(); map.remove(); mapRef.current = null; };
  }, []);

  // tile provider switch
  useEffectMap(() => {
    const map = mapRef.current; if (!map) return;
    if (tileRef.current) { map.removeLayer(tileRef.current); tileRef.current = null; }
    const p = TILE_PROVIDERS[mapStyle] || TILE_PROVIDERS.dark;
    tileRef.current = L.tileLayer(p.url, { ...p.options }).addTo(map);
  }, [mapStyle]);

  // boundary
  useEffectMap(() => {
    const map = mapRef.current; if (!map) return;
    if (boundaryRef.current) { map.removeLayer(boundaryRef.current); boundaryRef.current = null; }
    if (!boundary) return;
    const accent = readAccent();
    const layer = L.geoJSON(boundary, {
      style: { color: accent, weight: 2, fillOpacity: 0.06, opacity: 0.85 },
    }).addTo(map);
    boundaryRef.current = layer;
    if (fitTo === "boundary" || fitTo === "auto") {
      try { safeFit(map, layer.getBounds(), { padding: [20, 20] }); } catch {}
    }
  }, [boundary]);

  // Threshold above which we stop rendering every cell as a polygon. The
  // Leaflet SVG renderer dies around 5-10K shapes (MMR at 250m = 70K
  // polygons + 70K tooltip listeners = unresponsive map). Above this
  // threshold, we ONLY draw cells that have a status (done/running/failed)
  // — the un-touched cells are implied by the boundary outline. This keeps
  // the heatmap useful as Phase 1 progresses while the browser stays snappy.
  const LARGE_GRID_THRESHOLD = 5000;

  // Build the cell_id -> feature lookup once when grid loads. Used in the
  // large-grid path to render polygons on demand from cellStates.
  const cellFeatureMapRef = useRefMap({});
  useEffectMap(() => {
    const map = cellFeatureMapRef.current = {};
    if (!grid || !grid.features) return;
    for (const f of grid.features) {
      const cid = f.properties && f.properties.cell_id;
      if (cid) map[cid] = f;
    }
  }, [grid]);

  // For large grids, fit-to-bounds needs the grid bbox without drawing the
  // whole thing. Compute once from the feature collection.
  useEffectMap(() => {
    const map = mapRef.current; if (!map) return;
    if (!grid || !grid.features || grid.features.length <= LARGE_GRID_THRESHOLD) return;
    if (fitTo !== "grid" && fitTo !== "auto") return;
    try {
      let minLat = Infinity, maxLat = -Infinity, minLng = Infinity, maxLng = -Infinity;
      for (const f of grid.features) {
        const geom = f.geometry; if (!geom) continue;
        // Polygon coordinates: [[[lng,lat], ...]]
        const rings = geom.type === "Polygon" ? geom.coordinates : (geom.coordinates || []).flat(1);
        for (const ring of rings) {
          for (const c of ring) {
            const lng = c[0], lat = c[1];
            if (lat < minLat) minLat = lat;
            if (lat > maxLat) maxLat = lat;
            if (lng < minLng) minLng = lng;
            if (lng > maxLng) maxLng = lng;
          }
        }
      }
      if (isFinite(minLat) && isFinite(maxLat)) {
        safeFit(map, [[minLat, minLng], [maxLat, maxLng]], { padding: [20, 20] });
      }
    } catch {}
  }, [grid]);

  // Small grids: keep the old all-cells-rendered behavior.
  useEffectMap(() => {
    const map = mapRef.current; if (!map) return;
    if (gridRef.current) { map.removeLayer(gridRef.current); gridRef.current = null; cellByIdRef.current = {}; }
    if (!grid) return;
    if (grid.features && grid.features.length > LARGE_GRID_THRESHOLD) return;  // handled below
    const byId = {};
    const layer = L.geoJSON(grid, {
      style: () => ({ className: "heat-cell queued", color: "transparent", fillOpacity: 1, weight: 0.5, opacity: 1 }),
      onEachFeature: (feature, lyr) => {
        const cid = feature.properties && feature.properties.cell_id;
        if (cid) { byId[cid] = lyr; lyr.bindTooltip(cid, { direction: "top", className: "cell-tip" }); }
      },
    }).addTo(map);
    gridRef.current = layer;
    cellByIdRef.current = byId;
    if (fitTo === "grid" || fitTo === "auto") {
      try { safeFit(map, layer.getBounds(), { padding: [20, 20] }); } catch {}
    }
  }, [grid]);

  // cell states (small grid path) — apply CSS class to each existing path.
  useEffectMap(() => {
    if (!gridRef.current || !cellStates) return;
    const byId = cellByIdRef.current;
    for (const c of cellStates) {
      const lyr = byId[c.cell_id];
      if (!lyr) continue;
      const el = lyr.getElement && lyr.getElement();
      if (!el) continue;
      el.classList.remove("queued", "running", "done", "failed");
      el.classList.add(statusToCellClass(c.status));
    }
  }, [cellStates, grid]);

  // ── Large-grid path: incrementally draw touched cells only ─────────
  // We never instantiate polygons for the ~67K queued cells. Each time
  // cellStates updates, we diff against the rendered set: add new layers
  // for newly-touched cells, update class on existing ones, remove any
  // that vanished. Tooltips are omitted (they'd add back the 70K listener
  // hit we're avoiding) — hover-tooltips can be re-added later as a lazy
  // popup if needed.
  // Rendered on a single canvas: a finished state-sized run has 20-70K
  // touched cells, and that many SVG <path> nodes is what froze the tab on
  // every tab switch. Canvas can't take CSS classes, so colours are explicit.
  const CANVAS_STYLE = {
    queued:  { fillColor: "#8a8f99", fillOpacity: 0.18, color: "#8a8f99", opacity: 0.35 },
    running: { fillColor: "#7be060", fillOpacity: 0.45, color: "#7be060", opacity: 0.9 },
    done:    { fillColor: "#5fb3b3", fillOpacity: 0.35, color: "#5fb3b3", opacity: 0.6 },
    failed:  { fillColor: "#e0705a", fillOpacity: 0.40, color: "#e0705a", opacity: 0.8 },
  };
  const canvasRef = useRefMap(null);
  const lastSigRef = useRefMap("");
  useEffectMap(() => {
    const map = mapRef.current; if (!map) return;
    const isLarge = grid && grid.features && grid.features.length > LARGE_GRID_THRESHOLD;
    if (!isLarge) {
      // Tear down any leftover large-grid layer when switching to a small grid.
      if (gridRef.current && gridRef.current._isTouchedOnly) {
        map.removeLayer(gridRef.current);
        gridRef.current = null;
        cellByIdRef.current = {};
        lastSigRef.current = "";
      }
      return;
    }
    const featureMap = cellFeatureMapRef.current || {};
    const touched = (cellStates || []).filter(
      (c) => c.status && c.status !== "queued" && c.status !== "pending"
    );
    // Skip the rebuild when nothing changed (the poller already 304s, but a
    // remount re-delivers the cached array).
    let sig = touched.length + ":";
    for (const c of touched) sig += c.cell_id + c.status.charCodeAt(0);
    if (sig === lastSigRef.current && gridRef.current && gridRef.current._isTouchedOnly) return;
    lastSigRef.current = sig;

    if (gridRef.current) { map.removeLayer(gridRef.current); }
    if (!canvasRef.current) canvasRef.current = L.canvas({ padding: 0.5 });
    const features = [];
    for (const c of touched) {
      const f = featureMap[c.cell_id];
      if (f && f.geometry) features.push({ ...f, properties: { ...(f.properties || {}), status: c.status } });
    }
    const layer = L.geoJSON({ type: "FeatureCollection", features }, {
      renderer: canvasRef.current,
      interactive: false,
      style: (f) => ({ weight: 0.5, ...CANVAS_STYLE[statusToCellClass(f.properties.status)] }),
    }).addTo(map);
    layer._isTouchedOnly = true;
    gridRef.current = layer;
    cellByIdRef.current = {};
  }, [cellStates, grid]);

  // pins
  useEffectMap(() => {
    const map = mapRef.current; if (!map) return;
    if (pinGroupRef.current) { map.removeLayer(pinGroupRef.current); pinGroupRef.current = null; }
    if (!pins || pins.length === 0) return;
    const accent = readAccent();
    const valid = pins.filter(p => Number.isFinite(p.lat) && Number.isFinite(p.lng));
    const markers = valid.map(p => {
      const isSel = p.id === selectedPinId;
      const m = L.circleMarker([p.lat, p.lng], {
        radius: isSel ? 8 : 5,
        fillColor: accent, fillOpacity: isSel ? 1 : 0.85,
        color: "#0a0a0a", weight: 1.5,
        className: isSel ? "pin selected" : "pin",
      });
      if (onPinClick) m.on("click", () => onPinClick(p.id));
      return m;
    });
    const grp = L.featureGroup(markers).addTo(map);
    pinGroupRef.current = grp;
    if ((fitTo === "pins" || fitTo === "auto") && markers.length) {
      try { safeFit(map, grp.getBounds(), { padding: [40, 40] }); } catch {}
    }
  }, [pins, selectedPinId]);

  return (
    <div className="map-area" data-mapstyle={mapStyle}>
      <div ref={containerRef} style={{ position: "absolute", inset: 0, zIndex: 0 }} />
      {overlayLabel && <div className="map-overlay-tag">{overlayLabel}</div>}
      <div className="map-attr">© OpenStreetMap · Leaflet</div>
    </div>
  );
};

window.MapView = MapView;

// Back-compat shims so screen JSX can keep <window.FauxMap>...children with
// <BoundaryShape d="..."/> if it ever shows up. The real prop API is on MapView.
window.FauxMap = MapView;
window.BoundaryShape = () => null;
window.GridHeatmap = () => null;
window.PinLayer = () => null;
