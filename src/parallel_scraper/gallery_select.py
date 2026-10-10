"""Gallery photo selection for the Phase 2 refresh (pure; no I/O but the injected
thumb fetcher). Design: docs/design/2026-10-phase2-gallery-refresh.md §2, §3, §4.3.

Input item (one per walked viewer item):
    {kind, url, caption, source: owner|gallery|panel|street_view, ordinal, chip}
Flow: keep kind=photo only -> exact URL-base merge -> thumb dHash -> newest-first
greedy near-duplicate merge -> bands (a)-(e) -> keep cap.
"""
from __future__ import annotations

import io
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Callable

MAX_AGE_DAYS = 1095          # "3 years old or newer"
MAX_UNIQUE_THUMBS = 120      # §3: items after 120 unique thumbs get dhash=None
STALE_MAX = 2                # band (e) takes at most 2 ...
STALE_MIN_KEPT = 3           # ... and only when fewer than 3 are kept after (a)-(d)
OWNER_OLD_MAX = 20           # band (b) only when owner_count <= 20
CATEGORY_FIRST = {"storefront", "interior"}

# Same rule as cloud/index_media.parse_month, copied: src/ must not import cloud/.
_MONTHS = {m: i for i, m in enumerate(
    "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split(), 1)}
_MONTH_RE = re.compile(r"([A-Z][a-z]{2})[a-z]*\.?\s+(\d{4})")


@dataclass
class Selected:
    url: str
    sources: set = field(default_factory=set)
    photo_month: str | None = None       # 'YYYY-MM'
    photo_date_raw: str | None = None    # caption as walked
    dhash: int | None = None             # unsigned 64-bit; see signed64()
    stale: int = 0                       # 1 = band (e)
    ordinal: int = 0                     # first-seen walk ordinal
    source: str = "gallery"              # primary: owner when any member is owner
    band: str = ""                       # a | b | c | d | e


def parse_photo_month(caption: str | None) -> str | None:
    """'Photo - Jun 2026' -> '2026-06'; None when no month-year is present."""
    if not caption:
        return None
    m = _MONTH_RE.search(caption)
    if not m or m.group(1) not in _MONTHS:
        return None
    return f"{m.group(2)}-{_MONTHS[m.group(1)]:02d}"


def dhash64(img_bytes: bytes) -> int:
    """64-bit difference hash: grayscale, 9x8, compare horizontal neighbours."""
    from PIL import Image  # lazy: importing this module must not need Pillow
    img = Image.open(io.BytesIO(img_bytes)).convert("L").resize((9, 8), Image.LANCZOS)
    px = list(img.getdata())
    h = 0
    for row in range(8):
        for col in range(8):
            h = (h << 1) | (px[row * 9 + col] > px[row * 9 + col + 1])
    return h


def hamming(a: int, b: int) -> int:
    return (a ^ b).bit_count()


def signed64(h: int | None) -> int | None:
    """Unsigned dHash -> signed bigint for SQL (§3)."""
    return None if h is None else (h - (1 << 64) if h >= 1 << 63 else h)


def thumb_url(url: str) -> str:
    """The =w64-h64 thumb a fetch_thumb implementation should request (§3)."""
    return url.split("=")[0] + "=w64-h64"


def item_kind(item: dict) -> str:
    """photo | video | 360 | street_view | unknown. 360 also from the URL/caption (§6)."""
    kind = (item.get("kind") or "").lower()
    url = item.get("url") or ""
    caption = item.get("caption") or ""
    if kind == "street_view":
        return kind
    if kind in ("360", "pano") or "streetviewpixels" in url or "360" in caption:
        return "360"
    if kind in ("photo", "video"):
        return kind
    return "unknown"


def kind_counts(items) -> dict:
    """Counts of walked items that selection skips (videos, 360, unknown)."""
    out = {"video_count": 0, "pano_count": 0, "unknown_count": 0}
    for it in items:
        k = item_kind(it)
        if k == "video":
            out["video_count"] += 1
        elif k == "360":
            out["pano_count"] += 1
        elif k == "unknown":
            out["unknown_count"] += 1
    return out


def _month_start(month: str) -> date:
    y, m = month.split("-")
    return date(int(y), int(m), 1)


def _newest_first_key(g: dict, cat: int = 0):
    """Dated (newest month first) before undated; then category; then ordinal."""
    month = -int(g["month"].replace("-", "")) if g["month"] else 0
    return (g["month"] is None, month, cat, g["ordinal"])


def _hash_items(photos: list[dict], fetch_thumb, pool_workers: int) -> None:
    """Set g['dhash'] in place. One fetch per URL base; failures -> None."""
    bases: list[str] = []
    for g in photos:
        if g["base"] not in bases:
            bases.append(g["base"])
    bases = bases[:MAX_UNIQUE_THUMBS]
    url_by_base = {g["base"]: g["url"] for g in reversed(photos)}

    def one(base):
        try:
            body = fetch_thumb(url_by_base[base])
            return dhash64(body) if body else None
        except Exception:
            return None

    hashes: dict[str, int | None] = {}
    if fetch_thumb is not None and bases:
        with ThreadPoolExecutor(max_workers=max(1, min(pool_workers, len(bases)))) as ex:
            hashes = dict(zip(bases, ex.map(one, bases)))
    for g in photos:
        g["dhash"] = hashes.get(g["base"])


def _merge(into: dict, g: dict) -> None:
    """§2 step 4: union of sources, newest date, first-seen ordinal."""
    into["sources"] |= g["sources"]
    if g["month"] and (into["month"] is None or g["month"] > into["month"]):
        into["month"], into["caption"] = g["month"], g["caption"]
    into["ordinal"] = min(into["ordinal"], g["ordinal"])


def select_photos(items, *, now, cap: int = 20, thresh: int = 8,
                  owner_count_known: bool, owner_count: int | None,
                  fetch_thumb: Callable[[str], bytes | None] | None,
                  pool_workers: int = 6) -> list[Selected]:
    """Choose up to `cap` photos for one place (§2 bands a-e, §3 dedupe).

    Only kind=photo items are candidates; count the skipped ones with
    kind_counts(). `fetch_thumb(url)` gets the item URL (use thumb_url() to
    size it) and returns image bytes or None; None disables near-dup merging.
    """
    today = now.date() if isinstance(now, datetime) else now

    # 1. photos only, exact URL-base merge (an owner item seen again in All)
    photos: list[dict] = []
    by_base: dict[str, dict] = {}
    for it in sorted(items, key=lambda i: i.get("ordinal", 0)):
        if item_kind(it) != "photo" or not it.get("url"):
            continue
        g = {"url": it["url"], "base": it["url"].split("=")[0],
             "sources": set(it.get("sources") or [it.get("source") or "gallery"]),
             "month": parse_photo_month(it.get("caption")), "caption": it.get("caption"),
             "ordinal": int(it.get("ordinal", 0)), "chip": (it.get("chip") or "").lower(),
             "dhash": None}
        if g["base"] in by_base:
            _merge(by_base[g["base"]], g)
            continue
        by_base[g["base"]] = g
        photos.append(g)

    # 2. dHash, then newest-first greedy near-duplicate merge
    _hash_items(photos, fetch_thumb, pool_workers)
    kept: list[dict] = []
    for g in sorted(photos, key=_newest_first_key):
        twin = None
        if g["dhash"] is not None:
            twin = next((k for k in kept if k["dhash"] is not None
                         and hamming(k["dhash"], g["dhash"]) <= thresh), None)
        if twin is None:
            kept.append(g)
        else:
            _merge(twin, g)

    # 3. bands
    owner_old_ok = bool(owner_count_known) and owner_count is not None and owner_count <= OWNER_OLD_MAX
    bands: dict[str, list[dict]] = {b: [] for b in "abcde"}
    for g in kept:
        owner = "owner" in g["sources"]
        if g["month"] is None:
            bands["d"].append(g)
        elif (today - _month_start(g["month"])).days <= MAX_AGE_DAYS:
            bands["a" if owner else "c"].append(g)
        elif owner:
            if owner_old_ok:
                bands["b"].append(g)
        else:
            bands["e"].append(g)

    def band_key(g):  # category tie-break only reorders inside a band
        return _newest_first_key(g, 0 if g["chip"] in CATEGORY_FIRST else 1)

    out: list[Selected] = []
    for b in "abcd":
        for g in sorted(bands[b], key=band_key):
            if len(out) < cap:
                out.append(_to_selected(g, b))
    if len(out) < STALE_MIN_KEPT:
        for g in sorted(bands["e"], key=band_key)[:STALE_MAX]:
            if len(out) < cap:
                out.append(_to_selected(g, "e"))
    return out


def _to_selected(g: dict, band: str) -> Selected:
    return Selected(url=g["url"], sources=set(g["sources"]), photo_month=g["month"],
                    photo_date_raw=g["caption"], dhash=g["dhash"],
                    stale=1 if band == "e" else 0, ordinal=g["ordinal"],
                    source="owner" if "owner" in g["sources"] else sorted(g["sources"])[0],
                    band=band)


def summary(items, selected, walk_meta: dict, outcomes=None) -> dict:
    """`$.photo_dates.summary` (§4.3).

    walk_meta: walk_ok, reason, owner_count_known, limits_hit (list), chip_unavailable,
    optional latest_ordered. outcomes: mirror_selected() results for the selected
    items plus Street View; when given, photos_selected/photos_mirrored count them.
    """
    limits = list(walk_meta.get("limits_hit") or [])
    walk_ok = bool(walk_meta.get("walk_ok", True))
    out = {
        "photos_walked": sum(1 for it in items if item_kind(it) == "photo"),
        "photos_selected": len(outcomes) if outcomes is not None else len(selected),
        "photos_mirrored": sum(1 for o in (outcomes or [])
                               if o.get("status") in ("written", "exists")),
        "walk_ok": walk_ok,
        "owner_count_known": bool(walk_meta.get("owner_count_known", False)),
        "truncated": 1 if limits or walk_meta.get("truncated") else 0,
        "limits_hit": limits,
        "latest_ordered": walk_meta.get("latest_ordered", _latest_ordered(items)),
        "chip_unavailable": 1 if walk_meta.get("chip_unavailable") else 0,
    }
    if not walk_ok:
        out["reason"] = walk_meta.get("reason")
    return out


def _latest_ordered(items) -> int | None:
    """1 when the dated items of the Latest walk are non-increasing in walk order."""
    months = [parse_photo_month(it.get("caption"))
              for it in sorted(items, key=lambda i: i.get("ordinal", 0))
              if (it.get("chip") or "").lower() == "latest"]
    months = [m for m in months if m]
    if not months:
        return None
    return int(all(a >= b for a, b in zip(months, months[1:])))
