"""Standalone Google Places API Text Search discovery."""
from __future__ import annotations

import csv
import logging
import os
import random
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from parallel_scraper.phase1_discovery import DiscoveredPlace
from parallel_scraper.key_usage import DailyUsageLedger

logger = logging.getLogger(__name__)

TEXT_SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"

TIER_CONFIG = {
    "ESSENTIALS": {
        "text_search_mask": "places.id,places.name,nextPageToken",
    },
    "PRO": {
        "text_search_mask": "places.id,places.name,nextPageToken",
    },
    "ENTERPRISE": {
        "text_search_mask": "places.id,places.name,nextPageToken",
    },
}

_EARLY_STOP = {
    "MAX_PAGES": 6,
    "MAX_EMPTY": 2,
    "MIN_PAGES_BEFORE_LOW_YIELD": 3,
    "LOW_YIELD_THRESHOLD": 3,
    "SATURATION_RATIO": 0.15,
    "SATURATION_PAGES": 2,
}

_thread_local = threading.local()


def _get_session() -> requests.Session:
    if not hasattr(_thread_local, "session"):
        s = requests.Session()
        adapter = HTTPAdapter(
            pool_connections=10,
            pool_maxsize=10,
            max_retries=Retry(total=0),
        )
        s.mount("https://", adapter)
        s.mount("http://", adapter)
        _thread_local.session = s
    return _thread_local.session


@dataclass
class EnvKeyState:
    key_id: int
    api_key: str
    alias: str
    quota: Optional[int] = None   # per-key daily call cap; None = unlimited
    day_baseline: int = 0   # calls already logged today (from the ledger) before this run
    run_count: int = 0      # calls made during the current run
    cooldown_until: float = 0.0   # monotonic time the key is rate-limit-parked until
    recent_429: int = 0           # consecutive 429s (resets on a success)
    rate_limit_hits: int = 0      # total 429s this run (for display)
    next_available_at: float = 0.0  # next per-key paced request slot
    disabled: bool = False        # permanently removed from rotation this run
                                  # (auth error: expired / invalid / forbidden)

    @property
    def day_count(self) -> int:
        """Total calls for this key on the current billing day."""
        return self.day_baseline + self.run_count

    @property
    def exhausted(self) -> bool:
        """True when a capped key has reached its daily quota.
        Unlimited keys (quota is None) are never exhausted."""
        return self.quota is not None and self.day_count >= self.quota

    @property
    def cooling_down(self) -> bool:
        """True while the key is parked after repeated 429s."""
        return time.monotonic() < self.cooldown_until

    @property
    def cooldown_remaining_s(self) -> int:
        return max(0, round(self.cooldown_until - time.monotonic()))

    @property
    def available(self) -> bool:
        """True when the key may be handed out — not daily-exhausted, not in a
        429 cooldown, and not disabled by an auth error."""
        return not self.exhausted and not self.cooling_down and not self.disabled


