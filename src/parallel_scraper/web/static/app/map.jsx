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
  dark: {
    url: "https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png",
    options: { maxZoom: 19, subdomains: "abcd", attribution: "© OSM · CARTO" },
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
    return () => { map.remove(); mapRef.current = null; };
  }, []);

  // tile provider switch
  useEffectMap(() => {
    const map = mapRef.current; if (!map) return;
    if (tileRef.current) { map.removeLayer(tileRef.current); tileRef.current = null; }
    const p = TILE_PROVIDERS[mapStyle] || TILE_PROVIDERS.dark;
    tileRef.current = L.tileLayer(p.url, { ...p.options, r: "@2x" }).addTo(map);
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
      try { map.fitBounds(layer.getBounds(), { padding: [20, 20] }); } catch {}
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
        map.fitBounds([[minLat, minLng], [maxLat, maxLng]], { padding: [20, 20] });
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
      try { map.fitBounds(layer.getBounds(), { padding: [20, 20] }); } catch {}
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
  useEffectMap(() => {
    const map = mapRef.current; if (!map) return;
    const isLarge = grid && grid.features && grid.features.length > LARGE_GRID_THRESHOLD;
    if (!isLarge) {
      // Tear down any leftover large-grid layer when switching to a small grid.
      if (gridRef.current && gridRef.current._isTouchedOnly) {
        map.removeLayer(gridRef.current);
        gridRef.current = null;
        cellByIdRef.current = {};
      }
      return;
    }
    // Lazy-create the layer group on first call.
    if (!gridRef.current || !gridRef.current._isTouchedOnly) {
      if (gridRef.current) { map.removeLayer(gridRef.current); }
      const grp = L.layerGroup().addTo(map);
      grp._isTouchedOnly = true;
      gridRef.current = grp;
      cellByIdRef.current = {};
    }
    const grp = gridRef.current;
    const byId = cellByIdRef.current;
    const featureMap = cellFeatureMapRef.current || {};
    const touched = (cellStates || []).filter(
      (c) => c.status && c.status !== "queued" && c.status !== "pending"
    );
    const touchedIds = new Set(touched.map((c) => c.cell_id));

    // Drop layers for cells that are no longer touched (rare, but happens
    // on resume or status corrections).
    for (const cid of Object.keys(byId)) {
      if (!touchedIds.has(cid)) {
        try { grp.removeLayer(byId[cid]); } catch {}
        delete byId[cid];
      }
    }

    // Add/update each touched cell.
    for (const c of touched) {
      const cls = statusToCellClass(c.status);
      let lyr = byId[c.cell_id];
      if (!lyr) {
        const feature = featureMap[c.cell_id];
        if (!feature || !feature.geometry) continue;
        lyr = L.geoJSON(feature, {
          style: { className: `heat-cell ${cls}`, color: "transparent", fillOpacity: 1, weight: 0.5, opacity: 1 },
        });
        byId[c.cell_id] = lyr;
        grp.addLayer(lyr);
      } else {
        // Layer already drawn — update its CSS class for status changes.
        lyr.eachLayer((sub) => {
          const el = sub.getElement && sub.getElement();
          if (!el) return;
          el.classList.remove("queued", "running", "done", "failed");
          el.classList.add(cls);
        });
      }
    }
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
      try { map.fitBounds(grp.getBounds(), { padding: [40, 40] }); } catch {}
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
