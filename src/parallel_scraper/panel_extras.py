"""Overview-pane capture: price band, hours, popular times, outbound links,
share link and the structured Menu tab.

MUST run on a FRESH place panel. `PlaywrightSession.scrape()` expands the reviews
pane, which replaces Overview — every module below lives on Overview and is simply
absent afterwards. The caller navigates back to the place URL first; measured cost
of that re-goto is 0.3-0.5s. This is not an anti-bot gate: a logged-out headless
session with the stock UA sees everything once it is on the right pane.

Hard-won behaviours (do not "simplify" these away):
- Read the share link BEFORE touching any tab. The Share button lives on Overview;
  after a Menu-tab click it times out and returns nothing.
- Menu items sit behind a category chip. Clicking the Menu tab alone yields zero
  items — one chip has to be opened.
- Popular-times bars expose their value only in aria-label ("Tuesdays 6 AM 20%
  busy."), never in text.
- Google wraps outbound links as /url?q=<real>&fbclid=..&ved=..&usg=.. — store the
  unwrapped URL, the wrapper rots and carries tracking.
"""
from __future__ import annotations

import logging
import re
import urllib.parse

logger = logging.getLogger(__name__)

SETTLE_MS_DEFAULT = 5_000        # 2s proved flaky right after a gallery walk
MENU_SETTLE_MS = 3_500
CHIP_SETTLE_MS = 3_000
MAX_CHIP_TRIES = 3        # photo-only Menu tabs yield nothing; cap the hunt

_DOWS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
_TRACKING = {"fbclid", "opi", "sa", "ved", "usg", "gclid", "_aem",
             "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content"}
_LINK_KINDS = (
    ("instagram.com", "instagram"), ("facebook.com", "facebook"),
    ("tiktok.com", "tiktok"), ("snapchat.com", "snapchat"),
    ("twitter.com", "x"), ("x.com", "x"), ("t.me", "telegram"),
    ("youtube.com", "youtube"), ("linktr.ee", "linktree"),
    ("wa.me", "whatsapp"), ("api.whatsapp.com", "whatsapp"),
)
# 'Tuesdays 6 AM 20% busy.' / '0% busy at 6 AM.'
_BUSY_RE = re.compile(r"(\d{1,3})\s*%\s*busy", re.I)
_HOUR_RE = re.compile(r"(\d{1,2})\s*(AM|PM)", re.I)
_DOW_RE = re.compile("|".join(_DOWS), re.I)
# 'SAR 20–40 per person' / 'SAR 1-20' — en dash and hyphen both occur.
_PRICE_RE = re.compile(r"([A-Z]{3}|[^\W\d_]{1,4})\s*([\d,]+)\s*[–\-—]\s*([\d,]+)")
_PRICE_ONE = re.compile(r"([A-Z]{3})\s*([\d,]+)")
_REPORTED_RE = re.compile(r"Reported by\s+([\d,]+)", re.I)

_JS_PANEL = """() => {
  const root = document.querySelector('div[role="main"]') || document.body;
  const lines = (root.innerText || '').split('\\n').map(s => s.trim()).filter(Boolean);
  const aria = [...document.querySelectorAll('[aria-label]')]
    .map(e => e.getAttribute('aria-label') || '').filter(a => a && a.length < 160);
  const links = [...document.querySelectorAll('a[href]')]
    .map(a => a.href).filter(h => /^https?:/.test(h));
  return {
    lines, aria, links,
    tabs: [...document.querySelectorAll('button[role="tab"]')].map(b => b.innerText.trim()),
  };
}"""

_JS_SHARE = """() => {
  const i = document.querySelector('input[value*="goo.gl"], input[value*="google.com/maps"]');
  return i ? i.value : null;
}"""

_JS_CHIPS = """() => [...document.querySelectorAll('div[role="main"] button')]
    .map(b => (b.innerText || '').trim())"""

# Menu items are read from the DOM, not from flattened panel text: innerText loses
# the name/description boundary (a short description is indistinguishable from a
# category header sitting above a name). Here the price element's own container is
# walked, so the fields stay separated by structure.
_JS_MENU_ITEMS = """() => {
  const priceRe = /^[A-Z]{3}\\s*[\\d,]+(\\.\\d+)?$/;
  const root = document.querySelector('div[role="main"]') || document.body;
  const out = [];
  root.querySelectorAll('div,span,li').forEach(el => {
    if (el.children.length) return;
    const t = (el.innerText || '').replace(/\\u00a0/g, ' ').trim();
    if (!priceRe.test(t)) return;
    // climb to the smallest ancestor holding both the price and >=2 text lines
    let box = el, hops = 0;
    while (box.parentElement && hops < 5) {
      box = box.parentElement; hops++;
      const lines = (box.innerText || '').split('\\n').map(s => s.trim()).filter(Boolean);
      if (lines.length >= 2) {
        const rest = lines.filter(l => !priceRe.test(l.replace(/\\u00a0/g, ' ')));
        if (rest.length) {
          out.push({price: t, name: rest[0], description: rest[1] || null});
          return;
        }
      }
    }
  });
  const seen = new Set();
  return out.filter(o => !seen.has(o.name + o.price) && seen.add(o.name + o.price));
}"""