class LocalAPIKeyManager:
    """Round-robin key manager with per-key daily-quota awareness, 429
    cooldown, and a discovery rate that scales to the active key count.

    Each key carries its own daily call cap (`EnvKeyState.quota`; None means
    unlimited) from the `daily_quota` column of api_keys.csv. acquire_key()
    skips any key that is daily-exhausted *or* in a 429 cooldown.

    A key that returns repeated 429s is parked for `cooldown_s` seconds, then
    rejoins rotation. Whenever the set of available keys changes, the attached
    rate limiter is re-paced to `available_keys x (per_key_qpm/60) x
    utilization` requests/sec — so the run always runs at the configured
    fraction of the *currently* usable capacity.

    `keys` entries may be (api_key, alias) — which inherit `daily_quota` as the
    default cap — or (api_key, alias, quota) where quota is an int cap or None.
    """

    def __init__(self, keys: list,
                 ledger: "DailyUsageLedger | None" = None,
                 daily_quota: int = 75000,
                 rate_limiter=None,
                 per_key_qpm: int = 600,
                 utilization: float = 0.85,
                 cooldown_s: int = 30,
                 t429_threshold: int = 2) -> None:
        # Cooldown was 90s — too punishing when bursts cascade and put all
        # 33 keys on hold simultaneously. With per-task jitter now in the
        # worker hot path, isolated 429s should be rare; if one DOES land,
        # the key parks for 30s, plenty for Google's bucket to refill, and
        # the fleet recovers in under a minute instead of being dark for
        # 1.5 minutes.
        self._cv = threading.Condition(threading.Lock())
        default_quota = int(daily_quota) if daily_quota and int(daily_quota) > 0 else None
        self._keys: list[EnvKeyState] = []
        for i, entry in enumerate(keys):
            key = str(entry[0]).strip()
            if not key:
                continue
            alias = (entry[1] if len(entry) > 1 else "") or f"key#{i + 1}"
            # 3rd element (if present) is the explicit per-key quota; a plain
            # (key, alias) pair inherits the manager's default cap.
            quota = entry[2] if len(entry) > 2 else default_quota
            if quota is not None:
                quota = int(quota)
                if quota <= 0:
                    quota = None  # 0 / negative = unlimited
            self._keys.append(EnvKeyState(i + 1, key, alias, quota=quota))
        self._idx = 0
        self._ledger = ledger
        self._last_flush_monotonic = 0.0
        self._flushed_run_count: dict[str, int] = {}

        # Dynamic rate scaling. The operator target starts at `utilization`
        # (85% by default), then backs off after repeated 429s and slowly ramps
        # up again after quiet periods.
        self._rate_limiter = rate_limiter
        self._per_key_qpm = max(1, int(per_key_qpm))
        self._base_utilization = min(1.0, max(0.05, float(utilization)))
        self._utilization = self._base_utilization
        self._min_utilization = min(self._utilization, 0.55)
        self._last_429_monotonic = 0.0
        self._last_ramp_monotonic = time.monotonic()
        self._ramp_after_s = 120.0
        self._ramp_every_s = 60.0
        self._ramp_step = 0.03
        self._cooldown_s = max(1, int(cooldown_s))
        self._t429_threshold = max(1, int(t429_threshold))
        self._target_rps = 0.0
        self._update_per_key_interval_locked()

        now = time.monotonic()
        for ks in self._keys:
            ks.next_available_at = now + random.uniform(0.0, self._per_key_interval_s)

        if ledger is not None:
            try:
                today = ledger.read_today()
            except Exception:
                today = {}
            with self._cv:
                for ks in self._keys:
                    ks.day_baseline = int(today.get(ks.alias, 0))
                exhausted = [ks.alias for ks in self._keys if ks.exhausted]
            if exhausted:
                logger.warning("places_api.keys already at daily quota: %s",
                                ", ".join(exhausted))

        # pace the limiter to the initial available-key count
        self.sync_rate()

    @property
    def key_count(self) -> int:
        return len(self._keys)

    @property
    def available_key_count(self) -> int:
        with self._cv:
            return sum(1 for ks in self._keys if ks.available)

    @property
    def live_key_count(self) -> int:
        """Keys that can still serve a request now or after a cooldown — i.e.
        not permanently disabled (auth error) and not daily-exhausted.
        Cooling-down (429) keys DO count, since they rejoin after the cooldown.
        Used as a circuit breaker: when this hits 0, no amount of waiting helps
        and Phase 1 should abort rather than churn every cell into 'failed'."""
        with self._cv:
            return sum(1 for ks in self._keys if not ks.disabled and not ks.exhausted)

    @property
    def target_rps(self) -> float:
        return self._target_rps

    @property
    def utilization(self) -> float:
        return self._utilization

    def _update_per_key_interval_locked(self) -> None:
        per_key_rps = (self._per_key_qpm / 60.0) * max(0.01, self._utilization)
        self._per_key_interval_s = 1.0 / per_key_rps

    def _maybe_ramp_locked(self, now: float) -> bool:
        if self._utilization >= self._base_utilization:
            return False
        if (now - self._last_429_monotonic) < self._ramp_after_s:
            return False
        if (now - self._last_ramp_monotonic) < self._ramp_every_s:
            return False
        old = self._utilization
        self._utilization = min(self._base_utilization, self._utilization + self._ramp_step)
        self._last_ramp_monotonic = now
        self._update_per_key_interval_locked()
        logger.info("places_api.utilization_ramp %.2f -> %.2f", old, self._utilization)
        return True

    def _backoff_utilization_locked(self, now: float) -> bool:
        self._last_429_monotonic = now
        self._last_ramp_monotonic = now
        old = self._utilization
        self._utilization = max(self._min_utilization, self._utilization * 0.85)
        if abs(self._utilization - old) <= 0.001:
            return False
        self._update_per_key_interval_locked()
        logger.warning("places_api.utilization_backoff %.2f -> %.2f after repeated 429s",
                       old, self._utilization)
        return True

    def sync_rate(self) -> float:
        """Recompute the discovery rate from the currently-available key count
        and push it to the attached rate limiter. Safe to call often — it also
        catches keys whose 429 cooldown has just expired. Returns target rps."""
        with self._cv:
            now = time.monotonic()
            ramped = self._maybe_ramp_locked(now)
            available = sum(1 for ks in self._keys if ks.available)
            target = available * (self._per_key_qpm / 60.0) * self._utilization
            changed = abs(target - self._target_rps) > 0.01
            self._target_rps = target
            if changed or ramped:
                self._cv.notify_all()
        if target > 0 and changed and self._rate_limiter is not None:
            try:
                self._rate_limiter.set_rate(target, capacity=max(8.0, target))
            except Exception:
                logger.debug("places_api.set_rate_failed", exc_info=True)
        return target

    def _reserve_slot_locked(self, ks: EnvKeyState, now: float) -> None:
        base = max(now, ks.next_available_at)
        ks.next_available_at = base + self._per_key_interval_s

    def _next_wait_s_locked(self, now: float) -> Optional[float]:
        waits: list[float] = []
        for ks in self._keys:
            if ks.exhausted or ks.disabled:
                continue
            waits.append(max(ks.cooldown_until, ks.next_available_at) - now)
        if not waits:
            return None
        return max(0.01, min(max(0.0, min(waits)), 1.0))

    def acquire_key(self, block: bool = True, timeout: Optional[float] = None):
        """Return (key_id, api_key) after reserving that key's request slot."""
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._cv:
            while True:
                now = time.monotonic()
                n = len(self._keys)
                if n == 0:
                    return None
                for offset in range(n):
                    idx = (self._idx + offset) % n
                    ks = self._keys[idx]
                    if not ks.available or ks.next_available_at > now:
                        continue
                    self._idx = (idx + 1) % n
                    self._reserve_slot_locked(ks, now)
                    ks.run_count += 1
                    return ks.key_id, ks.api_key

                wait_s = self._next_wait_s_locked(now)
                if wait_s is None or not block:
                    return None
                if deadline is not None:
                    remaining = deadline - now
                    if remaining <= 0:
                        return None
                    wait_s = min(wait_s, remaining)
                self._cv.wait(timeout=wait_s)

    def wait_for_key_slot(self, key_id: int, timeout: Optional[float] = None) -> bool:
        """Reserve the next paced slot for an already-selected key."""
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._cv:
            while True:
                now = time.monotonic()
                ks = next((k for k in self._keys if k.key_id == key_id), None)
                if ks is None or ks.exhausted:
                    return False
                if not ks.cooling_down and ks.next_available_at <= now:
                    self._reserve_slot_locked(ks, now)
                    return True
                wait_s = max(0.01, min(max(ks.cooldown_until, ks.next_available_at) - now, 1.0))
                if deadline is not None:
                    remaining = deadline - now
                    if remaining <= 0:
                        return False
                    wait_s = min(wait_s, remaining)
                self._cv.wait(timeout=wait_s)

    def record_extra_usage(self, key_id: int, extra_calls: int):
        """Adjust a key's run count by extra_calls (pagination pages, or a
        negative value to undo a pre-count when the call never reached Google)."""
        with self._cv:
            for ks in self._keys:
                if ks.key_id == key_id:
                    ks.run_count = max(0, ks.run_count + extra_calls)
                    self._cv.notify_all()
                    return

    def report_rate_limit(self, key_id: int) -> None:
        """Record a 429 for a key. After `t429_threshold` consecutive 429s the
        key is parked for the cooldown window and the rate is rebalanced."""
        parked = None
        with self._cv:
            now = time.monotonic()
            for ks in self._keys:
                if ks.key_id == key_id:
                    ks.recent_429 += 1
                    ks.rate_limit_hits += 1
                    if ks.recent_429 >= self._t429_threshold:
                        ks.cooldown_until = now + self._cooldown_s
                        ks.recent_429 = 0
                        parked = ks.alias
                        self._backoff_utilization_locked(now)
                    break
            self._cv.notify_all()
        if parked:
            logger.warning("places_api.key_cooldown key=%s parked %ds after repeated 429s",
                            parked, self._cooldown_s)
            self.sync_rate()

    def report_success(self, key_id: int) -> None:
        """Clear a key's consecutive-429 counter after a successful call."""
        with self._cv:
            for ks in self._keys:
                if ks.key_id == key_id:
                    ks.recent_429 = 0
                    self._cv.notify_all()
                    return

    def report_auth_failure(self, key_id: int) -> None:
        """Permanently remove a key from rotation for the rest of the run after
        an auth error (expired / invalid / forbidden). Unlike a 429 cooldown
        these don't recover mid-run, so the key is disabled outright and the
        discovery rate is rebalanced over the remaining active keys."""
        disabled_alias = None
        with self._cv:
            for ks in self._keys:
                if ks.key_id == key_id and not ks.disabled:
                    ks.disabled = True
                    disabled_alias = ks.alias
                    break
            self._cv.notify_all()
        if disabled_alias:
            active = sum(1 for k in self._keys if not k.disabled)
            logger.warning(
                "places_api.key_disabled key=%s removed from rotation "
                "(auth error); %d key(s) still active", disabled_alias, active,
            )
            self.sync_rate()

    def report_usage(self, *args, **kwargs):
        return None

    def get_key_status(self) -> list[dict]:
        """Per-key usage snapshot for the dashboard / heartbeat."""
        with self._cv:
            now = time.monotonic()
            return [
                {
                    "key_id": ks.key_id,
                    "alias": ks.alias,
                    "run_count": ks.run_count,
                    "day_count": ks.day_count,
                    "quota": ks.quota or 0,   # 0 = unlimited (no cap)
                    "exhausted": ks.exhausted,
                    "disabled": ks.disabled,
                    "cooling_down": ks.cooling_down,
                    "cooldown_remaining_s": ks.cooldown_remaining_s,
                    "rate_limit_hits": ks.rate_limit_hits,
                    "available": ks.available,
                    "next_available_in_ms": max(0, round((ks.next_available_at - now) * 1000)),
                }
                for ks in self._keys
            ]

    def flush_to_ledger(self, force: bool = False, min_interval_s: float = 15.0) -> None:
        """Persist per-key call deltas to the shared daily ledger.

        Rate-limited: a no-op unless `force` is set or `min_interval_s` has
        elapsed since the last write — cheap to call after every cell.
        """
        if self._ledger is None:
            return
        now = time.monotonic()
        with self._cv:
            if not force and (now - self._last_flush_monotonic) < min_interval_s:
                return
            deltas: dict[str, int] = {}
            for ks in self._keys:
                prev = self._flushed_run_count.get(ks.alias, 0)
                d = ks.run_count - prev
                if d:
                    deltas[ks.alias] = d
                self._flushed_run_count[ks.alias] = ks.run_count
            self._last_flush_monotonic = now
        if deltas:
            try:
                self._ledger.add(deltas)
            except Exception:
                logger.warning("places_api.ledger_flush_failed")


