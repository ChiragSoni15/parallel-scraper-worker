"""Heartbeat-driven rich.live progress display + heartbeat.jsonl emission."""
from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from parallel_scraper.logging_setup import drain_log_events, rotate_path_with_backoff

logger = logging.getLogger(__name__)

try:
    from rich.console import Console
    from rich.live import Live
    from rich.table import Table
    _HAS_RICH = True
except ImportError:  # pragma: no cover
    _HAS_RICH = False


# Phase-1 discovery calls Places API Text Search with an IDs-Only field mask
# (`places.id,places.name,nextPageToken` — see places_api_discovery.TIER_CONFIG,
# which uses that same mask for every tier). That triggers Google's
# "Text Search — IDs Only" SKU, which is FREE — there is no per-call charge.
# Discovery calls are therefore *counted* (they still consume each key's daily
# request quota) but they contribute $0 to cost. Kept as a constant so a paid
# SKU can be priced here later if the field mask ever changes.
TEXT_SEARCH_IDS_ONLY_PRICE_PER_1K: float = 0.0


@dataclass
class Stats:
    discovered: int = 0
    scraped: int = 0
    errors: int = 0
    queue_depth: int = 0
    api_calls: int = 0
    cost_usd: float = 0.0
    started_at: float = field(default_factory=time.monotonic)
    workers: dict = field(default_factory=dict)  # worker_id -> {phase, state, task, query, started_at}
    api_keys: list = field(default_factory=list)  # per-key usage status (from the key manager)
    discovery_rps: float = 0.0   # current dynamic discovery rate (req/sec)
    active_keys: int = 0         # keys currently available (not exhausted / cooling)
    cells_done: int = 0
    cells_failed: int = 0
    cells_partial: int = 0
    cells_in_flight: int = 0
    cells_total: int = 0
    grid_cells_total: int = 0
    queries_total: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def record_api_calls(self, n: int, tier: str = "") -> None:
        """Count Places API Text Search calls. The IDs-Only SKU is free, so this
        increments the call counter (which matters for each key's daily quota)
        but contributes $0 to cost_usd. `tier` is accepted for call-site
        compatibility but no longer affects pricing."""
        if n <= 0:
            return
        with self._lock:
            self.api_calls += n
            self.cost_usd += n * (TEXT_SEARCH_IDS_ONLY_PRICE_PER_1K / 1000.0)

    def worker_set(self, worker_id: str, *, phase, state: str, task: str = "", query: str = "") -> None:
        with self._lock:
            self.workers[worker_id] = {
                "id": worker_id, "phase": phase, "state": state,
                "task": task, "query": query, "started_at": time.time(),
            }

    def worker_clear(self, worker_id: str) -> None:
        with self._lock:
            self.workers.pop(worker_id, None)

    def set_api_keys(self, status: list) -> None:
        """Replace the per-key usage status (called by the producer each cell)."""
        with self._lock:
            self.api_keys = list(status or [])

    def set_discovery_rate(self, rps: float, active_keys: int) -> None:
        """Record the current dynamic discovery rate + active-key count."""
        with self._lock:
            self.discovery_rps = round(float(rps), 1)
            self.active_keys = int(active_keys)

    def set_phase1_totals(
        self,
        *,
        cells_total: int,
        grid_cells_total: int,
        queries_total: int,
        cells_done: int = 0,
    ) -> None:
        with self._lock:
            self.cells_total = max(0, int(cells_total or 0))
            self.grid_cells_total = max(0, int(grid_cells_total or 0))
            self.queries_total = max(0, int(queries_total or 0))
            self.cells_done = max(self.cells_done, int(cells_done or 0))

    def record_cell_started(self) -> None:
        with self._lock:
            self.cells_in_flight += 1

    def record_cell_result(self, status: str) -> None:
        status = (status or "").lower()
        with self._lock:
            self.cells_in_flight = max(0, self.cells_in_flight - 1)
            if status == "failed":
                self.cells_failed += 1
            else:
                self.cells_done += 1
                if status == "partial":
                    self.cells_partial += 1

    def snapshot(self) -> dict:
        elapsed = time.monotonic() - self.started_at
        with self._lock:
            workers_list = [dict(w) for w in self.workers.values()]
            api_keys_list = [dict(k) for k in self.api_keys]
            discovered = self.discovered
            scraped = self.scraped
            errors = self.errors
            queue_depth = self.queue_depth
            api_calls = self.api_calls
            cost_usd = self.cost_usd
            discovery_rps = self.discovery_rps
            active_keys = self.active_keys
            cells_done = self.cells_done
            cells_failed = self.cells_failed
            cells_partial = self.cells_partial
            cells_in_flight = self.cells_in_flight
            cells_total = self.cells_total
            grid_cells_total = self.grid_cells_total
            queries_total = self.queries_total
        rps = scraped / elapsed if elapsed > 0 else 0.0
        return {
            "discovered": discovered,
            "scraped": scraped,
            "errors": errors,
            "queue_depth": queue_depth,
            "elapsed_s": round(elapsed, 1),
            "rps_inst": round(rps, 3),
            "api_calls": api_calls,
            "cost_usd": round(cost_usd, 4),
            "workers": workers_list,
            "api_keys": api_keys_list,
            "discovery_rps": discovery_rps,
            "active_keys": active_keys,
            "cells_done": cells_done,
            "cells_failed": cells_failed,
            "cells_partial": cells_partial,
            "cells_in_flight": cells_in_flight,
            "cells_total": cells_total,
            "grid_cells_total": grid_cells_total,
            "queries_total": queries_total,
        }