def unwrap_url(raw: str | None) -> str | None:
    """'/url?q=https%3A//x.com/y&fbclid=..' -> 'https://x.com/y' (tracking stripped)."""
    if not raw:
        return None
    u = raw.strip()
    if u.startswith("/url?") or "google.com/url?" in u:
        q = urllib.parse.parse_qs(urllib.parse.urlparse(u).query).get("q")
        if not q:
            return None
        u = q[0]
    parts = urllib.parse.urlsplit(u)
    if not parts.scheme.startswith("http"):
        return None
    kept = [(k, v) for k, v in urllib.parse.parse_qsl(parts.query) if k not in _TRACKING]
    return urllib.parse.urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urllib.parse.urlencode(kept), ""))


def classify_link(url: str) -> str:
    host = (urllib.parse.urlsplit(url).netloc or "").lower()
    for needle, kind in _LINK_KINDS:
        if needle in host:
            return kind
    return "website"


def parse_price(lines: list[str]) -> dict:
    """Pull the price band out of the panel lines.

    'SAR 20–40 per person' + 'Reported by 267 people' -> min/max/currency/count.
    innerText truncates the inline copy with an ellipsis ('SAR 1–20…'), so the
    'per person' row is the reliable source.
    """
    out: dict = {"price_range_text": None, "price_min": None, "price_max": None,
                 "price_currency": None, "price_reported_by": None}
    for ln in lines:
        if "per person" in ln.lower() and any(ch.isdigit() for ch in ln):
            out["price_range_text"] = ln.replace("\xa0", " ").strip()
            break
    if not out["price_range_text"]:
        for ln in lines:
            if _PRICE_ONE.match(ln.replace("\xa0", " ").strip()):
                out["price_range_text"] = ln.replace("\xa0", " ").strip()
                break
    txt = (out["price_range_text"] or "").replace("\xa0", " ")
    m = _PRICE_RE.search(txt)
    if m:
        out["price_currency"] = m.group(1)[:3].upper()
        out["price_min"] = float(m.group(2).replace(",", ""))
        out["price_max"] = float(m.group(3).replace(",", ""))
    else:
        m1 = _PRICE_ONE.search(txt)
        if m1:
            out["price_currency"] = m1.group(1)[:3].upper()
            out["price_min"] = out["price_max"] = float(m1.group(2).replace(",", ""))
    for ln in lines:
        r = _REPORTED_RE.search(ln)
        if r:
            out["price_reported_by"] = int(r.group(1).replace(",", ""))
            break
    return out


def parse_popular_times(aria: list[str]) -> list[dict]:
    """aria-labels -> [{dow, hour_24, busy_pct}]. Google emits one label per bar,
    e.g. 'Tuesdays 6 AM 20% busy.'; day is sometimes only on the group heading, so
    it is carried forward across bars."""
    out: list[dict] = []
    dow = None
    for a in aria:
        d = _DOW_RE.search(a)
        if d:
            dow = d.group(0).capitalize()
        b = _BUSY_RE.search(a)
        if not b:
            continue
        h = _HOUR_RE.search(a)
        hour = None
        if h:
            hour = int(h.group(1)) % 12 + (12 if h.group(2).upper() == "PM" else 0)
        out.append({"dow": dow, "hour_24": hour, "busy_pct": int(b.group(1))})
    return out


def parse_hours(aria: list[str]) -> list[dict]:
    """aria-labels carrying a weekday -> [{dow, text}]. Kept verbatim; splitting
    'Closed · Opens 9 AM' into open/close times is a downstream concern."""
    seen, out = set(), []
    for a in aria:
        d = _DOW_RE.search(a)
        if not d:
            continue
        dow = d.group(0).capitalize()
        if dow in seen or _BUSY_RE.search(a):
            continue
        seen.add(dow)
        out.append({"dow": dow, "text": _clean_hours_text(a)})
    return out


