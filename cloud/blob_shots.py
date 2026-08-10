"""Azure Blob media store for the PUBLIC Phase 2 worker (creds-free).

Blob is the PERMANENT store for Phase 2 media. The worker uploads during the
scrape; the private indexer (cloud/drain_screenshots_blob.py) records rows in
the DB. Layout mirrors the old per-place Drive folders:

    <container>/<run_id>/<place_id>/
        <place_id>_1.jpg            place photos (Google CDN -> Blob)
        <place_id>_2.jpg
        screenshot_overview.png     panel screenshots
        screenshot_reviews.png

Everything for one place is grouped under the ``<run_id>/<place_id>/`` prefix,
so "all media for a place" is one prefix-list call — no manifest needed.

Security model (matches the lease/submit API): public compute holds NO account
keys — only ``PHASE2_SHOT_BLOB_SAS``, a container SAS URL scoped to **create +
write only** (no read/list/delete), e.g.:

    https://<account>.blob.core.windows.net/<container>?sv=...&sp=cw&sig=...

Uploads set a real Content-Type + inline disposition so the stored URL renders
directly in a browser tab or an <img> tag (no download prompt).
"""
from __future__ import annotations

import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor

import requests

logger = logging.getLogger(__name__)

_PUT_ATTEMPTS = 3
_PUT_BACKOFF_S = 1.0
_PUT_TIMEOUT_S = 30
_PHOTO_GET_TIMEOUT_S = 20
_PHOTO_POOL_WORKERS = 6

_EXT_BY_CTYPE = {
    "image/jpeg": "jpg", "image/jpg": "jpg", "image/png": "png",
    "image/webp": "webp", "image/gif": "gif",
}

# Reuse the engine's photo-URL dedupe + hi-res upscale when the engine is present
# (it always is in the worker); fall back to order-preserving dedupe / identity.
try:
    from parallel_scraper.phase2_metadata import (
        _dedupe_image_urls as _dedupe_urls,
        _upscale_image_url as _upscale_url,
    )
except Exception:  # pragma: no cover
    def _dedupe_urls(urls):
        return list(dict.fromkeys(urls))

    def _upscale_url(u):
        return u


def blob_sas_url() -> str | None:
    """The container SAS URL from env, or None when Blob media is off."""
    return os.environ.get("PHASE2_SHOT_BLOB_SAS") or None


def photos_enabled() -> bool:
    """Photo mirroring (CDN -> Blob) is opt-out; needs the SAS to be set anyway."""
    return os.environ.get("PHASE2_PHOTOS_TO_BLOB", "1") != "0" and bool(blob_sas_url())


def photos_max() -> int:
    try:
        return max(0, int(os.environ.get("PHASE2_PHOTOS_MAX", "15")))
    except ValueError:
        return 15


def _split_sas(sas_url: str) -> tuple[str, str]:
    """Split a container SAS URL into (container_base_url, sas_query)."""
    base, _, query = sas_url.partition("?")
    return base.rstrip("/"), query


def screenshot_name(run_id: str, place_id: str, kind: str) -> str:
    return f"{run_id}/{place_id}/screenshot_{kind}.png"


def photo_name(run_id: str, place_id: str, seq: int, ext: str) -> str:
    return f"{run_id}/{place_id}/{place_id}_{seq}.{ext}"


def _put_blob(name: str, body: bytes, content_type: str, meta: dict,
              sas_url: str | None = None, session=None) -> None:
    """PUT one blob (BlockBlob). Raises on final failure."""
    sas_url = sas_url or blob_sas_url()
    if not sas_url:
        raise RuntimeError("PHASE2_SHOT_BLOB_SAS not set")
    base, query = _split_sas(sas_url)
    url = f"{base}/{name}?{query}"
    headers = {
        "x-ms-blob-type": "BlockBlob",
        # Persisted onto the blob so the URL renders inline in browsers/<img>.
        "Content-Type": content_type,
        "x-ms-blob-content-type": content_type,
        "x-ms-blob-content-disposition": "inline",
    }
    # Metadata names must be simple identifiers; values ASCII.
    for k, v in meta.items():
        headers[f"x-ms-meta-{k}"] = str(v)
    http = session or requests
    last_exc: Exception | None = None
    for attempt in range(_PUT_ATTEMPTS):
        try:
            resp = http.put(url, data=body, headers=headers, timeout=_PUT_TIMEOUT_S)
            if resp.status_code == 201:
                return
            # 403 = SAS expired/wrong perms: retrying won't help.
            if resp.status_code == 403:
                raise RuntimeError(f"blob PUT forbidden (SAS expired or missing cw perms): {resp.status_code}")
            last_exc = RuntimeError(f"blob PUT failed status={resp.status_code}")
        except RuntimeError:
            raise
        except Exception as exc:  # network/timeout — retry
            last_exc = exc
        if attempt + 1 < _PUT_ATTEMPTS:
            time.sleep(_PUT_BACKOFF_S * (attempt + 1))
    raise last_exc if last_exc else RuntimeError("blob PUT failed")


