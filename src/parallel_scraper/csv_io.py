"""Schema-locked, append-only CSV writer with cross-process file locking."""
from __future__ import annotations

import csv
import gzip
import logging
import os
import threading
from pathlib import Path
from typing import Iterable

from filelock import FileLock

logger = logging.getLogger(__name__)


class LockedCsvWriter:
    """Append-only CSV writer.

    Filters input rows down to declared columns (drops extras silently),
    writes header once on first append, serializes via filelock so multiple
    processes/threads cannot corrupt each other.
    """

    def __init__(self, path: str | os.PathLike, columns: Iterable[str]) -> None:
        self._path = Path(path)
        self._columns = tuple(columns)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = FileLock(str(self._path) + ".lock")

    @property
    def columns(self) -> tuple[str, ...]:
        return self._columns

    @property
    def path(self) -> Path:
        return self._path

    def append(self, row: dict) -> None:
        filtered = {k: row.get(k, "") for k in self._columns}
        with self._lock:
            need_header = not self._path.exists() or self._path.stat().st_size == 0
            with self._path.open("a", newline="", encoding="utf-8-sig") as f:
                writer = csv.DictWriter(f, fieldnames=self._columns, extrasaction="ignore")
                if need_header:
                    writer.writeheader()
                writer.writerow(filtered)

    def append_many(self, rows: Iterable[dict]) -> None:
        """Batched-append: acquire the FileLock + open the file ONCE for the
        whole batch. Phase 1 calls this with all places discovered in a single
        cell (typically 5-30 rows). With 330 concurrent workers, per-row
        appends would serialize every place through one OS-level filelock —
        the actual measured impact was workers spending 80%+ of their time
        waiting for the lock. Batching cuts that to ~one acquire per cell.
        """
        # Materialize first so an empty iterable doesn't take the lock.
        filtered = [
            {k: r.get(k, "") for k in self._columns}
            for r in rows
        ]
        if not filtered:
            return
        with self._lock:
            need_header = not self._path.exists() or self._path.stat().st_size == 0
            with self._path.open("a", newline="", encoding="utf-8-sig") as f:
                writer = csv.DictWriter(f, fieldnames=self._columns, extrasaction="ignore")
                if need_header:
                    writer.writeheader()
                writer.writerows(filtered)

    def close(self) -> None:
        # No persistent file handle; method exists for symmetry.
        pass


class GzipCsvWriter:
    """Append-only gzip-compressed CSV writer for the full-payload sidecar."""

    def __init__(self, path: str | os.PathLike, columns: Iterable[str]) -> None:
        self._path = Path(path)
        self._columns = tuple(columns)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = FileLock(str(self._path) + ".lock")

    @property
    def path(self) -> Path:
        return self._path

    def append(self, row: dict) -> None:
        filtered = {k: row.get(k, "") for k in self._columns}
        with self._lock:
            need_header = not self._path.exists() or self._path.stat().st_size == 0
            with gzip.open(self._path, "at", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=self._columns, extrasaction="ignore")
                if need_header:
                    writer.writeheader()
                writer.writerow(filtered)


class BufferedCsvWriter:
    """In-memory + periodic-flush CSV writer (web_app pattern).

    Workers call append() / append_many() which take ONLY a fast in-process
    threading.Lock and a list.append — no filesystem I/O on the hot path.
    A daemon flusher thread wakes up every `flush_seconds` and writes all
    pending rows to disk in a single open/write/close round.

    Trade-off vs LockedCsvWriter:
      Gain: workers never block on FileLock + OS file IO. With 330 workers
            previously bottlenecked on per-row file locks, this reclaims
            ~80% of worker time. Confirmed pattern in web_app phase 1.
      Loss: on a hard crash, up to `flush_seconds` of audit rows may be
            unwritten. For phase1_discovered.csv this is acceptable —
            state_placeids.csv (which drives resume) is flushed separately
            by StateBuffer, so resume correctness is unaffected.

    Cross-process safety: still uses FileLock on each flush so a sibling
    process can't corrupt the file. There's just one flusher writing at a
    time per instance, so contention is rare.
    """

    def __init__(self, path: str | os.PathLike, columns: Iterable[str],
                 flush_seconds: float = 3.0,
                 max_buffer: int = 50_000) -> None:
        self._path = Path(path)
        self._columns = tuple(columns)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._filelock = FileLock(str(self._path) + ".lock")

        self._buf: list[dict] = []
        self._buf_lock = threading.Lock()
        self._flush_seconds = float(flush_seconds)
        # Backstop: if we somehow accumulate more than this many rows without
        # the periodic flusher catching us (e.g. flusher thread died), force
        # an inline flush from the calling thread.
        self._max_buffer = int(max_buffer)

        self._stop = threading.Event()
        self._flusher = threading.Thread(
            target=self._flusher_loop,
            name=f"BufferedCsvWriter-flusher[{self._path.name}]",
            daemon=True,
        )
        self._flusher.start()

    @property
    def columns(self) -> tuple[str, ...]:
        return self._columns

    @property
    def path(self) -> Path:
        return self._path

    def append(self, row: dict) -> None:
        filtered = {k: row.get(k, "") for k in self._columns}
        overflow = False
        with self._buf_lock:
            self._buf.append(filtered)
            if len(self._buf) >= self._max_buffer:
                overflow = True
        if overflow:
            self.flush()

    def append_many(self, rows: Iterable[dict]) -> None:
        filtered = [
            {k: r.get(k, "") for k in self._columns}
            for r in rows
        ]
        if not filtered:
            return
        overflow = False
        with self._buf_lock:
            self._buf.extend(filtered)
            if len(self._buf) >= self._max_buffer:
                overflow = True
        if overflow:
            self.flush()

    def flush(self) -> None:
        """Drain the in-memory buffer to disk. Safe to call concurrently
        — the swap is done under the buffer lock; the actual file I/O
        happens outside it so other threads can keep appending."""
        with self._buf_lock:
            if not self._buf:
                return
            pending = self._buf
            self._buf = []
        try:
            with self._filelock:
                need_header = (not self._path.exists()
                               or self._path.stat().st_size == 0)
                with self._path.open("a", newline="", encoding="utf-8-sig") as f:
                    writer = csv.DictWriter(
                        f, fieldnames=self._columns, extrasaction="ignore"
                    )
                    if need_header:
                        writer.writeheader()
                    writer.writerows(pending)
        except Exception:
            # If the flush failed, push the rows back so the next attempt
            # retries them rather than losing data.
            logger.exception("BufferedCsvWriter flush failed path=%s", self._path)
            with self._buf_lock:
                pending.extend(self._buf)
                self._buf = pending

    def _flusher_loop(self) -> None:
        while not self._stop.wait(timeout=self._flush_seconds):
            try:
                self.flush()
            except Exception:
                logger.exception("BufferedCsvWriter periodic flush crashed")

    def close(self) -> None:
        """Stop the background flusher and drain anything still buffered."""
        self._stop.set()
        try:
            self._flusher.join(timeout=2.0)
        except Exception:
            pass
        self.flush()


def atomic_write_text(path: str | os.PathLike, text: str, encoding: str = "utf-8") -> None:
    """Write text to a file atomically via temp file + os.replace."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    with tmp.open("w", encoding=encoding, newline="") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, p)
