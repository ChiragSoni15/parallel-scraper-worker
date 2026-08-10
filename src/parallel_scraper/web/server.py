"""FastAPI dashboard for parallel-scraper.

Endpoints:
  GET  /                          dashboard HTML
  GET  /static/...                vendored oat + dashboard.js + dashboard.css
  GET  /api/columns               full column catalog + profiles
  GET  /api/runs                  list every run_dir under output_dir
  GET  /api/runs/{run_id}         full run summary (config, latest heartbeat, counts)
  GET  /api/runs/{run_id}/stream  SSE: tails heartbeat.jsonl in real time
  POST /api/runs                  start a new run (body = run config)
  POST /api/runs/{run_id}/stop    request graceful shutdown
"""
from __future__ import annotations

import asyncio
import csv
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from parallel_scraper.config import COLUMN_CATALOG, LEAN_PROFILE, PROFILES
from parallel_scraper.web import presets as presets_store
from parallel_scraper.web.runner import RunRegistry

logger = logging.getLogger(__name__)

_PKG_DIR = Path(__file__).resolve().parent
_STATIC_DIR = _PKG_DIR / "static"
_TEMPLATES_DIR = _PKG_DIR / "templates"

# Output dir is configurable via env (defaults to ./outputs in CWD).
OUTPUT_DIR = Path(os.environ.get("PARALLEL_SCRAPER_OUTPUT_DIR", "outputs"))

# Response-size discipline:
# /api/runs, /api/stats, and /api/runs/{id}/stream must stay scalar-only and
# must not parse state CSVs or return large arrays. Cell-level state belongs
# behind /api/runs/{id}/grid, where map rendering explicitly asks for it.
_DISK_USAGE_CACHE = {"path": None, "ts": 0.0, "value": 0}
_GRID_INDEX_CACHE: dict[str, dict] = {}
_TOUCHED_GRID_CACHE: dict[str, dict] = {}


def _new_run_id() -> str:
    import uuid
    return f"par_{datetime.now(timezone.utc):%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:6]}"


# ─── request models ───────────────────────────────────────

class StartRunBody(BaseModel):
    osm_id: int = 13312356
    osm_type: str = "relation"
    city: str = "Mumbai"
    grid_size_meters: int = 1000
    queries: str = "shops"
    discovery_backend: str = "places_api"
    places_tier: str = "ESSENTIALS"
    phase1_workers: int = 3
    phase2_workers: int = 5
    discovery_rps: float = 2.0
    consumer_rps: float = 2.0
    consumer_delay_ms: int = 500
    # Each Phase-2 consumer reuses one Chromium across this many places before
    # closing/relaunching it (bounds memory growth, ~25% faster than per-place
    # launches). Min 1; default 50.
    worker_browser_recycle_after: int = 50
    max_places: Optional[int] = None
    profile: Optional[str] = None
    columns: Optional[list[str]] = None
    master_dedup: str = "inputs/known_placeids.csv"
    dry_run: bool = False
    # Metadata only: skip downloading image bytes to disk (image_urls still in CSV).
    no_image_download: bool = False
    # Save per-place panel screenshots (overview + reviews/histogram) for LLM review.
    capture_screenshots: bool = False
    run_id: Optional[str] = None  # for resume
    # Custom polygon: list of [lat, lng] pairs, or a list of such rings for a
    # MultiPolygon (uploaded GeoJSON). When set, supersedes osm_id server-side.
    custom_polygon: Optional[list] = None

    def to_cli_args(self, output_dir: str, polygon_file: Optional[str] = None) -> list[str]:
        args = [
            "--osm-id", str(self.osm_id),
            "--osm-type", self.osm_type,
            "--city", self.city,
            "--grid-size-meters", str(self.grid_size_meters),
            "--queries", self.queries,
            "--discovery-backend", self.discovery_backend,
            "--places-tier", self.places_tier,
            "--phase1-workers", str(self.phase1_workers),
            "--phase2-workers", str(self.phase2_workers),
            "--discovery-rps", str(self.discovery_rps),
            "--consumer-rps", str(self.consumer_rps),
            "--consumer-delay-ms", str(self.consumer_delay_ms),
            "--worker-browser-recycle-after", str(max(1, int(self.worker_browser_recycle_after))),
            "--master-dedup", self.master_dedup,
            "--output-dir", output_dir,
            # Web-launched runs use a fast heartbeat so worker pills + cost feel live.
            "--heartbeat-interval-s", "2",
        ]
        if polygon_file:
            args += ["--polygon-file", polygon_file]
        if self.max_places is not None:
            args += ["--max-places", str(self.max_places)]
        if self.profile:
            args += ["--profile", self.profile]
        elif self.columns:
            args += ["--columns", ",".join(self.columns)]
        else:
            args += ["--profile", "lean"]
        if self.no_image_download:
            args += ["--no-image-download"]
        if self.capture_screenshots:
            args += ["--capture-screenshots"]
        if self.run_id:
            args += ["--run-id", self.run_id]
        if self.dry_run:
            args += ["--dry-run"]
        return args


class SearchCityBody(BaseModel):
    query: str


class GenerateGridBody(BaseModel):
    osm_id: Optional[int] = None
    osm_type: str = "relation"
    # Single [lat, lng] ring or a list of rings (MultiPolygon upload).
    custom_polygon: Optional[list] = None
    area_name: str = ""
    grid_size_meters: int = 1000
    grid_type: str = "square"


class QueryPresetItem(BaseModel):
    q: str
    label: Optional[str] = None


class SavePresetBody(BaseModel):
    name: str
    queries: list[QueryPresetItem]


PRESETS_PATH = Path(os.environ.get("PARALLEL_SCRAPER_PRESETS_PATH",
                                   "inputs/query_presets.json"))


# ─── helpers ──────────────────────────────────────────────

