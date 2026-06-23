"""
Core extraction logic for Google Maps place pages.

Strategy: Hybrid JS injection + Playwright interactions.
- Single page.evaluate() for all static DOM reads (fast, one roundtrip)
- Playwright click/wait for interactive elements (share link, hours, about)
- URL regex for coordinates
"""

import re
import logging
from typing import Optional
from playwright.async_api import Page, TimeoutError as PlaywrightTimeout

from .models import PlaceData

logger = logging.getLogger(__name__)

# Unicode artifacts Google Maps injects into text (incl. plus code symbol)
_UNICODE_JUNK = str.maketrans("", "", "\ue0c8\ue0b0\ue0ce\uf186")

# ── Lat/Long from APP_INITIALIZATION_STATE (instant, no URL wait) ──
LATLONG_JS = r"""
() => {
    try {
        const state = JSON.parse(JSON.stringify(window.APP_INITIALIZATION_STATE));
        const arr = state[0][0];
        if (arr && arr.length >= 3) {
            const lat = arr[2], lng = arr[1];
            if (lat >= -90 && lat <= 90 && lng >= -180 && lng <= 180) {
                return { latitude: parseFloat(lat.toFixed(7)).toString(),
                         longitude: parseFloat(lng.toFixed(7)).toString() };
            }
        }
    } catch(e) {}
    return { latitude: '', longitude: '' };
}
"""

# ── Category fallback: rating sibling + visible text parsing ──
CATEGORY_FALLBACK_JS = r"""
() => {
    const skip = ['open', 'closed', 'temporarily', 'permanently', 'hours',
                  '\u00b7', 'see all', 'website', 'directions', 'save', 'share',
                  'add a photo', 'add a label', 'add place', 'claim this', 'suggest an edit',
                  'send to phone', 'add website', 'add missing', 'see photos', "add place's",
                  'overview', 'about', 'reviews', 'nearby', '\u20b9', '$', 'updates',
                  'prices', 'photos', 'menu', 'order', 'book', 'reserve',
                  'mon,', 'tue,', 'wed,', 'thu,', 'fri,', 'sat,', 'sun,',
                  'jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'oct', 'nov', 'dec',
                  'check-in', 'check-out', 'check in', 'check out', 'guests'];
    // Strategy A: sibling of rating element
    const ratingParent = document.querySelector('div.F7nice')?.parentElement;
    if (ratingParent) {
        for (const s of ratingParent.querySelectorAll('span')) {
            const t = s.textContent.trim(), lower = t.toLowerCase();
            if (t && t.length > 2 && t.length < 40 && !/^[\d.]+$/.test(t)
                && !t.includes('(') && !skip.some(sk => lower.includes(sk))
                && !/^\d+\s*reviews?$/i.test(t)) return t;
        }
    }
    // Strategy B: line after rating in visible text
    const main = document.querySelector('[role="main"]');
    if (main) {
        const lines = main.innerText.substring(0, 500).split('\n').map(l => l.trim()).filter(l => l);
        for (let i = 0; i < lines.length - 1; i++) {
            if (/^\d\.\d$/.test(lines[i])) {
                let cat = lines[i + 1].replace(/[·]/g, '').trim();
                if (cat && cat.length > 1 && cat.length < 40
                    && /^[A-Za-z]/.test(cat)
                    && !skip.some(s => cat.toLowerCase().includes(s))
                    && !/^\d/.test(cat)) return cat;
            }
        }
    }
    return '';
}
"""

# ── Status fallback: check visible text for closed status ──
STATUS_FALLBACK_JS = r"""
() => {
    const main = document.querySelector('[role="main"]');
    if (!main) return '';
    const txt = main.innerText.substring(0, 600);
    if (txt.includes('Permanently closed')) return 'Permanently closed';
    if (txt.includes('Temporarily closed')) return 'Temporarily closed';
    return '';
}
"""

# Patterns for bad category values (UI buttons, phone numbers, URLs)
_BAD_CAT_PATTERNS = ['add a', 'add place', 'add website', 'add hour', 'add phone',
                     'claim this', 'suggest an edit', 'send to phone', 'see photos',
                     'overview', 'prices', 'check-in', 'check availability']

# ── Minimal extract for closed places (fast path) ───────────────────────
MINIMAL_EXTRACT_JS = """
() => {
    const r = {};
    const nameEl = document.querySelector('div[class*="fontHeadlineLarge"] h1')
                || document.querySelector('h1.DUwDvf')
                || document.querySelector('h1');
    r.name = nameEl ? nameEl.textContent.trim() : 'N/A';
    r.status = 'Open';
    const statusEl = document.querySelector('span.fCEvvc')
                  || document.querySelector('span.aSftqf')
                  || document.querySelector('div[aria-label*="closed" i]')
                  || document.querySelector('button[data-item-id*="oh"]');
    if (statusEl) {
        const t = (statusEl.textContent || statusEl.getAttribute('aria-label') || '').trim().toLowerCase();
        if (t.includes('permanently closed')) r.status = 'Permanently closed';
        else if (t.includes('temporarily closed')) r.status = 'Temporarily closed';
    }
    if (r.status === 'Open') {
        const main = document.querySelector('[role="main"]');
        if (main) {
            const txt = main.innerText || '';
            if (txt.includes('Permanently closed')) r.status = 'Permanently closed';
            else if (txt.includes('Temporarily closed')) r.status = 'Temporarily closed';
        }
    }
    return r;
}
"""

