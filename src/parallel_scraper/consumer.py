"""Consumer thread function: pulls placeIds from queue, runs Phase-2 deep-scrape, persists."""
from __future__ import annotations

import logging
import queue
import threading
from typing import Optional

from tenacity import RetryError

from parallel_scraper.config import ParallelConfig
from parallel_scraper.dedup import DedupCache
from parallel_scraper.logging_setup import phase_var, place_id_var
from pathlib import Path

from parallel_scraper.phase2_metadata import (
    build_place_url, scrape_one, download_place_images, PlaywrightSession,
)
from parallel_scraper.rate_limit import TokenBucket
from parallel_scraper.retry_policy import (
    PermanentError,
    TransientError,
    classify,
    transient_retry,
)
from parallel_scraper.state import StateBuffer

logger = logging.getLogger(__name__)


def consumer_loop(
    consumer_id: int,
    in_queue: "queue.Queue[Optional[str]]",
    config: ParallelConfig,
    dedup: DedupCache,
    csv_writer,  # LockedCsvWriter for phase2_data.csv
    full_writer,  # GzipCsvWriter for phase2_data.full.csv.gz
    rate_limiter: TokenBucket,
    state: StateBuffer,
    stop_event: threading.Event,
    stats,
    run_id: str,
    csv_path: str,
    failures_path: str,
    image_pool=None,
) -> None:
    phase_var.set(f"consumer.{consumer_id}")
    logger.info("consumer.start id=%d", consumer_id)
    wid = f"p2-{consumer_id}"
    stats.worker_set(wid, phase=2, state="idle", task="—", query="waiting on queue")

    # One persistent Playwright session per consumer — reused across many
    # places, recycled every `worker_browser_recycle_after` to bound memory.
    # Dry-run skips this (no browser launches).
    recycle_after = max(1, int(config.worker_browser_recycle_after or 50))
    session: Optional[PlaywrightSession] = None
    if not config.dry_run:
        try:
            session = PlaywrightSession(
                csv_path, failures_path,
                delay_ms=config.consumer_delay_ms,
                full_relaunch_every=config.worker_full_relaunch_every,
                capture_screenshots=config.capture_screenshots,
            )
            logger.info("consumer.session_open id=%d recycle_after=%d",
                        consumer_id, recycle_after)
        except Exception:
            logger.exception("consumer.session_init_failed id=%d", consumer_id)
            stats.worker_clear(wid)
            return

    try:
        while not stop_event.is_set():
            try:
                item = in_queue.get(timeout=1)
            except queue.Empty:
                continue
            try:
                if item is None:
                    logger.info("consumer.sentinel id=%d", consumer_id)
                    return
                place_id_var.set(item)
                stats.worker_set(wid, phase=2, state="busy",
                                 task=item, query="scraping metadata")
                _scrape_and_persist(
                    item, config, csv_writer, full_writer,
                    rate_limiter, state, stats, csv_path, failures_path,
                    session=session, image_pool=image_pool,
                )
                # Recycle the browser every N places to keep memory bounded.
                if session and session.places_scraped >= recycle_after:
                    logger.info("consumer.recycle id=%d after_places=%d",
                                consumer_id, session.places_scraped)
                    try:
                        session.recycle()
                    except Exception:
                        logger.exception("consumer.recycle_failed id=%d", consumer_id)
            except PermanentError as e:
                logger.warning("consumer.permanent place_id=%s err=%s", item, str(e)[:200])
                state.dead_letter_append(item, build_place_url(item),
                                         "PermanentError", str(e))
                state.placeids_set(item, status="failed", last_error=str(e)[:500])
                stats.errors += 1
            except RetryError as e:
                logger.warning("consumer.retry_exhausted place_id=%s err=%s", item, str(e)[:200])
                state.dead_letter_append(item, build_place_url(item),
                                         "RetryExhausted", str(e))
                state.placeids_set(item, status="failed", last_error=str(e)[:500])
                stats.errors += 1
            except Exception as e:
                logger.exception("consumer.unhandled place_id=%s", item)
                state.dead_letter_append(item, build_place_url(item),
                                         "Unhandled", str(e))
                state.placeids_set(item, status="failed", last_error=str(e)[:500])
                stats.errors += 1
            finally:
                try:
                    in_queue.task_done()
                except ValueError:
                    pass
                place_id_var.set("")
                stats.worker_set(wid, phase=2, state="idle", task="—", query="waiting on queue")
    finally:
        stats.worker_clear(wid)
        if session is not None:
            try:
                session.close()
                logger.info("consumer.session_closed id=%d total_places=%d",
                            consumer_id, session.places_scraped)
            except Exception:
                logger.exception("consumer.session_close_failed id=%d", consumer_id)


