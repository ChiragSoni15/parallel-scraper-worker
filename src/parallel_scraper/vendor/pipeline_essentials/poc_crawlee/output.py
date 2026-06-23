"""Output writers for scrape results — CSV only (no pandas dependency)."""

import csv
import os
import logging
from pathlib import Path
from typing import List, Dict
from datetime import datetime

logger = logging.getLogger(__name__)

# Column ordering for output files
COLUMN_ORDER = [
    "place_id",
    "name",
    "rating",
    "review_count",
    "category",
    "status",
    "address",
    "phone",
    "website",
    "latitude",
    "longitude",
    "image_urls",
    "image_downloaded",
    "source_url",
]


def append_csv_row(row: Dict, output_path: str, fieldnames: List[str] = None) -> None:
    """
    Append a single row to CSV (for incremental saves during Phase 1).
    Creates file with header if it doesn't exist.
    """
    if fieldnames is None:
        fieldnames = COLUMN_ORDER
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    file_exists = path.exists()
    with open(path, "a", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        if not file_exists:
            writer.writeheader()
        writer.writerow({k: row.get(k, "") for k in fieldnames})
