"""
Production 2-phase pipeline: Phase 1 (extract + save CSV) + Phase 2 (download images).

Optimizations:
- Incremental CSV save after each place (Phase 1)
- Browser restart every N places to limit RAM growth
- Configurable delay between requests (avoid 429)
- Resume support: skip already-processed place_ids
- Closed places: minimal extract (name only) for speed
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import logging
import multiprocessing
import os
import re
import threading
import time
import zipfile
import requests
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from pathlib import Path
from datetime import datetime

MAX_RETRIES = 2
INITIAL_BACKOFF_MS = 1000
MAX_BACKOFF_MS = 5000

from playwright.async_api import async_playwright

from .poc_crawlee.extraction import extract_all, extract_place_id_from_url
from .poc_crawlee.models import PlaceData, ScrapeConfig
from .poc_crawlee.output import COLUMN_ORDER as _FULL_COLUMN_ORDER, append_csv_row

LEAN_COLUMN_ORDER = [
    "place_id", "name", "rating", "review_count", "category",
    "status", "address", "phone", "latitude", "longitude", "source_url",
]

LEAN_ENRICH_COLUMN_ORDER = [
    "place_id", "name", "rating", "review_count", "category",
    "status", "address", "phone", "latitude", "longitude",
    "latest_review_date", "photo_count", "total_photos", "source_url",
]

# Default: full columns. Overridden to lean at startup if --lean flag is set.
COLUMN_ORDER = _FULL_COLUMN_ORDER
from .gdrive_upload import find_or_create_folder, upload_and_cleanup, init_cache

DOWNLOAD_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://www.google.com/maps/",
}


def load_input_rows(path: str, url_column: str = "url", place_id_column: str = "place_id", limit: int = 0) -> list:
    """Load rows from CSV. Returns list of {url, place_id, label, ...}."""
    rows = []
    with open(path, encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for i, row in enumerate(reader):
            if limit and i >= limit:
                break
            url = (row.get(url_column) or row.get("url") or "").strip()
            if not url or ("google.com/maps" not in url and "goo.gl" not in url):
                continue
            place_id = (row.get(place_id_column) or row.get("place_id") or "").strip()
            if not place_id:
                place_id = extract_place_id_from_url(url)
            rows.append({"url": url, "place_id": place_id, "label": row.get("label", ""), **row})
    return rows


def append_failure(place_id: str, url: str, error: str, failures_path: str) -> None:
    """Append failed URL to phase1_failures.csv for manual retry."""
    Path(failures_path).parent.mkdir(parents=True, exist_ok=True)
    file_exists = Path(failures_path).exists()
    with open(failures_path, "a", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=["place_id", "url", "error", "timestamp"], extrasaction="ignore")
        if not file_exists:
            writer.writeheader()
        writer.writerow({
            "place_id": place_id,
            "url": url,
            "error": str(error)[:500],
            "timestamp": datetime.now().isoformat(),
        })


def get_processed_place_ids(csv_path: str) -> set:
    """Return set of place_ids already in the Phase 1 CSV (for resume)."""
    if not Path(csv_path).exists():
        return set()
    seen = set()
    with open(csv_path, encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            pid = (row.get("place_id") or "").strip()
            if pid:
                seen.add(pid)
    return seen



def _upscale_image_url(url: str) -> str:
    """Rewrite a Google Maps image URL to request a higher-resolution version.

    Handles two URL formats:
    1) Street View thumbnails: replace w=NNN&h=NNN with w=1600&h=1200
    2) googleusercontent.com: replace size suffix (=sNNN, =wNNN-hNNN, etc.) with =w1200-h1200
    """
    if "streetviewpixels" in url:
        # Street View thumbnail: rewrite w= and h= params
        url = re.sub(r'[&?]w=\d+', '&w=1600', url)
        url = re.sub(r'[&?]h=\d+', '&h=1200', url)
        return url
    if "googleusercontent" in url:
        # Strip existing size suffix after the last '=' and replace
        # Typical patterns: =s250, =w408-h240-k-no, =s1024-k-no
        url = re.sub(r'=(?:s\d+|w\d+(?:-h\d+)?(?:-[a-zA-Z0-9-]+)*)$', '=w1200-h1200', url)
        return url
    return url


def _download_one_image(args):
    """Download a single image. Returns (index, ext, data) or None on failure."""
    idx, url = args
    url = _upscale_image_url(url)  # Request high-res version
    try:
        r = requests.get(url, headers=DOWNLOAD_HEADERS, timeout=10, stream=True)
        if r.status_code != 200:
            return None
        ct = r.headers.get("Content-Type", "")
        ext = "jpg" if "jpeg" in ct or "jpg" in ct else "png"
        data = b"".join(r.iter_content(8192))
        return (idx, ext, data)
    except Exception:
        return None


def download_images(urls: list, place_id: str, folder: Path, max_threads: int = 16) -> int:
    """Download all images for a place concurrently. Saves into a ZIP archive.
    
    Creates {folder}/{place_id}.zip containing images as {place_id}_1.jpg, etc.
    Returns count of images saved.
    """
    folder.mkdir(parents=True, exist_ok=True)
    # Deduplicate by base URL
    seen_bases = set()
    unique = []
    for url in urls:
        if not url or ("googleusercontent" not in url and "streetview" not in url):
            continue
        base = url.split("=")[0] if "=" in url else url
        if base not in seen_bases:
            seen_bases.add(base)
            unique.append(url)
    if not unique:
        return 0
    # Download in parallel
    results = {}
    with ThreadPoolExecutor(max_workers=min(max_threads, len(unique))) as pool:
        futures = {pool.submit(_download_one_image, (i, url)): i for i, url in enumerate(unique)}
        for future in as_completed(futures):
            res = future.result()
            if res:
                results[res[0]] = (res[1], res[2])
    if not results:
        return 0
    # Write into ZIP archive
    zip_path = folder / f"{place_id}.zip"
    count = 0
    try:
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for i in sorted(results):
                ext, data = results[i]
                filename = f"{place_id}_{count + 1}.{ext}"
                zf.writestr(filename, data)
                count += 1
    except Exception:
        # Clean up partial zip on failure
        if zip_path.exists():
            zip_path.unlink()
        return 0
    return count


def _is_rate_limited(page_content: str) -> bool:
    """Check if page indicates rate limit / block.
    
    NOTE: Do NOT match bare '429' — Google Maps JS bundles contain '429'
    in URLs/class names on perfectly valid pages, causing false positives.
    """
    if not page_content:
        return False
    lower = page_content.lower()
    # Actual block page: contains "sorry" AND an indicator like "blocked" or "unusual traffic"
    if "sorry" in lower and ("blocked" in lower or "unusual traffic" in lower):
        return True
    # Explicit HTTP error text (not bare "429" which appears in JS bundles)
    if "too many requests" in lower:
        return True
    if "error 429" in lower or "status 429" in lower or "http 429" in lower:
        return True
    return False


async def _process_one_place(
    page,
    row: dict,
    csv_path: str,
    failures_path: str,
    delay_ms: int,
    csv_lock: asyncio.Lock,
    progress_cb=None,
    lean: bool = False,
    lean_enrich: bool = False,
    photo_urls: bool = False,
    result_cb=None,
) -> dict | None:
    """Process a single place. Returns result dict. Appends to CSV under lock."""
    url = row.get("url", "")
    place_id = row.get("place_id", "")
    h1_timeout = row.get("_h1_timeout", 0)  # 0 = no wait (fast), >0 = retry pass
    if not url:
        return None
    try:
        # Retry navigation up to 2 extra times for transient errors
        nav_error = None
        for _nav_attempt in range(3):
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=15000)
                nav_error = None
                break
            except Exception as e:
                err_str = str(e).lower()
                if "timeout" in err_str or "interrupted" in err_str or "navigation" in err_str:
                    nav_error = e
                    await page.wait_for_timeout(500 * (_nav_attempt + 1))
                    continue
                raise
        if nav_error:
            raise nav_error
        if h1_timeout > 0:
            try:
                await page.wait_for_selector('h1', timeout=h1_timeout)
            except Exception:
                pass
        content = await page.content()
        if _is_rate_limited(content):
            failed = {"place_id": place_id, "name": "FAILED", "source_url": url, "error": "Rate limited"}
            async with csv_lock:
                append_failure(place_id, url, "Rate limited", failures_path)
            return failed
        if not lean and not lean_enrich:
            await page.evaluate("""
                () => {
                    const main = document.querySelector('div[role="main"]');
                    if (main) {
                        main.querySelectorAll('div[style*="overflow"], div[class*="m6QErb"]').forEach(el => {
                            el.scrollLeft = el.scrollWidth;
                            el.scrollTop = el.scrollHeight;
                        });
                    }
                }
            """)
            try:
                await page.wait_for_selector(
                    'button[jsaction*="heroHeaderImage"] img, div[class*="ZKCDEc"] img, '
                    'img.iPQvYb, img[src*="googleusercontent"], img[src*="streetviewpixels"]',
                    timeout=2000
                )
            except Exception:
                await page.wait_for_timeout(300)

        cfg = ScrapeConfig(extract_share_link=False)
        place = await extract_all(page, url, cfg, place_id=place_id or None, lean=lean, lean_enrich=lean_enrich, photo_urls=photo_urls)
        place.label = row.get("label", "")
        place.source_url = url

        d = place.to_dict()
        name = (d.get("name") or "").strip()

        # Blank name = page didn't load -> don't write to CSV, return for retry
        if not name or name == "N/A":
            d["_blank"] = True
            if progress_cb:
                progress_cb(d.get("name", "?"))
            await page.wait_for_timeout(delay_ms)
            return d

        # Filter to only the columns we want (prevents extra fields leaking)
        cols = COLUMN_ORDER
        filtered = {k: d.get(k, "") for k in cols}
        async with csv_lock:
            append_csv_row(filtered, csv_path, fieldnames=cols)
        if result_cb:
            try:
                result_cb(filtered)
            except Exception:
                pass
        if progress_cb:
            progress_cb(d.get("name", "?"))
        await page.wait_for_timeout(delay_ms)
        return d
    except Exception as e:
        failed = {"place_id": place_id, "name": "FAILED", "source_url": url, "error": str(e)[:200]}
        async with csv_lock:
            append_failure(place_id, url, str(e)[:200], failures_path)
        if result_cb:
            try:
                result_cb(failed)
            except Exception:
                pass
        if progress_cb:
            progress_cb("FAILED")
        try:
            await page.wait_for_timeout(delay_ms)
        except Exception:
            await asyncio.sleep(delay_ms / 1000)
        return failed


async def _worker(
    worker_id: int,
    page,
    queue: asyncio.Queue,
    csv_path: str,
    failures_path: str,
    delay_ms: int,
    csv_lock: asyncio.Lock,
    results: list,
    progress_cb=None,
    lean: bool = False,
    lean_enrich: bool = False,
    photo_urls: bool = False,
    retry_queue: asyncio.Queue = None,
    result_cb=None,
):
    """Worker that pulls from queue and processes places."""
    while True:
        try:
            row = queue.get_nowait()
        except asyncio.QueueEmpty:
            break
        try:
            result = await _process_one_place(
                page, row, csv_path, failures_path, delay_ms, csv_lock, progress_cb,
                lean=lean, lean_enrich=lean_enrich, photo_urls=photo_urls,
                result_cb=result_cb,
            )
        except Exception:
            # Browser/page died (TargetClosedError etc) — stop this worker
            break
        if result:
            if result.get("_blank") and retry_queue is not None:
                await retry_queue.put(row)
            else:
                results.append(result)


async def run_phase1_batch(
    rows: list,
    csv_path: str,
    failures_path: str,
    config: ScrapeConfig,
    delay_ms: int = 2000,
    workers: int = 1,
    browsers: int = 1,
    contexts_per_browser: int = 1,
    progress_cb=None,
    lean: bool = False,
    lean_enrich: bool = False,
    photo_urls: bool = False,
    result_cb=None,
) -> list:
    """Run Phase 1 for a batch of rows.

    Multi-browser architecture: launches `browsers` separate Chromium processes,
    each with `contexts_per_browser` parallel contexts. Total parallelism =
    browsers × contexts_per_browser. Separate browser processes get their own
    CPU threads and network stacks, enabling near-linear scaling.
    """
    results = []
    Path(csv_path).parent.mkdir(parents=True, exist_ok=True)
    rows = [r for r in rows if r.get("url")]
    if not rows:
        return results

    total_workers = browsers * contexts_per_browser

    # Shared queue + lock across all browsers
    queue = asyncio.Queue()
    for r in rows:
        await queue.put(r)
    csv_lock = asyncio.Lock()
    done_count = [0]

    def on_done(name):
        done_count[0] += 1
        if progress_cb:
            progress_cb(done_count[0], name)

    # HTTP cache retained: enables reuse of Google Maps JS bundles across URLs,
    # saves ~8% per place vs the --disk-cache-size=0 setting tried previously.
    BROWSER_ARGS = [
        "--disable-dev-shm-usage",
        "--disable-gpu",
    ]

    if total_workers <= 1:
        # Sequential: 1 browser, 1 context
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, args=BROWSER_ARGS)
            try:
                context = await browser.new_context(
                    viewport={"width": 1920, "height": 1080},
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                )
                page = await context.new_page()
                for i, row in enumerate(rows):
                    cb = (lambda idx: lambda n: progress_cb(idx + 1, n) if progress_cb else None)(i)
                    r = await _process_one_place(
                        page, row, csv_path, failures_path, delay_ms, csv_lock, cb,
                        lean=lean, lean_enrich=lean_enrich, photo_urls=photo_urls,
                        result_cb=result_cb,
                    )
                    if r:
                        results.append(r)
            finally:
                await browser.close()
    else:
        # Multi-browser parallel: N browsers × M contexts each
        async def _run_browser_group(browser_id, num_contexts, pw):
            browser = await pw.chromium.launch(headless=True, args=BROWSER_ARGS)
            try:
                pages = []
                for i in range(num_contexts):
                    ctx = await browser.new_context(
                        viewport={"width": 1920, "height": 1080},
                        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                    )
                    page = await ctx.new_page()
                    pages.append(page)
                tasks = [
                    _worker(
                        f"{browser_id}-{i}", pages[i], queue, csv_path, failures_path,
                        delay_ms, csv_lock, results, on_done, lean=lean,
                        lean_enrich=lean_enrich, photo_urls=photo_urls, retry_queue=None,
                        result_cb=result_cb,
                    )
                    for i in range(len(pages))
                ]
                await asyncio.gather(*tasks)
            finally:
                await browser.close()

        async with async_playwright() as p:
            await asyncio.gather(*[
                _run_browser_group(b, min(contexts_per_browser, max(1, queue.qsize())), p)
                for b in range(browsers)
            ])

    return results


def _make_progress_fn(total, t0, stats, pass_label=""):
    """Create a progress bar callback. Uses stats['ok']+stats['fail'] for true progress."""
    def _progress(n, name):
        stats["done"] += 1
        # True progress = successfully written + failed (not blanks pending retry)
        true_done = stats["ok"] + stats["fail"]
        elapsed = time.time() - t0
        rate = stats["done"] / elapsed * 60 if elapsed > 1 else 0
        remaining = total - true_done
        eta_min = remaining / rate * 60 / 60 if rate > 0 else 0
        pct = min(true_done / total * 100, 100)
        elapsed_str = f"{int(elapsed//3600)}h{int(elapsed%3600//60)}m" if elapsed >= 3600 else f"{int(elapsed//60)}m{int(elapsed%60)}s"
        eta_str = f"{int(eta_min//60)}h{int(eta_min%60)}m" if eta_min >= 60 else f"{int(eta_min)}m"
        bar_len = 30
        filled = min(int(bar_len * true_done / total), bar_len)
        bar = "#" * filled + "-" * (bar_len - filled)
        print(f"\r  [{bar}] {pct:5.1f}% | OK:{stats['ok']} FAIL:{stats['fail']} | {remaining} left | {rate:.0f}/min | {elapsed_str} elapsed | ETA {eta_str} {pass_label}  ", end="", flush=True)
    return _progress


async def _run_phase1_async(
    pending: list,
    csv_path: str,
    failures_path: str,
    config: ScrapeConfig,
    batch_size: int,
    delay_ms: int,
    workers: int = 1,
    browsers: int = 1,
    contexts_per_browser: int = 1,
    lean: bool = False,
    lean_enrich: bool = False,
    photo_urls: bool = False,
    result_cb=None,
) -> tuple:
    """
    Async Phase 1 — 3-pass extraction:
      Pass 1: Fast (no h1 wait) — most pages load instantly
      Pass 2: Fast retry for blanks from pass 1
      Pass 3: Slow retry with 3s h1 timeout for remaining blanks
    """
    total = len(pending)
    t0 = time.time()
    stats = {"done": 0, "ok": 0, "fail": 0}

    async def _run_pass(rows_to_process, pass_label, h1_timeout, num_browsers, num_contexts):
        """Run one pass through a set of rows. Returns list of blank-name rows for retry."""
        if not rows_to_process:
            return []
        # Tag rows with h1 timeout
        for r in rows_to_process:
            r["_h1_timeout"] = h1_timeout

        progress_fn = _make_progress_fn(total, t0, stats, pass_label=pass_label)
        blanks = []

        def progress_with_stats(n, name):
            # Update ok/fail in real-time for progress bar
            if name and name != "?" and name != "FAILED" and name != "N/A":
                stats["ok"] += 1
            elif name == "FAILED":
                stats["fail"] += 1
            progress_fn(n, name)

        for start in range(0, len(rows_to_process), batch_size):
            batch = rows_to_process[start : start + batch_size]
            results = await run_phase1_batch(
                batch, csv_path, failures_path, config,
                delay_ms=delay_ms, browsers=num_browsers,
                contexts_per_browser=num_contexts,
                progress_cb=progress_with_stats, lean=lean,
                lean_enrich=lean_enrich, photo_urls=photo_urls,
                result_cb=result_cb,
            )
            for r in results:
                if r.get("_blank"):
                    blanks.append({"place_id": r.get("place_id", ""), "url": r.get("source_url", "")})
        return blanks

    total_batches = (total + batch_size - 1) // batch_size
    total_workers = browsers * contexts_per_browser
    retry_path = csv_path.replace(".csv", "_retry.txt")
    print(f"  {total} places | {total_batches} batches | {browsers} browsers × {contexts_per_browser} contexts = {total_workers} workers\n")

    def _save_retry_list(blanks, label):
        """Write pending retry place_ids to file for visibility."""
        with open(retry_path, "w", encoding="utf-8") as f:
            f.write(f"# {label}: {len(blanks)} place_ids pending retry\n")
            for r in blanks:
                f.write(f"{r.get('place_id','')},{r.get('url','')}\n")

    # ── Pass 1: Fast (1s h1 wait) ──
    blanks1 = await _run_pass(pending, "[Pass 1]", 1000, browsers, contexts_per_browser)

    # ── Pass 2: Fast retry ──
    if blanks1:
        _save_retry_list(blanks1, "After Pass 1")
        print(f"\n  Retry {len(blanks1)} blanks... (logged to {retry_path})")
        retry_browsers = min(browsers, max(1, len(blanks1) // contexts_per_browser))
        blanks2 = await _run_pass(blanks1, "[Retry 1]", 1000, retry_browsers, contexts_per_browser)
    else:
        blanks2 = []

    # ── Pass 3: Slow retry with 3s h1 timeout ──
    if blanks2:
        _save_retry_list(blanks2, "After Retry 1")
        print(f"\n  Retry {len(blanks2)} remaining (3s h1 wait)... (logged to {retry_path})")
        blanks3 = await _run_pass(blanks2, "[Retry 2]", 3000, min(2, len(blanks2)), 1)
    else:
        blanks3 = []

    # Still blank after 3 passes -> write as FAILED
    if blanks3:
        _save_retry_list(blanks3, "FINAL - failed after 3 passes")
        csv_lock = asyncio.Lock()
        for r in blanks3:
            pid = r.get("place_id", "")
            url = r.get("url", "")
            failed = {"place_id": pid, "name": "FAILED", "source_url": url, "status": "Removed/Not found"}
            async with csv_lock:
                append_csv_row(failed, csv_path, fieldnames=COLUMN_ORDER)
                append_failure(pid, url, "Blank after 3 passes", failures_path)
            stats["fail"] += 1
            stats["done"] += 1
        print(f"\n  {len(blanks3)} places failed after 3 passes -> {retry_path}")
    else:
        # All retries resolved — clean up retry file
        if Path(retry_path).exists():
            Path(retry_path).unlink()

    elapsed = time.time() - t0
    rate = (stats["ok"] + stats["fail"]) / elapsed * 60 if elapsed > 0 else 0
    elapsed_str = f"{int(elapsed//3600)}h{int(elapsed%3600//60)}m{int(elapsed%60)}s" if elapsed >= 3600 else f"{int(elapsed//60)}m{int(elapsed%60)}s"
    print(f"\n\n{'=' * 60}")
    print(f"  Phase 1 Complete in {elapsed_str}")
    print(f"  OK: {stats['ok']} | FAIL: {stats['fail']} | {rate:.0f} places/min")
    if blanks3:
        print(f"  Retry file: {retry_path} ({len(blanks3)} place_ids)")
    print(f"{'=' * 60}")
    return stats["ok"], stats["fail"]


def _phase1_worker(args_tuple: tuple) -> None:
    """Worker for process pool: runs Phase 1 for a chunk in separate process."""
    try:
        chunk_rows, csv_path, failures_path, batch_size, delay_ms, browsers, contexts_per_browser, lean, lean_enrich, photo_urls, col_order = args_tuple
        # Propagate COLUMN_ORDER to child process (globals don't cross process boundary)
        global COLUMN_ORDER
        COLUMN_ORDER = col_order
        Path(csv_path).parent.mkdir(parents=True, exist_ok=True)
        config = ScrapeConfig(extract_share_link=False)
        asyncio.run(_run_phase1_async(
            chunk_rows, csv_path, failures_path, config, batch_size, delay_ms,
            browsers=browsers, contexts_per_browser=contexts_per_browser,
            lean=lean, lean_enrich=lean_enrich, photo_urls=photo_urls,
        ))
    except Exception as e:
        print(f"  WORKER ERROR: {e}")
        raise


async def run_phase1(
    rows: list,
    csv_path: str,
    failures_path: str,
    config: ScrapeConfig,
    batch_size: int = 50,
    delay_ms: int = 2000,
    process_batch_size: int = 0,
    browsers: int = 1,
    contexts_per_browser: int = 1,
    resume: bool = True,
    lean: bool = False,
    lean_enrich: bool = False,
    photo_urls: bool = False,
    result_cb=None,
) -> tuple:
    """
    Phase 1: Extract data, save to CSV incrementally.
    If process_batch_size > 0: run each mega-batch in separate process (for 150K+ URLs).
    """
    Path(csv_path).parent.mkdir(parents=True, exist_ok=True)
    processed = get_processed_place_ids(csv_path) if resume else set()
    pending = [r for r in rows if r.get("place_id") not in processed]
    if not pending:
        print("Phase 1: All places already processed (resume).")
        return 0, 0

    total = len(pending)
    total_success = 0
    total_fail = 0

    if process_batch_size > 0:
        # Process pooling: each chunk runs in separate process, exits to free memory
        chunks = []
        for start in range(0, len(pending), process_batch_size):
            chunks.append(pending[start : start + process_batch_size])
        for i, chunk in enumerate(chunks):
            print(f"\n[Phase 1] Process batch {i + 1}/{len(chunks)} ({len(chunk)} places)...")
            args = (chunk, csv_path, failures_path, batch_size, delay_ms, browsers, contexts_per_browser, lean, lean_enrich, photo_urls, COLUMN_ORDER)
            proc = multiprocessing.Process(target=_phase1_worker, args=(args,))
            proc.start()
            proc.join()
            if proc.exitcode != 0:
                print(f"  WARNING: Process exited with code {proc.exitcode}")
            print("  Process exited (memory freed).")
        total_success, total_fail = get_phase1_stats(csv_path)
    else:
        total_success, total_fail = await _run_phase1_async(
            pending, csv_path, failures_path, config, batch_size, delay_ms,
            browsers=browsers, contexts_per_browser=contexts_per_browser,
            lean=lean, lean_enrich=lean_enrich, photo_urls=photo_urls,
            result_cb=result_cb,
        )

    return total_success, total_fail


# Sidecar tracking: thread-safe append-only log for crash-proof progress
_sidecar_lock = threading.Lock()


def _append_sidecar(sidecar_path: Path, place_id: str):
    """Append a place_id to the sidecar log (one ID per line, crash-safe)."""
    with _sidecar_lock:
        with open(sidecar_path, "a", encoding="utf-8") as f:
            f.write(place_id + "\n")
            f.flush()
            os.fsync(f.fileno())  # Force write to disk


def _load_sidecar(sidecar_path: Path) -> set:
    """Load completed place_ids from the sidecar log."""
    if not sidecar_path.exists():
        return set()
    with open(sidecar_path, encoding="utf-8") as f:
        return {line.strip() for line in f if line.strip()}


def _download_place_images(args):
    """Worker: download images for one place. Returns (place_id, name, count, uploaded)."""
    place_id, image_urls_str, name, images_folder = args[:4]
    gdrive_config = args[4] if len(args) > 4 else None
    sidecar_path = args[5] if len(args) > 5 else None
    urls = [u.strip() for u in image_urls_str.split(";") if u.strip()]
    n = download_images(urls, place_id, images_folder)
    uploaded = False
    if n > 0 and gdrive_config:
        zip_path = images_folder / f"{place_id}.zip"
        if zip_path.exists():
            uploaded = upload_and_cleanup(
                zip_path,
                gdrive_config["run_folder_id"],
                gdrive_config["credentials_path"],
            )
    # Write to sidecar immediately (crash-proof: survives process kill)
    if n > 0 and sidecar_path:
        _append_sidecar(sidecar_path, place_id)
    return (place_id, name, n, uploaded)


def _update_csv_image_status(csv_path: str, successful_ids: set):
    """
    Single-pass CSV rewrite: set image_downloaded=1 for all place_ids in successful_ids.
    Reads entire CSV into memory, updates the column, writes it back atomically.
    """
    if not successful_ids:
        return
    rows = []
    fieldnames = None
    with open(csv_path, encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        fieldnames = list(reader.fieldnames or [])
        # Ensure image_downloaded column exists
        if "image_downloaded" not in fieldnames:
            idx = fieldnames.index("source_url") if "source_url" in fieldnames else len(fieldnames)
            fieldnames.insert(idx, "image_downloaded")
        for row in reader:
            pid = (row.get("place_id") or "").strip()
            if pid in successful_ids:
                row["image_downloaded"] = "1"
            elif not row.get("image_downloaded"):
                row["image_downloaded"] = "0"
            rows.append(row)

    # Atomic write: write to temp file, then replace
    tmp_path = csv_path + ".tmp"
    with open(tmp_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    Path(tmp_path).replace(csv_path)


def _upload_sweep(images_folder: Path, folder_id: str, credentials_path: str,
                   upload_workers: int = 8, max_upload_workers: int = 512):
    """Upload any local ZIPs not yet on Drive using adaptive concurrency."""
    from gdrive_upload import _drive_cache

    if not _drive_cache:
        return

    local_zips = [
        p for p in images_folder.iterdir()
        if p.is_file() and p.suffix.lower() == ".zip"
        and not _drive_cache.exists(p.name)
    ] if images_folder.exists() else []

    if not local_zips:
        print("  All ZIPs already uploaded to Drive.")
        return

    print(f"\n[Upload Sweep] {len(local_zips)} ZIPs on disk but not on Drive.")
    uploader = AdaptiveUploader(
        sorted(local_zips), folder_id, credentials_path,
        initial_workers=upload_workers,
        max_workers=max_upload_workers,
    )
    uploader.run()


def run_phase2(csv_path: str, images_folder: Path, resume: bool = True, place_workers: int = 32,
               gdrive_folder_id: str | None = None, credentials_path: str | None = None,
               upload_workers: int = 8, max_upload_workers: int = 512) -> int:
    """
    Phase 2: Read Phase 1 CSV, download images concurrently across places.
    Hybrid resume: uses image_downloaded column (fast) + filesystem scan (crash recovery).
    After downloads, updates the CSV with image_downloaded=1 for successful places.
    """
    if not Path(csv_path).exists():
        print("Phase 2: No Phase 1 CSV found.")
        return 0

    images_folder.mkdir(parents=True, exist_ok=True)

    # Sidecar log: load any place_ids completed in a previous crashed run
    sidecar_path = Path(csv_path).parent / "completed_ids.log"
    sidecar_ids = set()
    if resume:
        sidecar_ids = _load_sidecar(sidecar_path)
        if sidecar_ids:
            print(f"  Recovered {len(sidecar_ids)} places from sidecar log (crash recovery).")

    # Filesystem scan: detect images per place_id (crash recovery fallback)
    # Uses os.scandir for speed; ZIPs are checked by existence only (no open/read)
    fs_counts = {}
    if resume:
        print("[Phase 2] Scanning filesystem for crash recovery...")
        with os.scandir(images_folder) as entries:
            for entry in entries:
                if not entry.is_file():
                    continue
                name_lower = entry.name.lower()
                # ZIP archives: existence = download complete (ZIP is created atomically)
                if name_lower.endswith(".zip"):
                    pid = Path(entry.name).stem
                    fs_counts[pid] = 999  # sentinel: ZIP exists = all images present
                    continue
                # Loose image files
                stem = Path(entry.name).stem
                last_under = stem.rfind("_")
                if last_under == -1:
                    continue
                pid = stem[:last_under]
                fs_counts[pid] = fs_counts.get(pid, 0) + 1
        if fs_counts:
            print(f"  Found {len(fs_counts)} places with images on disk.")

    rows = []
    skipped_csv = 0
    skipped_fs = 0
    fs_recovered_ids = set()  # places found on disk but not marked in CSV
    with open(csv_path, encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row.get("name") == "FAILED":
                continue
            place_id = (row.get("place_id") or "").strip()
            image_urls = (row.get("image_urls") or "").strip()
            if not place_id or not image_urls or image_urls == "N/A":
                continue
            url_count = len([u for u in image_urls.split(";") if u.strip()])
            if resume:
                # Primary: CSV column check (instant)
                if row.get("image_downloaded", "0").strip() == "1":
                    skipped_csv += 1
                    continue
                # Sidecar log check (crash recovery — previous run completed this place)
                if place_id in sidecar_ids:
                    skipped_fs += 1
                    fs_recovered_ids.add(place_id)
                    continue
                # Fallback: filesystem check (crash recovery)
                if fs_counts.get(place_id, 0) >= url_count:
                    skipped_fs += 1
                    fs_recovered_ids.add(place_id)
                    continue
            rows.append((place_id, image_urls, row.get("name", "?")))

    total = len(rows) + skipped_csv + skipped_fs
    pending = len(rows)
    print(f"[Phase 2] Total: {total} places with image URLs.")
    if skipped_csv:
        print(f"  Skipping {skipped_csv} already downloaded (image_downloaded=1).")
    if skipped_fs:
        print(f"  Recovered {skipped_fs} from filesystem (images exist, CSV not updated).")
    print(f"  Pending: {pending} places need image downloads.")

    if pending == 0:
        # Still update CSV for filesystem-recovered places
        if fs_recovered_ids:
            print(f"  Updating CSV for {len(fs_recovered_ids)} filesystem-recovered places...")
            _update_csv_image_status(csv_path, fs_recovered_ids)
        print("  Nothing to download.")
        # Upload sweep: check for local ZIPs not yet on Drive
        if gdrive_folder_id and credentials_path:
            csv_basename = Path(csv_path).stem
            try:
                run_folder_id = find_or_create_folder(credentials_path, gdrive_folder_id, csv_basename)
                init_cache(credentials_path, run_folder_id)
                _upload_sweep(images_folder, run_folder_id, credentials_path,
                              upload_workers, max_upload_workers)
            except Exception as e:
                print(f"  WARNING: Upload sweep failed: {e}")
        return 0

    total_downloaded = 0
    total_uploaded = 0
    done = 0
    success = 0
    failed = 0
    successful_ids = set()

    # Google Drive upload config
    gdrive_config = None
    if gdrive_folder_id and credentials_path:
        # Use input CSV name as Drive folder name (e.g. 'input_partA')
        csv_basename = Path(csv_path).stem
        print(f"[Phase 2] Google Drive upload enabled. Folder: {csv_basename}")
        try:
            run_folder_id = find_or_create_folder(credentials_path, gdrive_folder_id, csv_basename)
            gdrive_config = {
                "run_folder_id": run_folder_id,
                "credentials_path": credentials_path,
            }
            print(f"  Run folder created on Drive (ID: {run_folder_id})")
            print(f"  ZIPs will be uploaded and deleted locally after each place.")
            # Load all existing filenames into cache (one API call, then O(1) checks)
            init_cache(credentials_path, run_folder_id)
        except Exception as e:
            print(f"  WARNING: Failed to create Drive folder: {e}")
            print(f"  Continuing without upload (ZIPs will be kept locally).")

    args_list = [(pid, urls, name, images_folder, gdrive_config, sidecar_path) for pid, urls, name in rows]

    t0 = time.time()

    with ThreadPoolExecutor(max_workers=place_workers) as pool:
        futures = {pool.submit(_download_place_images, a): a[2] for a in args_list}
        for future in as_completed(futures):
            place_id, name, count, uploaded = future.result()
            done += 1
            if count > 0:
                total_downloaded += count
                success += 1
                successful_ids.add(place_id)
                if uploaded:
                    total_uploaded += 1
            else:
                failed += 1
            # Progress bar
            upload_str = f" | {total_uploaded} uploaded" if gdrive_config else ""
            _print_progress(done, pending, t0, f" | {total_downloaded} imgs{upload_str}")

    # Final newline after progress bar
    print()

    # Update CSV: mark successful + filesystem-recovered places as image_downloaded=1
    all_done_ids = successful_ids | fs_recovered_ids
    if all_done_ids:
        print(f"  Updating CSV with {len(all_done_ids)} places (image_downloaded=1)...")
        _update_csv_image_status(csv_path, all_done_ids)
    # Delete sidecar log after successful CSV reconciliation
    if sidecar_path.exists():
        sidecar_path.unlink()
        print(f"  Sidecar log reconciled and deleted.")

    # Upload sweep: catch any ZIPs that failed inline upload
    if gdrive_config:
        _upload_sweep(images_folder, gdrive_config["run_folder_id"],
                      gdrive_config["credentials_path"],
                      upload_workers, max_upload_workers)

    elapsed = time.time() - t0
    print(f"\n  ── Phase 2 Summary ──")
    print(f"  OK: {success} places downloaded ({total_downloaded} images)")
    if gdrive_config:
        print(f"  ☁ {total_uploaded} places uploaded to Google Drive (inline)")
        upload_failed = success - total_uploaded
        if upload_failed > 0:
            print(f"  ⚠ {upload_failed} places failed inline upload (handled by sweep)")
    if failed:
        print(f"  FAIL: {failed} places had 0 downloadable images (broken/expired URLs)")
    if skipped_fs:
        print(f"  ⚡ {skipped_fs} places recovered from filesystem")
    print(f"  ⏱ Completed in {int(elapsed // 60)}m {int(elapsed % 60)}s")
    return total_downloaded


def _print_progress(done: int, total: int, t0: float, extra: str = ""):
    """Print a progress bar with ETA."""
    pct = int(done * 100 / total) if total > 0 else 100
    bar_len = 30
    filled = int(bar_len * done / total) if total > 0 else bar_len
    bar = "#" * filled + "-" * (bar_len - filled)
    elapsed = time.time() - t0
    rate = done / elapsed if elapsed > 0 else 0
    eta = int((total - done) / rate) if rate > 0 else 0
    eta_str = f"{eta // 60}m{eta % 60:02d}s" if eta >= 60 else f"{eta}s"
    print(f"\r  [{bar}] {pct}% ({done}/{total}){extra} | ETA: {eta_str}  ", end="", flush=True)


def _compress_worker(args):
    """Worker: compress loose images into a zip and delete originals."""
    pid, files, images_folder = args
    zip_path = images_folder / f"{pid}.zip"
    try:
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for filename, full_path in sorted(files):
                zf.write(full_path, filename)
        # Verify ZIP integrity before deleting originals
        with zipfile.ZipFile(zip_path, "r") as zf:
            bad = zf.testzip()
            if bad is not None:
                raise RuntimeError(f"ZIP verification failed: corrupt entry '{bad}'")
        for _, full_path in files:
            full_path.unlink()
        return (pid, len(files), zip_path, None)
    except Exception as e:
        if zip_path.exists():
            zip_path.unlink()
        return (pid, -1, None, str(e))


def compress_existing_images(images_folder: Path, workers: int = None) -> tuple:
    """Migrate loose image files into ZIP archives (concurrently).

    Uses ProcessPoolExecutor for true parallelism (ZIP compression is CPU-bound).
    """
    if not images_folder.exists():
        print("No images folder found.")
        return 0, 0, []

    if workers is None:
        workers = min(multiprocessing.cpu_count(), 16)

    # Group loose images by place_id (os.scandir is more memory-efficient)
    place_images = {}
    with os.scandir(images_folder) as entries:
        for entry in entries:
            if not entry.is_file() or entry.name.lower().endswith(".zip"):
                continue
            fname = Path(entry.name).stem
            last_under = fname.rfind("_")
            if last_under == -1:
                continue
            pid = fname[:last_under]
            place_images.setdefault(pid, []).append(
                (entry.name, Path(entry.path))
            )

    if not place_images:
        print("No loose images found to compress.")
        return 0, 0, []

    total_places = 0
    total_images = 0
    generated_zips = []
    failed = 0
    total_to_process = len(place_images)

    print(f"\n[Compress] Grouped loose images into {total_to_process} places.")
    args_list = [(pid, files, images_folder) for pid, files in place_images.items()]

    t0 = time.time()
    done = 0

    with ProcessPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_compress_worker, a): a[0] for a in args_list}
        for future in as_completed(futures):
            pid, count, zip_path, error = future.result()
            done += 1
            if count > 0:
                total_places += 1
                total_images += count
                generated_zips.append(zip_path)
            else:
                failed += 1
                logging.error(f"Compression failed for {pid}: {error}")

            _print_progress(done, total_to_process, t0, f" | {total_images} imgs")

    print()
    if failed:
        print(f"  FAIL: {failed} places failed compression.")
    print(f"  OK: {total_places} places compressed ({total_images} images).")
    return total_places, total_images, generated_zips

def _upload_worker_adaptive(zpath, folder_id, credentials_path, error_event, semaphore, drive_cache=None):
    """Worker: upload a zip to Google Drive with adaptive concurrency support.

    Signals errors via error_event so the controller can scale down.
    Detects failures from both exceptions AND False returns (upload_and_cleanup
    catches errors internally and returns False).
    Skips files already on Drive (checked via cache, no API call).
    Uses semaphore to respect the current concurrency limit.
    """
    # Skip if already on Drive (O(1) cache lookup, no API call, no semaphore needed)
    if drive_cache and drive_cache.exists(zpath.name):
        # Clean up orphaned local file (upload succeeded previously but delete failed)
        try:
            zpath.unlink()
        except OSError:
            pass
        return (zpath.name, True, True)  # (name, success, skipped)

    semaphore.acquire()
    try:
        for attempt in range(MAX_RETRIES):
            try:
                result = upload_and_cleanup(zpath, folder_id, credentials_path)
                if result:
                    return (zpath.name, True, False)
                # upload_and_cleanup returned False — it caught the error internally
                error_event.set()
                logging.error(
                    f"Upload returned False for {zpath.name} (attempt {attempt + 1}/{MAX_RETRIES})"
                )
            except Exception as e:
                error_event.set()
                logging.error(
                    f"Upload exception for {zpath.name} (attempt {attempt + 1}/{MAX_RETRIES}): {e}"
                )
            # Backoff before retry
            if attempt < MAX_RETRIES - 1:
                backoff = min(INITIAL_BACKOFF_MS * (2 ** attempt), MAX_BACKOFF_MS) / 1000
                time.sleep(backoff)
        return (zpath.name, False, False)
    finally:
        semaphore.release()


def _upload_worker(args):
    """Worker: upload a zip file to Google Drive and delete it locally.

    Retries with exponential backoff on transient failures.
    """
    zpath, folder_id, credentials_path = args
    for attempt in range(MAX_RETRIES):
        try:
            if upload_and_cleanup(zpath, folder_id, credentials_path):
                return True
        except Exception as e:
            backoff = min(INITIAL_BACKOFF_MS * (2 ** attempt), MAX_BACKOFF_MS) / 1000
            logging.error(
                f"Upload failed for {zpath.name} (attempt {attempt + 1}/{MAX_RETRIES}): {e}"
            )
            if attempt < MAX_RETRIES - 1:
                time.sleep(backoff)
    return False


class AdaptiveUploader:
    """Uploads ZIP files to Google Drive with auto-scaling concurrency.

    Strategy (AIMD with learned ceiling):
    - Starts at `initial_workers` concurrent uploads.
    - Every `PROBE_INTERVAL` consecutive successes, adds `SCALE_UP_STEP` workers
      (additive increase), up to the effective ceiling.
    - On any error, immediately halves workers (multiplicative decrease).
    - Tracks error counts per worker-count zone. If the same zone causes errors
      `CEILING_STRIKE_LIMIT` times, the ceiling is permanently locked just below
      that zone and autoscaling stops — no more wasted retries.
    - Uses a threading.Semaphore to dynamically throttle in-flight uploads
      without tearing down / recreating the thread pool.
    """

    PROBE_INTERVAL = 20        # successes between scale-up attempts
    SCALE_UP_STEP = 4          # add N workers on scale-up
    SCALE_DOWN_FACTOR = 0.5    # halve on error
    MIN_WORKERS = 2
    CEILING_STRIKE_LIMIT = 2   # errors at same zone before locking ceiling
    ZONE_BUCKET_SIZE = 4       # group worker counts into zones (e.g. 12-15 = zone 3)

    def __init__(self, zip_files, folder_id, credentials_path,
                 initial_workers=4, max_workers=64):
        self.zip_files = zip_files
        self.folder_id = folder_id
        self.credentials_path = credentials_path
        self.max_workers = max_workers
        self.current_workers = min(initial_workers, max_workers)

        # Semaphore controls how many uploads run concurrently
        self._semaphore = threading.Semaphore(self.current_workers)
        self._error_event = threading.Event()
        self._lock = threading.Lock()

        # Stats
        self.uploaded = 0
        self.failed = 0
        self.done = 0
        self.total = len(zip_files)
        self._successes_since_change = 0  # successes since last scale change

        # Learned ceiling: tracks error strikes per worker-count zone
        self._zone_strikes = {}  # {zone_id: strike_count}
        self._ceiling = max_workers  # effective ceiling (lowered when learned)
        self._ceiling_locked = False

    def _worker_zone(self, worker_count):
        """Map a worker count to a zone bucket for strike tracking."""
        return worker_count // self.ZONE_BUCKET_SIZE

    def _record_error_zone(self):
        """Record a strike for the current worker-count zone.

        If the same zone hits CEILING_STRIKE_LIMIT, lock the ceiling
        just below this zone so autoscaling never wastes cycles here again.
        """
        zone = self._worker_zone(self.current_workers)
        self._zone_strikes[zone] = self._zone_strikes.get(zone, 0) + 1
        strikes = self._zone_strikes[zone]

        if strikes >= self.CEILING_STRIKE_LIMIT and not self._ceiling_locked:
            # Lock ceiling to the bottom of this zone (the last stable level)
            new_ceiling = max(zone * self.ZONE_BUCKET_SIZE - 1, self.MIN_WORKERS)
            self._ceiling = new_ceiling
            self._ceiling_locked = True
            logging.info(
                f"[Autoscale] CEILING LOCKED at {new_ceiling} workers "
                f"(zone {zone} failed {strikes} times — autoscaling stopped)"
            )
            print(f"\n  [Autoscale] Ceiling locked at {new_ceiling} workers "
                  f"(repeated errors at ~{zone * self.ZONE_BUCKET_SIZE}+)")

    def _scale_up(self):
        """Additive increase: release extra semaphore permits."""
        with self._lock:
            if self._ceiling_locked:
                return
            effective_max = min(self.max_workers, self._ceiling)
            new = min(self.current_workers + self.SCALE_UP_STEP, effective_max)
            added = new - self.current_workers
            if added <= 0:
                return
            for _ in range(added):
                self._semaphore.release()
            old = self.current_workers
            self.current_workers = new
            self._successes_since_change = 0
            logging.info(f"[Autoscale] UP {old} -> {new} workers")

    def _scale_down(self):
        """Multiplicative decrease: drain semaphore permits and learn ceiling."""
        with self._lock:
            self._record_error_zone()

            new = max(int(self.current_workers * self.SCALE_DOWN_FACTOR),
                      self.MIN_WORKERS)
            removed = self.current_workers - new
            if removed <= 0:
                self._error_event.clear()
                return
            # Drain permits — acquire without blocking (best-effort)
            drained = 0
            for _ in range(removed):
                got = self._semaphore.acquire(blocking=False)
                if got:
                    drained += 1
            old = self.current_workers
            self.current_workers = new
            self._successes_since_change = 0
            self._error_event.clear()
            logging.info(f"[Autoscale] DOWN {old} -> {new} workers (drained {drained} permits)")

    def run(self):
        """Execute all uploads with adaptive concurrency. Returns (uploaded, failed)."""
        from gdrive_upload import _drive_cache

        t0 = time.time()
        skipped = 0

        print(f"  [Autoscale] Starting at {self.current_workers} workers, "
              f"max {self.max_workers} (AIMD + learned ceiling)")

        # Submit all tasks up front — semaphore throttles concurrency
        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            futures = {
                pool.submit(
                    _upload_worker_adaptive,
                    zpath, self.folder_id, self.credentials_path,
                    self._error_event, self._semaphore, _drive_cache,
                ): zpath.name
                for zpath in self.zip_files
            }

            for future in as_completed(futures):
                name, success, was_skipped = future.result()
                self.done += 1

                if was_skipped:
                    skipped += 1
                elif success:
                    self.uploaded += 1
                    with self._lock:
                        self._successes_since_change += 1
                else:
                    self.failed += 1

                # --- Adaptive scaling logic (skip for cached/skipped results) ---
                if not was_skipped:
                    # Check for errors (any worker can set this)
                    if self._error_event.is_set():
                        self._scale_down()

                    # Probe for scale-up after sustained success (skip if ceiling locked)
                    with self._lock:
                        should_scale = (
                            not self._ceiling_locked
                            and self._successes_since_change >= self.PROBE_INTERVAL
                            and self.current_workers < self._ceiling
                        )
                    if should_scale:
                        self._scale_up()

                # Progress — show ceiling if locked
                ceiling_str = f" ceil={self._ceiling}" if self._ceiling_locked else ""
                skip_str = f" | {skipped} skipped" if skipped else ""
                _print_progress(
                    self.done, self.total, t0,
                    f" | {self.uploaded} uploaded{skip_str} | w={self.current_workers}{ceiling_str}"
                )

        print()  # newline after progress bar
        elapsed = time.time() - t0
        rate = self.uploaded / elapsed if elapsed > 0 else 0
        print(f"  OK: {self.uploaded} ZIPs uploaded to Drive ({rate:.1f} files/sec).")
        if skipped:
            print(f"  ⏭ {skipped} ZIPs already on Drive (skipped, no API call).")
        if self.failed:
            print(f"  FAIL: {self.failed} ZIPs failed to upload (kept locally).")
        if self._ceiling_locked:
            print(f"  ⚡ Learned optimal concurrency ceiling: {self._ceiling} workers")
        return self.uploaded, self.failed


def _list_drive_filenames(service, folder_id):
    """Returns a set of filenames in the given Drive folder."""
    names = set()
    page_token = None
    page = 0
    while True:
        resp = service.files().list(
            q=f"'{folder_id}' in parents and trashed=false",
            fields="nextPageToken, files(name)",
            pageSize=1000,
            pageToken=page_token,
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        ).execute()
        batch = resp.get("files", [])
        names.update(f["name"] for f in batch)
        page += 1
        print(f"\r  Listed {len(names)} files from Drive ({page} pages)...", end="", flush=True)
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    print()
    return names


def _read_expected_from_xlsx(xlsx_path):
    """Returns dict of {place_id: image_urls_str} for rows with image_urls."""
    import openpyxl
    wb = openpyxl.load_workbook(xlsx_path, read_only=True)
    ws = wb.active
    headers = [cell.value for cell in next(ws.iter_rows(min_row=1, max_row=1))]
    idx_pid = headers.index("place_id")
    idx_urls = headers.index("image_urls")
    idx_name = headers.index("name") if "name" in headers else None

    result = {}
    for row in ws.iter_rows(min_row=2, values_only=True):
        urls = str(row[idx_urls] or "").strip()
        if urls and urls != "N/A" and urls != "None":
            pid = row[idx_pid]
            name = row[idx_name] if idx_name is not None else "?"
            result[pid] = (urls, name)
    wb.close()
    return result


def verify_drive(xlsx_path: str, images_folder: Path,
                 gdrive_folder_id: str, credentials_path: str,
                 place_workers: int = 32, upload_workers: int = 8,
                 max_upload_workers: int = 512, redownload: bool = True) -> int:
    """Verify Drive completeness against xlsx and optionally re-download missing files.

    1. Lists all .zip files in the Drive dest folder
    2. Reads xlsx for all place_ids with image_urls
    3. Reports the gap
    4. If redownload=True, downloads and uploads the missing files
    """
    from gdrive_upload import _get_service, find_or_create_folder, init_cache

    print(f"\n{'='*60}")
    print(f"  DRIVE VERIFICATION")
    print(f"{'='*60}")

    service = _get_service(credentials_path)

    # Find the phase1_data folder on Drive
    dest_folder_name = "phase1_data"
    query = (
        f"name='{dest_folder_name}' and "
        f"'{gdrive_folder_id}' in parents and "
        f"mimeType='application/vnd.google-apps.folder' and "
        f"trashed=false"
    )
    resp = service.files().list(
        q=query, fields="files(id,name)", pageSize=1,
        supportsAllDrives=True, includeItemsFromAllDrives=True
    ).execute()
    folders = resp.get("files", [])
    if not folders:
        print(f"  ERROR: '{dest_folder_name}' folder not found on Drive.")
        return 1
    dest_folder_id = folders[0]["id"]

    # Step 1: List Drive files
    print(f"\n[Step 1] Listing files in '{dest_folder_name}' on Drive...")
    drive_filenames = _list_drive_filenames(service, dest_folder_id)
    print(f"  Files on Drive: {len(drive_filenames)}")

    # Step 2: Read expected from xlsx
    print(f"\n[Step 2] Reading expected place_ids from {xlsx_path}...")
    expected = _read_expected_from_xlsx(xlsx_path)
    print(f"  Places with image_urls: {len(expected)}")

    # Step 3: Compare
    print(f"\n[Step 3] Cross-referencing...")
    expected_filenames = {f"{pid}.zip" for pid in expected}
    present = expected_filenames & drive_filenames
    missing_filenames = expected_filenames - drive_filenames
    extra = drive_filenames - expected_filenames

    missing_pids = {fn.replace(".zip", "") for fn in missing_filenames}

    print(f"\n  {'─'*50}")
    print(f"  Expected on Drive (from xlsx):  {len(expected_filenames)}")
    print(f"  Actually on Drive:              {len(drive_filenames)}")
    print(f"  Present (matched):              {len(present)}")
    print(f"  MISSING from Drive:             {len(missing_pids)}")
    print(f"  Extra on Drive (not in xlsx):   {len(extra)}")
    print(f"  {'─'*50}")

    # Save missing list
    out_dir = Path(xlsx_path).parent
    if missing_pids:
        missing_file = out_dir / "missing_from_drive.txt"
        sorted_missing = sorted(missing_pids)
        with open(missing_file, "w", encoding="utf-8") as f:
            for pid in sorted_missing:
                f.write(pid + "\n")
        print(f"\n  Missing place_ids saved to: {missing_file}")
        print(f"  First 10 missing: {sorted_missing[:10]}")

    if not missing_pids:
        print(f"\n  All files present on Drive!")
        return 0

    if not redownload:
        print(f"\n  Run with --verify --phase 2 to re-download missing files.")
        return 0

    # Step 4: Re-download and upload missing files
    print(f"\n[Step 4] Re-downloading {len(missing_pids)} missing places...")
    images_folder.mkdir(parents=True, exist_ok=True)

    gdrive_config = None
    try:
        run_folder_id = find_or_create_folder(credentials_path, gdrive_folder_id, dest_folder_name)
        gdrive_config = {
            "run_folder_id": run_folder_id,
            "credentials_path": credentials_path,
        }
        init_cache(credentials_path, run_folder_id)
    except Exception as e:
        print(f"  WARNING: Failed to setup Drive upload: {e}")

    # Build args list for missing places
    args_list = []
    for pid in missing_pids:
        urls_str, name = expected[pid]
        args_list.append((pid, urls_str, name, images_folder, gdrive_config, None))

    total_downloaded = 0
    total_uploaded = 0
    done = 0
    success = 0
    failed = 0
    t0 = time.time()

    with ThreadPoolExecutor(max_workers=place_workers) as pool:
        futures = {pool.submit(_download_place_images, a): a[0] for a in args_list}
        for future in as_completed(futures):
            place_id, name, count, uploaded = future.result()
            done += 1
            if count > 0:
                total_downloaded += count
                success += 1
                if uploaded:
                    total_uploaded += 1
            else:
                failed += 1
            upload_str = f" | {total_uploaded} uploaded" if gdrive_config else ""
            _print_progress(done, len(args_list), t0, f" | {total_downloaded} imgs{upload_str}")

    print()

    # Upload sweep for any that failed inline upload
    if gdrive_config:
        _upload_sweep(images_folder, gdrive_config["run_folder_id"],
                      gdrive_config["credentials_path"],
                      upload_workers, max_upload_workers)

    elapsed = time.time() - t0
    print(f"\n  ── Verify + Re-download Summary ──")
    print(f"  Missing from Drive:    {len(missing_pids)}")
    print(f"  Successfully downloaded: {success} ({total_downloaded} images)")
    print(f"  Uploaded to Drive:     {total_uploaded}")
    if failed:
        print(f"  Failed (0 images):     {failed}")
    print(f"  Completed in {int(elapsed // 60)}m {int(elapsed % 60)}s")
    return total_downloaded


def decompress_images(images_folder: Path) -> tuple:
    """Extract all ZIP archives in the images folder back to loose files.
    
    Returns (places_extracted, images_extracted).
    """
    if not images_folder.exists():
        print("No images folder found.")
        return 0, 0

    zips = [p for p in images_folder.iterdir() if p.is_file() and p.suffix.lower() == ".zip"]
    if not zips:
        print("No ZIP archives found to decompress.")
        return 0, 0

    total_places = 0
    total_images = 0
    for zip_path in zips:
        try:
            with zipfile.ZipFile(zip_path, "r") as zf:
                zf.extractall(images_folder)
                total_images += len(zf.namelist())
            zip_path.unlink()  # Remove zip after successful extraction
            total_places += 1
        except Exception as e:
            print(f"  ERROR decompressing {zip_path.name}: {e}")

    print(f"  Extracted {total_images} images from {total_places} ZIP archives.")
    return total_places, total_images


def validate_input(path: str, url_column: str = "url") -> bool:
    """Check input CSV has url column."""
    try:
        with open(path, encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            cols = reader.fieldnames or []
            if url_column not in cols and not any(c.lower() == url_column.lower() for c in cols):
                print(f"ERROR: Input CSV must have '{url_column}' column.")
                return False
    except Exception as e:
        print(f"ERROR: Cannot read input: {e}")
        return False
    return True


def get_phase1_stats(csv_path: str) -> tuple:
    """Return (success_count, fail_count) from Phase 1 CSV."""
    if not Path(csv_path).exists():
        return 0, 0
    success = fail = 0
    with open(csv_path, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            if row.get("name") == "FAILED":
                fail += 1
            else:
                success += 1
    return success, fail


def main():
    parser = argparse.ArgumentParser(description="Production 2-phase pipeline (150K+ URLs)")
    parser.add_argument("input", nargs="?", default=None, help="Input CSV with place_id, url columns")
    parser.add_argument("--output-dir", "-o", default="output", help="Output directory for CSV and images")
    parser.add_argument("--limit", "-n", type=int, default=0, help="Max places to process (0=all)")
    parser.add_argument("--batch-size", "-b", type=int, default=50, help="Restart browsers every N places")
    parser.add_argument("--process-batch", "-p", type=int, default=2000,
        help="Places per process (0=no process pooling). Use 2000 for 150K+ URLs")
    parser.add_argument("--delay", "-d", type=int, default=500,
        help="Delay between requests (ms). 500ms recommended per machine (separate IPs)")
    parser.add_argument("--browsers", type=int, default=1,
        help="Number of separate Chromium browser processes (each ~100MB RAM)")
    parser.add_argument("--contexts", type=int, default=1,
        help="Parallel contexts per browser. Total workers = browsers × contexts")
    parser.add_argument("--workers", "-w", type=int, default=0,
        help="(Legacy) If set, treated as browsers=workers, contexts=1. Use --browsers/--contexts instead")
    parser.add_argument("--phase", "-P", choices=["1", "2", "both"], default="both", help="Run phase 1, 2, or both")
    parser.add_argument("--no-resume", action="store_true", help="Disable resume (reprocess all)")
    parser.add_argument("--lean", action="store_true", help="Lean mode: skip hours/about/amenities/images, output 10 fields only (faster)")
    parser.add_argument("--lean-enrich", action="store_true",
        help="Lean enrich mode: extract rating, review_count, latest_review_date, total_photos. Fast (no UI interaction).")
    parser.add_argument("--photo-urls", action="store_true",
        help="(With --lean-enrich) Also extract photo_count from carousel image URLs. Adds ~0.8s/place.")
    parser.add_argument("--url-column", default="url", help="Input CSV column for URLs")
    parser.add_argument("--place-id-column", default="place_id", help="Input CSV column for place_id")
    parser.add_argument("--compress-existing", action="store_true",
        help="Compress already-downloaded loose images into ZIP archives (one per place)")
    parser.add_argument("--upload-existing", action="store_true",
        help="Upload all existing ZIP archives to Google Drive (requires --gdrive-folder-id)")
    parser.add_argument("--decompress", action="store_true",
        help="Extract all ZIP archives back to loose image files (for ML training)")
    parser.add_argument("--verify", action="store_true",
        help="Verify Drive completeness against xlsx and re-download missing files")
    parser.add_argument("--verify-only", action="store_true",
        help="Verify Drive completeness (report only, no re-download)")
    parser.add_argument("--source-xlsx", default=None,
        help="Path to xlsx file for verification (default: output/complete_data.xlsx)")
    parser.add_argument("--compress-workers", type=int, default=None,
        help="Parallel workers for ZIP compression (default: CPU count, max 16). CPU-bound.")
    parser.add_argument("--upload-workers", type=int, default=8,
        help="Initial parallel workers for Google Drive uploads (default: 8). Autoscaler starts here.")
    parser.add_argument("--max-upload-workers", type=int, default=512,
        help="Maximum workers the autoscaler can scale up to (default: 512).")
    parser.add_argument("--gdrive-folder-id", default=None,
        help="Google Drive folder ID to upload ZIPs to (enables auto-upload + local delete)")
    parser.add_argument("--credentials", default=None,
        help="Path to Google OAuth2 credentials JSON (required with --gdrive-folder-id). First run opens browser for login.")
    args = parser.parse_args()

    # Project root = parent of src/ (e.g. pipeline/)
    script_dir = Path(__file__).parent.resolve()
    project_root = script_dir.parent

    # Resolve paths relative to project root (so -o output means pipeline/output)
    out_dir = (project_root / args.output_dir).resolve()
    csv_path = str(out_dir / "phase1_data.csv")
    failures_path = str(out_dir / "phase1_failures.csv")
    images_folder = out_dir / "images"
    log_path = out_dir / f"run_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"

    # Auto-detect settings from config.json if not specified on CLI
    config_path = project_root / "config.json"
    config = {}
    if config_path.exists():
        try:
            with open(config_path, encoding="utf-8") as f:
                config = json.load(f)
        except Exception:
            pass

    # Auto-read gdrive_folder_id from config.json
    if not args.gdrive_folder_id and config.get("gdrive_folder_id"):
        args.gdrive_folder_id = config["gdrive_folder_id"]
        print(f"[Auto] Using Drive folder ID from config.json")

    # Auto-detect credentials from credentials/ folder if not specified
    if not args.credentials and args.gdrive_folder_id:
        creds_dir = project_root / "credentials"
        creds_files = list(creds_dir.glob("client_secret_*.json")) if creds_dir.exists() else []
        if creds_files:
            args.credentials = str(creds_files[0])
            print(f"[Auto] Using credentials: {creds_files[0].name}")
        else:
            print("WARNING: --gdrive-folder-id set but no credentials found in credentials/")
            print("  Provide --credentials or place client_secret_*.json in credentials/")

    # Handle --compress-existing and --decompress (no input CSV needed)
    if args.compress_existing:
        out_dir.mkdir(parents=True, exist_ok=True)
        print(f"[Compress] Compressing loose images in {images_folder}...")
        places, imgs, generated_zips = compress_existing_images(
            images_folder, workers=args.compress_workers
        )

        # Upload compressed ZIPs to Drive if credentials provided
        if args.gdrive_folder_id and args.credentials and generated_zips:
            print(f"\n[Upload] Uploading {len(generated_zips)} ZIPs to Google Drive using folder ID {args.gdrive_folder_id}...")
            folder_name = Path(args.input).stem if args.input else "compressed_images"
            try:
                folder_id = find_or_create_folder(args.credentials, args.gdrive_folder_id, folder_name)
                init_cache(args.credentials, folder_id)

                # Convert Path list to match AdaptiveUploader interface
                zip_paths = [p if isinstance(p, Path) else Path(p) for p in generated_zips]
                uploader = AdaptiveUploader(
                    zip_paths, folder_id, args.credentials,
                    initial_workers=args.upload_workers,
                    max_workers=args.max_upload_workers,
                )
                uploader.run()

            except Exception as e:
                print(f"  ERROR: Google Drive upload failed: {e}")
        return 0
    if args.upload_existing:
        out_dir.mkdir(parents=True, exist_ok=True)
        if not args.gdrive_folder_id or not args.credentials:
            print("ERROR: --upload-existing requires --gdrive-folder-id and --credentials (or auto-detect).")
            return 1
        zip_files = sorted(
            p for p in images_folder.iterdir()
            if p.is_file() and p.suffix.lower() == ".zip"
        ) if images_folder.exists() else []
        if not zip_files:
            print("No ZIP archives found to upload.")
            return 0
        print(f"[Upload] Found {len(zip_files)} ZIPs in {images_folder}")
        folder_name = Path(args.input).stem if args.input else "compressed_images"
        try:
            folder_id = find_or_create_folder(args.credentials, args.gdrive_folder_id, folder_name)
            init_cache(args.credentials, folder_id)

            uploader = AdaptiveUploader(
                zip_files, folder_id, args.credentials,
                initial_workers=args.upload_workers,
                max_workers=args.max_upload_workers,
            )
            uploader.run()
        except Exception as e:
            print(f"  ERROR: Google Drive upload failed: {e}")
        return 0

    if args.decompress:
        out_dir.mkdir(parents=True, exist_ok=True)
        print(f"[Decompress] Extracting ZIP archives in {images_folder}...")
        places, imgs = decompress_images(images_folder)
        print(f"Done: {places} places, {imgs} images extracted.")
        return 0

    if args.verify or args.verify_only:
        if not args.gdrive_folder_id or not args.credentials:
            print("ERROR: --verify requires Google Drive credentials (auto-detected or --credentials + --gdrive-folder-id).")
            return 1
        xlsx_path = args.source_xlsx or str(out_dir / "complete_data.xlsx")
        if not Path(xlsx_path).exists():
            print(f"ERROR: xlsx file not found: {xlsx_path}")
            print("  Provide --source-xlsx or place complete_data.xlsx in the output directory.")
            return 1
        out_dir.mkdir(parents=True, exist_ok=True)
        verify_drive(
            xlsx_path=xlsx_path,
            images_folder=images_folder,
            gdrive_folder_id=args.gdrive_folder_id,
            credentials_path=args.credentials,
            place_workers=32,
            upload_workers=args.upload_workers,
            max_upload_workers=args.max_upload_workers,
            redownload=not args.verify_only,
        )
        return 0

    # Input CSV is required for normal pipeline operation
    if not args.input:
        print("ERROR: Input CSV is required (unless using --compress-existing, --upload-existing, or --decompress).")
        parser.print_usage()
        return 1

    # Auto-resolve input: if not found as-is, check inputs/ folder under project root
    if not Path(args.input).exists():
        inputs_dir = project_root / "inputs"
        candidate = inputs_dir / Path(args.input).name
        if candidate.exists():
            args.input = str(candidate)
            print(f"[Auto] Found input in {inputs_dir / Path(args.input).name}")

    if not validate_input(args.input, url_column=args.url_column):
        return 1

    rows = load_input_rows(
        args.input,
        url_column=args.url_column,
        place_id_column=args.place_id_column,
        limit=args.limit,
    )
    if not rows:
        print("No valid URLs found.")
        return 1

    config = ScrapeConfig(extract_share_link=False)
    resume = not args.no_resume

    out_dir.mkdir(parents=True, exist_ok=True)
    log_file = open(log_path, "a", encoding="utf-8")

    def log(msg: str):
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{ts}] {msg}"
        print(line)
        log_file.write(line + "\n")
        log_file.flush()

    log("=" * 60)
    log("PRODUCTION 2-PHASE PIPELINE")
    log("=" * 60)
    log(f"Input: {args.input}")
    log(f"Places: {len(rows)}")
    log(f"Output: {out_dir}")
    global COLUMN_ORDER
    if getattr(args, 'lean_enrich', False):
        if getattr(args, 'photo_urls', False):
            COLUMN_ORDER = LEAN_ENRICH_COLUMN_ORDER  # includes photo_count + total_photos
            log(f"Lean enrich mode + photo URLs: {len(LEAN_ENRICH_COLUMN_ORDER)} fields")
        else:
            # Drop photo_count, keep total_photos only
            COLUMN_ORDER = [c for c in LEAN_ENRICH_COLUMN_ORDER if c != "photo_count"]
            log(f"Lean enrich mode: {len(COLUMN_ORDER)} fields (no carousel photo_count)")
    elif getattr(args, 'lean', False):
        COLUMN_ORDER = LEAN_COLUMN_ORDER
        log(f"Lean mode: 10 fields only (skipping hours/about/amenities/images)")

    # Resolve browsers/contexts from args (legacy --workers fallback)
    if args.workers > 0 and args.browsers == 1 and args.contexts == 1:
        # Legacy mode: --workers N -> N browsers × 1 context each
        browsers = args.workers
        contexts = 1
    else:
        browsers = args.browsers
        contexts = args.contexts
    total_workers = browsers * contexts

    log(f"Batch: {args.batch_size} | Browsers: {browsers} × Contexts: {contexts} = {total_workers} workers | Process batch: {args.process_batch} | Delay: {args.delay}ms | Resume: {resume}")
    log("")

    try:
        if args.phase in ("1", "both"):
            log("[Phase 1] Extracting data + saving CSV...")
            success, fail = asyncio.run(run_phase1(
                rows, csv_path, failures_path, config,
                args.batch_size, args.delay, args.process_batch,
                browsers=browsers, contexts_per_browser=contexts,
                resume=resume, lean=getattr(args, 'lean', False),
                lean_enrich=getattr(args, 'lean_enrich', False),
                photo_urls=getattr(args, 'photo_urls', False),
            ))
            log(f"Phase 1 CSV: {csv_path}")
            if Path(csv_path).exists():
                s, f = get_phase1_stats(csv_path)
                log(f"Phase 1 stats: {s} success, {f} failed")
            if Path(failures_path).exists():
                fail_count = sum(1 for _ in open(failures_path, encoding="utf-8-sig")) - 1
                log(f"Failures logged: {failures_path} ({fail_count} rows)")

        if args.phase in ("2", "both"):
            if not Path(csv_path).exists():
                log("ERROR: Phase 1 CSV not found. Run Phase 1 first.")
            else:
                log("[Phase 2] Downloading images...")
                n = run_phase2(
                    csv_path, images_folder, resume,
                    gdrive_folder_id=args.gdrive_folder_id,
                    credentials_path=args.credentials,
                    upload_workers=args.upload_workers,
                    max_upload_workers=args.max_upload_workers,
                )
                log(f"Phase 2: {n} images downloaded")

        log("")
        log("=" * 60)
        log("DONE")
        if Path(csv_path).exists():
            s, f = get_phase1_stats(csv_path)
            log(f"Summary: {s} places extracted, {f} failed")
        if images_folder.exists():
            n_img = sum(1 for _ in images_folder.glob("*") if _.is_file())
            log(f"Summary: {n_img} images in {images_folder}")
        log("=" * 60)
    finally:
        log_file.close()

    return 0


if __name__ == "__main__":
    multiprocessing.freeze_support()
    sys.exit(main())