def put_shot(run_id: str, place_id: str, kind: str, attempts: int,
             local_path: str, sas_url: str | None = None, session=None) -> None:
    """PUT one screenshot PNG. Raises on final failure so the caller can fall
    back to the artifact-dir path."""
    with open(local_path, "rb") as f:
        body = f.read()
    _put_blob(screenshot_name(run_id, place_id, kind), body, "image/png",
              {"runid": run_id, "placeid": place_id, "kind": kind,
               "attempts": int(attempts)},
              sas_url=sas_url, session=session)


def upload_menu_photos(run_id: str, place_id: str, menu_photos,
                       sas_url: str | None = None, session=None) -> int:
    """Mirror menu-card photos into Blob as
    ``<run_id>/<place_id>/<place_id>_menu_N.jpg`` (0-based, matching the
    horeca capture order so DB rows pair 1:1). `menu_photos` is the horeca
    list of {url, date}. Full-res via =w2000. Best-effort like place photos."""
    items = [(i, p.get("url"), p.get("date") or "")
             for i, p in enumerate(menu_photos or []) if p.get("url")]
    if not items:
        return 0
    http = session or requests

    def _one(args) -> bool:
        seq, url, date = args
        try:
            full = url.split("=")[0] + "=w2000"
            r = http.get(full, timeout=_PHOTO_GET_TIMEOUT_S)
            if r.status_code != 200 or not r.content:
                return False
            _put_blob(f"{run_id}/{place_id}/{place_id}_menu_{seq}.jpg", r.content,
                      "image/jpeg",
                      {"runid": run_id, "placeid": place_id, "kind": "menu",
                       "seq": seq, "caption": date.encode("ascii", "ignore").decode()},
                      sas_url=sas_url, session=session)
            return True
        except Exception:
            logger.debug("menu_upload_failed pid=%s seq=%d", place_id, seq, exc_info=True)
            return False

    with ThreadPoolExecutor(max_workers=min(_PHOTO_POOL_WORKERS, len(items))) as ex:
        stored = sum(ex.map(_one, items))
    if stored < len(items):
        logger.info("menu_photos_partial pid=%s stored=%d of %d", place_id, stored, len(items))
    return stored


def upload_place_photos(run_id: str, place_id: str, image_urls,
                        max_photos: int | None = None,
                        sas_url: str | None = None, session=None) -> int:
    """Mirror a place's photos (Google CDN URLs) into Blob as
    ``<run_id>/<place_id>/<place_id>_N.jpg``. Best-effort: per-photo failures
    are logged and skipped; returns the number stored. Numbering follows the
    deduped URL order (1-based), matching the old local/Drive convention."""
    if max_photos is None:
        max_photos = photos_max()
    urls = [u for u in (image_urls or []) if u]
    try:
        urls = [_upscale_url(u) for u in _dedupe_urls(urls)]
    except Exception:
        pass
    urls = urls[:max_photos]
    if not urls:
        return 0
    http = session or requests

    def _one(args) -> bool:
        seq, url = args
        try:
            r = http.get(url, timeout=_PHOTO_GET_TIMEOUT_S)
            if r.status_code != 200 or not r.content:
                return False
            ctype = (r.headers.get("Content-Type") or "image/jpeg").split(";")[0].strip().lower()
            ext = _EXT_BY_CTYPE.get(ctype, "jpg")
            _put_blob(photo_name(run_id, place_id, seq, ext), r.content,
                      ctype if ctype in _EXT_BY_CTYPE else "image/jpeg",
                      {"runid": run_id, "placeid": place_id, "kind": "photo", "seq": seq},
                      sas_url=sas_url, session=session)
            return True
        except Exception:
            logger.debug("photo_upload_failed pid=%s seq=%d", place_id, seq, exc_info=True)
            return False

    with ThreadPoolExecutor(max_workers=min(_PHOTO_POOL_WORKERS, len(urls))) as ex:
        stored = sum(ex.map(_one, enumerate(urls, start=1)))
    if stored < len(urls):
        logger.info("photos_partial pid=%s stored=%d of %d", place_id, stored, len(urls))
    return stored