# ── Lean JS — only 8 DOM fields (name, rating, review_count, category, status, address, phone) ──
# Skips: website, price_level, plus_code, photo_count, menu_link, image_urls, latest_review_date
LEAN_EXTRACT_JS = """
() => {
    const r = {};

    const nameEl = document.querySelector('div[class*="fontHeadlineLarge"] h1')
                || document.querySelector('h1.DUwDvf')
                || document.querySelector('h1');
    r.name = nameEl ? nameEl.textContent.trim() : 'N/A';

    const ratingEl = document.querySelector('div.F7nice span[aria-hidden="true"]');
    r.rating = ratingEl ? ratingEl.textContent.trim() : 'N/A';

    // Review Count — scoped to place header only (not reviewer profile badges)
    const rcCandidates = [];
    // S1: span with (number) inside F7nice (place's rating area)
    for (const span of document.querySelectorAll('div.F7nice span')) {
        const t = span.textContent;
        if (t.includes('(') && t.includes(')')) {
            const num = parseInt(t.replace(/[^0-9]/g, ''));
            if (num > 0 && num < 100000) rcCandidates.push(num);
        }
    }
    // S2: aria-label on F7nice parent only (not entire page — avoids reviewer "5.0 stars 1 Reviews" badges)
    const f7 = document.querySelector('div.F7nice');
    if (f7) {
        const scope = f7.parentElement || f7;
        for (const el of scope.querySelectorAll('[aria-label*="star" i]')) {
            const label = el.getAttribute('aria-label') || '';
            const m = label.match(/(\\d[\\d,]*)\\s*[Rr]eview/);
            if (m) { rcCandidates.push(parseInt(m[1].replace(/,/g, ''))); break; }
        }
    }
    // S3: userRatingCount / reviewCount from embedded JSON in script tags
    for (const script of document.querySelectorAll('script')) {
        const c = script.textContent || '';
        for (const p of [/"userRatingCount"\\s*:\\s*(\\d+)/, /"reviewCount"\\s*:\\s*(\\d+)/]) {
            const m = c.match(p);
            if (m && parseInt(m[1]) > 0 && parseInt(m[1]) < 100000) { rcCandidates.push(parseInt(m[1])); break; }
        }
        if (rcCandidates.length > 2) break;
    }
    r.review_count = rcCandidates.length > 0 ? String(Math.max(...rcCandidates)) : '0';

    // Category
    const typeEl = document.querySelector('button[jsaction*="category"]')
                || document.querySelector('button.DkEaL')
                || document.querySelector('span.DkEaL')
                || document.querySelector('[role="main"] button:first-of-type');
    r.category = typeEl ? typeEl.textContent.trim() : 'N/A';

    // Status — only Temporarily/Permanently closed
    r.status = 'Open';
    const statusEl = document.querySelector('span.fCEvvc')
                  || document.querySelector('span.aSftqf')
                  || document.querySelector('div[aria-label*="closed" i]')
                  || document.querySelector('button[data-item-id*="oh"]');
    if (statusEl) {
        const t = (statusEl.textContent || statusEl.getAttribute('aria-label') || '').trim().toLowerCase();
        if (t.includes('permanently closed')) r.status = 'Permanently closed';
        else if (t.includes('temporarily closed')) r.status = 'Temporarily closed';
    }
    if (r.status === 'Open') {
        const main = document.querySelector('[role="main"]');
        if (main) {
            const txt = main.innerText || '';
            if (txt.includes('Permanently closed')) r.status = 'Permanently closed';
            else if (txt.includes('Temporarily closed')) r.status = 'Temporarily closed';
        }
    }

    // Address
    const addrEl = document.querySelector('button[data-item-id*="address"]');
    r.address = addrEl ? addrEl.textContent.trim() : 'N/A';

    // Phone
    const phoneEl = document.querySelector('button[data-item-id*="phone:tel:"]');
    r.phone = phoneEl ? phoneEl.textContent.trim() : 'N/A';

    return r;
}
"""

# ── Lean Enrich JS — all 4 target fields from DOM + APP_INITIALIZATION_STATE ──
# Single evaluate() call: no tab clicking, no Playwright interactions needed.
# Extracts latest_review_date + photo_count from APP_INITIALIZATION_STATE (deterministic).
LEAN_ENRICH_JS = """
() => {
    const r = {};

    const nameEl = document.querySelector('div[class*="fontHeadlineLarge"] h1')
                || document.querySelector('h1.DUwDvf')
                || document.querySelector('h1');
    r.name = nameEl ? nameEl.textContent.trim() : 'N/A';

    const ratingEl = document.querySelector('div.F7nice span[aria-hidden="true"]');
    r.rating = ratingEl ? ratingEl.textContent.trim() : 'N/A';

    // Review Count — scoped to place header only (not reviewer profile badges)
    const rcCandidates = [];
    // S1: span with (number) inside F7nice (place's rating area)
    for (const span of document.querySelectorAll('div.F7nice span')) {
        const t = span.textContent;
        if (t.includes('(') && t.includes(')')) {
            const num = parseInt(t.replace(/[^0-9]/g, ''));
            if (num > 0 && num < 100000) rcCandidates.push(num);
        }
    }
    // S2: aria-label on F7nice parent only (not entire page — avoids reviewer "5.0 stars 1 Reviews" badges)
    const f7 = document.querySelector('div.F7nice');
    if (f7) {
        const scope = f7.parentElement || f7;
        for (const el of scope.querySelectorAll('[aria-label*="star" i]')) {
            const label = el.getAttribute('aria-label') || '';
            const m = label.match(/(\\d[\\d,]*)\\s*[Rr]eview/);
            if (m) { rcCandidates.push(parseInt(m[1].replace(/,/g, ''))); break; }
        }
    }
    // S3: userRatingCount / reviewCount from embedded JSON in script tags
    for (const script of document.querySelectorAll('script')) {
        const c = script.textContent || '';
        for (const p of [/"userRatingCount"\\s*:\\s*(\\d+)/, /"reviewCount"\\s*:\\s*(\\d+)/]) {
            const m = c.match(p);
            if (m && parseInt(m[1]) > 0 && parseInt(m[1]) < 100000) { rcCandidates.push(parseInt(m[1])); break; }
        }
        if (rcCandidates.length > 2) break;
    }
    r.review_count = rcCandidates.length > 0 ? String(Math.max(...rcCandidates)) : '0';

    // ── Latest Review Date from APP_INITIALIZATION_STATE (deterministic) ──
    r.latest_review_date = 'N/A';
    r.photo_count = '0';
    try {
        const state = JSON.stringify(window.APP_INITIALIZATION_STATE);

        // Find all "X ago" patterns, keep the most recent
        const agoRe = /(\\d+)\\s+(minute|hour|day|week|month|year)s?\\s+ago|a\\s+(minute|hour|day|week|month|year)\\s+ago/gi;
        const units = { minute: 1, hour: 60, day: 1440, week: 10080, month: 43200, year: 525600 };
        let bestMinutes = Infinity;
        let bestMatch = '';
        let m;
        while ((m = agoRe.exec(state)) !== null) {
            const full = m[0];
            const num = m[1] ? parseInt(m[1]) : 1;
            const unit = (m[2] || m[3] || '').toLowerCase();
            const mins = num * (units[unit] || 999999999);
            if (mins < bestMinutes) { bestMinutes = mins; bestMatch = full; }
        }
        if (bestMatch) r.latest_review_date = bestMatch;
    } catch(e) {}

    // ── Total Photos from APP_STATE (includes Street View / Maps data) ──
    r.total_photos = '0';
    try {
        const state = JSON.stringify(window.APP_INITIALIZATION_STATE);
        const photoM = state.match(/(\\d+)\\+?\\s+Photos?/);
        if (photoM && parseInt(photoM[1]) > 0) r.total_photos = photoM[1];
    } catch(e) {}

    // photo_count (user-uploaded) will be set after a carousel scroll in extract_all

    // Category
    const typeEl = document.querySelector('button[jsaction*="category"]')
                || document.querySelector('button.DkEaL')
                || document.querySelector('span.DkEaL')
                || document.querySelector('[role="main"] button:first-of-type');
    r.category = typeEl ? typeEl.textContent.trim() : 'N/A';

    // Status — for closed fast path
    r.status = 'Open';
    const statusEl = document.querySelector('span.fCEvvc')
                  || document.querySelector('span.aSftqf')
                  || document.querySelector('div[aria-label*="closed" i]')
                  || document.querySelector('button[data-item-id*="oh"]');
    if (statusEl) {
        const t = (statusEl.textContent || statusEl.getAttribute('aria-label') || '').trim().toLowerCase();
        if (t.includes('permanently closed')) r.status = 'Permanently closed';
        else if (t.includes('temporarily closed')) r.status = 'Temporarily closed';
    }
    if (r.status === 'Open') {
        const main = document.querySelector('[role="main"]');
        if (main) {
            const txt = main.innerText || '';
            if (txt.includes('Permanently closed')) r.status = 'Permanently closed';
            else if (txt.includes('Temporarily closed')) r.status = 'Temporarily closed';
        }
    }

    // Address
    const addrEl = document.querySelector('button[data-item-id*="address"]');
    r.address = addrEl ? addrEl.textContent.trim() : 'N/A';

    // Phone
    const phoneEl = document.querySelector('button[data-item-id*="phone:tel:"]');
    r.phone = phoneEl ? phoneEl.textContent.trim() : 'N/A';

    return r;
}
"""

