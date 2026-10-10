"""Pure normalisers for Phase 2 place fields (design 2026-10-phase2-gallery-refresh §8.1, §8.5, §8.7).

Raw values stay untouched in the row; these produce the extra normalised keys.
"""
from __future__ import annotations

import calendar
import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

_SENTINELS = {"N/A", "", "None"}


def null_if_sentinel(v):
    """'N/A', '' and 'None' (after strip) -> None; anything else unchanged."""
    if isinstance(v, str) and v.strip() in _SENTINELS:
        return None
    return v


# ── phone (§8.7 R4-2) ──
# national number length per calling code; small on purpose, extend when a new country runs
NATIONAL_LEN = {"91": 10, "1": 10, "44": 10, "966": 9, "971": 9, "65": 8, "880": 10, "977": 10, "94": 9}


def normalise_phone(raw, country_code: str = "91") -> str | None:
    """'+91 98765 43210' / '098765 43210' -> '+919876543210'; anything else None."""
    raw = null_if_sentinel(raw)
    if not raw:
        return None
    digits = re.sub(r"\D", "", str(raw))
    n = NATIONAL_LEN.get(country_code)
    if not digits or not n:
        return None
    if digits.startswith("0"):
        digits = digits[1:]
    if len(digits) == len(country_code) + n and digits.startswith(country_code):
        return "+" + digits
    if len(digits) == n:
        return "+" + country_code + digits
    return None


def website_host(raw) -> str | None:
    """Host of a URL, lower case, without 'www.'."""
    raw = null_if_sentinel(raw)
    if not raw:
        return None
    raw = str(raw).strip()
    host = urlparse(raw if "//" in raw else "//" + raw).hostname
    if not host:
        return None
    return host[4:] if host.startswith("www.") else host


# ── price (§8.7 R4-5) ──
# longest token first, so 'US$' / 'S$' win over '$' (sorted below; table order does not matter)
_CURRENCIES = sorted([("₹", "INR"), ("INR", "INR"), ("Rs", "INR"), ("US$", "USD"), ("USD", "USD"), ("$", "USD"),
                      ("SAR", "SAR"), ("AED", "AED"), ("€", "EUR"), ("EUR", "EUR"), ("£", "GBP"), ("GBP", "GBP"),
                      ("S$", "SGD"), ("SGD", "SGD")], key=lambda t: -len(t[0]))
_NUM_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")


def parse_price(raw) -> tuple[str, float, float] | None:
    """'₹200–400' -> ('INR', 200.0, 400.0); '₹1,000+' -> ('INR', 1000.0, 1000.0); '$$' or no currency -> None."""
    raw = null_if_sentinel(raw)
    if not raw:
        return None
    s = str(raw)
    cur = next((code for tok, code in _CURRENCIES if tok in s), None)
    nums = [float(x.replace(",", "")) for x in _NUM_RE.findall(s)]
    if not cur or not 1 <= len(nums) <= 2:
        return None
    return cur, min(nums), max(nums)


def parse_rating(raw) -> float | None:
    """'4.5' / '4,5' -> 4.5; outside 1..5 or absent -> None."""
    raw = null_if_sentinel(raw)
    if raw is None:
        return None
    m = re.search(r"\d(?:[.,]\d)?", str(raw))
    if not m:
        return None
    v = float(m.group().replace(",", "."))
    return v if 1.0 <= v <= 5.0 else None


def parse_review_count(raw) -> int | None:
    """'1,234' / '(1,234)' -> 1234; absent -> None (never 0 for absent)."""
    raw = null_if_sentinel(raw)
    if raw is None:
        return None
    m = re.search(r"\d[\d,]*", str(raw))
    return int(m.group().replace(",", "")) if m else None