def _parse_quota(raw) -> Optional[int]:
    """Parse a `daily_quota` cell. A positive integer is the per-day call cap;
    blank / 0 / 'unlimited' / 'none' / 'inf' / '-' all mean no cap (unlimited)."""
    s = str(raw if raw is not None else "").strip().lower()
    if s in ("", "0", "unlimited", "none", "inf", "infinite", "-", "na", "n/a"):
        return None
    try:
        v = int(float(s))
        return v if v > 0 else None
    except (TypeError, ValueError):
        return None


def build_key_manager(daily_quota: int = 75000,
                      usage_ledger_path: Optional[str] = None,
                      rate_limiter=None,
                      per_key_qpm: int = 600,
                      utilization: float = 0.85,
                      cooldown_s: int = 90,
                      t429_threshold: int = 2):
    """Load API keys from env or local CSV.

    Supported env vars:
      GOOGLE_MAPS_API_KEYS / GOOGLE_PLACES_API_KEYS: comma-separated keys
      GOOGLE_MAPS_API_KEY / GOOGLE_PLACES_API_KEY / PLACES_API_KEY / GOOGLE_API_KEY: single key

    Supported CSV (inputs/api_keys.csv):
      required: `api_key` (or `key`); optional: `alias`, `is_active`, `daily_quota`.
      `daily_quota` sets each key's per-day call cap — a positive integer to
      cap it, or blank/0/'unlimited' for no cap. When the column is absent
      entirely, every CSV key inherits the `daily_quota` argument as its cap.
    """

    keys: list[tuple] = []
    raw = (
        os.environ.get("GOOGLE_MAPS_API_KEYS")
        or os.environ.get("GOOGLE_PLACES_API_KEYS")
        or os.environ.get("GOOGLE_MAPS_API_KEY")
        or os.environ.get("GOOGLE_PLACES_API_KEY")
        or os.environ.get("PLACES_API_KEY")
        or os.environ.get("GOOGLE_API_KEY")
        or ""
    )
    keys.extend((key.strip(), f"env#{i + 1}") for i, key in enumerate(raw.split(",")) if key.strip())

    csv_path = Path(os.environ.get("PARALLEL_SCRAPER_API_KEYS_CSV", "inputs/api_keys.csv"))
    if csv_path.exists():
        with csv_path.open("r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            has_quota_col = "daily_quota" in (reader.fieldnames or [])
            for i, row in enumerate(reader):
                active = str(row.get("is_active", "1")).strip().lower()
                if active in {"0", "false", "no", "n"}:
                    continue
                key = (row.get("api_key") or row.get("key") or row.get("google_api_key") or "").strip()
                alias = (row.get("alias") or row.get("key_alias") or f"csv#{i + 1}").strip()
                if not key:
                    continue
                if has_quota_col:
                    # explicit per-key cap; blank cell = unlimited
                    keys.append((key, alias, _parse_quota(row.get("daily_quota"))))
                else:
                    # no column — inherit the default cap
                    keys.append((key, alias))

    ledger_path = usage_ledger_path or os.environ.get(
        "PARALLEL_SCRAPER_KEY_USAGE_PATH", "inputs/api_key_usage.json")
    ledger = DailyUsageLedger(ledger_path)
    km = LocalAPIKeyManager(keys, ledger=ledger, daily_quota=daily_quota,
                            rate_limiter=rate_limiter, per_key_qpm=per_key_qpm,
                            utilization=utilization, cooldown_s=cooldown_s,
                            t429_threshold=t429_threshold)
    if km.key_count:
        capped = sum(1 for ks in km._keys if ks.quota is not None)
        logger.info("places_api.keys source=standalone count=%d (%d capped, %d unlimited) ledger=%s",
                    km.key_count, capped, km.key_count - capped, ledger_path)
    return km


# Substrings (case-insensitive) that identify a *key-level* auth failure — a
# 4xx that renewing the key elsewhere can't fix mid-run, as opposed to a 429
# (transient) or a generic request error. Used to disable the dead key and fail
# over to another one instead of silently accepting a zero-coverage result.
_AUTH_ERROR_MARKERS = (
    "api key expired",
    "renew the api key",
    "api key not valid",
    "api_key_invalid",
    "permission_denied",
    "permission denied",
    "the request is missing a valid api key",
)


def _is_auth_error(status_code: int, body_text: str) -> bool:
    """True when a non-200 response is a key-specific auth failure."""
    if status_code not in (400, 401, 403):
        return False
    low = (body_text or "").lower()
    return any(m in low for m in _AUTH_ERROR_MARKERS)


def text_search_cell(
    api_key: str,
    query: str,
    lat: float,
    lon: float,
    radius_m: int,
    tier: str = "ESSENTIALS",
    language_code: str = "en",
    region_code: str = "IN",
    log_context: Optional[dict] = None,
    bbox: Optional[dict] = None,
    before_page_request: Optional[Callable[[int], bool]] = None,
) -> tuple[list[str], int, Optional[int], bool]:
    """Google Places API Text Search call matching the Flask grid scraper behavior.

    Returns ``(place_ids, ok_count, fail_code, auth_failed)`` where ``auth_failed``
    flags a key-specific auth error (expired / invalid / forbidden) so the caller
    can disable that key and fail over.
    """

    ctx = log_context or {}
    cell_id = ctx.get("cell_id", "")
    key_alias = ctx.get("key_alias", "")
    field_mask = TIER_CONFIG[tier]["text_search_mask"]
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": field_mask,
    }

    data = {
        "textQuery": query,
        "languageCode": language_code,
        "regionCode": region_code,
    }
    if bbox:
        data["locationRestriction"] = {
            "rectangle": {
                "low": {"latitude": bbox["south"], "longitude": bbox["west"]},
                "high": {"latitude": bbox["north"], "longitude": bbox["east"]},
            }
        }
    else:
        data["locationBias"] = {
            "circle": {
                "center": {"latitude": lat, "longitude": lon},
                "radius": float(radius_m),
            }
        }

    all_place_ids: list[str] = []
    seen_ids: set[str] = set()
    ok_count = 0
    page = 0
    session = _get_session()

    empty_streak = 0
    low_yield_streak = 0
    saturation_streak = 0
    stop_reason = None

    while True:
        page += 1
        try:
            if page > 1 and before_page_request is not None:
                if not before_page_request(page):
                    logger.error(
                        "[P1] page pacing unavailable cell=%s query=%s page=%s after_ok_pages=%s",
                        cell_id, (query[:30] if query else ""), page, ok_count,
                    )
                    return all_place_ids, ok_count, 0, False
            logger.debug(
                "[P1] request cell=%s query=%s radius_m=%s key=%s page=%s",
                cell_id, (query[:50] if query else ""), radius_m, key_alias or "?", page,
            )
            response = session.post(TEXT_SEARCH_URL, json=data, headers=headers, timeout=15)

            if response.status_code != 200:
                auth_failed = _is_auth_error(response.status_code, response.text)
                logger.error(
                    "[P1] API error cell=%s query=%s status=%s auth=%s body=%s after_ok_pages=%s",
                    cell_id, (query[:30] if query else ""), response.status_code,
                    auth_failed, response.text[:200], ok_count,
                )
                return all_place_ids, ok_count, response.status_code, auth_failed

            ok_count += 1
            result = response.json()
            places = result.get("places", [])
            next_token = result.get("nextPageToken")
            results_count = len(places)

            new_unique = 0
            for p in places:
                pid = p.get("id")
                if pid:
                    all_place_ids.append(pid)
                    if pid not in seen_ids:
                        seen_ids.add(pid)
                        new_unique += 1

            logger.debug(
                "[P1] response cell=%s results=%s new=%s has_next_page=%s page=%s",
                cell_id, results_count, new_unique, bool(next_token), page,
            )

            if not next_token:
                break
            if page >= _EARLY_STOP["MAX_PAGES"]:
                stop_reason = "max_pages"
                break

            empty_streak = empty_streak + 1 if results_count == 0 else 0
            if empty_streak >= _EARLY_STOP["MAX_EMPTY"]:
                stop_reason = "empty_streak"
                break

            low_yield_streak = low_yield_streak + 1 if new_unique < _EARLY_STOP["LOW_YIELD_THRESHOLD"] else 0
            if page >= _EARLY_STOP["MIN_PAGES_BEFORE_LOW_YIELD"] and low_yield_streak >= 2:
                stop_reason = "low_yield"
                break

            ratio = (new_unique / results_count) if results_count > 0 else 0.0
            saturation_streak = saturation_streak + 1 if ratio < _EARLY_STOP["SATURATION_RATIO"] else 0
            if saturation_streak >= _EARLY_STOP["SATURATION_PAGES"]:
                stop_reason = "saturation"
                break

            data["pageToken"] = next_token
            # Page tokens need a brief moment to become valid; too early risks
            # losing deeper pages. 0.5s is the validated value (HANDOFF §7 #9) —
            # fast enough not to bottleneck pagination while staying polite.
            time.sleep(0.5)

        except requests.RequestException as e:
            logger.error(
                "[P1] network error cell=%s query=%s err=%s after_ok_pages=%s",
                cell_id, (query[:30] if query else ""), str(e)[:200], ok_count,
            )
            return all_place_ids, ok_count, 0, False

    if stop_reason:
        logger.info(
            "[P1] early-stop cell=%s query=%s page=%s reason=%s unique=%s",
            cell_id, (query[:50] if query else ""), page, stop_reason, len(seen_ids),
        )

    return all_place_ids, ok_count, None, False