def _read_run_config(run_dir: Path) -> dict:
    cfg_path = run_dir / "run_config.json"
    if not cfg_path.exists():
        return {}
    try:
        return json.loads(cfg_path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _read_last_heartbeat(run_dir: Path) -> dict:
    hb_path = run_dir / "heartbeat.jsonl"
    if not hb_path.exists():
        return {}
    try:
        # Tail the last line. heartbeat.jsonl lines are lean (scalar counters
        # only — the heavy per-key/per-worker arrays live in live_state.json),
        # so a modest tail comfortably holds the last line.
        with hb_path.open("rb") as f:
            f.seek(0, 2)
            size = f.tell()
            chunk = min(size, 16384)
            f.seek(max(0, size - chunk))
            tail = f.read().decode("utf-8", errors="replace")
        last = tail.strip().splitlines()
        if not last:
            return {}
        return json.loads(last[-1])
    except Exception:
        return {}


def _read_live_state(run_dir: Path) -> dict:
    """Read live_state.json — the full current snapshot (scalar counters plus
    the workers + api_keys arrays), overwritten by the run on every heartbeat.
    Returns {} if absent (e.g. older runs, or the run hasn't started)."""
    p = run_dir / "live_state.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _csv_count(path: Path) -> int:
    if not path.exists():
        return 0
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as f:
            n = sum(1 for _ in f)
        return max(0, n - 1)
    except Exception:
        return 0


def _state_status_summary(run_dir: Path) -> dict:
    out = {"cells_done": 0, "cells_total": 0, "placeids_done": 0,
           "placeids_failed": 0, "placeids_pending": 0}
    cells = run_dir / "state_cells.csv"
    placeids = run_dir / "state_placeids.csv"
    if cells.exists():
        try:
            with cells.open("r", encoding="utf-8-sig", newline="") as f:
                rows = list(csv.DictReader(f))
            # state_cells.csv only grows as (cell, query) tasks transition
            # through states — it is NOT the total work. The true total is
            # unique_cells_in_grid × len(queries). Read grid.geojson +
            # run_config.json to compute the real denominator so the
            # dashboard progress bar matches actual remaining work.
            out["cells_done"] = sum(1 for r in rows if r.get("status") == "done")
            cells_touched = len({r.get("cell_id") for r in rows if r.get("cell_id")})
            grid_path = run_dir / "grid.geojson"
            cfg_path = run_dir / "run_config.json"
            grid_cells_total = 0
            queries_total = 0
            if grid_path.exists():
                try:
                    import json as _json
                    with grid_path.open("r", encoding="utf-8") as gf:
                        gdata = _json.load(gf)
                    grid_cells_total = len(gdata.get("features", []))
                except Exception:
                    pass
            if cfg_path.exists():
                try:
                    import json as _json
                    with cfg_path.open("r", encoding="utf-8") as cf:
                        cfg = _json.load(cf)
                    queries_total = len(cfg.get("queries", []) or [])
                except Exception:
                    pass
            if grid_cells_total > 0 and queries_total > 0:
                out["cells_total"] = grid_cells_total * queries_total
            else:
                # Fall back to the old (wrong, but non-zero) denominator
                out["cells_total"] = len(rows)
            out["grid_cells_total"] = grid_cells_total
            out["grid_cells_touched"] = cells_touched
            out["queries_total"] = queries_total
        except Exception:
            pass
    if placeids.exists():
        try:
            with placeids.open("r", encoding="utf-8-sig", newline="") as f:
                rows = list(csv.DictReader(f))
            out["placeids_done"] = sum(1 for r in rows if r.get("status") == "done")
            out["placeids_failed"] = sum(1 for r in rows if r.get("status") == "failed")
            out["placeids_pending"] = sum(
                1 for r in rows if r.get("status") in ("queued", "scraping")
            )
        except Exception:
            pass
    return out


def _open_csv_any(path: Path):
    """Open a .csv or .csv.gz transparently for csv.DictReader."""
    if not path.exists():
        return None
    if path.suffix == ".gz":
        import gzip
        return gzip.open(path, "rt", encoding="utf-8-sig", newline="")
    return path.open("r", encoding="utf-8-sig", newline="")


def _coerce_float(v) -> Optional[float]:
    if v in (None, "", "—"):
        return None
    try:
        f = float(v)
        return f if f == f else None  # filter NaN
    except (TypeError, ValueError):
        return None


def _coerce_int(v) -> Optional[int]:
    if v in (None, "", "—"):
        return None
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _row_to_lead(row: dict, *, scraped: bool) -> dict:
    """Map a phase2_data.csv (or phase1_discovered.csv) row → frontend lead shape."""
    place_id = row.get("place_id") or row.get("placeId") or ""
    img_field = row.get("image_urls") or ""
    image_url = ""
    if img_field:
        # Stored either as JSON list, ";"-joined, or single URL — try each.
        try:
            parsed = json.loads(img_field)
            if isinstance(parsed, list) and parsed:
                image_url = str(parsed[0])
        except (json.JSONDecodeError, TypeError):
            for sep in (";", "|", ","):
                if sep in img_field:
                    image_url = img_field.split(sep, 1)[0].strip(); break
            if not image_url:
                image_url = img_field.strip()
    return {
        "id": place_id,
        "name": row.get("name") or "",
        "cat": row.get("category") or "",
        "rating": _coerce_float(row.get("rating")),
        "rcount": _coerce_int(row.get("review_count")),
        "addr": row.get("address") or "",
        "phone": row.get("phone") or "",
        "website": row.get("website") or "",
        "lat": _coerce_float(row.get("latitude") or row.get("lat")),
        "lng": _coerce_float(row.get("longitude") or row.get("lng")),
        "image_url": image_url,
        "source_url": row.get("source_url") or "",
        "found_via": row.get("query") or row.get("found_via") or "",
        "scraped": scraped,
    }


def _read_phase2_leads(run_dir: Path, q: str = "", page: int = 0, limit: int = 200) -> tuple[list[dict], int]:
    """Return (leads, total). Prefers phase2_data.full.csv.gz for richer fields."""
    full = run_dir / "phase2_data.full.csv.gz"
    lean = run_dir / "phase2_data.csv"
    src = full if full.exists() else (lean if lean.exists() else None)
    rows: list[dict] = []
    if src is not None:
        try:
            f = _open_csv_any(src)
            if f is not None:
                with f as fh:
                    rows = list(csv.DictReader(fh))
        except Exception:
            logger.exception("read phase2 csv failed")

    # Backfill lat/lng + found_via from phase1_discovered for rows missing them.
    p1 = run_dir / "phase1_discovered.csv"
    p1_by_id: dict[str, dict] = {}
    if p1.exists():
        try:
            with p1.open("r", encoding="utf-8-sig", newline="") as fh:
                for r in csv.DictReader(fh):
                    pid = r.get("place_id")
                    if pid and pid not in p1_by_id:
                        p1_by_id[pid] = r
        except Exception:
            pass

    leads_all: list[dict] = []
    seen_ids: set[str] = set()
    for r in rows:
        pid = r.get("place_id") or ""
        if not pid or pid in seen_ids:
            continue
        seen_ids.add(pid)
        # Backfill missing fields from phase1.
        p1r = p1_by_id.get(pid, {})
        merged = dict(r)
        for k, v in p1r.items():
            if not merged.get(k) and v:
                merged[k] = v
        leads_all.append(_row_to_lead(merged, scraped=True))

    # Append discovered-only (not yet scraped) entries.
    for pid, r in p1_by_id.items():
        if pid in seen_ids:
            continue
        leads_all.append(_row_to_lead(r, scraped=False))

    # Filter by q (case-insensitive contains over name + category + address).
    if q:
        ql = q.lower()
        leads_all = [
            l for l in leads_all
            if ql in (l.get("name") or "").lower()
            or ql in (l.get("cat") or "").lower()
            or ql in (l.get("addr") or "").lower()
        ]

    total = len(leads_all)
    start = max(0, page) * max(1, limit)
    end = start + max(1, limit)
    return leads_all[start:end], total


def _csv_escape(s) -> str:
    s = "" if s is None else str(s)
    if any(ch in s for ch in (",", '"', "\n", "\r")):
        return '"' + s.replace('"', '""') + '"'
    return s


def _disk_usage_mb(path: Path) -> int:
    """Recursive size of a directory in MB. Returns 0 if missing."""
    if not path.exists():
        return 0
    total = 0
    try:
        for p in path.rglob("*"):
            if p.is_file():
                try: total += p.stat().st_size
                except OSError: pass
    except Exception:
        pass
    return int(total / (1024 * 1024))


def _disk_usage_mb_cached(path: Path, ttl_s: int = 60) -> int:
    now = time.monotonic()
    resolved = str(path.resolve()) if path.exists() else str(path)
    if (
        _DISK_USAGE_CACHE["path"] == resolved
        and now - float(_DISK_USAGE_CACHE["ts"]) < ttl_s
    ):
        return int(_DISK_USAGE_CACHE["value"])
    value = _disk_usage_mb(path)
    _DISK_USAGE_CACHE.update({"path": resolved, "ts": now, "value": value})
    return value


def _parse_quota_cell(raw) -> Optional[int]:
    s = str(raw if raw is not None else "").strip().lower()
    if s in ("", "0", "unlimited", "none", "inf", "infinite", "-", "na", "n/a"):
        return None
    try:
        v = int(float(s))
        return v if v > 0 else None
    except (TypeError, ValueError):
        return None


def _read_api_key_inventory() -> dict:
    """Read configured Google API keys and daily usage without exposing secrets."""
    csv_path = Path(os.environ.get("PARALLEL_SCRAPER_API_KEYS_CSV", "inputs/api_keys.csv"))
    usage_path = Path(os.environ.get("PARALLEL_SCRAPER_KEY_USAGE_PATH", "inputs/api_key_usage.json"))
    try:
        from parallel_scraper.key_usage import billing_day
        day = billing_day()
    except Exception:
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    usage: dict[str, int] = {}
    try:
        ledger = json.loads(usage_path.read_text(encoding="utf-8"))
        usage = {str(k): int(v) for k, v in (ledger.get(day, {}) or {}).items()}
    except Exception:
        usage = {}

    keys: list[dict] = []
    if csv_path.exists():
        try:
            with csv_path.open("r", encoding="utf-8-sig", newline="") as f:
                reader = csv.DictReader(f)
                for idx, row in enumerate(reader, start=1):
                    active_raw = str(row.get("is_active", "1")).strip().lower()
                    active = active_raw not in {"0", "false", "no", "n"}
                    key = (row.get("api_key") or row.get("key") or row.get("google_api_key") or "").strip()
                    alias = (row.get("alias") or row.get("key_alias") or f"csv#{idx}").strip()
                    if not key and not alias:
                        continue
                    quota = _parse_quota_cell(row.get("daily_quota"))
                    day_count = int(usage.get(alias, 0))
                    exhausted = bool(active and quota is not None and day_count >= quota)
                    keys.append({
                        "key_id": idx,
                        "alias": alias,
                        "is_active": active,
                        "day_count": day_count,
                        "quota": quota or 0,
                        "exhausted": exhausted,
                        "available": active and not exhausted,
                    })
        except Exception:
            logger.exception("api_key_inventory.read_failed path=%s", csv_path)

    configured = len(keys)
    active = sum(1 for k in keys if k["is_active"])
    available = sum(1 for k in keys if k["available"])
    capped = sum(1 for k in keys if k["is_active"] and k["quota"])
    unlimited = sum(1 for k in keys if k["is_active"] and not k["quota"])
    day_calls = sum(int(k["day_count"] or 0) for k in keys)
    quota_total = sum(int(k["quota"] or 0) for k in keys if k["is_active"] and k["quota"])
    quota_used = sum(int(k["day_count"] or 0) for k in keys if k["is_active"] and k["quota"])
    return {
        "day": day,
        "csv_path": str(csv_path),
        "usage_path": str(usage_path),
        "configured": configured,
        "active": active,
        "available": available,
        "capped": capped,
        "unlimited": unlimited,
        "day_calls": day_calls,
        "quota_total": quota_total,
        "quota_used": quota_used,
        "keys": keys,
    }


def _read_cell_states(run_dir: Path) -> list[dict]:
    """Collapse state_cells.csv into [{cell_id, status, place_count}] (one row per cell_id)."""
    p = run_dir / "state_cells.csv"
    if not p.exists():
        return []
    out: dict[str, dict] = {}
    with p.open("r", encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            cid = row.get("cell_id") or ""
            status = row.get("status") or "pending"
            try:
                pc = int(row.get("place_count") or 0)
            except (TypeError, ValueError):
                pc = 0
            # Status priority on duplicate cell_id rows: in_progress > done > failed > pending.
            prev = out.get(cid)
            order = {"pending": 0, "failed": 1, "done": 2, "in_progress": 3}
            if prev is None or order.get(status, 0) >= order.get(prev["status"], 0):
                out[cid] = {"cell_id": cid, "status": status, "place_count": pc}
    return list(out.values())


def _path_signature(path: Path) -> tuple[int, int]:
    try:
        st = path.stat()
        return st.st_mtime_ns, st.st_size
    except OSError:
        return 0, 0


def _read_touched_cell_states_cached(run_dir: Path) -> dict[str, dict]:
    scells = run_dir / "state_cells.csv"
    sig = _path_signature(scells)
    cache_key = str(scells.resolve())
    cached = _TOUCHED_GRID_CACHE.get(cache_key)
    if cached and cached.get("state_sig") == sig and "states" in cached:
        return cached["states"]

    out: dict[str, dict] = {}
    if scells.exists():
        try:
            with scells.open("r", encoding="utf-8-sig", newline="") as f:
                for row in csv.DictReader(f):
                    cid = (row.get("cell_id") or "").strip()
                    status = (row.get("status") or "").strip()
                    if not cid or not status or status in ("queued", "pending"):
                        continue
                    try:
                        pc = int(row.get("place_count") or 0)
                    except (TypeError, ValueError):
                        pc = 0
                    order = {"pending": 0, "failed": 1, "done": 2, "in_progress": 3}
                    prev = out.get(cid)
                    if prev is None or order.get(status, 0) >= order.get(prev["status"], 0):
                        out[cid] = {"cell_id": cid, "status": status, "place_count": pc}
        except Exception:
            logger.debug("grid.touched_state_read_failed run_dir=%s", run_dir, exc_info=True)

    cached = dict(cached or {})
    cached.update({"state_sig": sig, "states": out})
    _TOUCHED_GRID_CACHE[cache_key] = cached
    return out


def _grid_feature_index_cached(gpath: Path) -> dict:
    sig = _path_signature(gpath)
    cache_key = str(gpath.resolve())
    cached = _GRID_INDEX_CACHE.get(cache_key)
    if cached and cached.get("grid_sig") == sig:
        return cached
    gj = json.loads(gpath.read_text(encoding="utf-8"))
    features = gj.get("features", []) or []
    by_id = {
        (f.get("properties") or {}).get("cell_id"): f
        for f in features
        if (f.get("properties") or {}).get("cell_id")
    }
    cached = {
        "grid_sig": sig,
        "feature_count": len(features),
        "by_id": by_id,
    }
    _GRID_INDEX_CACHE[cache_key] = cached
    return cached


def _touched_grid_payload_cached(run_dir: Path, gpath: Path) -> dict:
    states = _read_touched_cell_states_cached(run_dir)
    if not states:
        return {"type": "FeatureCollection", "features": []}

    grid_index = _grid_feature_index_cached(gpath)
    cache_key = f"{run_dir.resolve()}::payload"
    state_sig = _path_signature(run_dir / "state_cells.csv")
    grid_sig = grid_index.get("grid_sig")
    cached = _TOUCHED_GRID_CACHE.get(cache_key)
    if cached and cached.get("state_sig") == state_sig and cached.get("grid_sig") == grid_sig:
        return cached["payload"]

    by_id = grid_index.get("by_id") or {}
    filtered = []
    for cid, state in states.items():
        feature = by_id.get(cid)
        if not feature:
            continue
        props = dict(feature.get("properties") or {})
        props.update(state)
        filtered.append({
            "type": feature.get("type", "Feature"),
            "geometry": feature.get("geometry"),
            "properties": props,
        })
    payload = {"type": "FeatureCollection", "features": filtered}
    _TOUCHED_GRID_CACHE[cache_key] = {
        "state_sig": state_sig,
        "grid_sig": grid_sig,
        "payload": payload,
    }
    return payload


def _truncate_in_place(path: Path) -> bool:
    if not path.exists() or not path.is_file():
        return False
    with path.open("r+b") as f:
        f.truncate(0)
    return True


def _truncate_run_logs(run_dir: Path) -> dict:
    patterns = (
        "run.log",
        "run.log.*",
        "run.debug.log",
        "run.debug.log.*",
        "subprocess.log",
        "subprocess.log.*",
    )
    touched: list[str] = []
    failed: list[str] = []
    seen: set[Path] = set()
    for pattern in patterns:
        for path in run_dir.glob(pattern):
            if path.name == "run.errors.log" or path in seen:
                continue
            seen.add(path)
            try:
                if _truncate_in_place(path):
                    touched.append(path.name)
            except Exception:
                failed.append(path.name)
    return {"truncated": touched, "failed": failed}


def _live_summary_base(run_dir: Path, registry: RunRegistry, *, include_arrays: bool = False) -> dict:
    cfg = _read_run_config(run_dir)
    hb = _read_live_state(run_dir) or _read_last_heartbeat(run_dir)
    run_id = run_dir.name
    managed = registry.get(run_id)
    is_active = managed is not None and managed.exit_code is None

    out = {
        "run_id": run_id,
        "started_at": cfg.get("started_at"),
        "city": cfg.get("city_name"),
        "queries": cfg.get("queries", []),
        "selected_columns": cfg.get("selected_columns", []),
        "osm_relation_id": cfg.get("osm_relation_id"),
        "osm_type": cfg.get("osm_type", "relation"),
        "grid_size_meters": cfg.get("grid_size_meters", 250),
        "num_consumer_threads": cfg.get("num_consumer_threads", 8),
        "discovery_backend": cfg.get("discovery_backend", "places_api"),
        "places_tier": cfg.get("places_tier", "ESSENTIALS"),
        "active": is_active,
        "exit_code": managed.exit_code if managed else None,
        "discovered": _coerce_int(hb.get("discovered")) or 0,
        "scraped": _coerce_int(hb.get("scraped")) or 0,
        "errors": _coerce_int(hb.get("errors")) or 0,
        "queue_depth": hb.get("queue_depth", 0),
        "rps_inst": hb.get("rps_inst", 0.0),
        "elapsed_s": hb.get("elapsed_s", 0),
        "api_calls": hb.get("api_calls", 0),
        "cost_usd": hb.get("cost_usd", 0.0),
        "discovery_rps": hb.get("discovery_rps", 0.0),
        "active_keys": hb.get("active_keys", 0),
        "cells_done": _coerce_int(hb.get("cells_done")) or 0,
        "cells_failed": _coerce_int(hb.get("cells_failed")) or 0,
        "cells_partial": _coerce_int(hb.get("cells_partial")) or 0,
        "cells_in_flight": _coerce_int(hb.get("cells_in_flight")) or 0,
        "cells_total": _coerce_int(hb.get("cells_total")) or 0,
        "grid_cells_total": _coerce_int(hb.get("grid_cells_total")) or 0,
        "queries_total": _coerce_int(hb.get("queries_total")) or len(cfg.get("queries", []) or []),
        "placeids_done": _coerce_int(hb.get("placeids_done")) or 0,
        "placeids_failed": _coerce_int(hb.get("placeids_failed")) or 0,
        "placeids_pending": _coerce_int(hb.get("placeids_pending")) or 0,
    }
    if include_arrays:
        out["workers"] = hb.get("workers", [])
        out["api_keys"] = hb.get("api_keys", [])
    return out


def _summarize_run_lite(run_dir: Path, registry: RunRegistry) -> dict:
    """Cheap summary for list/stats. Must not open CSV files."""
    return _live_summary_base(run_dir, registry, include_arrays=False)


def _summarize_run_live(run_dir: Path, registry: RunRegistry) -> dict:
    """Cheap live summary for an opened run. Includes small live arrays only."""
    return _live_summary_base(run_dir, registry, include_arrays=True)


def _summarize_run_detail(run_dir: Path, registry: RunRegistry) -> dict:
    """Expensive detail summary. Only use for explicit detail/debug requests."""
    out = _summarize_run_live(run_dir, registry)
    state = _state_status_summary(run_dir)
    discovered_csv = _csv_count(run_dir / "phase1_discovered.csv")
    phase2_csv = _csv_count(run_dir / "phase2_data.csv")
    failures_csv = _csv_count(run_dir / "phase2_failures.csv")
    out.update({
        "discovered": max(out["discovered"], discovered_csv),
        "scraped": max(out["scraped"], state["placeids_done"], phase2_csv),
        "errors": max(out["errors"], state["placeids_failed"], failures_csv),
        "phase1_csv_rows": discovered_csv,
        "phase2_csv_rows": phase2_csv,
        "failures_rows": failures_csv,
        "cells": _read_cell_states(run_dir),
        **state,
    })
    return out


def _summarize_run(run_dir: Path, registry: RunRegistry) -> dict:
    cfg = _read_run_config(run_dir)
    # Prefer live_state.json (full current snapshot incl. workers + api_keys);
    # fall back to the last heartbeat.jsonl line for older runs.
    hb = _read_live_state(run_dir) or _read_last_heartbeat(run_dir)
    state = _state_status_summary(run_dir)
    discovered_csv = _csv_count(run_dir / "phase1_discovered.csv")
    phase2_csv = _csv_count(run_dir / "phase2_data.csv")
    failures_csv = _csv_count(run_dir / "phase2_failures.csv")

    run_id = run_dir.name
    managed = registry.get(run_id)
    is_active = managed is not None and managed.exit_code is None
    discovered = max(_coerce_int(hb.get("discovered")) or 0, discovered_csv)
    scraped = max(
        _coerce_int(hb.get("scraped")) or 0,
        state["placeids_done"],
        phase2_csv,
    )
    errors = max(
        _coerce_int(hb.get("errors")) or 0,
        state["placeids_failed"],
        failures_csv,
    )

    return {
        "run_id": run_id,
        "started_at": cfg.get("started_at"),
        "city": cfg.get("city_name"),
        "queries": cfg.get("queries", []),
        "selected_columns": cfg.get("selected_columns", []),
        # Resume-relevant config fields — surfaced so the resume button can
        # build a StartRunBody that matches the run's original area, grid,
        # and column selection (mandatory — the scraper refuses to resume
        # if columns drift).
        "osm_relation_id": cfg.get("osm_relation_id"),
        "osm_type": cfg.get("osm_type", "relation"),
        "grid_size_meters": cfg.get("grid_size_meters", 250),
        "num_consumer_threads": cfg.get("num_consumer_threads", 8),
        "discovery_backend": cfg.get("discovery_backend", "places_api"),
        "places_tier": cfg.get("places_tier", "ESSENTIALS"),
        "active": is_active,
        "exit_code": managed.exit_code if managed else None,
        "discovered": discovered,
        "scraped": scraped,
        "errors": errors,
        "queue_depth": hb.get("queue_depth", 0),
        "rps_inst": hb.get("rps_inst", 0.0),
        "elapsed_s": hb.get("elapsed_s", 0),
        "api_calls": hb.get("api_calls", 0),
        "cost_usd": hb.get("cost_usd", 0.0),
        "workers": hb.get("workers", []),
        "api_keys": hb.get("api_keys", []),
        "discovery_rps": hb.get("discovery_rps", 0.0),
        "active_keys": hb.get("active_keys", 0),
        "phase1_csv_rows": discovered_csv,
        "phase2_csv_rows": phase2_csv,
        "failures_rows": failures_csv,
        "cells": _read_cell_states(run_dir),
        **state,
    }


def _list_run_dirs() -> list[Path]:
    if not OUTPUT_DIR.exists():
        return []
    out = [p for p in OUTPUT_DIR.iterdir() if p.is_dir() and (p / "run_config.json").exists()]
    out.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return out


# ─── app ──────────────────────────────────────────────────

def create_app() -> FastAPI:
    app = FastAPI(title="parallel-scraper", docs_url=None, redoc_url=None)
    registry = RunRegistry()
    app.state.registry = registry

    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")
    _dashboard_html = (_TEMPLATES_DIR / "dashboard.html").read_text(encoding="utf-8")

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request):
        return HTMLResponse(_dashboard_html, headers={"cache-control": "no-store"})

    # ─── grid-scraper UX endpoints ───────────────────────

    @app.post("/api/grid-scraper/search-city")
    async def search_city_route(body: SearchCityBody):
        from parallel_scraper.boundary import search_city
        try:
            cands = search_city(body.query.strip(), limit=8)
        except Exception as e:
            logger.exception("search-city failed")
            raise HTTPException(502, f"nominatim error: {e}")
        return {"candidates": [
            {
                "display_name": c.display_name,
                "osm_id": c.osm_id,
                "osm_type": c.osm_type,
                "category": c.category,
                "lat": c.lat,
                "lon": c.lon,
                "bbox": list(c.bbox),
            } for c in cands
        ]}

    @app.post("/api/grid-scraper/generate-grid")
    async def generate_grid_route(body: GenerateGridBody):
        from parallel_scraper.boundary import (
            boundary_from_polygon, boundary_to_geojson,
            fetch_boundary, generate_grid, grid_to_geojson,
        )

        if body.custom_polygon:
            try:
                # Flat ring or list of rings — boundary_from_polygon handles both.
                boundary = boundary_from_polygon(body.custom_polygon)
            except Exception as e:
                raise HTTPException(400, f"invalid polygon: {e}")
            display_name = body.area_name or "custom polygon"
        elif body.osm_id:
            try:
                boundary = fetch_boundary(body.osm_id, body.osm_type)
            except Exception as e:
                logger.exception("fetch_boundary failed")
                raise HTTPException(502, f"boundary fetch failed: {e}")
            display_name = boundary["display_name"].iloc[0] if "display_name" in boundary else ""
        else:
            raise HTTPException(400, "either osm_id or custom_polygon required")

        prefix = (body.area_name[:3] or display_name[:3] or "AREA").upper()
        try:
            cells = generate_grid(boundary, body.grid_size_meters,
                                  city_prefix=prefix, grid_type=body.grid_type)
        except Exception as e:
            logger.exception("generate_grid failed")
            raise HTTPException(500, f"grid generation failed: {e}")

        return {
            "boundary_geojson": boundary_to_geojson(boundary),
            "grid_geojson": grid_to_geojson(cells),
            "total_cells": len(cells),
            "city_display_name": display_name,
        }

    # ─── query presets (JSON-backed store) ───────────────

    @app.get("/api/presets/queries")
    async def list_query_presets():
        return {"presets": presets_store.list_presets(PRESETS_PATH)}

    @app.post("/api/presets/queries")
    async def save_query_preset(body: SavePresetBody):
        name = body.name.strip()
        if not name:
            raise HTTPException(400, "name required")
        try:
            presets_store.save_preset(
                PRESETS_PATH, name,
                [{"q": q.q, "label": q.label or q.q} for q in body.queries],
            )
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {"ok": True, "name": name}

    @app.delete("/api/presets/queries/{name}")
    async def delete_query_preset(name: str):
        ok = presets_store.delete_preset(PRESETS_PATH, name)
        if not ok:
            raise HTTPException(404, "preset not found")
        return {"ok": True}

    # ─── columns/runs ────────────────────────────────────

    @app.get("/api/columns")
    async def get_columns():
        return {
            "catalog": COLUMN_CATALOG,
            "profiles": {k: list(v) for k, v in PROFILES.items()},
            "lean": list(LEAN_PROFILE),
        }

    @app.get("/api/runs")
    async def list_runs():
        runs = [_summarize_run_lite(p, registry) for p in _list_run_dirs()]
        return {"runs": runs, "active_count": sum(1 for r in runs if r["active"])}

    @app.get("/api/runs/{run_id}")
    async def get_run(run_id: str):
        run_dir = OUTPUT_DIR / run_id
        if not run_dir.exists() or not (run_dir / "run_config.json").exists():
            raise HTTPException(404, "run not found")
        return _summarize_run_live(run_dir, registry)

    @app.get("/api/runs/{run_id}/leads")
    async def get_run_leads(run_id: str, q: str = "", page: int = 0, limit: int = 200):
        run_dir = OUTPUT_DIR / run_id
        if not run_dir.exists():
            raise HTTPException(404, "run not found")
        try:
            limit = max(1, min(int(limit), 1000))
            page = max(0, int(page))
        except (TypeError, ValueError):
            limit, page = 200, 0
        leads, total = _read_phase2_leads(run_dir, q=q.strip(), page=page, limit=limit)
        return {"leads": leads, "total": total, "page": page, "limit": limit}

    @app.get("/api/runs/{run_id}/leads.csv")
    async def get_run_leads_csv(run_id: str, q: str = ""):
        """Stream phase2_data.csv directly (preferred) or phase2_data.full.csv.gz inflated.
        If `q` is provided, filter rows by case-insensitive contains across name/category/address."""
        run_dir = OUTPUT_DIR / run_id
        full = run_dir / "phase2_data.full.csv.gz"
        lean = run_dir / "phase2_data.csv"
        src = lean if lean.exists() else (full if full.exists() else None)
        if src is None:
            raise HTTPException(404, "no scraped data yet")

        if False and not q.strip():
            # No filter — stream the file as-is.
            from fastapi.responses import FileResponse
            if src == lean:
                return FileResponse(src, media_type="text/csv",
                                    filename=f"{run_id}_leads.csv")
            # gzipped: inflate on the fly into a generator
            import gzip, io
            def gen():
                with gzip.open(src, "rt", encoding="utf-8-sig", newline="") as f:
                    while True:
                        chunk = f.read(64 * 1024)
                        if not chunk: break
                        yield chunk
            return StreamingResponse(gen(), media_type="text/csv",
                                     headers={"content-disposition": f'attachment; filename="{run_id}_leads.csv"'})

        # Filter case — stream rows whose name/category/address contains q.
        ql = q.strip().lower()
        def gen():
            f = _open_csv_any(src)
            if f is None:
                return
            with f as fh:
                reader = csv.reader(fh)
                try:
                    header = next(reader)
                except StopIteration:
                    return
                yield ",".join(_csv_escape(h) for h in header) + "\n"
                idx = {h: i for i, h in enumerate(header)}
                pid_i = idx.get("place_id")
                seen: set[str] = set()
                for row in reader:
                    if pid_i is not None and pid_i < len(row):
                        pid = (row[pid_i] or "").strip()
                        if pid:
                            if pid in seen:
                                continue
                            seen.add(pid)
                    def col(name):
                        i = idx.get(name)
                        return (row[i] if i is not None and i < len(row) else "") or ""
                    haystack = (col("name") + " " + col("category") + " " + col("address")).lower()
                    if not ql or ql in haystack:
                        yield ",".join(_csv_escape(c) for c in row) + "\n"
        return StreamingResponse(gen(), media_type="text/csv",
                                 headers={"content-disposition": f'attachment; filename="{run_id}_leads.csv"'})

    @app.get("/api/stats")
    async def get_stats():
        runs = [_summarize_run_lite(p, registry) for p in _list_run_dirs()]
        total_runs = len(runs)
        total_places = sum(r.get("scraped") or 0 for r in runs)
        total_spend = 0.0
        api_calls_today = 0
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        for r in runs:
            total_spend += float(r.get("cost_usd") or 0)
            started = r.get("started_at") or ""
            if isinstance(started, str) and started.startswith(today):
                api_calls_today += int(r.get("api_calls") or 0)
        # rough throughput: scraped per minute over the last hour, summed across active runs
        throughput = 0
        for r in runs:
            if r.get("active") and (r.get("elapsed_s") or 0) > 0:
                throughput += int(60 * (r.get("scraped") or 0) / max(1, r.get("elapsed_s")))
        archived = sum(1 for r in runs if not r.get("active") and (r.get("scraped") or 0) > 0)
        return {
            "total_runs": total_runs,
            "total_places": total_places,
            "total_spend": round(total_spend, 2),
            "api_calls_today": api_calls_today,
            "throughput_per_min": throughput,
            "disk_usage_mb": _disk_usage_mb_cached(OUTPUT_DIR),
            "archived_count": archived,
        }

    @app.get("/api/api-keys")
    async def get_api_keys():
        return _read_api_key_inventory()

    @app.get("/api/runs/{run_id}/boundary")
    async def get_run_boundary(run_id: str):
        """Return the area outline (Polygon/MultiPolygon FeatureCollection)
        so the dashboard can show the scrape boundary on the map.

        Lazily generated on first call: reads osm_relation_id (or saved
        custom polygon) from run_config.json, fetches the boundary from
        Nominatim or builds it locally, caches to boundary.geojson.
        """
        run_dir = OUTPUT_DIR / run_id
        bpath = run_dir / "boundary.geojson"
        # Fast path — cached
        if bpath.exists():
            try:
                return JSONResponse(json.loads(bpath.read_text(encoding="utf-8")))
            except Exception:
                # Cache file is corrupt; fall through to regenerate
                pass

        cfg_path = run_dir / "run_config.json"
        if not cfg_path.exists():
            raise HTTPException(404, "run_config.json missing — run hasn't initialized")
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))

        try:
            from parallel_scraper.boundary import (  # type: ignore
                fetch_boundary, boundary_from_polygon, boundary_to_geojson,
            )
        except Exception as e:
            raise HTTPException(500, f"boundary module unavailable: {e}")

        # Prefer a saved custom polygon if present (operator-drawn area).
        custom_polygon_path = run_dir / "boundary_polygon.json"
        try:
            if custom_polygon_path.exists():
                coords_raw = json.loads(custom_polygon_path.read_text(encoding="utf-8"))
                # boundary_polygon.json stores [[lat, lng], ...] or a list of
                # such rings (MultiPolygon) — boundary_from_polygon handles both.
                gdf = boundary_from_polygon(coords_raw)
            else:
                osm_id = cfg.get("osm_relation_id")
                osm_type = cfg.get("osm_type", "relation")
                if not osm_id:
                    raise HTTPException(404, "no boundary source (osm_relation_id missing and no custom polygon)")
                gdf = fetch_boundary(int(osm_id), str(osm_type))
            payload = boundary_to_geojson(gdf)
        except HTTPException:
            raise
        except Exception as e:
            logger.exception("boundary.build_failed run_id=%s", run_id)
            raise HTTPException(500, f"boundary build failed: {e}")

        # Cache to disk so subsequent calls are instant.
        try:
            bpath.write_text(json.dumps(payload), encoding="utf-8")
        except Exception:
            logger.warning("boundary.cache_write_failed run_id=%s", run_id)
        return JSONResponse(payload)

    @app.get("/api/runs/{run_id}/grid")
    async def get_run_grid(run_id: str, mode: str = "full"):
        """Serve the grid GeoJSON.

        mode=full (default): the full FeatureCollection. Fine for small grids.
        mode=touched: only features whose cell_id appears in state_cells.csv
                      with a non-queued status. For MMR-scale grids (70K
                      cells / 22 MB) this drops the payload by 10-100x and
                      avoids freezing the browser on JSON parse.
        mode=meta:    no features, just feature count + bbox. Use when you
                      only need to know how big the grid is.
        """
        run_dir = OUTPUT_DIR / run_id
        gpath = run_dir / "grid.geojson"
        if not gpath.exists():
            raise HTTPException(404, "grid not yet persisted (producer hasn't started)")

        if mode == "meta":
            try:
                return JSONResponse({
                    "type": "FeatureCollection",
                    "features": [],
                    "feature_count": int(_grid_feature_index_cached(gpath).get("feature_count") or 0),
                })
            except Exception as e:
                raise HTTPException(500, f"grid read failed: {e}")

        if mode == "touched":
            return JSONResponse(_touched_grid_payload_cached(run_dir, gpath))
            # Read state_cells.csv first — small file even for MMR (~few MB).
            scells = run_dir / "state_cells.csv"
            touched_ids: set[str] = set()
            if scells.exists():
                try:
                    with scells.open("r", encoding="utf-8-sig", newline="") as f:
                        for r in csv.DictReader(f):
                            status = (r.get("status") or "").strip()
                            cid = (r.get("cell_id") or "").strip()
                            if cid and status and status not in ("queued", "pending"):
                                touched_ids.add(cid)
                except Exception:
                    pass
            if not touched_ids:
                # Nothing touched yet — return the empty FC immediately.
                return JSONResponse({"type": "FeatureCollection", "features": []})
            gj = json.loads(gpath.read_text(encoding="utf-8"))
            filtered = [
                f for f in gj.get("features", [])
                if (f.get("properties") or {}).get("cell_id") in touched_ids
            ]
            return JSONResponse({"type": "FeatureCollection", "features": filtered})

        # full
        return JSONResponse(json.loads(gpath.read_text(encoding="utf-8")))

    @app.get("/api/runs/{run_id}/logs")
    async def get_run_logs(run_id: str, n: int = 200):
        """Return the last `n` lines of run.log, parsed as either JSON-lines or plain text."""
        run_dir = OUTPUT_DIR / run_id
        lpath = run_dir / "run.log"
        if not lpath.exists():
            return {"lines": []}
        try:
            with lpath.open("rb") as f:
                f.seek(0, 2)
                size = f.tell()
                read = min(size, max(8192, n * 256))
                f.seek(max(0, size - read))
                tail = f.read().decode("utf-8", errors="replace")
        except Exception:
            return {"lines": []}
        raw_lines = tail.splitlines()[-n:]
        out = []
        for line in raw_lines:
            line = line.strip()
            if not line:
                continue
            parsed = _parse_log_line(line)
            if parsed:
                out.append(parsed)
        return {"lines": out}

    @app.post("/api/runs/{run_id}/logs/truncate")
    async def truncate_run_logs(run_id: str):
        run_dir = OUTPUT_DIR / run_id
        if not run_dir.exists():
            raise HTTPException(404, "run not found")
        result = _truncate_run_logs(run_dir)
        return {"ok": not result["failed"], **result}

    @app.get("/api/runs/{run_id}/stream")
    async def stream_run(run_id: str):
        run_dir = OUTPUT_DIR / run_id
        if not run_dir.exists():
            raise HTTPException(404, "run not found")

        async def event_gen():
            yield _sse("snapshot", _summarize_run_live(run_dir, registry))
            hb_path = run_dir / "heartbeat.jsonl"
            offset = hb_path.stat().st_size if hb_path.exists() else 0
            while True:
                await asyncio.sleep(2)
                try:
                    if hb_path.exists():
                        size = hb_path.stat().st_size
                        if size < offset:
                            offset = 0
                        if size > offset:
                            with hb_path.open("rb") as f:
                                f.seek(offset)
                                chunk = f.read().decode("utf-8", errors="replace")
                            offset = size
                            for line in chunk.splitlines():
                                line = line.strip()
                                if not line:
                                    continue
                                try:
                                    yield _sse("heartbeat", json.loads(line))
                                except json.JSONDecodeError:
                                    continue
                    managed = registry.get(run_id)
                    yield _sse("heartbeat", {
                        "event": "status",
                        "active": managed is not None and managed.exit_code is None,
                        "exit_code": managed.exit_code if managed else None,
                    })
                except Exception:
                    logger.exception("sse.stream_error run_id=%s", run_id)
                    yield _sse("error", {"message": "stream error"})

        return StreamingResponse(event_gen(), media_type="text/event-stream",
                                 headers={"cache-control": "no-cache",
                                          "x-accel-buffering": "no"})

    @app.post("/api/runs")
    async def start_run(body: StartRunBody):
        # Server-side sanitize: the run_id becomes a filesystem directory under
        # OUTPUT_DIR, so anything outside [A-Za-z0-9_-] is collapsed to "_".
        # Defense in depth — the frontend already sanitizes, but a direct POST
        # could bypass that. Reject reserved names + path separators outright.
        import re as _re
        if body.run_id:
            raw = body.run_id.strip()
            cleaned = _re.sub(r"[^A-Za-z0-9_-]", "_", raw).lstrip(".")
            if not cleaned or cleaned in {".", "..", ""}:
                raise HTTPException(400, f"invalid run_id (empty after sanitize): {body.run_id!r}")
            if len(cleaned) > 64:
                raise HTTPException(400, "run_id too long (max 64 chars)")
            run_id = cleaned
        else:
            run_id = _new_run_id()
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        run_dir = OUTPUT_DIR / run_id
        # Defensive: ensure the resolved run_dir is actually inside OUTPUT_DIR.
        try:
            run_dir.resolve().relative_to(OUTPUT_DIR.resolve())
        except ValueError:
            raise HTTPException(400, f"run_id resolves outside outputs/: {run_id!r}")
        run_dir.mkdir(parents=True, exist_ok=True)
        existing = registry.get(run_id)
        if existing and existing.exit_code is None:
            raise HTTPException(409, "run already active")

        # Persist the polygon (if any) into the run_dir so the subprocess can read it.
        polygon_file: Optional[str] = None
        if body.custom_polygon:
            polygon_file = str((run_dir / "boundary_polygon.json").resolve())
            (run_dir / "boundary_polygon.json").write_text(
                json.dumps(body.custom_polygon), encoding="utf-8")

        body_with_id = body.model_copy(update={"run_id": run_id})
        args = body_with_id.to_cli_args(str(OUTPUT_DIR), polygon_file=polygon_file)
        managed = registry.start(run_id, args, cwd=Path.cwd())
        return {"run_id": run_id, "pid": managed.pid, "cmd": managed.cmd}

    @app.post("/api/runs/{run_id}/stop")
    async def stop_run(run_id: str):
        ok = registry.stop(run_id)
        if not ok:
            raise HTTPException(404, "no active run with that id")
        return JSONResponse({"run_id": run_id, "stop_requested": True})

    return app