# ── JS injection script ────────────────────────────────────────────────
# Extracts all static attributes in a single evaluate() call.
# Ported from scraper.py:55-161 and expanded with new selectors.

EXTRACT_JS = """
async () => {
    // Wait for the F7nice rating block to finish rendering. Bug seen on
    // Sadar Bazar runs: a place with rating "4.7 (22)" returned
    // review_count=0 because the "(22)" span lags the rating number by
    // ~1-2.5s after domcontentloaded — extract_all fired in the gap.
    // Adaptive: resolves immediately if F7nice is missing (unrated) or
    // already fully rendered (count span OR "N reviews" aria-label present).
    // Hard ceiling 3500ms — only paid on rated places whose count is still
    // pending.
    await new Promise(resolve => {
        const start = Date.now();
        const check = () => {
            if (Date.now() - start > 3500) return resolve();
            const f7 = document.querySelector('div.F7nice');
            if (!f7) return resolve();
            // Path A: literal "(N)" text inside F7nice
            if (/\\(\\d[\\d,]*\\)/.test(f7.textContent || '')) return resolve();
            // Path B: aria-label "N review(s)" anywhere under F7nice's parent
            const scope = f7.parentElement || f7;
            for (const el of scope.querySelectorAll('[aria-label]')) {
                if (/\\d[\\d,]*\\s*[Rr]eview/.test(el.getAttribute('aria-label') || '')) {
                    return resolve();
                }
            }
            setTimeout(check, 50);
        };
        check();
    });

    const r = {};

    // ── Name ──
    const nameEl = document.querySelector('div[class*="fontHeadlineLarge"] h1')
                || document.querySelector('h1.DUwDvf')
                || document.querySelector('h1');
    r.name = nameEl ? nameEl.textContent.trim() : 'N/A';

    // ── Rating (multi-strategy) ──
    r.rating = 'N/A';
    // S1: F7nice span — the main rating block
    const ratingEl = document.querySelector('div.F7nice span[aria-hidden="true"]');
    if (ratingEl) {
        const t = ratingEl.textContent.trim();
        if (/^\\d+\\.?\\d*$/.test(t)) r.rating = t;
    }
    // S2: aria-label scoped to F7nice ("4.5 stars …")
    if (r.rating === 'N/A') {
        const f7 = document.querySelector('div.F7nice');
        if (f7) {
            const scope = f7.parentElement || f7;
            for (const el of scope.querySelectorAll('[aria-label*="star" i]')) {
                const m = (el.getAttribute('aria-label') || '').match(/(\\d+\\.?\\d*)\\s*star/i);
                if (m) {
                    const v = parseFloat(m[1]);
                    if (v >= 1 && v <= 5) { r.rating = m[1]; break; }
                }
            }
        }
    }
    // S3: APP_INITIALIZATION_STATE — ratingValue / [N.N,"stars"] embedded JSON
    //
    // GATED: APP_STATE contains data for nearby places too, not just the
    // focused one. For an unrated place with rated neighbors (Vakeel/Raj
    // Kirana case), an ungated regex would pick up a sibling's rating. Only
    // fire this fallback when the focused place's F7nice block is missing
    // ENTIRELY (which would indicate a different page structure, not an
    // unrated place). If F7nice exists but its text is empty, the focused
    // place truly has no rating — leave r.rating = 'N/A'.
    if (r.rating === 'N/A') {
        const f7 = document.querySelector('div.F7nice');
        if (!f7) {
            try {
                const state = JSON.stringify(window.APP_INITIALIZATION_STATE);
                const m = state.match(/"ratingValue"\\s*:\\s*"?(\\d+\\.?\\d*)"?/)
                      || state.match(/\\[(\\d\\.\\d+),"[^"]*?stars?"/);
                if (m) {
                    const v = parseFloat(m[1]);
                    if (v >= 1 && v <= 5) r.rating = String(v);
                }
            } catch(e) {}
        }
    }

    // ── Review Count (4 strategies) ──
    r.review_count = '0';

    // Strategy 1: capture "(N)" group inside any F7nice descendant span.
    // Extracting via regex (not stripping all non-digits) avoids picking up
    // a wrapper span like "4.7(22) · …" and producing "4722".
    for (const span of document.querySelectorAll('div.F7nice span')) {
        const m = (span.textContent || '').match(/\\((\\d[\\d,]*)\\)/);
        if (m) {
            const n = m[1].replace(/,/g, '');
            if (parseInt(n) > 0 && parseInt(n) < 100000) {
                r.review_count = n;
                break;
            }
        }
    }

    // Strategy 2: aria-label scoped to F7nice parent (not reviewer profile badges)
    if (r.review_count === '0') {
        const f7 = document.querySelector('div.F7nice');
        if (f7) {
            const scope = f7.parentElement || f7;
            for (const el of scope.querySelectorAll('[aria-label*="star" i]')) {
                const label = el.getAttribute('aria-label') || '';
                const m = label.match(/(\\d[\\d,]*)\\s*[Rr]eview/);
                if (m) { r.review_count = m[1].replace(/,/g, ''); break; }
            }
        }
    }

    // Strategy 4: embedded JSON in script tags
    if (r.review_count === '0') {
        for (const script of document.querySelectorAll('script')) {
            const c = script.textContent || '';
            const patterns = [
                /"userRatingCount"\\s*:\\s*(\\d+)/,
                /"reviewCount"\\s*:\\s*(\\d+)/,
                /"reviews"\\s*:\\s*(\\d+)/
            ];
            for (const p of patterns) {
                const m = c.match(p);
                if (m && parseInt(m[1]) > 0 && parseInt(m[1]) < 100000) {
                    r.review_count = m[1];
                    break;
                }
            }
            if (r.review_count !== '0') break;
        }
    }

    // Strategy 5: APP_INITIALIZATION_STATE — userRatingCount/reviewCount
    if (r.review_count === '0') {
        try {
            const state = JSON.stringify(window.APP_INITIALIZATION_STATE);
            const m = state.match(/"userRatingCount"\\s*:\\s*(\\d+)/)
                  || state.match(/"reviewCount"\\s*:\\s*(\\d+)/);
            if (m) {
                const v = parseInt(m[1]);
                if (v > 0 && v < 100000) r.review_count = String(v);
            }
        } catch(e) {}
    }

    // ── Category / Type ──
    const typeEl = document.querySelector('button[jsaction*="category"]')
                || document.querySelector('button.DkEaL')
                || document.querySelector('span.DkEaL')
                || document.querySelector('[role="main"] button:first-of-type');
    r.category = typeEl ? typeEl.textContent.trim() : 'N/A';
    if (r.category === 'N/A') {
        const skip = ['open', 'closed', 'temporarily', 'permanently', 'hours', '·', 'see all', 'website', 'directions', 'save', 'share'];
        const header = document.querySelector('[role="main"]');
        if (header) {
            const candidates = header.querySelectorAll('button[jsaction], span[class*="fontBody"], span[class*="DkEaL"], a[data-item-id], button.DkEaL, span.DkEaL');
            for (const c of candidates) {
                const t = c.textContent.trim();
                const lower = t.toLowerCase();
                if (t && t.length < 50 && t.length > 1 && !t.includes('(') && !/^[\\d.]+$/.test(t)
                    && !skip.some(s => lower.includes(s)) && !/^\\d+\\s*reviews?$/i.test(t)) {
                    r.category = t;
                    break;
                }
            }
        }
        if (r.category === 'N/A' && nameEl) {
            const parent = nameEl.closest('div') || nameEl.parentElement;
            if (parent) {
                const sibs = parent.querySelectorAll(':scope > button, :scope > span, :scope > a');
                for (const s of sibs) {
                    const t = s.textContent.trim();
                    if (t && t !== r.name && t.length < 50 && !t.includes('(') && !/^[\\d.]+$/.test(t)
                        && !skip.some(x => t.toLowerCase().includes(x))) {
                        r.category = t;
                        break;
                    }
                }
            }
        }
        if (r.category === 'N/A') {
            const main = document.querySelector('[role="main"]');
            if (main) {
                const txt = (main.innerText || main.textContent || '').replace(/\\s+/g, ' ');
                const patterns = [
                    /\\)\\s*[·•]\\s*([A-Za-z][A-Za-z\\s]{1,30})/,
                    /[·•]\\s*([A-Za-z][A-Za-z\\s]{1,30}?)(?:\\s|$|\\d|₹)/,
                    /\\d+\\s+reviews?\\s*[·•]\\s*([A-Za-z]+)/
                ];
                for (const re of patterns) {
                    const match = txt.match(re);
                    if (match) {
                        const cat = match[1].trim();
                        if (cat && !/^(Open|Closed|Temporarily|Permanently|hours|Check|Compare)$/i.test(cat))
                            { r.category = cat; break; }
                    }
                }
            }
        }
    }

    // ── Status (only Temporarily closed / Permanently closed — NOT business hours) ──
    r.status = 'Open';
    const statusEl = document.querySelector('span.fCEvvc')
                  || document.querySelector('span.aSftqf')
                  || document.querySelector('div[aria-label*="closed" i]')
                  || document.querySelector('button[data-item-id*="oh"]');
    if (statusEl) {
        const t = (statusEl.textContent || statusEl.getAttribute('aria-label') || '').trim().toLowerCase();
        if (t.includes('permanently closed')) r.status = 'Permanently closed';
        else if (t.includes('temporarily closed')) r.status = 'Temporarily closed';
        // "Closed · Opens 8 am" = just outside business hours, NOT closed status
    }
    if (r.status === 'Open') {
        const main = document.querySelector('[role="main"]');
        if (main) {
            const txt = main.innerText || '';
            if (txt.includes('Permanently closed')) r.status = 'Permanently closed';
            else if (txt.includes('Temporarily closed')) r.status = 'Temporarily closed';
        }
    }

    // ── Address ──
    const addrEl = document.querySelector('button[data-item-id*="address"]');
    r.address = addrEl ? addrEl.textContent.trim() : 'N/A';

    // ── Phone ──
    const phoneEl = document.querySelector('button[data-item-id*="phone:tel:"]');
    r.phone = phoneEl ? phoneEl.textContent.trim() : 'N/A';

    // ── Website (NEW) ──
    const webEl = document.querySelector('a[data-item-id="authority"]')
               || document.querySelector('button[data-item-id*="authority"]')
               || document.querySelector('a[data-tooltip="Open website"]');
    if (webEl) {
        let href = webEl.getAttribute('href') || '';
        if (href.startsWith('/url?')) {
            const m = href.match(/[?&]q=([^&]+)/);
            if (m) href = decodeURIComponent(m[1]);
        }
        r.website = (href && href.startsWith('http')) ? href : (webEl.textContent.trim() || 'N/A');
    } else {
        r.website = 'N/A';
    }

    // ── Price Level (NEW) ──
    const priceEl = document.querySelector('span[aria-label*="Price"]')
                 || document.querySelector('span[aria-label*="price"]');
    if (priceEl) {
        r.price_level = priceEl.textContent.trim() || priceEl.getAttribute('aria-label') || 'N/A';
    } else {
        // Fallback: look for $ symbols near the category
        const priceCandidates = document.querySelectorAll('span');
        r.price_level = 'N/A';
        for (const sp of priceCandidates) {
            const txt = sp.textContent.trim();
            if (/^\\$+$/.test(txt) || /^\\u00b7\\s*\\$/.test(txt)) {
                r.price_level = txt.replace(/\\u00b7/g, '').trim();
                break;
            }
        }
    }

    // ── Plus Code (NEW) ──
    const pcEl = document.querySelector('button[data-item-id="oloc"]');
    r.plus_code = pcEl ? pcEl.textContent.trim() : 'N/A';

    // ── Photo Count (NEW) ──
    // Match the actual Google Maps count wording — "<digits> photos" — so we
    // never grab an unrelated number that just happens to share a "photo"
    // aria-label (e.g. an address like "Photo of Al madina kirana store, 9155…"
    // was bogusly producing photo_count=9155). Pick the strongest button: scan
    // every "photo" aria-label, prefer one whose digits are followed by
    // "photo"/"photos".
    r.photo_count = '0';
    const photoBtns = document.querySelectorAll(
        'button[aria-label*="photo" i], a[aria-label*="photo" i]'
    );
    for (const el of photoBtns) {
        const aria = el.getAttribute('aria-label') || '';
        const m = aria.match(/([0-9][0-9,]*)\\s*photos?/i);
        if (m) {
            const v = parseInt(m[1].replace(/,/g, ''));
            if (v >= 0 && v < 100000) { r.photo_count = String(v); break; }
        }
    }

    // ── Menu Link (NEW) ──
    const menuEl = document.querySelector('a[data-item-id="menu"]')
                || document.querySelector('a[aria-label*="Menu" i]');
    r.menu_link = menuEl ? (menuEl.getAttribute('href') || 'N/A') : 'N/A';

    // ── Image URLs (NEW) — grab src from hero/carousel images ──
    const imgEls = document.querySelectorAll(
        'button[jsaction*="heroHeaderImage"] img, ' +
        'div[class*="ZKCDEc"] img, ' +
        'div[aria-label*="Photo"] img, ' +
        'img.iPQvYb'
    );
    const urls = [];
    for (const img of imgEls) {
        const src = img.src || img.getAttribute('data-src') || '';
        if (src && src.startsWith('http') && !urls.includes(src)) {
            urls.push(src);
        }
    }
    // Also check background-image styles on photo containers
    for (const div of document.querySelectorAll('div[style*="background-image"]')) {
        const style = div.getAttribute('style') || '';
        const m = style.match(/url\\(["']?(https?[^"')]+)["']?\\)/);
        if (m && !urls.includes(m[1])) urls.push(m[1]);
    }
    r.image_urls = urls.length > 0 ? urls.join(';') : 'N/A';

    // ── Latest Review Date (NEW) ──
    r.latest_review_date = 'N/A';
    const reviewEls = document.querySelectorAll('span.rsqaWe');
    if (reviewEls.length > 0) {
        r.latest_review_date = reviewEls[0].textContent.trim();
    } else {
        // Fallback: look for relative time patterns in review sections
        const reviewSection = document.querySelectorAll('div.jftiEf, div.GHT2ce');
        for (const rev of reviewSection) {
            const spans = rev.querySelectorAll('span');
            for (const sp of spans) {
                const t = sp.textContent.trim();
                if (/\\d+\\s+(day|week|month|year|hour|minute)s?\\s+ago/i.test(t)
                    || /a\\s+(day|week|month|year)\\s+ago/i.test(t)) {
                    r.latest_review_date = t;
                    break;
                }
            }
            if (r.latest_review_date !== 'N/A') break;
        }
    }
    // Final fallback: APP_INITIALIZATION_STATE — keep the most recent "X ago" string
    if (r.latest_review_date === 'N/A') {
        try {
            const state = JSON.stringify(window.APP_INITIALIZATION_STATE);
            const agoRe = /(\\d+)\\s+(minute|hour|day|week|month|year)s?\\s+ago|a\\s+(minute|hour|day|week|month|year)\\s+ago/gi;
            const units = { minute: 1, hour: 60, day: 1440, week: 10080, month: 43200, year: 525600 };
            let bestMinutes = Infinity, bestMatch = '';
            let m;
            while ((m = agoRe.exec(state)) !== null) {
                const num = m[1] ? parseInt(m[1]) : 1;
                const unit = (m[2] || m[3] || '').toLowerCase();
                const mins = num * (units[unit] || 999999999);
                if (mins < bestMinutes) { bestMinutes = mins; bestMatch = m[0]; }
            }
            if (bestMatch) r.latest_review_date = bestMatch;
        } catch(e) {}
    }

    // ── Total Photos (NEW) — from APP_INITIALIZATION_STATE (Street View + Maps data) ──
    r.total_photos = '0';
    try {
        const state = JSON.stringify(window.APP_INITIALIZATION_STATE);
        const photoM = state.match(/(\\d+)\\+?\\s+Photos?/);
        if (photoM && parseInt(photoM[1]) > 0) r.total_photos = photoM[1];
    } catch(e) {}

    return r;
}
"""

