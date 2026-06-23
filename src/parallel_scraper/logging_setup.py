"""JSON-lines structured logging with ContextVar-backed run/phase/cell/place fields."""
from __future__ import annotations

import contextvars
import json
import logging
import os
import sys
import threading
import time
from collections import deque
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

try:
    from pythonjsonlogger import jsonlogger
    _HAS_JSON_LOGGER = True
except ImportError:
    _HAS_JSON_LOGGER = False


run_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("run_id", default="")
phase_var: contextvars.ContextVar[str] = contextvars.ContextVar("phase", default="")
cell_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("cell_id", default="")
place_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("place_id", default="")

_MAX_LIVE_LOG_EVENTS = 500
_MAX_LIVE_LOG_BYTES = 256 * 1024
_MAX_EVENT_MSG_CHARS = 2000
_live_log_lock = threading.Lock()
_live_log_events: deque[dict] = deque()
_live_log_bytes = 0
_live_log_next_id = 0


def _json_size(obj: dict) -> int:
    try:
        return len(json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    except Exception:
        return 0


def _backoff_sleep(attempt: int) -> None:
    time.sleep(0.05 * (2 ** attempt))


def _retry_file_op(fn) -> None:
    last: BaseException | None = None
    for attempt in range(7):
        try:
            fn()
            return
        except FileNotFoundError:
            return
        except OSError as exc:
            last = exc
            if attempt >= 6:
                break
            _backoff_sleep(attempt)
    if last is not None:
        raise last


def rotate_path_with_backoff(path: str | Path, backup_count: int) -> None:
    """Rotate a plain file with the same retry rhythm used for OneDrive races."""
    p = Path(path)
    if backup_count <= 0 or not p.exists():
        return
    for idx in range(backup_count - 1, 0, -1):
        src = Path(f"{p}.{idx}")
        dst = Path(f"{p}.{idx + 1}")
        if not src.exists():
            continue
        if dst.exists():
            _retry_file_op(lambda dst=dst: os.remove(dst))
        _retry_file_op(lambda src=src, dst=dst: os.replace(src, dst))
    first = Path(f"{p}.1")
    if first.exists():
        _retry_file_op(lambda first=first: os.remove(first))
    _retry_file_op(lambda: os.replace(p, first))


class RetryingRotatingFileHandler(RotatingFileHandler):
    """RotatingFileHandler with short retries around Windows/OneDrive locks."""

    def doRollover(self) -> None:
        if self.stream:
            self.stream.close()
            self.stream = None
        if self.backupCount > 0:
            rotate_path_with_backoff(self.baseFilename, self.backupCount)
        if not self.delay:
            self.stream = self._open()


class LiveEventHandler(logging.Handler):
    """Capture compact INFO+ records for heartbeat delta streaming."""

    def emit(self, record: logging.LogRecord) -> None:
        global _live_log_bytes, _live_log_next_id
        try:
            msg = record.getMessage()
            if len(msg) > _MAX_EVENT_MSG_CHARS:
                msg = msg[:_MAX_EVENT_MSG_CHARS] + "..."
            event = {
                "id": 0,
                "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
                "level": record.levelname,
                "name": record.name,
                "msg": msg,
                "phase": getattr(record, "phase", "") or "",
                "cell_id": getattr(record, "cell_id", "") or "",
                "place_id": getattr(record, "place_id", "") or "",
            }
            with _live_log_lock:
                _live_log_next_id += 1
                event["id"] = _live_log_next_id
                size = _json_size(event)
                _live_log_events.append(event)
                _live_log_bytes += size
                while (
                    len(_live_log_events) > _MAX_LIVE_LOG_EVENTS
                    or _live_log_bytes > _MAX_LIVE_LOG_BYTES
                ) and _live_log_events:
                    old = _live_log_events.popleft()
                    _live_log_bytes = max(0, _live_log_bytes - _json_size(old))
        except Exception:
            self.handleError(record)


def drain_log_events() -> list[dict]:
    """Return and clear log events captured since the previous heartbeat."""
    global _live_log_bytes
    with _live_log_lock:
        events = list(_live_log_events)
        _live_log_events.clear()
        _live_log_bytes = 0
    return events


class ContextFilter(logging.Filter):
    """Inject run_id / phase / cell_id / place_id from ContextVars onto every record."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.run_id = run_id_var.get()
        record.phase = phase_var.get()
        record.cell_id = cell_id_var.get()
        record.place_id = place_id_var.get()
        return True


def setup_logging(log_path: str | Path | None, level: str = "INFO") -> None:
    """Configure the root logger to emit JSON lines to log_path and human-readable to stderr."""
    root = logging.getLogger()
    debug_enabled = str(os.environ.get("PARALLEL_SCRAPER_DEBUG_LOG", "")).lower() in {"1", "true", "yes", "on"}
    root.setLevel("DEBUG" if debug_enabled else level)
    root.handlers.clear()

    ctx_filter = ContextFilter()

    def _json_formatter():
        if _HAS_JSON_LOGGER:
            return jsonlogger.JsonFormatter(
                "%(asctime)s %(levelname)s %(name)s %(message)s "
                "%(run_id)s %(phase)s %(cell_id)s %(place_id)s"
            )
        return logging.Formatter(
            "%(asctime)s [%(levelname)s] [%(run_id)s/%(phase)s/%(cell_id)s/%(place_id)s] %(name)s: %(message)s"
        )

    # Rotated INFO JSON log.
    if log_path:
        log_path = Path(log_path)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RetryingRotatingFileHandler(
            log_path,
            maxBytes=100 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        )
        file_handler.setLevel(logging.INFO)
        file_handler.setFormatter(_json_formatter())
        file_handler.addFilter(ctx_filter)
        root.addHandler(file_handler)

        errors_handler = logging.FileHandler(log_path.with_name("run.errors.log"), encoding="utf-8")
        errors_handler.setLevel(logging.ERROR)
        errors_handler.setFormatter(_json_formatter())
        errors_handler.addFilter(ctx_filter)
        root.addHandler(errors_handler)

        if debug_enabled:
            debug_handler = RetryingRotatingFileHandler(
                log_path.with_name("run.debug.log"),
                maxBytes=50 * 1024 * 1024,
                backupCount=1,
                encoding="utf-8",
            )
            debug_handler.setLevel(logging.DEBUG)
            debug_handler.setFormatter(_json_formatter())
            debug_handler.addFilter(ctx_filter)
            root.addHandler(debug_handler)

    live_handler = LiveEventHandler(level=logging.INFO)
    live_handler.addFilter(ctx_filter)
    root.addHandler(live_handler)

    # Stderr stays warning+ so web/runner.py can keep native crash traces
    # without duplicating every INFO line into subprocess.log.
    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setLevel(os.environ.get("PARALLEL_SCRAPER_STDERR_LEVEL", "WARNING").upper())
    stderr_handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    ))
    stderr_handler.addFilter(ctx_filter)
    root.addHandler(stderr_handler)