def _sse(event: str, data) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


_LEVEL_RE = None  # lazily compiled

def _parse_log_line(line: str) -> Optional[dict]:
    """Parse a JSON-lines log entry; fall back to a regex on the human format."""
    if line.startswith("{") and line.endswith("}"):
        try:
            obj = json.loads(line)
            return {
                "ts": obj.get("asctime") or obj.get("timestamp") or "",
                "level": obj.get("levelname") or obj.get("level") or "INFO",
                "name": obj.get("name") or "",
                "msg": obj.get("message") or obj.get("msg") or "",
                "cell_id": obj.get("cell_id") or "",
                "place_id": obj.get("place_id") or "",
                "phase": obj.get("phase") or "",
            }
        except json.JSONDecodeError:
            pass
    # Fallback: "12:34:56 [LEVEL] [run/phase/cell/place] name: msg"
    global _LEVEL_RE
    import re
    if _LEVEL_RE is None:
        _LEVEL_RE = re.compile(
            r"^(?P<ts>[\d:.\- T]+)?\s*\[(?P<level>[A-Z]+)\]\s*"
            r"(\[(?P<ctx>[^\]]*)\]\s*)?(?P<name>[\w\.]+)?:?\s*(?P<msg>.*)$"
        )
    m = _LEVEL_RE.match(line)
    if not m:
        return {"ts": "", "level": "INFO", "name": "", "msg": line, "cell_id": "", "place_id": "", "phase": ""}
    ctx = (m.group("ctx") or "").split("/")
    cell_id = ctx[2] if len(ctx) > 2 else ""
    place_id = ctx[3] if len(ctx) > 3 else ""
    phase = ctx[1] if len(ctx) > 1 else ""
    return {
        "ts": (m.group("ts") or "").strip(),
        "level": m.group("level") or "INFO",
        "name": m.group("name") or "",
        "msg": m.group("msg") or "",
        "cell_id": cell_id,
        "place_id": place_id,
        "phase": phase,
    }


def run() -> None:
    """Console-script entry point. Launch via `parallel-scraper-web`."""
    import argparse
    import uvicorn

    parser = argparse.ArgumentParser(prog="parallel-scraper-web")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--output-dir", default=os.environ.get(
        "PARALLEL_SCRAPER_OUTPUT_DIR", "outputs"))
    parser.add_argument("--reload", action="store_true")
    args = parser.parse_args()

    os.environ["PARALLEL_SCRAPER_OUTPUT_DIR"] = args.output_dir
    global OUTPUT_DIR
    OUTPUT_DIR = Path(args.output_dir)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    print(f"\n  parallel-scraper dashboard")
    print(f"  output dir: {OUTPUT_DIR.resolve()}")
    print(f"  serving:    http://{args.host}:{args.port}\n")
    uvicorn.run(
        "parallel_scraper.web.server:create_app",
        host=args.host, port=args.port,
        factory=True, reload=args.reload,
        log_level="info",
    )


if __name__ == "__main__":
    run()
