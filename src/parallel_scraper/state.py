"""CSV-backed state store with atomic writes and flush-batching."""
from __future__ import annotations

import csv
import io
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

from filelock import FileLock

logger = logging.getLogger(__name__)


CELLS_FIELDS = ("cell_id", "query", "status", "last_attempted_at", "place_count", "error_msg")
PLACEIDS_FIELDS = ("place_id", "status", "attempts", "last_error", "updated_at")
DEAD_LETTER_FIELDS = ("place_id", "url", "error_class", "error_msg", "attempted_at")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _atomic_write_csv(path: Path, fieldnames: Iterable[str], rows: Iterable[dict]) -> None:
    """Buffer + atomic-replace the state CSV.

    Windows quirk: ``os.replace`` fails with ``PermissionError: [WinError 5]``
    when ANY process holds the destination file open at that instant —
    OneDrive sync, an Excel preview, the dashboard's CSV reader, etc. When
    the project lives inside OneDrive (as here), this happens often enough
    that a single occurrence used to crash Phase 1's producer (which then
    cancelled all sibling workers via asyncio.gather and bogus-completed
    the run with only ~96s of work). We now retry with short backoff so a
    transient lock is invisible.
    """
    import time as _time
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(fieldnames), extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    with tmp.open("w", encoding="utf-8-sig", newline="") as f:
        f.write(buf.getvalue())
        f.flush()
        os.fsync(f.fileno())
    # Retry the rename. Backoff: 50ms, 100ms, 200ms, 400ms, 800ms, 1600ms.
    # Total worst case ~3.15s — plenty for OneDrive to release the handle.
    last_err: Optional[OSError] = None
    for attempt in range(7):
        try:
            os.replace(tmp, path)
            return
        except PermissionError as e:
            last_err = e
            _time.sleep(0.05 * (2 ** attempt))
        except OSError as e:
            # Other transient OS errors (sharing violations, etc) — same handling
            last_err = e
            _time.sleep(0.05 * (2 ** attempt))
    # Final attempt — if this still fails, propagate so the caller can decide.
    # Logging at WARNING (not ERROR) because by here we've already retried hard.
    try:
        os.replace(tmp, path)
    except Exception:
        # As a last resort, try a non-atomic write directly to the final path.
        # Worse than atomic (a reader could see a partial file) but better than
        # crashing Phase 1.
        try:
            with path.open("w", encoding="utf-8-sig", newline="") as f:
                f.write(buf.getvalue())
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass
            import logging
            logging.getLogger(__name__).warning(
                "state.atomic_write_fallback path=%s last_err=%r — wrote non-atomically",
                path, last_err,
            )
            return
        except Exception:
            raise last_err if last_err is not None else RuntimeError("unknown state write failure")


def _read_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


