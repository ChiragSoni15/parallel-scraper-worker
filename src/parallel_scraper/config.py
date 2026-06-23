"""Configuration: column catalog + ParallelConfig dataclass."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


COLUMN_CATALOG: dict[str, str] = {
    "name":               "Name",
    "rating":             "Rating",
    "review_count":       "Review count",
    "category":           "Category",
    "status":             "Open/closed status",
    "address":            "Address",
    "phone":              "Phone",
    "website":            "Website",
    "latitude":           "Latitude",
    "longitude":          "Longitude",
    "image_urls":         "Image URLs (semicolon-joined)",
    "source_url":         "Source Maps URL",
    "hours":              "Hours of operation",
    "price_level":        "Price level",
    "plus_code":          "Plus code",
    "photo_count":        "Photo count (carousel)",
    "total_photos":       "Total photos",
    "about":              "About / description",
    "amenities":          "Amenities (semicolon-joined)",
    "menu_link":          "Menu URL",
    "latest_review_date": "Latest review date",
}

ALWAYS_INCLUDED: tuple[str, ...] = ("place_id",)

LEAN_PROFILE: tuple[str, ...] = (
    "name", "rating", "review_count", "category", "status",
    "address", "phone", "latitude", "longitude", "source_url",
)

PROFILES: dict[str, tuple[str, ...]] = {
    "lean": LEAN_PROFILE,
    "rich": tuple(COLUMN_CATALOG.keys()),
    "reviews-only": ("name", "rating", "review_count", "latest_review_date", "source_url"),
    "default": LEAN_PROFILE,
}


@dataclass(frozen=True)
class ParallelConfig:
    osm_relation_id: int = 13312356
    osm_type: str = "relation"
    city_name: str = "Mumbai"
    grid_size_meters: int = 1000
    grid_type: str = "square"
    queries: tuple[str, ...] = ("shops",)
    # Phase-1 (placeId discovery): Google Places Text Search by default.
    # Use "maps_frontend" only for the older Playwright Maps-search path.
    discovery_backend: str = "places_api"
    places_tier: str = "ESSENTIALS"
    places_language_code: str = "en"
    places_region_code: str = "IN"
    places_use_location_restriction: bool = True
    # Phase-1 workers are API worker threads for places_api, Chromium contexts for maps_frontend.
    # Phase-2 (deep metadata): N concurrent Chromium browsers hitting place pages.
    phase1_workers: int = 3
    phase2_workers: int = 5
    # Back-compat alias: prior CLI used `--threads` -> num_consumer_threads.
    num_consumer_threads: int = 5
    max_queue_size: int = 1000
    discovery_rps: float = 2.0
    consumer_rps_per_thread: float = 2.0
    consumer_delay_ms: int = 500
    max_retries_transient: int = 4
    heartbeat_interval_s: int = 30
    # Per-key Places API daily request quota. A key that reaches this many
    # calls in a billing day is skipped by the rotator (avoids its 429s).
    api_key_daily_quota: int = 75000
    # ── dynamic discovery rate scaling ──────────────────────────────────
    # Per-key Places API per-minute request limit (Google default ~600/min).
    per_key_qpm: int = 600
    # Fraction of the aggregate per-key limit to target. Discovery paces to
    # active_keys x (per_key_qpm/60) x rps_utilization requests/sec.
    rps_utilization: float = 0.85
    # When a key returns this many consecutive 429s it is put in cooldown.
    key_429_threshold: int = 2
    # How long a rate-limited key sits out of rotation before returning.
    # Reduced from 90s — with per-task jitter in the worker hot path,
    # 429s are rare; when they DO land, 30s is enough for Google's per-
    # second token bucket to refill while keeping the fleet recovery time
    # under a minute (vs 1.5min for the old value).
    key_429_cooldown_s: int = 30
    selected_columns: tuple[str, ...] = ()
    master_dedup_csv: str = "inputs/known_placeids.csv"
    output_dir: str = "outputs"
    run_id: Optional[str] = None
    max_places: Optional[int] = None
    dry_run: bool = False
    worker_browser_recycle_after: int = 50
    # Every Nth recycle does a full Chromium relaunch (to reclaim RSS growth);
    # the other recycles just swap the page on the same warm context (~50ms vs
    # ~1.5-2s). 0 disables full relaunches (page-only forever).
    worker_full_relaunch_every: int = 10
    # Phase-2 image downloads run on a shared background pool instead of inline
    # in the scrape critical path. 0 reverts to inline downloads.
    image_download_workers: int = 6
    # Bounded backlog for the image pool. When full, the consumer downloads
    # inline (best-effort) so images are never dropped and memory can't grow.
    image_queue_maxsize: int = 512
    discovery_max_scroll: int = 8
    discovery_scroll_settle_ms: int = 800
    # When set, takes precedence over osm_relation_id for boundary lookup.
    # Coords are [(lat, lng), ...] of the polygon's outer ring.
    boundary_polygon: Optional[tuple[tuple[float, float], ...]] = None

    def output_columns(self) -> tuple[str, ...]:
        """Final column list for phase2_data.csv: place_id first, then operator selection."""
        if not self.selected_columns:
            return ALWAYS_INCLUDED + tuple(COLUMN_CATALOG.keys())
        return ALWAYS_INCLUDED + tuple(c for c in self.selected_columns if c in COLUMN_CATALOG)