# ── Python-side regex fallback for Review Count ──
_REVIEW_PATTERNS = [
    r'"userRatingCount"\s*:\s*(\d+)',
    r'\\"userRatingCount\\":\s*(\d+)',
    r'"reviewCount"\s*:\s*(\d+)',
    # Removed: broad patterns that match reviewer profile badges
    # r'(\d+)\s+reviews?"'  — matches "1 Reviews" on reviewer cards
    # r'aria-label="[^"]*?(\d+)\s+[Rr]eview' — same issue
    # r'\((\d+)\)' — matches any parenthesized number in JS bundles
]

# ── Coordinate extraction from URL ──
_COORD_RE = re.compile(r'@(-?\d+\.\d+),(-?\d+\.\d+)')

# ── Place ID extraction from URL ──
_PLACE_ID_RE = re.compile(r'place_id[:=]([A-Za-z0-9_-]+)')
_PLACE_ID_1S_RE = re.compile(r'!1s(0x[a-fA-F0-9]+)')


def _clean(text: str) -> str:
    """Remove Google Maps unicode artifacts and strip whitespace."""
    return text.translate(_UNICODE_JUNK).strip()


def _is_closed(status: str) -> bool:
    """True if status indicates Temporarily or Permanently closed."""
    if not status or status == "N/A":
        return False
    s = status.lower()
    return "temporarily closed" in s or "permanently closed" in s