def _clean_hours_text(a: str) -> str:
    """'Tuesday, 6 am to 1 am, Copy open hours' -> 'Tuesday, 6 am to 1 am'.

    The aria-label ends with the button's own affordance text, and Google uses
    narrow no-break spaces around the times.
    """
    t = a.replace(" ", " ").replace(" ", " ")
    t = re.sub(r",?\s*Copy open hours\.?$", "", t, flags=re.I)
    return re.sub(r"\s+", " ", t).strip(" ,.")


async def capture_panel_extras(page, want_menu: bool = True,
                               settle_ms: int = SETTLE_MS_DEFAULT) -> dict:
    """Capture the Overview modules from the panel currently on `page`.

    `page` must already be on a fresh place URL. Never raises — each section
    degrades to empty plus an `errors` entry, matching horeca_capture.
    """
    out: dict = {
        "share_link": None, "price_range_text": None, "price_min": None,
        "price_max": None, "price_currency": None, "price_reported_by": None,
        "popular_times": [], "hours": [], "links": [],
        "has_menu_tab": False, "menu_items": [], "errors": [],
    }
    try:
        await page.wait_for_timeout(settle_ms)
        panel = await page.evaluate(_JS_PANEL)
    except Exception as exc:
        out["errors"].append(f"panel: {exc}")
        return out

    lines, aria = panel.get("lines") or [], panel.get("aria") or []
    try:
        out.update(parse_price(lines))
        out["popular_times"] = parse_popular_times(aria)
        out["hours"] = parse_hours(aria)
    except Exception as exc:
        out["errors"].append(f"parse: {exc}")

    try:
        seen = set()
        for raw in panel.get("links") or []:
            u = unwrap_url(raw)
            if not u or "google.com/maps" in u or "/maps/" in u:
                continue
            host = urllib.parse.urlsplit(u).netloc.lower()
            if host.endswith("google.com") or host.endswith("google.co.in"):
                continue
            if u in seen:
                continue
            seen.add(u)
            out["links"].append({"kind": classify_link(u), "url": u[:2000]})
    except Exception as exc:
        out["errors"].append(f"links: {exc}")

    # Share BEFORE any tab switch — the button is not on the Menu pane.
    try:
        await page.locator('button[aria-label^="Share"], button:has-text("Share")'
                           ).first.click(timeout=6_000)
        for _ in range(40):                      # poll ~4s rather than sleep blind
            out["share_link"] = await page.evaluate(_JS_SHARE)
            if out["share_link"]:
                break
            await page.wait_for_timeout(100)
        await page.keyboard.press("Escape")
    except Exception as exc:
        out["errors"].append(f"share: {exc}")

    tabs = panel.get("tabs") or []
    out["has_menu_tab"] = any("menu" in t.lower() for t in tabs)
    if want_menu and out["has_menu_tab"]:
        try:
            out["menu_items"] = await _capture_menu(page)
        except Exception as exc:
            out["errors"].append(f"menu: {exc}")
    return out


async def _capture_menu(page) -> list[dict]:
    """Open the Menu tab and the first category chip, then read item rows.

    Rows render as name / description / price stacked as separate lines, with the
    price line starting with the currency code.
    """
    await page.locator('button[role="tab"]').filter(has_text="Menu").first.click(timeout=6_000)
    await page.wait_for_timeout(MENU_SETTLE_MS)
    items = normalize_menu_items(await page.evaluate(_JS_MENU_ITEMS))
    if items:
        return items
    tried = 0
    for chip in await page.evaluate(_JS_CHIPS):   # items hide behind a category
        if not chip or len(chip) > 28 or chip in ("Overview", "Menu", "Reviews", "About"):
            continue
        if tried >= MAX_CHIP_TRIES:
            break                                  # photo-only menu: stop paying 3s a chip
        tried += 1
        try:
            await page.locator('div[role="main"] button').filter(has_text=chip
                                                                 ).first.click(timeout=5_000)
            await page.wait_for_timeout(CHIP_SETTLE_MS)
        except Exception:
            continue
        items = normalize_menu_items(await page.evaluate(_JS_MENU_ITEMS))
        if items:
            return items
    return []


def normalize_menu_items(rows) -> list[dict]:
    """DOM rows [{price, name, description}] -> typed items.

    Rows whose price will not parse are dropped rather than stored as 0.0, so a
    selector drift shows up as missing items instead of a menu priced at nothing.
    """
    out: list[dict] = []
    for r in rows or []:
        m = _PRICE_ONE.match((r.get("price") or "").replace(" ", " ").strip())
        name = (r.get("name") or "").strip()
        if not m or not name:
            continue
        desc = (r.get("description") or "").strip() or None
        out.append({"name": name[:200], "description": desc[:500] if desc else None,
                    "currency": m.group(1).upper(),
                    "price_amount": float(m.group(2).replace(",", ""))})
    return out