# ── address (India: 6-digit pincode, state from a fixed list) ──
_STATES = ["Andhra Pradesh", "Arunachal Pradesh", "Assam", "Bihar", "Chhattisgarh", "Goa", "Gujarat", "Haryana",
           "Himachal Pradesh", "Jharkhand", "Karnataka", "Kerala", "Madhya Pradesh", "Maharashtra", "Manipur",
           "Meghalaya", "Mizoram", "Nagaland", "Odisha", "Punjab", "Rajasthan", "Sikkim", "Tamil Nadu", "Telangana",
           "Tripura", "Uttar Pradesh", "Uttarakhand", "West Bengal", "Andaman and Nicobar Islands", "Chandigarh",
           "Dadra and Nagar Haveli and Daman and Diu", "Delhi", "Jammu and Kashmir", "Ladakh", "Lakshadweep",
           "Puducherry"]
_STATE_ALIASES = {"orissa": "Odisha", "pondicherry": "Puducherry", "uttaranchal": "Uttarakhand",
                  "new delhi": "Delhi", "nct of delhi": "Delhi"}
_STATE_RE = re.compile(r"\b(" + "|".join(re.escape(s) for s in
                                          sorted(_STATES + list(_STATE_ALIASES), key=len, reverse=True)) + r")\b", re.I)
_STATE_BY_LOWER = {s.lower(): s for s in _STATES} | _STATE_ALIASES


def parse_address(raw) -> tuple[str | None, str | None] | None:
    """-> (postcode, state); None when neither is found. Last match wins (state/pincode sit at the end)."""
    raw = null_if_sentinel(raw)
    if not raw:
        return None
    s = str(raw)
    pins = re.findall(r"\b\d{6}\b", s)
    states = _STATE_RE.findall(s)
    pin = pins[-1] if pins else None
    state = _STATE_BY_LOWER[states[-1].lower()] if states else None
    return (pin, state) if (pin or state) else None


# ── category (§8.7) ──
CATEGORY_MAP_PATH = Path(__file__).resolve().parent / "data" / "category_map.json"
_category_map: dict[str, str] | None = None


def load_category_map(path: Path = CATEGORY_MAP_PATH) -> dict[str, str]:
    global _category_map
    if _category_map is None:
        try:
            _category_map = {k.lower(): v for k, v in json.loads(path.read_text(encoding="utf-8")).items()}
        except (OSError, ValueError):
            _category_map = {}
    return _category_map


def category_class(raw, mapping: dict[str, str] | None = None) -> str | None:
    """Raw Google category -> class; exact (case-insensitive) key first, then the longest key contained in raw;
    unknown -> 'other'; absent -> None."""
    raw = null_if_sentinel(raw)
    if not raw:
        return None
    mapping = load_category_map() if mapping is None else {k.lower(): v for k, v in mapping.items()}
    s = str(raw).strip().lower()
    if s in mapping:
        return mapping[s]
    hits = [k for k in mapping if k in s]
    return mapping[max(hits, key=len)] if hits else "other"


# ── review date interval (§8.5) ──
_AGE_RE = re.compile(r"^(edited\s+)?(\d+|an?)\s+(minute|hour|day|week|month|year)s?\s+ago$", re.I)
_GRAN = {"minute": "hour", "hour": "hour", "day": "day", "week": "week", "month": "month", "year": "year"}


def _add_months(d: datetime, months: int) -> datetime:
    y, m = divmod(d.month - 1 + months, 12)
    y += d.year
    return d.replace(year=y, month=m + 1, day=min(d.day, calendar.monthrange(y, m + 1)[1]))


def _back(obs: datetime, unit: str, n: int) -> datetime:
    if unit == "month":
        return _add_months(obs, -n)
    if unit == "year":
        return _add_months(obs, -12 * n)
    return obs - timedelta(**{unit + "s": n})


def review_interval(relative_str, observed_at: datetime):
    """'3 months ago' at obs -> (obs - 4 months, obs - 3 months, 'month'). 'Edited ...' -> same interval,
    granularity 'edited:<gran>'. Unparseable -> (None, None, 'unknown')."""
    m = _AGE_RE.match((relative_str or "").strip())
    if not m or observed_at is None:
        return None, None, "unknown"
    n = 1 if m[2].lower() in ("a", "an") else int(m[2])
    unit = m[3].lower()
    gran = _GRAN[unit]
    return _back(observed_at, unit, n + 1), _back(observed_at, unit, n), ("edited:" + gran if m[1] else gran)
