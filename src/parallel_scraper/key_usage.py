"""Persistent per-key daily API-call ledger.

Google Maps Platform request quotas reset at midnight US-Pacific, which is
07:00 UTC (PDT) / 08:00 UTC (PST). We use 07:00 UTC as the billing-day
boundary — the same constant the web_app's db_manager uses.

The ledger is a small JSON file keyed by billing day:

    {
      "2026-05-22": {"key-1": 4230, "Dehaat Routes API": 3980, ...},
      "2026-05-21": {...}
    }

Writes are an atomic read-modify-write of additive per-key *deltas*, so two
parallel-scraper runs sharing the same ledger converge correctly instead of
clobbering each other.
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

# Billing-day boundary — keep in sync with web_app/core/db_manager.DAILY_RESET_HOUR_UTC.
_DAILY_RESET_HOUR_UTC = 7
_KEEP_DAYS = 8  # prune ledger buckets older than this


def billing_day(now: datetime | None = None) -> str:
    """Return the current Google-quota billing day as an ISO date string."""
    now = now or datetime.now(timezone.utc)
    reset = now.replace(hour=_DAILY_RESET_HOUR_UTC, minute=0, second=0, microsecond=0)
    if now < reset:
        reset -= timedelta(days=1)
    return reset.strftime("%Y-%m-%d")


class DailyUsageLedger:
    """File-backed per-key daily call counter."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        return self._path

    def _read_all(self) -> dict:
        try:
            return json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except Exception:
            logger.warning("key_usage.ledger_read_failed path=%s", self._path)
            return {}

    def _write_all(self, data: dict) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(self._path)

    def read_today(self) -> dict[str, int]:
        """Return {alias: count} already logged for the current billing day."""
        with self._lock:
            return dict(self._read_all().get(billing_day(), {}))

    def add(self, deltas: dict[str, int]) -> dict[str, int]:
        """Atomically merge per-key call deltas into today's bucket.

        Returns the updated {alias: count} totals for the current billing day.
        """
        deltas = {k: int(v) for k, v in (deltas or {}).items() if v}
        with self._lock:
            data = self._read_all()
            day = billing_day()
            bucket = data.setdefault(day, {})
            if deltas:
                for alias, n in deltas.items():
                    bucket[alias] = int(bucket.get(alias, 0)) + n
                # prune stale buckets
                cutoff = (datetime.now(timezone.utc) - timedelta(days=_KEEP_DAYS)).strftime("%Y-%m-%d")
                data = {d: v for d, v in data.items() if d >= cutoff}
                self._write_all(data)
            return dict(bucket)
