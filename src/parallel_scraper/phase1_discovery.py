"""Playwright Maps-search scraper: zero-cost placeId discovery per grid cell."""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Optional
from urllib.parse import quote_plus

logger = logging.getLogger(__name__)

_PLACE_ID_1S_RE = re.compile(r"!1s(0x[a-fA-F0-9:]+)")

# Anchor-href layout: /maps/place/<name>/data=...!1s0x...
_HREF_PLACE_RE = re.compile(r"/maps/place/[^/]+/data=[^\"'\s]*?!1s(0x[a-fA-F0-9:]+)")
# Lat/lng segment in a result href: @<lat>,<lng>,<zoom>z
_LATLNG_RE = re.compile(r"@(-?\d+\.\d+),(-?\d+\.\d+)")


@dataclass
class DiscoveredPlace:
    place_id: str
    place_id_form: str  # 'ChIJ' | '0x'
    lat: Optional[float] = None
    lng: Optional[float] = None


async def discover_cell(
    page,
    query: str,
    lat: float,
    lon: float,
    cell_id: str,
    bbox: Optional[dict] = None,
    max_scroll: int = 8,
    scroll_settle_ms: int = 800,
) -> list[DiscoveredPlace]:
    """Open Maps search for `query` at (lat, lon), scroll the results panel,
    harvest placeIds. Returns deduped list of DiscoveredPlace.

    bbox: {'north', 'south', 'east', 'west'} — if set, drop results outside.
    """
    url = f"https://www.google.com/maps/search/{quote_plus(query)}/@{lat},{lon},15z"
    seen: dict[str, DiscoveredPlace] = {}

    # Network tap: capture ChIJ-form placeIds from JSON responses.
    chij_from_network: set[str] = set()

    def on_response(response):
        try:
            url_l = response.url
            if "/maps/" not in url_l:
                return
            ctype = (response.headers or {}).get("content-type", "")
            if "json" not in ctype.lower() and "javascript" not in ctype.lower():
                return
            # Don't await body for non-text resources or large payloads — best-effort.
        except Exception:
            return

    page.on("response", on_response)

    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=20000)
    except Exception as e:
        logger.warning("phase1.goto_failed cell=%s err=%s", cell_id, str(e)[:120])
        return []

    # Wait briefly for the results panel.
    try:
        await page.wait_for_selector('div[role="feed"], a[href*="/maps/place/"]', timeout=8000)
    except Exception:
        pass

    last_count = -1
    plateau = 0
    for i in range(max_scroll):
        # Harvest current anchors.
        try:
            hrefs = await page.evaluate(
                """() => Array.from(document.querySelectorAll('a[href*="/maps/place/"]'))
                          .map(a => a.getAttribute('href') || '')"""
            )
        except Exception:
            hrefs = []
        for h in hrefs:
            _absorb_href(h, seen, bbox)

        # Try to also grab embedded ChIJ ids from any JSON in inline scripts.
        try:
            chij_ids = await page.evaluate(
                """() => {
                    const re = /(ChIJ[A-Za-z0-9_-]{20,})/g;
                    const txt = document.documentElement.innerHTML;
                    const out = new Set();
                    let m;
                    while ((m = re.exec(txt)) !== null) out.add(m[1]);
                    return Array.from(out);
                }"""
            )
        except Exception:
            chij_ids = []
        for cid in chij_ids:
            chij_from_network.add(cid)
            if cid not in seen:
                seen[cid] = DiscoveredPlace(place_id=cid, place_id_form="ChIJ")

        if len(seen) == last_count:
            plateau += 1
            if plateau >= 2:
                break
        else:
            plateau = 0
            last_count = len(seen)

        # Scroll the results feed.
        try:
            await page.evaluate(
                """() => {
                    const feed = document.querySelector('div[role="feed"]');
                    if (feed) feed.scrollBy(0, 1200);
                    else window.scrollBy(0, 1200);
                }"""
            )
        except Exception:
            pass
        await asyncio.sleep(scroll_settle_ms / 1000)

    page.remove_listener("response", on_response)

    out = list(seen.values())
    logger.info("phase1.cell_done cell=%s query=%s found=%d (chij=%d)",
                cell_id, query, len(out), sum(1 for p in out if p.place_id_form == "ChIJ"))
    return out


def _absorb_href(href: str, seen: dict[str, DiscoveredPlace], bbox: Optional[dict]) -> None:
    if not href or "/maps/place/" not in href:
        return
    # Try ChIJ-form first (rare in hrefs but possible)
    m_chij = re.search(r"(ChIJ[A-Za-z0-9_-]{20,})", href)
    pid: Optional[str] = None
    form = "0x"
    if m_chij:
        pid = m_chij.group(1)
        form = "ChIJ"
    else:
        m = _HREF_PLACE_RE.search(href) or _PLACE_ID_1S_RE.search(href)
        if m:
            pid = m.group(1)
            form = "0x"

    if not pid or pid in seen:
        return

    lat = lng = None
    m_ll = _LATLNG_RE.search(href)
    if m_ll:
        try:
            lat = float(m_ll.group(1))
            lng = float(m_ll.group(2))
        except ValueError:
            pass

    if bbox and lat is not None and lng is not None:
        if not (bbox["south"] <= lat <= bbox["north"] and bbox["west"] <= lng <= bbox["east"]):
            return

    seen[pid] = DiscoveredPlace(place_id=pid, place_id_form=form, lat=lat, lng=lng)
