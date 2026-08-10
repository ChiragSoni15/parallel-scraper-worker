"""Boundary fetch + grid generation. Uses Nominatim directly (no osmnx) and
supports custom polygons drawn by the operator."""
from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass
from typing import Optional, Sequence
from urllib.parse import urlencode
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)

NOMINATIM_BASE = "https://nominatim.openstreetmap.org"
NOMINATIM_UA = "parallel-scraper/0.1 (+https://github.com/local)"


@dataclass
class GridCell:
    cell_id: str
    centroid_lat: float
    centroid_lon: float
    bbox_north: float
    bbox_south: float
    bbox_east: float
    bbox_west: float


@dataclass
class CityCandidate:
    display_name: str
    osm_id: int
    osm_type: str       # 'relation' | 'way' | 'node'
    category: str
    lat: float
    lon: float
    bbox: tuple[float, float, float, float]   # (south, north, west, east)


# ─── nominatim wrappers ────────────────────────────────────

def _nominatim_get(path: str, params: dict) -> object:
    url = f"{NOMINATIM_BASE}{path}?{urlencode(params)}"
    req = Request(url, headers={"User-Agent": NOMINATIM_UA, "Accept": "application/json"})
    with urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def search_city(query: str, limit: int = 8) -> list[CityCandidate]:
    """Free-text Nominatim search. Returns up to `limit` candidates."""
    raw = _nominatim_get("/search", {
        "q": query,
        "format": "jsonv2",
        "limit": str(limit),
        "polygon_geojson": "0",
        "addressdetails": "0",
    })
    out: list[CityCandidate] = []
    for r in raw or []:
        try:
            bb = r.get("boundingbox") or []
            if len(bb) != 4:
                continue
            out.append(CityCandidate(
                display_name=r.get("display_name", ""),
                osm_id=int(r.get("osm_id", 0)),
                osm_type=str(r.get("osm_type", "")),
                category=str(r.get("category", "")),
                lat=float(r.get("lat", 0.0)),
                lon=float(r.get("lon", 0.0)),
                bbox=(float(bb[0]), float(bb[1]), float(bb[2]), float(bb[3])),
            ))
        except (ValueError, TypeError):
            continue
    return out


def fetch_boundary(osm_id: int, osm_type: str = "relation"):
    """Fetch the boundary polygon as a GeoDataFrame in WGS84 via Nominatim
    `lookup` with `polygon_geojson=1`. Replaces osmnx."""
    try:
        import geopandas as gpd
        from shapely.geometry import shape
    except ImportError as e:
        raise RuntimeError(f"geopandas/shapely required: {e}")

    type_prefix = {"relation": "R", "way": "W", "node": "N"}.get(osm_type.lower(), "R")
    osm_lookup_id = f"{type_prefix}{int(osm_id)}"
    logger.info("boundary.fetch osm_id=%s osm_type=%s", osm_id, osm_type)
    raw = _nominatim_get("/lookup", {
        "osm_ids": osm_lookup_id,
        "format": "jsonv2",
        "polygon_geojson": "1",
    })
    if not raw:
        raise RuntimeError(f"Nominatim returned no results for {osm_lookup_id}")
    rec = raw[0]
    geom_geojson = rec.get("geojson")
    if not geom_geojson:
        raise RuntimeError(f"Nominatim has no polygon_geojson for {osm_lookup_id}")
    geom = shape(geom_geojson)
    gdf = gpd.GeoDataFrame(
        {"display_name": [rec.get("display_name", "")]},
        geometry=[geom],
        crs="EPSG:4326",
    )
    return gdf


def _is_ring(coords) -> bool:
    """True when coords looks like a single ring [(lat, lng), ...] rather than a
    list of rings [[(lat, lng), ...], ...]."""
    first = coords[0]
    return isinstance(first[0], (int, float))


def polygon_rings_from_geojson(data) -> list[list[tuple[float, float]]]:
    """Normalize a polygon source to a list of outer rings in (lat, lng) order.

    Accepts: a bare coordinate list [[lat, lng], ...], a bare list of such rings,
    or GeoJSON — Polygon, MultiPolygon, Feature, FeatureCollection (all features'
    Polygon/MultiPolygon geometries are collected). Holes are ignored (grid cells
    over a hole cost a few extra $0 discovery calls, same as the OSM path).
    GeoJSON coordinates are (lon, lat) and get swapped; bare lists are (lat, lng).
    """
    if isinstance(data, (list, tuple)):
        if not data:
            raise ValueError("empty polygon coordinates")
        if _is_ring(data):
            return [[(float(lat), float(lng)) for lat, lng in data]]
        return [[(float(lat), float(lng)) for lat, lng in ring] for ring in data]

    if not isinstance(data, dict):
        raise ValueError("polygon source must be a coordinate list or GeoJSON object")

    gtype = data.get("type")
    if gtype == "FeatureCollection":
        rings: list[list[tuple[float, float]]] = []
        for feat in data.get("features", []):
            rings.extend(polygon_rings_from_geojson(feat))
        if not rings:
            raise ValueError("FeatureCollection contains no Polygon/MultiPolygon features")
        return rings
    if gtype == "Feature":
        return polygon_rings_from_geojson(data.get("geometry") or {})
    if gtype == "Polygon":
        outer = data["coordinates"][0]
        return [[(float(lat), float(lng)) for lng, lat in outer]]
    if gtype == "MultiPolygon":
        return [[(float(lat), float(lng)) for lng, lat in poly[0]]
                for poly in data["coordinates"]]
    raise ValueError(f"unsupported GeoJSON type for a boundary: {gtype!r}")


