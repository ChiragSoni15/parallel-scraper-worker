"""ParallelScraper orchestrator: wires producer, consumers, heartbeat, signal handling."""
from __future__ import annotations

import csv
import gzip
import json
import logging
import queue
import signal
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from parallel_scraper.config import ALWAYS_INCLUDED, COLUMN_CATALOG, ParallelConfig
from parallel_scraper.consumer import consumer_loop
from parallel_scraper.csv_io import (
    BufferedCsvWriter, GzipCsvWriter, LockedCsvWriter, atomic_write_text,
)
from parallel_scraper.dedup import DedupCache
from parallel_scraper.logging_setup import run_id_var, setup_logging
from parallel_scraper.phase2_metadata import (
    IMAGE_STATE_FIELDS, ImageDownloadPool, load_completed_image_ids,
)
from parallel_scraper.producer import Producer, QueueRefiller
from parallel_scraper.progress import Heartbeat, Stats
from parallel_scraper.rate_limit import TokenBucket
from parallel_scraper.state import StateBuffer

logger = logging.getLogger(__name__)


_DISCOVERED_FIELDS = ("place_id", "cell_id", "query", "form", "lat", "lng", "discovered_at")


class ParallelScraper:
    def __init__(self, config: ParallelConfig) -> None:
        self._config = config
        self._stop_event = threading.Event()
        self._stats = Stats()
        self._signal_installed = False

        self._run_id = config.run_id or self._new_run_id()
        self._run_dir = Path(config.output_dir) / self._run_id
        self._run_dir.mkdir(parents=True, exist_ok=True)

        run_id_var.set(self._run_id)
        setup_logging(self._run_dir / "run.log", level="INFO")

        # Persist run config (or refuse if mismatched on resume)
        self._maybe_load_or_write_run_config()

        self._queue: "queue.Queue[Optional[str]]" = queue.Queue(maxsize=config.max_queue_size)
        self._dedup = DedupCache(config.master_dedup_csv, run_dir=self._run_dir)
        self._discovery_limiter = TokenBucket(rate=config.discovery_rps, capacity=4)
        # Sequential-phases mode: phase2_workers == 0 means "Phase 1 only,
        # don't spawn deep-scrape consumers." Phase 1 still records every
        # discovered place_id to state_placeids.csv, so a follow-up run with
        # phase2_workers > 0 can drain them via _requeue_resumable_placeids.
        # Any value != 0 (including unset/None) falls back to num_consumer_threads.
        self._phase2_disabled = (config.phase2_workers == 0)
        n_phase2 = 0 if self._phase2_disabled else (
            config.phase2_workers or config.num_consumer_threads
        )
        self._consumer_limiter = TokenBucket(
            rate=config.consumer_rps_per_thread * max(1, n_phase2 or 1),
            capacity=max(2, (n_phase2 or 1) * 2),
        )
        self._state = StateBuffer(self._run_dir)

        cols = config.output_columns()
        self._csv_writer = LockedCsvWriter(self._run_dir / "phase2_data.csv", cols)
        self._failures_path = self._run_dir / "phase2_failures.csv"
        # Sidecar full-payload gzip — full COLUMN_CATALOG + place_id
        full_cols = ALWAYS_INCLUDED + tuple(COLUMN_CATALOG.keys())
        self._full_writer = GzipCsvWriter(self._run_dir / "phase2_data.full.csv.gz", full_cols)
        # phase1_discovered.csv is a high-volume audit log (every place_id
        # found by Phase 1). BufferedCsvWriter buffers in memory and flushes
        # every 3s, matching web_app's pattern. Workers don't block on file
        # IO when writing it. State-of-the-world for resume comes from
        # state_placeids.csv (driven by StateBuffer, flushed separately),
        # so a few seconds of unflushed audit data does NOT compromise
        # resume correctness.
        self._discovered_writer = BufferedCsvWriter(
            self._run_dir / "phase1_discovered.csv", _DISCOVERED_FIELDS,
            flush_seconds=3.0,
        )

        # Background image-download pool (#8): keeps photo downloads off the
        # scrape critical path. Skipped in dry-run / Phase-1-only mode, or when
        # disabled via config (image_download_workers == 0 → inline downloads).
        # An append-only images_state.csv marks places whose image job ran to
        # completion, so a hard kill (place marked done before its async images
        # land) is recoverable on resume via _repair_incomplete_images().
        self._image_pool: Optional[ImageDownloadPool] = None
        self._image_marker: Optional[BufferedCsvWriter] = None
        if (not config.dry_run and not self._phase2_disabled
                and config.image_download_workers > 0):
            marker_path = self._run_dir / "images_state.csv"
            seed_needed = not marker_path.exists()
            self._image_marker = BufferedCsvWriter(
                marker_path, IMAGE_STATE_FIELDS, flush_seconds=3.0,
            )
            self._image_pool = ImageDownloadPool(
                self._run_dir / "images",
                workers=config.image_download_workers,
                maxsize=config.image_queue_maxsize,
                marker_writer=self._image_marker,
            )
            if seed_needed:
                # First enable on a (possibly pre-existing) run: the old inline
                # downloader always finished before marking a place done, so
                # every already-done place's images are complete. Seed them as
                # done to avoid a mass re-download on first resume.
                done_ids = self._state.get_done_placeids()
                for pid in done_ids:
                    self._image_marker.append({
                        "place_id": pid, "expected": "", "saved": "",
                        "completed_at": "seed",
                    })
                self._image_marker.flush()
                logger.info("scraper.image_marker_seeded count=%d", len(done_ids))

        self._producer: Optional[Producer] = None
        self._refiller: Optional[QueueRefiller] = None
        self._consumers: list[threading.Thread] = []
        self._heartbeat: Optional[Heartbeat] = None

    # ─── public ────────────────────────────────────

    def run(self) -> dict:
        self._install_signal_handler()
        self._requeue_resumable_placeids()
        self._repair_incomplete_images()

        self._heartbeat = Heartbeat(
            self._stats, self._queue,
            heartbeat_path=self._run_dir / "heartbeat.jsonl",
            interval_s=self._config.heartbeat_interval_s,
            use_rich=True,
            state=self._state,
        )
        self._heartbeat.start()

        self._producer = Producer(
            config=self._config,
            out_queue=self._queue,
            dedup=self._dedup,
            rate_limiter=self._discovery_limiter,
            state=self._state,
            stop_event=self._stop_event,
            stats=self._stats,
            run_id=self._run_id,
            discovered_writer=self._discovered_writer,
        )
        self._producer.start()

        # Sequential mode: skip consumer threads entirely. Phase 1 still
        # produces place_ids (records to state_placeids.csv) but they don't
        # get scraped this run. A later run with phase2_workers > 0 picks
        # them up via _requeue_resumable_placeids.
        n_phase2 = 0 if self._phase2_disabled else (
            self._config.phase2_workers or self._config.num_consumer_threads
        )
        if self._phase2_disabled:
            logger.info("scraper.phase2_disabled — Phase 1 only mode; "
                        "discovered place_ids will be recorded but not scraped")
        for i in range(n_phase2):
            t = threading.Thread(
                target=consumer_loop,
                name=f"consumer-{i}",
                daemon=True,
                kwargs=dict(
                    consumer_id=i,
                    in_queue=self._queue,
                    config=self._config,
                    dedup=self._dedup,
                    csv_writer=self._csv_writer,
                    full_writer=self._full_writer,
                    rate_limiter=self._consumer_limiter,
                    state=self._state,
                    stop_event=self._stop_event,
                    stats=self._stats,
                    run_id=self._run_id,
                    csv_path=str(self._run_dir / "phase2_pe.csv"),  # internal extractor csv
                    failures_path=str(self._failures_path),
                    image_pool=self._image_pool,
                ),
            )
            t.start()
            self._consumers.append(t)

        # Sole queue producer: feeds the bounded queue from the durable state
        # backlog so Phase 1 never blocks. Only when there are consumers to
        # drain it (skipped in sequential / phase2-disabled mode).
        if self._consumers:
            self._refiller = QueueRefiller(
                out_queue=self._queue,
                state=self._state,
                max_attempts=self._config.max_retries_transient + 1,
                stop_event=self._stop_event,
            )
            self._refiller.start()

        # Wait for Phase 1 to finish, then let Phase 2 drain every queued
        # place_id before declaring the run complete.
        self._producer.join()
        producer_err = self._producer.error if self._producer is not None else None
        # Tell the refiller Phase 1 is done — it will feed the last of the
        # backlog (consumers draining concurrently) and then exit.
        if self._refiller is not None:
            self._refiller.producer_finished()

        if self._consumers:
            if producer_err is None and not self._stop_event.is_set():
                # Let the refiller flush the entire remaining backlog into the
                # queue (consumers drain concurrently) BEFORE we conclude the
                # queue is drained — otherwise a transiently-empty queue could
                # be mistaken for "done" while the refiller still has ids to
                # feed, stranding them.
                if self._refiller is not None:
                    self._refiller.join()
                logger.info(
                    "scraper.wait_phase2_drain queue_depth=%d unfinished=%d",
                    self._queue.qsize(),
                    self._queue_unfinished_tasks(),
                )
                if self._wait_for_queue_drain():
                    logger.info("scraper.phase2_drained")
                else:
                    logger.warning(
                        "scraper.phase2_drain_interrupted queue_depth=%d unfinished=%d",
                        self._queue.qsize(),
                        self._queue_unfinished_tasks(),
                    )
            else:
                # Stopping or producer crashed: stop feeding and don't wait.
                if self._refiller is not None:
                    self._refiller.request_stop()
                logger.warning(
                    "scraper.skip_phase2_drain stopped=%s producer_error=%s "
                    "queue_depth=%d unfinished=%d",
                    self._stop_event.is_set(),
                    repr(producer_err) if producer_err is not None else "",
                    self._queue.qsize(),
                    self._queue_unfinished_tasks(),
                )

        # Ensure the refiller thread is wound down before teardown.
        if self._refiller is not None:
            self._refiller.request_stop()
            self._refiller.join(timeout=10)

        self._send_consumer_sentinels(len(self._consumers))
        for t in self._consumers:
            t.join(timeout=30)
            if t.is_alive():
                logger.warning("scraper.consumer_still_alive name=%s", t.name)

        # Flush any images still queued in the background pool. Consumers have
        # stopped submitting by now, so this drains the remaining backlog.
        if self._image_pool is not None:
            logger.info("scraper.image_pool_draining")
            self._image_pool.drain_and_close(timeout=60)
            logger.info("scraper.image_pool_closed")
        if self._image_marker is not None:
            # Flush remaining completion markers after the pool has drained.
            try:
                self._image_marker.close()
            except Exception:
                logger.exception("image_marker.close failed")

        self._heartbeat.stop()
        try:
            self._heartbeat.emit()
        except Exception:
            logger.exception("heartbeat.final_emit_failed")
        self._heartbeat.join(timeout=5)
        self._state.close()
        self._csv_writer.close()
        # Drain the buffered audit-trail writer so we don't lose the last
        # few seconds of phase1_discovered.csv rows on a clean shutdown.
        try:
            self._discovered_writer.close()
        except Exception:
            logger.exception("discovered_writer.close failed")

        snapshot = self._snapshot_with_state()
        snapshot["run_id"] = self._run_id
        snapshot["run_dir"] = str(self._run_dir)
        # Distinguish a genuine completion from one where the producer crashed
        # and bogus-completed after the consumer queue drained. Without this,
        # the dashboard treats a 96s crash-out as a successful 96s run.
        unfinished = self._queue_unfinished_tasks()
        if producer_err is not None:
            snapshot["producer_error"] = repr(producer_err)
            snapshot["status"] = "failed"
            logger.error("scraper.complete_with_producer_error %s", snapshot)
        elif self._stop_event.is_set():
            snapshot["status"] = "stopped"
            logger.warning("scraper.stopped %s", snapshot)
        elif unfinished:
            snapshot["unfinished_tasks"] = unfinished
            snapshot["status"] = "incomplete"
            logger.error("scraper.incomplete_queue_not_drained %s", snapshot)
        else:
            snapshot["status"] = "completed"
            logger.info("scraper.complete %s", snapshot)
        return snapshot

    def stop(self, timeout: float = 30.0) -> None:
        logger.warning("scraper.stop_requested")
        self._stop_event.set()

    # ─── internals ─────────────────────────────────

    @staticmethod
    def _new_run_id() -> str:
        return f"par_{datetime.now(timezone.utc):%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:6]}"

    def _maybe_load_or_write_run_config(self) -> None:
        cfg_path = self._run_dir / "run_config.json"
        cfg_payload = {
            "run_id": self._run_id,
            "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "schema_version": 1,
            "selected_columns": list(self._config.output_columns()),
            "osm_relation_id": self._config.osm_relation_id,
            "osm_type": self._config.osm_type,
            "city_name": self._config.city_name,
            "grid_size_meters": self._config.grid_size_meters,
            "queries": list(self._config.queries),
            "discovery_backend": self._config.discovery_backend,
            "places_tier": self._config.places_tier,
            "num_consumer_threads": self._config.num_consumer_threads,
            "discovery_rps": self._config.discovery_rps,
            "consumer_rps_per_thread": self._config.consumer_rps_per_thread,
            "consumer_delay_ms": self._config.consumer_delay_ms,
            "max_retries_transient": self._config.max_retries_transient,
            "master_dedup_csv": self._config.master_dedup_csv,
        }
        if cfg_path.exists():
            existing = json.loads(cfg_path.read_text(encoding="utf-8"))
            ex_cols = tuple(existing.get("selected_columns", []))
            new_cols = tuple(cfg_payload["selected_columns"])
            if ex_cols and ex_cols != new_cols:
                raise SystemExit(
                    f"This run was started with columns:\n  {list(ex_cols)}\n"
                    f"You're trying to resume with:\n  {list(new_cols)}\n"
                    f"Re-run without --columns/--profile or pass exactly the same set, "
                    f"or use a fresh run-id to start over."
                )
            return
        atomic_write_text(cfg_path, json.dumps(cfg_payload, indent=2))

    def _requeue_resumable_placeids(self) -> None:
        resumable = self._state.placeids_get_resumable(self._config.max_retries_transient + 1)
        if not resumable:
            return
        if self._phase2_disabled:
            logger.info("scraper.resume_placeids_skipped phase2_disabled count=%d", len(resumable))
            return
        # Repair only: any resumable id already present in this run's scraped
        # output (run_seen, rebuilt from phase2_data.csv at boot) is actually
        # done — mark it so the refiller won't re-feed it. Queue feeding itself
        # is owned by the QueueRefiller, which pulls the remaining resumable
        # backlog from state (decoupled from Phase 1's rate).
        repaired = 0
        for pid in resumable:
            if self._dedup.is_run_seen(pid):
                self._state.placeids_set(pid, status="done")
                repaired += 1
        logger.info("scraper.resume_placeids count=%d repaired_from_output=%d "
                    "(remaining fed via refiller)", len(resumable), repaired)

    def _repair_incomplete_images(self) -> None:
        """Re-download images for `done` places whose image job never completed
        (process hard-killed after the place was marked done but before its
        async images landed). Identified as: present in the full-payload CSV
        with image_urls, marked done in state, but missing from images_state.csv.
        Idempotent — a clean prior run leaves nothing to repair."""
        if self._image_pool is None:
            return
        full_path = self._run_dir / "phase2_data.full.csv.gz"
        if not full_path.exists():
            return
        completed = load_completed_image_ids(self._run_dir / "images_state.csv")
        submitted = 0
        try:
            with gzip.open(full_path, "rt", encoding="utf-8", newline="") as f:
                for row in csv.DictReader(f):
                    pid = (row.get("place_id") or "").strip()
                    if not pid or pid in completed:
                        continue
                    urls = (row.get("image_urls") or "").strip()
                    if not urls or urls == "N/A":
                        continue
                    if not self._state.is_placeid_done(pid):
                        continue  # not done → will be re-scraped, re-downloads anyway
                    if not self._image_pool.submit(pid, urls):
                        self._image_pool.download_inline(pid, urls)
                    submitted += 1
        except Exception:
            logger.warning("scraper.image_repair_scan_failed", exc_info=True)
        if submitted:
            logger.info("scraper.image_repair_resubmitted count=%d", submitted)

    def _send_consumer_sentinels(self, n_consumers: int) -> None:
        for _ in range(max(0, n_consumers)):
            try:
                self._queue.put(None, timeout=5)
            except queue.Full:
                logger.warning("scraper.sentinel_queue_full")
                break

    def _queue_unfinished_tasks(self) -> int:
        try:
            return int(getattr(self._queue, "unfinished_tasks", 0) or 0)
        except Exception:
            return 0

    def _snapshot_with_state(self) -> dict:
        snapshot = self._stats.snapshot()
        try:
            place_counts = self._state.placeids_status_counts()
            snapshot.update(place_counts)
            snapshot["discovered"] = max(snapshot.get("discovered", 0), place_counts["placeids_total"])
            snapshot["scraped"] = max(snapshot.get("scraped", 0), place_counts["placeids_done"])
            snapshot["errors"] = max(snapshot.get("errors", 0), place_counts["placeids_failed"])
        except Exception:
            logger.debug("scraper.placeid_counts_failed", exc_info=True)
        return snapshot

    def _wait_for_queue_drain(self) -> bool:
        last_log = 0.0
        while self._queue_unfinished_tasks() > 0:
            if self._stop_event.is_set():
                return False
            import time
            now = time.monotonic()
            if now - last_log >= 30:
                logger.info(
                    "scraper.phase2_draining queue_depth=%d unfinished=%d",
                    self._queue.qsize(),
                    self._queue_unfinished_tasks(),
                )
                last_log = now
            time.sleep(1)
        return True

    def _install_signal_handler(self) -> None:
        if self._signal_installed:
            return
        try:
            signal.signal(signal.SIGINT, lambda *_: self.stop())
            self._signal_installed = True
        except (ValueError, OSError):
            # Not running in main thread — skip handler installation.
            pass