def discover_cell_places_api(
    key_manager,
    query: str,
    lat: float,
    lon: float,
    cell_id: str,
    radius_m: int,
    tier: str = "ESSENTIALS",
    language_code: str = "en",
    region_code: str = "IN",
    bbox: Optional[dict] = None,
    max_key_attempts: int = 4,
) -> tuple[list[DiscoveredPlace], int, Optional[int]]:
    """Run Places API Text Search and return canonical place IDs.

    On an HTTP 429 (rate limit) the call is reported to the key manager — which
    may park the key — and retried on a *different* key, up to
    `max_key_attempts` keys. The cell only comes back rate-limited (fail_code
    429) when every attempted key was throttled; otherwise the 429 is invisible
    to the caller and no coverage is lost.
    """

    place_ids: list[str] = []
    ok_count = 0
    fail_code: Optional[int] = None

    for attempt in range(1, max_key_attempts + 1):
        key_result = key_manager.acquire_key()
        if not key_result:
            raise RuntimeError("No Google Places API keys available; all keys are exhausted")
        key_id, api_key = key_result
        key_alias = f"Key#{key_id}"
        for ks in getattr(key_manager, "_keys", []):
            if getattr(ks, "key_id", None) == key_id:
                key_alias = str(getattr(ks, "alias", key_alias) or key_alias)
                break

        place_ids, ok_count, fail_code, auth_failed = text_search_cell(
            api_key=api_key,
            query=query,
            lat=lat,
            lon=lon,
            radius_m=radius_m,
            tier=tier,
            language_code=language_code,
            region_code=region_code,
            log_context={"cell_id": cell_id, "key_alias": key_alias},
            bbox=bbox,
            before_page_request=lambda _page, kid=key_id: key_manager.wait_for_key_slot(kid),
        )

        # reconcile this key's call count with the pages that actually succeeded
        if ok_count > 1:
            key_manager.record_extra_usage(key_id, ok_count - 1)
        elif ok_count == 0:
            key_manager.record_extra_usage(key_id, -1)

        if fail_code == 429:
            if hasattr(key_manager, "report_rate_limit"):
                key_manager.report_rate_limit(key_id)
            logger.warning(
                "[P1] 429 cell=%s query=%s key=%s attempt=%d/%d — retrying on another key",
                cell_id, (query[:30] if query else ""), key_alias,
                attempt, max_key_attempts,
            )
            continue  # failover to a different key
        if auth_failed:
            # Key-level auth failure (expired / invalid / forbidden). Renewing
            # can't happen mid-run, so disable the key for the rest of the run
            # and fail over to another — instead of accepting a zero-coverage
            # result that the producer would otherwise mark "done".
            if hasattr(key_manager, "report_auth_failure"):
                key_manager.report_auth_failure(key_id)
            logger.warning(
                "[P1] auth error %s cell=%s query=%s key=%s attempt=%d/%d — "
                "disabled key, retrying on another",
                fail_code, cell_id, (query[:30] if query else ""), key_alias,
                attempt, max_key_attempts,
            )
            continue  # failover to a different key
        # 200, or a non-429 partial result — accept it
        if hasattr(key_manager, "report_success"):
            key_manager.report_success(key_id)
        break

    discovered = [
        DiscoveredPlace(
            place_id=pid,
            place_id_form="ChIJ" if pid.startswith("ChIJ") else "places_api",
        )
        for pid in dict.fromkeys(place_ids)
    ]
    return discovered, ok_count, fail_code
