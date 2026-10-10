#!/usr/bin/env python3
"""Copy each shortlisted Kwality outlet's storefront photo into Supabase Storage.

Source: the scraper's close-ups in the private Azure container
  scraper-media/<source_run>/<place_id>/<place_id>_<index>.jpg|png
  (the index is the one the vision pass chose as the storefront).
Target: public bucket kwality-photos/<place_id>.jpg — a 480px JPEG thumbnail
  (~25-35 KB) the field app can load from Supabase's CDN with no Google and
  no SAS on the phone.

Work list: public.kw_photo_job_list (migration 113), paged by seq; each row
is marked done (with bytes) or error, so the job is resumable and progress is
visible in SQL:  select count(*) filter (where done), count(*) from kw_photo_job_list;

Env (from micromarket-py/.env): SUPABASE_DEDUP_URL, SUPABASE_DEDUP_SERVICE_KEY,
AZURE_MEDIA_CONTAINER_URL, AZURE_STORAGE_SAS.

Usage:  python3 scripts/ops/kw_photo_thumbs.py [--workers 12] [--limit N] [--retry-errors]
        [--seq-from A --seq-to B]   one slice of the list (A < seq <= B), so fleet runners can split it

Fleet copy (27 Sep 2026): workflow kw-photo-thumbs on mm-fleet-21 runs one slice per matrix job.
"""
from __future__ import annotations

import argparse
import io
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from PIL import Image, ImageOps

SUPABASE_URL = os.environ["SUPABASE_DEDUP_URL"].rstrip("/")
SERVICE_KEY = os.environ["SUPABASE_DEDUP_SERVICE_KEY"]
AZ_BASE = os.environ["AZURE_MEDIA_CONTAINER_URL"].split("?")[0].rstrip("/")
AZ_SAS = os.environ["AZURE_STORAGE_SAS"].lstrip("?")
BUCKET = "kwality-photos"
MAX_W = 480
QUALITY = 80

H_DB = {"apikey": SERVICE_KEY, "Authorization": f"Bearer {SERVICE_KEY}"}
session = requests.Session()
session.headers.update({"User-Agent": "kw-photo-thumbs/1"})


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def next_batch(after_seq: int, size: int, retry_errors: bool, seq_to: int = 0) -> list[dict]:
    params = {
        "select": "seq,place_id,source_run,image_index,alt_run,google_url",
        "seq": f"gt.{after_seq}",
        "order": "seq.asc",
        "limit": str(size),
    }
    params["done"] = "is.false"
    # Default pass: untouched rows. Retry pass: ONLY the errored rows.
    params["error"] = "not.is.null" if retry_errors else "is.null"
    if seq_to:
        params["and"] = f"(seq.lte.{seq_to})"
    r = session.get(f"{SUPABASE_URL}/rest/v1/kw_photo_job_list", headers=H_DB, params=params, timeout=60)
    r.raise_for_status()
    return r.json()


def blob_url(run: str, pid: str, idx: int, ext: str) -> str:
    return f"{AZ_BASE}/{run}/{pid}/{pid}_{idx}.{ext}?{AZ_SAS}"