class StateBuffer:
    """In-memory state with periodic flush to CSV.

    Two virtual tables:
      - cells:     keyed by (cell_id, query)
      - placeids:  keyed by place_id

    Flushes every `flush_every` writes OR every `flush_seconds` (whichever
    first). Final flush on close().
    """

    def __init__(self, run_dir: str | Path,
                 flush_every: int = 2000,
                 flush_seconds: float = 3.0) -> None:
        # Step 4 of perf plan: write batching. The previous defaults
        # (flush_every=50, flush_seconds=5) caused _tick to force-flush every
        # 50 writes — at 250 cells/sec that's a flush every 200ms, ~5
        # atomic-rename calls per second hammering OneDrive sync. Workers
        # serialized on the file lock. New defaults: time-based flush every
        # 3s is the primary cadence; the count-based ceiling at 2000 is a
        # safety net for the rare burst that exceeds the 3s window. Resume
        # safety is unchanged — at worst we lose ~3s of state on a crash,
        # and the (cell, query) pairs that were "in_progress" get re-tried.
        self._run_dir = Path(run_dir)
        self._cells_path = self._run_dir / "state_cells.csv"
        self._placeids_path = self._run_dir / "state_placeids.csv"
        self._dead_letter_path = self._run_dir / "phase2_failures.csv"

        self._cells_lock = threading.Lock()
        self._placeids_lock = threading.Lock()
        self._dl_lock = threading.Lock()
        self._dl_filelock = FileLock(str(self._dead_letter_path) + ".lock")

        self._cells: dict[tuple[str, str], dict] = {}
        self._placeids: dict[str, dict] = {}

        # Load existing state if present (resume)
        for r in _read_csv(self._cells_path):
            key = (r.get("cell_id", ""), r.get("query", ""))
            self._cells[key] = dict(r)
        for r in _read_csv(self._placeids_path):
            self._placeids[r.get("place_id", "")] = dict(r)

        self._dirty_cells = False
        self._dirty_placeids = False
        self._writes_since_flush = 0
        self._flush_every = flush_every
        self._flush_seconds = flush_seconds
        self._last_flush = 0.0

        # Periodic flush timer
        self._stop_event = threading.Event()
        self._flusher = threading.Thread(target=self._flusher_loop, daemon=True, name="StateBuffer-flusher")
        self._flusher.start()

    # ─── cells ───────────────────────────────────────

    def cells_set(self, cell_id: str, query: str, status: str,
                  place_count: int | None = None, error_msg: str | None = None) -> None:
        key = (cell_id, query)
        with self._cells_lock:
            row = self._cells.get(key, {
                "cell_id": cell_id, "query": query, "status": "pending",
                "last_attempted_at": "", "place_count": "0", "error_msg": "",
            })
            row["status"] = status
            row["last_attempted_at"] = _utc_now()
            if place_count is not None:
                row["place_count"] = str(place_count)
            if error_msg is not None:
                row["error_msg"] = error_msg[:500]
            self._cells[key] = row
            self._dirty_cells = True
        self._tick()

    def get_done_cell_keys(self) -> set[tuple[str, str]]:
        with self._cells_lock:
            return {k for k, v in self._cells.items() if v.get("status") == "done"}

    # ─── placeids ─────────────────────────────────────

    def placeids_set(self, place_id: str, status: str,
                     attempts_inc: int = 0, last_error: str | None = None) -> None:
        with self._placeids_lock:
            row = self._placeids.get(place_id, {
                "place_id": place_id, "status": "queued",
                "attempts": "0", "last_error": "", "updated_at": "",
            })
            row["status"] = status
            if attempts_inc:
                try:
                    cur = int(row.get("attempts") or 0)
                except ValueError:
                    cur = 0
                row["attempts"] = str(cur + attempts_inc)
            if last_error is not None:
                row["last_error"] = last_error[:500]
            row["updated_at"] = _utc_now()
            self._placeids[place_id] = row
            self._dirty_placeids = True
        self._tick()

    def get_done_placeids(self) -> list[str]:
        with self._placeids_lock:
            return [pid for pid, r in self._placeids.items()
                    if r.get("status") == "done"]

    def is_placeid_done(self, place_id: str) -> bool:
        with self._placeids_lock:
            r = self._placeids.get(place_id)
            return bool(r) and r.get("status") == "done"

    def placeids_get_resumable(self, max_attempts: int) -> list[str]:
        with self._placeids_lock:
            return [
                pid for pid, r in self._placeids.items()
                if r.get("status") in ("queued", "scraping", "failed")
                and int(r.get("attempts") or 0) < max_attempts
            ]

    # ─── dead letter ──────────────────────────────────

    def placeids_status_counts(self) -> dict[str, int]:
        with self._placeids_lock:
            done = failed = pending = 0
            for row in self._placeids.values():
                status = row.get("status")
                if status == "done":
                    done += 1
                elif status == "failed":
                    failed += 1
                elif status in ("queued", "scraping"):
                    pending += 1
            return {
                "placeids_total": len(self._placeids),
                "placeids_done": done,
                "placeids_failed": failed,
                "placeids_pending": pending,
            }

    def dead_letter_append(self, place_id: str, url: str,
                           error_class: str, error_msg: str) -> None:
        row = {
            "place_id": place_id,
            "url": url,
            "error_class": error_class,
            "error_msg": (error_msg or "")[:500],
            "attempted_at": _utc_now(),
        }
        with self._dl_lock, self._dl_filelock:
            need_header = not self._dead_letter_path.exists() or self._dead_letter_path.stat().st_size == 0
            self._dead_letter_path.parent.mkdir(parents=True, exist_ok=True)
            with self._dead_letter_path.open("a", encoding="utf-8-sig", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=DEAD_LETTER_FIELDS, extrasaction="ignore")
                if need_header:
                    writer.writeheader()
                writer.writerow(row)

    # ─── flushing ─────────────────────────────────────

    def _tick(self) -> None:
        """Records a write. NO inline flush here — the background flusher is
        the SOLE writer to disk. Previously this could synchronously call
        flush() from inside a worker's call to cells_set()/placeids_set(),
        which then held the dict lock for 1-3 seconds while atomically
        rewriting state_placeids.csv (~hundreds of thousands of rows). All
        330 workers contending for that lock got serialized through the
        flusher. Web_app pattern: workers ONLY touch in-memory dicts;
        every byte of file I/O happens on the dedicated flusher thread.
        """
        self._writes_since_flush += 1

    def flush(self) -> None:
        """Snapshot-and-write: copy the dict references under the lock
        (microseconds), then release and do the slow atomic-replace OUTSIDE
        the lock. Workers can keep mutating the dicts during the write.

        Crash window: at most `flush_seconds` of un-written state. Resume
        loads from these CSVs so the lost work is exactly the (cell, query)
        completions that finished in the last few seconds — those cells get
        re-attempted on resume, which is correct behavior.
        """
        # Snapshot cells
        cells_snapshot = None
        with self._cells_lock:
            if self._dirty_cells:
                cells_snapshot = list(self._cells.values())
                self._dirty_cells = False
        if cells_snapshot is not None:
            try:
                _atomic_write_csv(self._cells_path, CELLS_FIELDS, cells_snapshot)
            except Exception:
                # Mark dirty again so the next tick retries the write.
                with self._cells_lock:
                    self._dirty_cells = True
                raise

        # Snapshot placeids
        placeids_snapshot = None
        with self._placeids_lock:
            if self._dirty_placeids:
                placeids_snapshot = list(self._placeids.values())
                self._dirty_placeids = False
        if placeids_snapshot is not None:
            try:
                _atomic_write_csv(self._placeids_path, PLACEIDS_FIELDS, placeids_snapshot)
            except Exception:
                with self._placeids_lock:
                    self._dirty_placeids = True
                raise

        self._writes_since_flush = 0

    def _flusher_loop(self) -> None:
        while not self._stop_event.wait(timeout=self._flush_seconds):
            try:
                self.flush()
            except Exception:
                logger.exception("StateBuffer flush failed")

    def close(self) -> None:
        self._stop_event.set()
        try:
            self._flusher.join(timeout=2.0)
        except Exception:
            pass
        self.flush()