def extract_coords_from_url(url: str) -> tuple:
    """Extract (lat, lng) from a Google Maps URL, or (None, None)."""
    m = _COORD_RE.search(url)
    if m:
        return float(m.group(1)), float(m.group(2))
    return None, None


def extract_place_id_from_url(url: str) -> str:
    """Extract place_id from Google Maps URL (place_id:ChIJ... or !1s0x...)."""
    if not url:
        return "N/A"
    m = _PLACE_ID_RE.search(url)
    if m:
        return m.group(1)
    m = _PLACE_ID_1S_RE.search(url)
    if m:
        return m.group(1)
    return "N/A"


async def _fallback_rating(page: Page) -> str:
    """Fallback: get rating from an aria-label '<N> stars' SCOPED to F7nice's
    container — never the whole page. A page-wide query would catch nearby
    place cards' star labels and bleed their rating into the focused place
    (seen on Vakeel Kirana, where the panel has no rating but a sibling
    'M.A. Kirana Store' card has 4.7 stars)."""
    try:
        scoped = await page.evaluate(
            """() => {
                const f7 = document.querySelector('div.F7nice');
                if (!f7) return [];
                const scope = f7.parentElement || f7;
                const out = [];
                for (const el of scope.querySelectorAll('[aria-label*="star" i]')) {
                    out.push(el.getAttribute('aria-label') || '');
                }
                return out;
            }"""
        )
        for aria in scoped:
            m = re.search(r"(\d+\.?\d*)\s*star", aria or "", re.I)
            if m:
                val = float(m.group(1))
                if 1 <= val <= 5:
                    return m.group(1)
    except Exception:
        pass
    return "N/A"