class Heartbeat(threading.Thread):
    def __init__(self, stats: Stats, in_queue,
                 heartbeat_path: Path, interval_s: int = 30,
                 use_rich: bool = True, state=None) -> None:
        super().__init__(name="heartbeat", daemon=True)
        self._stats = stats
        self._queue = in_queue
        self._path = Path(heartbeat_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # Full current snapshot (incl. the heavy workers/api_keys arrays),
        # overwritten each beat — keeps heartbeat.jsonl itself lean.
        self._live_path = self._path.parent / "live_state.json"
        self._interval = interval_s
        self._stop = threading.Event()
        self._use_rich = use_rich and _HAS_RICH
        self._state = state
        self._console: Optional["Console"] = Console() if self._use_rich else None
        self._live: Optional["Live"] = None

    def stop(self) -> None:
        self._stop.set()

    def emit(self) -> dict:
        """Write one heartbeat snapshot immediately."""
        return self._emit_heartbeat()

    def run(self) -> None:
        if self._use_rich:
            self._run_rich()
        else:
            self._run_plain()

    # Per-worker / per-key arrays are large and change every beat — they go
    # to live_state.json (overwritten), never into the append-only trail.
    _HEAVY_KEYS = ("workers", "api_keys")
    _HEARTBEAT_MAX_BYTES = 25 * 1024 * 1024
    _HEARTBEAT_BACKUPS = 2

    def _emit_heartbeat(self) -> dict:
        self._stats.queue_depth = self._queue.qsize() if hasattr(self._queue, "qsize") else 0
        snap = self._stats.snapshot()
        if self._state is not None:
            try:
                place_counts = self._state.placeids_status_counts()
                snap.update(place_counts)
                snap["discovered"] = max(snap.get("discovered", 0), place_counts["placeids_total"])
                snap["scraped"] = max(snap.get("scraped", 0), place_counts["placeids_done"])
                snap["errors"] = max(snap.get("errors", 0), place_counts["placeids_failed"])
            except Exception:
                logger.debug("heartbeat.placeid_counts_failed", exc_info=True)
        # heartbeat.jsonl — append-only trail, kept lean (scalar counters only)
        # so a multi-hour run doesn't grow it to tens of MB.
        lean = {k: v for k, v in snap.items() if k not in self._HEAVY_KEYS}
        log_events = drain_log_events()
        if log_events:
            lean["log_events"] = log_events
        self._rotate_heartbeat_if_needed()
        with self._path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"event": "heartbeat", **lean}) + "\n")
        # live_state.json — full current snapshot, overwritten each beat (never grows)
        self._write_live_state(snap)
        return snap

    def _rotate_heartbeat_if_needed(self) -> None:
        try:
            if self._path.exists() and self._path.stat().st_size >= self._HEARTBEAT_MAX_BYTES:
                rotate_path_with_backoff(self._path, self._HEARTBEAT_BACKUPS)
        except Exception:
            logger.debug("heartbeat.rotate_failed", exc_info=True)

    def _write_live_state(self, snap: dict) -> None:
        try:
            tmp = self._live_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(snap), encoding="utf-8")
            tmp.replace(self._live_path)
        except Exception:
            logger.debug("heartbeat.live_state_write_failed", exc_info=True)

    def _make_table(self, snap: dict) -> "Table":
        t = Table(title="parallel-scraper", show_header=False, expand=False)
        t.add_column("k", style="cyan")
        t.add_column("v", style="white")
        for k in ("discovered", "scraped", "errors", "queue_depth", "rps_inst", "elapsed_s", "api_calls", "cost_usd"):
            if k in snap:
                t.add_row(k, str(snap[k]))
        return t

    def _run_rich(self) -> None:
        snap = self._emit_heartbeat()
        with Live(self._make_table(snap), console=self._console, refresh_per_second=2) as live:
            self._live = live
            while not self._stop.wait(timeout=self._interval):
                snap = self._emit_heartbeat()
                live.update(self._make_table(snap))

    def _run_plain(self) -> None:
        while not self._stop.wait(timeout=self._interval):
            snap = self._emit_heartbeat()
            logger.info("heartbeat %s", snap)
