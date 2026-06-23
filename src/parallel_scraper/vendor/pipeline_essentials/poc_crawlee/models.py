"""Data models for the Crawlee Google Maps scraper POC."""

from dataclasses import dataclass, field, asdict
from typing import Optional, List, Dict


@dataclass
class PlaceData:
    """All attributes extracted from a Google Maps place page."""

    # --- Identifier ---
    place_id: str = "N/A"

    # --- Existing (ported from scraper.py) ---
    name: str = "N/A"
    rating: str = "N/A"
    review_count: str = "0"
    category: str = "N/A"
    status: str = "Open"
    address: str = "N/A"
    phone: str = "N/A"
    share_link: str = "N/A"

    # --- New attributes ---
    website: str = "N/A"
    hours: str = "N/A"
    price_level: str = "N/A"
    plus_code: str = "N/A"
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    photo_count: str = "0"
    total_photos: str = "0"          # all photos including Street View / Maps data
    about: str = "N/A"
    amenities: str = "N/A"          # semicolon-separated for CSV compat
    menu_link: str = "N/A"
    latest_review_date: str = "N/A"
    image_urls: str = "N/A"         # semicolon-separated
    image_downloaded: str = "0"     # 0 = not downloaded, 1 = downloaded

    # --- Metadata ---
    source_url: str = ""
    label: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ScrapeConfig:
    """Configuration for a scrape run."""

    place_id: str = ""  # Optional: pass from input CSV; else extracted from URL
    location: str = ""
    location_full: str = ""
    search_queries: List[Dict[str, str]] = field(default_factory=list)
    headless: bool = True
    max_concurrency: int = 5
    max_results_per_query: int = 0      # 0 = unlimited
    extract_share_link: bool = False
    request_timeout: int = 30
    max_retries: int = 3
    proxy_urls: List[str] = field(default_factory=list)
    output_format: str = "csv"          # csv or xlsx
    output_path: str = ""