async def _fallback_rating_from_html(page: Page) -> str:
    """Fallback: get rating from embedded JSON. Only fires when F7nice is
    missing ENTIRELY (different page structure, e.g. temporarily closed) —
    otherwise it would pick up nearby places' ratings out of APP_STATE."""
    try:
        f7_present = await page.evaluate(
            "() => !!document.querySelector('div.F7nice')"
        )
        if f7_present:
            return "N/A"
        html = await page.content()
        for pattern in [
            r'"ratingValue"\s*:\s*"?(\d+\.?\d*)"?',
            r'"rating"\s*:\s*(\d+\.?\d*)',
            r'"aggregateRating"[^}]*"ratingValue"\s*:\s*"?(\d+\.?\d*)"?',
            r'\["(\d+\.?\d*)","[^"]*stars?"\]',
            r'aria-label="[^"]*?(\d+\.?\d*)\s*star',
        ]:
            m = re.search(pattern, html)
            if m:
                val = float(m.group(1))
                if 1 <= val <= 5:
                    return m.group(1)
    except Exception:
        pass
    return "N/A"


async def _fallback_review_count(page: Page) -> str:
    """Python regex fallback on page HTML when JS strategies return 0."""
    try:
        html = await page.content()
        for pattern in _REVIEW_PATTERNS:
            matches = re.findall(pattern, html)
            for m in matches:
                num = int(m)
                if 1 <= num <= 99999:
                    return str(num)
    except Exception:
        pass
    return "0"


async def extract_share_link(page: Page) -> str:
    """Click the Share button, wait for the modal, read the short URL."""
    try:
        btn = await page.query_selector("button[data-value='Share'], button[aria-label='Share']")
        if not btn or not await btn.is_visible():
            return "N/A"

        await btn.click()
        try:
            inp = await page.wait_for_selector(
                "input[value*='maps.app.goo.gl']", timeout=5000
            )
            value = await inp.get_attribute("value") if inp else "N/A"
        except PlaywrightTimeout:
            value = "N/A"

        await page.keyboard.press("Escape")
        return value or "N/A"
    except Exception:
        try:
            await page.keyboard.press("Escape")
        except Exception:
            pass
        return "N/A"


async def extract_hours(page: Page) -> str:
    """Try to read opening hours from the place page."""
    try:
        for sel in (
            'div[aria-label*="hour" i]',
            'button[data-item-id*="oh"]',
            'div[class*="o0Svhf"]',
            'div[aria-label*="Open" i]',
            '[role="main"] button[aria-label*="hour" i]',
            'span.aSftqf',
            'span.fCEvvc',
        ):
            el = await page.query_selector(sel)
            if el:
                try:
                    aria = await el.get_attribute("aria-label")
                    if aria:
                        return _clean(aria)
                    text = await el.inner_text()
                    if text and len(text.strip()) > 2:
                        return _clean(text)
                except Exception:
                    continue
    except Exception:
        pass
    return "N/A"


async def extract_about(page: Page) -> str:
    """Try to read the About/description section."""
    try:
        about_el = await page.query_selector(
            'div[aria-label*="About"] p, '
            'div.WeS02d, '
            'div[class*="PYvSYb"]'
        )
        if about_el:
            text = await about_el.inner_text()
            if text and len(text.strip()) > 2:
                return _clean(text)
    except Exception:
        pass
    return "N/A"