def _scrape_and_persist(
    place_id: str,
    config: ParallelConfig,
    csv_writer,
    full_writer,
    rate_limiter: TokenBucket,
    state: StateBuffer,
    stats,
    csv_path: str,
    failures_path: str,
    session: "Optional[PlaywrightSession]" = None,
    image_pool=None,
) -> None:
    """Tenacity-wrapped scrape + dual-write persistence."""

    @transient_retry(max_attempts=config.max_retries_transient + 1)
    def _try() -> dict:
        rate_limiter.acquire(1)
        state.placeids_set(place_id, status="scraping", attempts_inc=1)
        if config.dry_run:
            return _fake_row(place_id)
        if session is not None:
            row = session.scrape(place_id)
        else:
            row = scrape_one(
                place_id,
                csv_path=csv_path,
                failures_path=failures_path,
                delay_ms=config.consumer_delay_ms,
            )
        if not row:
            raise TransientError(f"empty result for {place_id}")
        name = (row.get("name") or "").strip()
        if name == "FAILED" or not name:
            err = row.get("error") or "blank result"
            classified = classify(err)
            raise classified
        return row

    row = _try()
    # Phase 2 already wrote to its own CSV via append_csv_row.
    # Dual-write: our schema-locked CSV + sidecar full-payload gzip.
    csv_writer.append(row)
    full_writer.append(row)
    # Save each place's photos as loose files in outputs/<run_id>/images/<place_id>/.
    # Best-effort — never fails the scrape. Offloaded to a background pool so it
    # doesn't gate the next place; falls back to inline if the pool is absent or
    # its backlog is full.
    image_urls = (row or {}).get("image_urls") if not config.dry_run else None
    if config.download_images and image_urls and image_urls != "N/A":
        pid_for_images = row.get("place_id", "") or place_id
        if image_pool is not None:
            # Hand off to the background pool; if its backlog is full, download
            # inline through the pool so the completion marker is still written.
            if not image_pool.submit(pid_for_images, image_urls):
                try:
                    image_pool.download_inline(pid_for_images, image_urls)
                except Exception:
                    logger.warning("consumer.image_download_failed place_id=%s", place_id, exc_info=True)
        else:
            # No pool (inline mode / image_download_workers=0) — bare download,
            # no marker (auto-repair on resume only applies when the pool runs).
            try:
                run_dir = Path(csv_path).parent
                saved = download_place_images(pid_for_images, image_urls, run_dir / "images")
                if saved:
                    logger.debug("consumer.images_saved place_id=%s count=%d", place_id, saved)
            except Exception:
                logger.warning("consumer.image_download_failed place_id=%s", place_id, exc_info=True)
    state.placeids_set(place_id, status="done")
    stats.scraped += 1


def _fake_row(place_id: str) -> dict:
    """Synthesize a row for --dry-run."""
    return {
        "place_id": place_id,
        "name": f"DryRun {place_id[-4:]}",
        "rating": "4.2",
        "review_count": "10",
        "category": "Test",
        "status": "Operational",
        "address": "Test Address",
        "phone": "+91-0000000000",
        "website": "",
        "latitude": "19.0",
        "longitude": "72.8",
        "image_urls": "",
        "source_url": build_place_url(place_id),
        "hours": "",
        "price_level": "",
        "plus_code": "",
        "photo_count": "0",
        "total_photos": "0",
        "about": "",
        "amenities": "",
        "menu_link": "",
        "latest_review_date": "",
    }