def boundary_from_polygon(coords):
    """Build a GeoDataFrame from operator-supplied polygon coordinates.

    Accepts a single ring [(lat, lng), ...] (legacy shape) or a list of rings
    [[(lat, lng), ...], ...] (e.g. from a MultiPolygon coverage boundary); multiple
    rings are unioned so grid generation covers every part."""
    try:
        import geopandas as gpd
        from shapely.geometry import Polygon
        from shapely.ops import unary_union
    except ImportError as e:
        raise RuntimeError(f"geopandas/shapely required: {e}")

    if len(coords) == 0:
        raise ValueError("polygon needs at least 3 points")
    rings = [coords] if _is_ring(coords) else list(coords)
    polys = []
    for ring in rings:
        if len(ring) < 3:
            raise ValueError("polygon needs at least 3 points")
        # Shapely uses (lon, lat) order.
        poly = Polygon([(lng, lat) for lat, lng in ring])
        if not poly.is_valid:
            poly = poly.buffer(0)
        polys.append(poly)
    geom = polys[0] if len(polys) == 1 else unary_union(polys)
    gdf = gpd.GeoDataFrame(
        {"display_name": ["custom_polygon"]},
        geometry=[geom],
        crs="EPSG:4326",
    )
    return gdf


# ─── grid generation ───────────────────────────────────────

def _utm_crs_for(lat: float, lon: float) -> str:
    """Return the EPSG code for the UTM zone covering (lat, lon)."""
    zone = int(math.floor((lon + 180) / 6) % 60) + 1
    if lat >= 0:
        return f"EPSG:{32600 + zone}"
    return f"EPSG:{32700 + zone}"


def generate_grid(boundary_gdf, grid_size_meters: int, city_prefix: str = "MUM",
                  grid_type: str = "square") -> list[GridCell]:
    """Reproject to local UTM, build square grid, clip to boundary, return WGS84 cells."""
    try:
        import geopandas as gpd
        from shapely.geometry import box
    except ImportError as e:
        raise RuntimeError(f"geopandas/shapely required: {e}")

    if grid_type != "square":
        logger.warning("grid_type=%s not implemented; falling back to square", grid_type)

    # Pick UTM CRS based on boundary centroid (avoids osmnx dep).
    centroid = boundary_gdf.geometry.iloc[0].centroid
    utm_crs = _utm_crs_for(centroid.y, centroid.x)
    bnd_utm = boundary_gdf.to_crs(utm_crs)
    minx, miny, maxx, maxy = bnd_utm.total_bounds
    poly = bnd_utm.geometry.iloc[0]

    cells_utm = []
    nx = int(math.ceil((maxx - minx) / grid_size_meters))
    ny = int(math.ceil((maxy - miny) / grid_size_meters))
    for ix in range(nx):
        for iy in range(ny):
            x0 = minx + ix * grid_size_meters
            y0 = miny + iy * grid_size_meters
            cell_box = box(x0, y0, x0 + grid_size_meters, y0 + grid_size_meters)
            if not cell_box.intersects(poly):
                continue
            cells_utm.append(cell_box)

    if not cells_utm:
        return []

    gdf_cells = gpd.GeoDataFrame(geometry=cells_utm, crs=bnd_utm.crs).to_crs(epsg=4326)

    out: list[GridCell] = []
    for idx, geom_wgs in enumerate(gdf_cells.geometry):
        c = geom_wgs.centroid
        bx_w, bx_s, bx_e, bx_n = geom_wgs.bounds
        out.append(GridCell(
            cell_id=f"{city_prefix}_{idx:04d}",
            centroid_lat=float(c.y),
            centroid_lon=float(c.x),
            bbox_north=float(bx_n), bbox_south=float(bx_s),
            bbox_east=float(bx_e), bbox_west=float(bx_w),
        ))
    logger.info("grid.generated cells=%d size_m=%d", len(out), grid_size_meters)
    return out


def grid_to_geojson(cells: list[GridCell]) -> dict:
    """Render grid cells as a GeoJSON FeatureCollection for the frontend."""
    features = []
    for c in cells:
        features.append({
            "type": "Feature",
            "geometry": {
                "type": "Polygon",
                "coordinates": [[
                    [c.bbox_west, c.bbox_south],
                    [c.bbox_east, c.bbox_south],
                    [c.bbox_east, c.bbox_north],
                    [c.bbox_west, c.bbox_north],
                    [c.bbox_west, c.bbox_south],
                ]],
            },
            "properties": {"cell_id": c.cell_id},
        })
    return {"type": "FeatureCollection", "features": features}


def boundary_to_geojson(boundary_gdf) -> dict:
    """Render the boundary GeoDataFrame as GeoJSON for the frontend."""
    return json.loads(boundary_gdf.to_crs(epsg=4326).to_json())