async def extract_latest_review_date(page: Page) -> str:
    """
    Click Reviews tab, Sort > Newest, then read first review date.
    Default sort is 'Most relevant' — must sort by Newest to get latest.
    All fallback clicks use short timeouts (3s) to avoid 30s Playwright default hangs.
    """
    try:
        # 0. Wait for tab buttons to render (lazy-loaded on some pages)
        try:
            await page.wait_for_selector(
                'button[data-tab-index="1"], button[aria-label*="Reviews" i]',
                timeout=5000,
            )
        except Exception:
            pass

        # 1. Click Reviews tab. Prefer aria-label over data-tab-index because
        # tab indices vary by place: for places WITHOUT reviews,
        # data-tab-index="1" is the "About" tab, not Reviews — clicking it
        # would silently land on the wrong content and the rest of this
        # function would extract nothing.
        clicked_reviews = False
        for sel in [
            'button[aria-label^="Reviews for" i]',
            'button[aria-label*="Reviews" i][role="tab"]',
            'button[role="tab"][aria-label*="Reviews" i]',
        ]:
            tab = await page.query_selector(sel)
            if tab and await tab.is_visible():
                aria = (await tab.get_attribute("aria-label")) or ""
                if "review" in aria.lower():
                    await tab.click()
                    clicked_reviews = True
                    break
        if not clicked_reviews:
            # Tab-index fallbacks (only valid for places that DO have reviews).
            for sel in ['button[data-tab-index="1"]', 'button[data-tab-index="2"]']:
                tab = await page.query_selector(sel)
                if tab and await tab.is_visible():
                    aria = (await tab.get_attribute("aria-label")) or ""
                    if "review" in aria.lower():
                        await tab.click()
                        clicked_reviews = True
                        break
        if not clicked_reviews:
            try:
                await page.get_by_role("tab", name=re.compile(r"^Reviews", re.I)).first.click(timeout=3000)
                clicked_reviews = True
            except Exception:
                pass
        if not clicked_reviews:
            return "N/A"

        # Wait for reviews content to appear
        try:
            await page.wait_for_selector(
                'span.rsqaWe, div.jftiEf, button[aria-label*="Sort" i]',
                timeout=5000,
            )
        except Exception:
            await page.wait_for_timeout(1000)

        # 2. Click Sort > Newest (skip if Sort button doesn't exist — few reviews)
        sort_clicked = False
        sort_btn = await page.query_selector('button[aria-label*="Sort" i]')
        if sort_btn and await sort_btn.is_visible():
            await sort_btn.click()
            sort_clicked = True
        if not sort_clicked:
            try:
                await page.get_by_text("Sort", exact=False).first.click(timeout=2000)
                sort_clicked = True
            except Exception:
                pass
        if sort_clicked:
            await page.wait_for_timeout(500)
            # The Sort menu wraps the label text "Newest" inside a child div
            # (<div class="mLuXec">Newest</div>) that does NOT receive pointer
            # events — the outer <div role="menuitemradio"> does. Clicking the
            # text node fails with "intercepts pointer events". Target the
            # menuitemradio role directly.
            newest_clicked = False
            for sel in [
                'div[role="menuitemradio"]:has-text("Newest")',
                'li[role="menuitemradio"]:has-text("Newest")',
                'button[role="menuitemradio"]:has-text("Newest")',
                '[role="menuitemradio"][data-index="1"]',
            ]:
                try:
                    el = await page.query_selector(sel)
                    if el and await el.is_visible():
                        await el.click(timeout=2000)
                        newest_clicked = True
                        break
                except Exception:
                    continue
            if not newest_clicked:
                try:
                    await page.get_by_role("menuitemradio", name="Newest").first.click(timeout=2000)
                    newest_clicked = True
                except Exception:
                    pass
            if not newest_clicked:
                try:
                    await page.get_by_text("Newest", exact=True).first.click(timeout=2000, force=True)
                    newest_clicked = True
                except Exception:
                    pass
            if newest_clicked:
                await page.wait_for_timeout(1500)

        # 3. Extract first review date
        review_el = await page.query_selector("span.rsqaWe")
        if review_el:
            return _clean(await review_el.inner_text())
        for rev in await page.query_selector_all("div.jftiEf, div.GHT2ce"):
            for span in await rev.query_selector_all("span"):
                t = await span.inner_text()
                t = (t or "").strip()
                if re.search(
                    r"\d+\s+(day|week|month|year|hour|minute)s?\s+ago|a\s+(day|week|month|year)\s+ago",
                    t,
                    re.I,
                ):
                    return _clean(t)
    except Exception as e:
        logger.debug("extract_latest_review_date: %s", e)
    return "N/A"


async def extract_amenities(page: Page) -> str:
    """Parse service options / amenity chips (dine-in, takeout, etc.)."""
    try:
        chips = await page.query_selector_all(
            'div[aria-label*="Service options"] li, '
            'div[class*="qty3Ue"] span, '
            'div[data-attrid] span'
        )
        items = []
        for chip in chips:
            text = await chip.inner_text()
            text = _clean(text)
            if text and text not in items:
                items.append(text)
        if items:
            return "; ".join(items)
    except Exception:
        pass
    return "N/A"


