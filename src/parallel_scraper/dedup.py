"""In-memory dedup cache. Loaded once at boot from a master CSV; never queries disk after."""
from __future__ import annotations

import csv
import logging
import threading
from pathlib import Path
from typing import Iterable

logger = logging.getLogger(__name__)


def _load_master(master_csv_path: str | Path | None) -> frozenset[str]:
    if not master_csv_path:
        return frozenset()
    p = Path(master_csv_path)
    if not p.exists():
        logger.warning("master_dedup_csv not found: %s (starting with empty cache)", p)
        return frozenset()
    seen: set[str] = set()
    with p.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None or "place_id" not in reader.fieldnames:
            logger.warning("master_dedup_csv has no 'place_id' column: %s", p)
            return frozenset()
        for row in reader:
            pid = (row.get("place_id") or "").strip()
            if pid:
                seen.add(pid)
    logger.info("dedup_cache.master_loaded count=%d path=%s", len(seen), p)
    return frozenset(seen)


def _load_run_seen(run_dir: str | Path | None) -> set[str]:
    """Rebuild per-run seen set from this run's phase2_data.csv (for resume)."""
    if not run_dir:
        return set()
    p = Path(run_dir) / "phase2_data.csv"
    if not p.exists():
        return set()
    seen: set[str] = set()
    with p.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames and "place_id" in reader.fieldnames:
            for row in reader:
                pid = (row.get("place_id") or "").strip()
                if pid:
                    seen.add(pid)
    if seen:
        logger.info("dedup_cache.run_seen_loaded count=%d path=%s", len(seen), p)
    return seen


class DedupCache:
    """Two-tier in-memory dedup: master frozenset (cross-run history) +
    per-run mutable set (within this run, including overlapping cells)."""

    def __init__(self, master_csv_path: str | Path | None,
                 run_dir: str | Path | None = None) -> None:
        self._master: frozenset[str] = _load_master(master_csv_path)
        self._run_seen: set[str] = _load_run_seen(run_dir)
        self._lock = threading.Lock()

    @property
    def master_size(self) -> int:
        return len(self._master)

    @property
    def run_seen_size(self) -> int:
        with self._lock:
            return len(self._run_seen)

    def is_known(self, place_id: str) -> bool:
        if place_id in self._master:
            return True
        with self._lock:
            return place_id in self._run_seen

    def is_run_seen(self, place_id: str) -> bool:
        with self._lock:
            return place_id in self._run_seen

    def filter(self, place_ids: Iterable[str]) -> list[str]:
        """Return survivors (not in master, not in run_seen). Adds survivors to run_seen."""
        survivors: list[str] = []
        with self._lock:
            for pid in place_ids:
                if not pid:
                    continue
                if pid in self._master or pid in self._run_seen:
                    continue
                self._run_seen.add(pid)
                survivors.append(pid)
        return survivors

    def add_run_seen(self, place_id: str) -> None:
        with self._lock:
            self._run_seen.add(place_id)