def fetch_source(run: str, pid: str, idx: int, alt_run: str | None = None, google_url: str | None = None) -> bytes:
    """The chosen close-up: jpg first, png second, then whichever numbered
    close-up exists (the chosen index can be missing when a download failed).
    Then the same under the STATE folder — 16,290 outlets sit in regions whose
    source run (kw_batch1_7states) has no folder of its own — and finally the
    Google URL the vision pass recorded, fetched once from here rather than
    from every phone."""
    for r_id in [run] + ([alt_run] if alt_run and alt_run != run else []):
        for ext in ("jpg", "png", "jpeg", "webp"):
            r = session.get(blob_url(r_id, pid, idx, ext), timeout=30)
            if r.status_code == 200 and r.content:
                return r.content
    if google_url:
        r = session.get(google_url, timeout=30, headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/128.0 Safari/537.36", "Accept": "image/avif,image/webp,image/*,*/*;q=0.8"})
        if r.status_code == 200 and r.content and r.headers.get("content-type", "").startswith("image/"):
            return r.content
    # Fall back to listing the folder and taking the lowest-numbered close-up.
    lst = session.get(
        f"{AZ_BASE}?{AZ_SAS}&restype=container&comp=list&prefix={run}/{pid}/{pid}_&maxresults=20", timeout=30
    )
    if lst.status_code == 200:
        import re

        names = re.findall(r"<Name>([^<]+)</Name>", lst.text)
        names = [n for n in names if re.search(r"_(\d+)\.(jpe?g|png|webp)$", n, re.I)]
        names.sort(key=lambda n: int(re.search(r"_(\d+)\.", n).group(1)))
        if names:
            r = session.get(f"{AZ_BASE}/{names[0]}?{AZ_SAS}", timeout=30)
            if r.status_code == 200 and r.content:
                return r.content
    raise FileNotFoundError("no close-up in blob")


def thumbnail(raw: bytes) -> bytes:
    im = Image.open(io.BytesIO(raw))
    im = ImageOps.exif_transpose(im)
    if im.mode not in ("RGB", "L"):
        im = im.convert("RGB")
    w, h = im.size
    if w > MAX_W:
        im = im.resize((MAX_W, max(1, round(h * MAX_W / w))), Image.LANCZOS)
    out = io.BytesIO()
    im.save(out, "JPEG", quality=QUALITY, optimize=True, progressive=True)
    return out.getvalue()


def upload(pid: str, data: bytes) -> None:
    r = session.post(
        f"{SUPABASE_URL}/storage/v1/object/{BUCKET}/{pid}.jpg",
        headers={**H_DB, "Content-Type": "image/jpeg", "x-upsert": "true", "Cache-Control": "public, max-age=2592000"},
        data=data,
        timeout=60,
    )
    if r.status_code not in (200, 201):
        raise RuntimeError(f"upload {r.status_code}: {r.text[:120]}")


def mark(rows: list[dict]) -> None:
    """Batch the bookkeeping: one upsert per batch, not one PATCH per photo."""
    if not rows:
        return
    r = session.post(
        f"{SUPABASE_URL}/rest/v1/kw_photo_job_list",
        headers={**H_DB, "Content-Type": "application/json", "Prefer": "resolution=merge-duplicates,return=minimal"},
        params={"on_conflict": "place_id"},
        json=rows,
        timeout=60,
    )
    if r.status_code not in (200, 201, 204):
        log(f"mark failed {r.status_code}: {r.text[:160]}")


def do_one(job: dict) -> dict:
    pid, run, idx, seq = job["place_id"], job["source_run"], int(job["image_index"] or 1), job["seq"]
    base = {"place_id": pid, "seq": seq, "source_run": run, "image_index": idx, "city": job.get("city")}
    try:
        raw = fetch_source(run, pid, idx, job.get("alt_run"), job.get("google_url"))
        data = thumbnail(raw)
        upload(pid, data)
        return {**base, "done": True, "error": None, "bytes": len(data), "done_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    except Exception as e:  # noqa: BLE001 — every failure is recorded per row
        return {**base, "done": False, "error": str(e)[:200], "bytes": None, "done_at": None}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=12)
    ap.add_argument("--batch", type=int, default=400)
    ap.add_argument("--limit", type=int, default=0, help="stop after N photos (0 = all)")
    ap.add_argument("--retry-errors", action="store_true")
    ap.add_argument("--seq-from", type=int, default=0, help="start after this seq")
    ap.add_argument("--seq-to", type=int, default=0, help="stop at this seq (0 = end)")
    args = ap.parse_args()

    done = failed = total_bytes = 0
    after = args.seq_from
    t0 = time.time()
    log(f"start: workers={args.workers} bucket={BUCKET} max_w={MAX_W}")
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        while True:
            batch = next_batch(after, args.batch, args.retry_errors, args.seq_to)
            if not batch:
                break
            after = batch[-1]["seq"]
            # city is not selected to keep the page small; re-fetch is unnecessary for the upsert
            # because merge-duplicates only touches the columns we send.
            for j in batch:
                j.pop("city", None)
            results = [f.result() for f in as_completed([pool.submit(do_one, j) for j in batch])]
            for r in results:
                r.pop("city", None)
            mark(results)
            for r in results:
                if r["done"]:
                    done += 1
                    total_bytes += r["bytes"] or 0
                else:
                    failed += 1
            elapsed = time.time() - t0
            rate = (done + failed) / elapsed if elapsed else 0
            log(f"PROGRESS seq<={after} done={done} failed={failed} avgKB={(total_bytes / max(1, done)) / 1024:.0f} rate={rate:.1f}/s")
            if args.limit and done + failed >= args.limit:
                break
    log(f"DONE done={done} failed={failed} in {time.time() - t0:.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