async def extract_all(page: Page, url: str, config=None, place_id: Optional[str] = None, lean: bool = False, lean_enrich: bool = False, photo_urls: bool = False) -> PlaceData:
    """
    Orchestrator: extract all attributes from the current place page.

    For closed places (Temporarily/Permanently closed): only extracts name, place_id, status
    to speed up long scrapes. Full extraction for open places.

    lean=True: skip hours, about, amenities, share link (saves ~15ms/place).
    lean_enrich=True: extract rating, review_count, photo_count via JS + latest_review_date
                      via Playwright Reviews tab interaction (Sort by Newest).
    """
    place = PlaceData(source_url=url)

    # ── Place ID: from param, config (input CSV), or extract from URL ──
    if place_id:
        place.place_id = place_id
    elif config and getattr(config, "place_id", ""):
        place.place_id = config.place_id
    else:
        place.place_id = extract_place_id_from_url(url)
    if place.place_id == "N/A":
        place.place_id = extract_place_id_from_url(page.url)

    # ── Fast path: minimal extract for closed places (name + status only) ──
    try:
        minimal = await page.evaluate(MINIMAL_EXTRACT_JS)
        place.name = _clean(minimal.get("name", "N/A"))
        place.status = _clean(minimal.get("status", "Open"))
    except Exception as e:
        logger.debug("Minimal extract failed, continuing with full: %s", e)

    # ── Status fallback: check visible text (catches headless/limited view) ──
    if place.status == "Open":
        try:
            closed_status = await page.evaluate(STATUS_FALLBACK_JS)
            if closed_status:
                place.status = closed_status
        except Exception:
            pass

    if _is_closed(place.status):
        logger.debug("Skipping full extraction for closed place: %s", place.name)
        # Still extract lat/long for closed places (useful for mapping)
        try:
            coords = await page.evaluate(LATLONG_JS)
            if coords.get("latitude"):
                place.latitude = float(coords["latitude"])
                place.longitude = float(coords["longitude"])
        except Exception:
            pass
        return place

    # ── Phase 1: JS injection for static DOM reads ──
    if lean_enrich:
        # Lean enrich: rating, review_count, photo_count + status (latest_review_date via Playwright later)
        try:
            data = await page.evaluate(LEAN_ENRICH_JS)
        except Exception as e:
            logger.warning("Lean enrich JS extraction failed for %s: %s", url, e)
            return place

        place.name = _clean(data.get("name", "N/A"))
        place.rating = _clean(data.get("rating", "N/A"))
        place.review_count = data.get("review_count", "0")
        place.category = _clean(data.get("category", "N/A"))
        place.latest_review_date = _clean(data.get("latest_review_date", "N/A"))
        place.total_photos = data.get("total_photos", "0")
        place.status = _clean(data.get("status", "Open"))
        place.address = _clean(data.get("address", "N/A"))
        place.phone = _clean(data.get("phone", "N/A"))

        # Photo count (user-uploaded): scroll carousel, wait for images, count URLs
        if photo_urls:
            try:
                await page.evaluate("""
                    () => {
                        const main = document.querySelector('div[role="main"]');
                        if (main) {
                            main.querySelectorAll('div[style*="overflow"], div[class*="m6QErb"]').forEach(el => {
                                el.scrollLeft = el.scrollWidth;
                            });
                        }
                    }
                """)
                try:
                    await page.wait_for_selector(
                        'button[jsaction*="heroHeaderImage"] img, div[class*="ZKCDEc"] img, '
                        'img.iPQvYb, img[src*="googleusercontent"]',
                        timeout=2000,
                    )
                except Exception:
                    await page.wait_for_timeout(300)
                img_count = await page.evaluate("""
                    () => {
                        const imgs = document.querySelectorAll(
                            'button[jsaction*="heroHeaderImage"] img, ' +
                            'div[class*="ZKCDEc"] img, ' +
                            'div[aria-label*="Photo"] img, ' +
                            'img.iPQvYb'
                        );
                        const seen = new Set();
                        for (const img of imgs) {
                            const src = img.src || img.getAttribute('data-src') || '';
                            if (src && src.startsWith('http')) {
                                seen.add(src.split('=')[0]);
                            }
                        }
                        for (const div of document.querySelectorAll('div[style*="background-image"]')) {
                            const style = div.getAttribute('style') || '';
                            const m = style.match(/url\\(["']?(https?[^"')]+)["']?\\)/);
                            if (m) seen.add(m[1].split('=')[0]);
                        }
                        return seen.size;
                    }
                """)
                place.photo_count = str(img_count) if img_count > 0 else "0"
            except Exception:
                place.photo_count = "0"
    elif lean:
        # Lean: only 8 fields (name, rating, review_count, category, status, address, phone)
        try:
            data = await page.evaluate(LEAN_EXTRACT_JS)
        except Exception as e:
            logger.warning("Lean JS extraction failed for %s: %s", url, e)
            return place

        place.name = _clean(data.get("name", "N/A"))
        place.rating = _clean(data.get("rating", "N/A"))
        place.review_count = data.get("review_count", "0")
        place.category = _clean(data.get("category", "N/A"))
        place.status = _clean(data.get("status", "Open"))
        place.address = _clean(data.get("address", "N/A"))
        place.phone = _clean(data.get("phone", "N/A"))
    else:
        # Full: all 14 fields
        try:
            data = await page.evaluate(EXTRACT_JS)
        except Exception as e:
            logger.warning("JS extraction failed for %s: %s", url, e)
            return place

        place.name = _clean(data.get("name", "N/A"))
        place.rating = _clean(data.get("rating", "N/A"))
        if place.rating == "N/A":
            place.rating = await _fallback_rating(page)
        if place.rating == "N/A":
            place.rating = await _fallback_rating_from_html(page)
        place.review_count = data.get("review_count", "0")
        place.category = _clean(data.get("category", "N/A"))
        place.status = _clean(data.get("status", "Open"))
        place.address = _clean(data.get("address", "N/A"))
        place.phone = _clean(data.get("phone", "N/A"))
        place.website = data.get("website", "N/A")
        if place.website and place.website.startswith("/url?"):
            import urllib.parse
            try:
                parsed = urllib.parse.parse_qs(urllib.parse.urlparse(place.website).query)
                place.website = parsed.get("q", [place.website])[0] if parsed.get("q") else "N/A"
            except Exception:
                pass
        place.price_level = _clean(data.get("price_level", "N/A"))
        place.plus_code = _clean(data.get("plus_code", "N/A"))
        # image_urls must be assigned BEFORE the photo_count fallback below,
        # otherwise the fallback reads the default "N/A" and never triggers.
        place.image_urls = data.get("image_urls", "N/A")
        place.photo_count = data.get("photo_count", "0")
        if place.photo_count == "0" and place.image_urls and place.image_urls != "N/A":
            # aria-label count missing — fall back to the number of captured photo URLs
            place.photo_count = str(len([u for u in place.image_urls.split(";") if u.strip()]))
        place.total_photos = data.get("total_photos", "0")
        place.menu_link = data.get("menu_link", "N/A")
        place.latest_review_date = _clean(data.get("latest_review_date", "N/A"))

    # ── Phase 3: Python regex fallback for review count ──
    if place.review_count == "0":
        place.review_count = await _fallback_review_count(page)

    # ── Phase 3.5: Playwright path — always prefer Sort > Newest ──
    # EXTRACT_JS reads the first `span.rsqaWe` which is from Maps' default
    # "Most relevant" sort, NOT the newest review. For places with old
    # high-engagement reviews + recent low-engagement ones, this returned
    # a stale date (e.g. Sindhi Store: default sort first = "4 years ago"
    # but actual newest = "4 months ago"). To always get the newest, we open
    # the Reviews tab and explicitly Sort > Newest when reviews exist —
    # overwriting whatever EXTRACT_JS read.
    try:
        rc = int(place.review_count or "0")
    except (TypeError, ValueError):
        rc = 0
    if rc > 0:
        try:
            newest_date = await extract_latest_review_date(page)
            if newest_date and newest_date != "N/A":
                place.latest_review_date = newest_date
        except Exception:
            pass

    # ── Phase 4: Coordinates from APP_INITIALIZATION_STATE (instant) ──
    try:
        coords = await page.evaluate(LATLONG_JS)
        if coords.get("latitude"):
            place.latitude = float(coords["latitude"])
            place.longitude = float(coords["longitude"])
    except Exception:
        pass
    # Fallback: URL @lat,lng
    if not place.latitude:
        current_url = page.url
        place.latitude, place.longitude = extract_coords_from_url(current_url)

    # ── Phase 4.5: Fix category — reject UI buttons, phone numbers, URLs ──
    if True:
        cat = (place.category or "").strip()
        is_bad_cat = (
            cat.lower() in ("n/a", "")
            or any(s in cat.lower() for s in _BAD_CAT_PATTERNS)
            or re.match(r'^[\d\s\-\+\(\)]+$', cat)  # pure phone/digits
            or (len(cat) > 3 and sum(c.isdigit() for c in cat) > len(cat) * 0.5)  # mostly digits
            or ".com" in cat.lower()  # website leaked
        )
        if is_bad_cat:
            try:
                better = await page.evaluate(CATEGORY_FALLBACK_JS)
                if better:
                    place.category = _clean(better)
                else:
                    place.category = "N/A"
            except Exception:
                place.category = "N/A"

    # ── Phase 4.6: Fix rating 0.0 → N/A ──
    if place.rating in ("0.0", "0"):
        place.rating = "N/A"

    # ── Phase 5: Playwright interactions for dynamic content (skip in lean/lean_enrich) ──
    if not lean and not lean_enrich:
        place.hours = await extract_hours(page)
        place.about = await extract_about(page)
        place.amenities = await extract_amenities(page)

        # ── Phase 6: Optional share link ──
        if config and config.extract_share_link:
            place.share_link = await extract_share_link(page)

    return place
