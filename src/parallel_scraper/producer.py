"""Producer thread: walks the OSM grid, runs Phase-1 discovery, dedupes, enqueues."""
from __future__ import annotations

import asyncio
from collections import deque
import logging
import queue
import threading
import time
from typing import Optional

from parallel_scraper.boundary import (
    GridCell,
    boundary_from_polygon,
    fetch_boundary,
    generate_grid,
)
from parallel_scraper.config import ParallelConfig
from parallel_scraper.dedup import DedupCache
from parallel_scraper.logging_setup import cell_id_var, phase_var
from parallel_scraper.phase1_discovery import discover_cell
from parallel_scraper.places_api_discovery import (
    build_key_manager,
    discover_cell_places_api,
)
from parallel_scraper.rate_limit import TokenBucket
from parallel_scraper.state import StateBuffer

logger = logging.getLogger(__name__)

# Rough mean latency of a Places API Text Search call — used only to size the
# Phase-1 worker pool (workers ≈ target_rps × latency).
_EST_CALL_LATENCY_S = 0.6


class AllKeysUnavailableError(RuntimeError):
    """Raised to abort Phase 1 when every API key is permanently unusable
    (disabled by an auth error or daily-exhausted). Surfaced as the producer's
    error so the run ends status='failed' and a resume continues cleanly once
    keys are restored — instead of churning every remaining cell into 'failed'
    and flooding run.errors.log."""


class QueueRefiller(threading.Thread):
    """Feeds the Phase-2 queue from the durable state backlog, decoupling
    Phase 1's discovery rate from Phase 2's drain rate.

    Phase 1 only writes discovered place_ids to state_placeids.csv (status
    'queued') and never touches the queue, so it never blocks on Phase 2. This
    thread is the SOLE producer to the bounded in-memory queue: it repeatedly
    pulls resumable (queued/scraping/failed) ids it hasn't fed yet and enqueues
    them, blocking on a full queue — which back-pressures the refiller, NOT
    Phase 1, and keeps the queue (hence memory) bounded regardless of how far
    Phase 1 races ahead.

    Lifecycle: runs until stopped, or until Phase 1 is done AND every backlog
    id has been fed. `_fed` prevents re-feeding an id already handed to the
    queue (so a place that ends 'failed' this run is not retried mid-run — same
    as before; only a fresh resume re-feeds it)."""

    def __init__(self, out_queue: "queue.Queue", state: StateBuffer,
                 max_attempts: int, stop_event: threading.Event,
                 poll_interval_s: float = 0.5) -> None:
        super().__init__(name="queue-refiller", daemon=True)
        self._queue = out_queue
        self._state = state
        self._max_attempts = max_attempts
        self._stop = stop_event
        self._poll = poll_interval_s
        self._fed: set[str] = set()
        self._producer_done = threading.Event()
        self._stop_feeding = threading.Event()
        self.fed_total = 0

    def producer_finished(self) -> None:
        """Signal that Phase 1 has stopped discovering — once the current
        backlog is fully fed, the refiller may exit."""
        self._producer_done.set()

    def request_stop(self) -> None:
        """Stop feeding immediately (used on run stop / producer error)."""
        self._stop_feeding.set()

    def _stopping(self) -> bool:
        return self._stop.is_set() or self._stop_feeding.is_set()

    def run(self) -> None:
        phase_var.set("refiller")
        logger.info("refiller.start")
        try:
            while not self._stopping():
                # Snapshot the backlog ONCE, then feed the whole batch, blocking
                # per-item on a full queue (paced by consumers, never by Phase
                # 1). We only re-scan state after exhausting the batch — so the
                # O(N) scan + state-lock hold happens rarely, not every loop,
                # which matters at 100K-1M placeids.
                backlog = self._state.placeids_get_resumable(self._max_attempts)
                new = [pid for pid in backlog if pid not in self._fed]
                if new:
                    for pid in new:
                        if self._stopping():
                            break
                        while not self._stopping():
                            try:
                                self._queue.put(pid, timeout=self._poll)
                            except queue.Full:
                                continue  # consumers draining; retry same id
                            self._fed.add(pid)
                            self.fed_total += 1
                            break
                    continue  # re-scan to pick up newly discovered ids
                if self._producer_done.is_set():
                    break     # Phase 1 done and backlog fully fed
                self._stop.wait(self._poll)  # idle: wait for new discoveries
        finally:
            logger.info("refiller.stop fed_total=%d", self.fed_total)


class Producer(threading.Thread):
    """Phase-1 producer thread. Owns one persistent Playwright Chromium for the run."""

    def __init__(
        self,
        config: ParallelConfig,
        out_queue: "queue.Queue[Optional[str]]",
        dedup: DedupCache,
        rate_limiter: TokenBucket,
        state: StateBuffer,
        stop_event: threading.Event,
        stats,  # Stats — duck-typed
        run_id: str,
        discovered_writer,  # LockedCsvWriter for phase1_discovered.csv
    ) -> None:
        super().__init__(name="producer", daemon=True)
        self._config = config
        self._queue = out_queue
        self._dedup = dedup
        self._limiter = rate_limiter
        self._state = state
        self._stop = stop_event
        self._stats = stats
        self._run_id = run_id
        self._discovered_writer = discovered_writer
        self._error: Optional[BaseException] = None

    @property
    def error(self) -> Optional[BaseException]:
        return self._error

    def run(self) -> None:
        phase_var.set("producer")
        try:
            asyncio.run(self._run_async())
        except Exception as e:
            self._error = e
            logger.exception("producer.crashed")

    async def _run_async(self) -> None:
        cells = self._build_cells()
        if not cells:
            logger.warning("producer.no_cells")
            return

        # Persist grid geometry for the dashboard's live-grid view.
        try:
            from parallel_scraper.boundary import grid_to_geojson
            import json as _json
            run_dir = self._discovered_writer.path.parent if hasattr(
                self._discovered_writer, 'path') else None
            if run_dir is not None:
                grid_path = run_dir / "grid.geojson"
                grid_path.write_text(_json.dumps(grid_to_geojson(cells)),
                                     encoding="utf-8")
        except Exception:
            logger.exception("producer.grid_geojson_save_failed")

        done_keys = self._state.get_done_cell_keys()
        self._stats.set_phase1_totals(
            cells_total=len(cells) * len(self._config.queries),
            grid_cells_total=len(cells),
            queries_total=len(self._config.queries),
            cells_done=len(done_keys),
        )
        logger.info("producer.start total_cells=%d done_already=%d queries=%s",
                    len(cells), len(done_keys), list(self._config.queries))

        if self._config.dry_run:
            await self._run_dry(cells, done_keys)
            return

        if self._config.discovery_backend == "places_api":
            key_manager = build_key_manager(
                daily_quota=self._config.api_key_daily_quota,
                rate_limiter=self._limiter,
                per_key_qpm=self._config.per_key_qpm,
                utilization=self._config.rps_utilization,
                cooldown_s=self._config.key_429_cooldown_s,
                t429_threshold=self._config.key_429_threshold,
            )
            if key_manager.key_count == 0:
                raise RuntimeError(
                    "No Google Places API keys found. Set GOOGLE_MAPS_API_KEY / GOOGLE_MAPS_API_KEYS "
                    "or add inputs/api_keys.csv."
                )
            n_workers = self._auto_phase1_workers(key_manager.key_count)
            logger.info(
                "producer.places_api keys=%d phase1_workers=%d (auto) target_rps=%.1f",
                key_manager.key_count, n_workers, key_manager.target_rps,
            )
            await self._iterate_cells_parallel_places_api(
                cells, done_keys, n_workers, key_manager
            )
            return

        # Live Playwright path: N concurrent contexts driven by an asyncio.Queue.
        from playwright.async_api import async_playwright
        n_workers = max(1, self._config.phase1_workers)
        logger.info("producer.parallel phase1_workers=%d", n_workers)
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=True,
                args=["--disable-dev-shm-usage", "--disable-gpu"],
            )
            try:
                await self._iterate_cells_parallel(browser, cells, done_keys, n_workers)
            finally:
                try:
                    await browser.close()
                except Exception:
                    pass

    async def _run_dry(self, cells, done_keys) -> None:
        """Dry-run: emit fake placeIds without launching Playwright."""
        for ci, cell in enumerate(cells):
            for q in self._config.queries:
                if self._stop.is_set():
                    return
                if (cell.cell_id, q) in done_keys:
                    continue
                cell_id_var.set(cell.cell_id)
                self._stats.record_cell_started()
                self._state.cells_set(cell.cell_id, q, status="in_progress")
                fake_pids = [f"ChIJ_DRY_{cell.cell_id}_{q}_{i:02d}" for i in range(3)]
                for pid in fake_pids:
                    self._discovered_writer.append({
                        "place_id": pid, "cell_id": cell.cell_id,
                        "query": q, "form": "ChIJ", "discovered_at": "",
                    })
                survivors = self._dedup.filter(fake_pids)
                self._enqueue_all(survivors)
                self._stats.discovered += len(survivors)
                self._state.cells_set(cell.cell_id, q, status="done", place_count=len(survivors))
                self._stats.record_cell_result("done")
                if self._max_places_reached():
                    return

    async def _iterate_cells_parallel(self, browser, cells, done_keys,
                                      n_workers: int) -> None:
        """N concurrent Playwright contexts pull (cell, query) tuples from a queue."""
        work_q: asyncio.Queue = asyncio.Queue()
        for cell in cells:
            for q in self._config.queries:
                if (cell.cell_id, q) in done_keys:
                    continue
                await work_q.put((cell, q))

        if work_q.empty():
            logger.info("producer.no_work all cells already done")
            return

        async def worker(worker_id: int) -> None:
            ctx = await browser.new_context(
                viewport={"width": 1280, "height": 800},
                locale="en-IN",
                extra_http_headers={"accept-language": "en-IN,en;q=0.9"},
            )
            page = await ctx.new_page()
            try:
                while True:
                    if self._stop.is_set() or self._max_places_reached():
                        return
                    try:
                        cell, q = work_q.get_nowait()
                    except asyncio.QueueEmpty:
                        return
                    await self._process_one_cell(page, cell, q, worker_id)
            finally:
                try:
                    await ctx.close()
                except Exception:
                    pass

        await asyncio.gather(*[worker(i) for i in range(n_workers)])

    def _auto_phase1_workers(self, key_count: int) -> int:
        """Worker pool sizing.

        Lesson from the 33-keys-all-cooling incident: pushing keys × 8 = 264
        workers with no per-task jitter caused instantaneous-burst 429s that
        cascaded all 33 keys into cooldown simultaneously. Google's per-key
        limit is enforced as a token bucket — short bursts above the
        per-second rate trigger 429s even when the per-minute average is
        well under quota.

        New formula: keys × 4 = 132 workers. Each worker now also takes a
        50-200ms random jitter before the API call (see
        _process_one_cell_places_api_sync). Combined: 132 workers spread
        over a 125ms average jitter window = ~1056 RPS peak instead of
        ~5300 RPS peak. Steady-state at 132 workers × ~1.5 RPS each =
        ~200 RPS total = 6 RPS/key = 60% of ceiling. Headroom for normal
        burstiness without 429s.

        Current sizing uses keys * 10 because per-key slots now throttle
        actual request starts; workers only keep enough backlog to saturate
        keys while some tasks wait on pagination.

        Cap at 400 for very-large key fleets; cfg.phase1_workers acts as
        a floor for manual override.
        """
        cfg = self._config
        target = max(32, key_count * 10)
        return max(1, min(400, max(cfg.phase1_workers, target)))

    async def _iterate_cells_parallel_places_api(self, cells, done_keys,
                                                n_workers: int, key_manager) -> None:
        """Thread-pool dispatch for Phase 1 (web_app parity, Steps 2-3 of perf plan).

        Replaces the previous asyncio.gather + asyncio.Queue worker loop. The
        plain ThreadPoolExecutor pattern has three big wins:

        1. Workers stay alive for the entire job — no silent decay on
           QueueEmpty. The executor's internal scheduling reuses threads
           across tasks.
        2. Each thread keeps its own requests.Session (TCP+TLS keep-alive) —
           cuts ~150ms per Places API call.
        3. No asyncio.to_thread roundtrip per call — the HTTP request is on
           the worker thread directly, fewer context switches.

        We run the executor inside asyncio.to_thread so the outer producer
        coroutine stays async (matters for the SSE/stats heartbeats).
        """
        await asyncio.to_thread(
            self._run_threadpool_places_api, cells, done_keys, n_workers, key_manager
        )

    def _run_threadpool_places_api(self, cells, done_keys,
                                   n_workers: int, key_manager) -> None:
        import concurrent.futures
        # Build the work list. Same skip-if-done logic as before — resume
        # safety preserved (state_cells.csv "done" rows are the source of truth).
        queries = tuple(self._config.queries)
        total_tasks = sum(
            1
            for cell in cells
            for q in queries
            if (cell.cell_id, q) not in done_keys
        )
        if not total_tasks:
            logger.info("producer.no_work all cells already done")
            return

        logger.info(
            "producer.threadpool_start tasks=%d workers=%d keys=%d",
            total_tasks, n_workers, key_manager.key_count,
        )

        def _iter_tasks():
            for cell in cells:
                for q in queries:
                    if (cell.cell_id, q) in done_keys:
                        continue
                    yield cell, q

        # Periodic housekeeping (key ledger + dashboard set_api_keys + rate
        # update) used to fire on every cell completion — at 250 cells/s that
        # was 250 dict copies + 250 ledger I/Os per second. Move it to a
        # debounced cadence (every ~50 completions) so the hot path only
        # writes csvs through the state buffer.
        completion_count = 0
        housekeep_every = 50
        started_at = time.monotonic()
        last_progress_log = started_at
        completion_times: deque[float] = deque()

        def _housekeep(force: bool = False) -> None:
            try:
                key_manager.flush_to_ledger(force=force)
                self._stats.set_api_keys(key_manager.get_key_status())
                key_manager.sync_rate()
                self._stats.set_discovery_rate(
                    key_manager.target_rps, key_manager.available_key_count
                )
            except Exception:
                logger.exception("producer.housekeep_failed")

        def _log_batch_progress(force: bool = False) -> None:
            nonlocal last_progress_log
            now = time.monotonic()
            if not force and completion_count % 500 != 0 and (now - last_progress_log) < 30:
                return
            snap = self._stats.snapshot()
            while completion_times and completion_times[0] < now - 60:
                completion_times.popleft()
            rate_window_s = min(60.0, max(1.0, now - started_at))
            rate_1m = len(completion_times) / rate_window_s
            key_status = key_manager.get_key_status()
            cooling_keys = sum(1 for k in key_status if k.get("cooling_down"))
            remaining = max(0, total_tasks - completion_count)
            eta_s = remaining / rate_1m if rate_1m > 0 else 0
            logger.info(
                "producer.places_api_batch done=%d failed=%d partial=%d elapsed_s=%.1f "
                "rate_1m=%.1f api_calls=%d target_rps=%.1f active_keys=%d cooling_keys=%d "
                "in_flight=%d remaining=%d eta_s=%.0f",
                int(snap.get("cells_done") or 0),
                int(snap.get("cells_failed") or 0),
                int(snap.get("cells_partial") or 0),
                float(snap.get("elapsed_s") or 0),
                rate_1m,
                int(snap.get("api_calls") or 0),
                float(getattr(key_manager, "target_rps", 0.0) or 0.0),
                int(getattr(key_manager, "available_key_count", 0) or 0),
                cooling_keys,
                int(snap.get("cells_in_flight") or 0),
                remaining,
                eta_s,
            )
            last_progress_log = now

        worker_counter = [0]
        worker_counter_lock = __import__("threading").Lock()

        def _next_worker_id() -> int:
            with worker_counter_lock:
                wid = worker_counter[0] % max(1, n_workers)
                worker_counter[0] += 1
                return wid

        def _do_one(task):
            cell, q = task
            wid = _next_worker_id()
            try:
                return self._process_one_cell_places_api_sync(cell, q, wid, key_manager)
            except Exception:
                logger.exception(
                    "producer.worker_cell_failed worker=%d cell=%s query=%s",
                    wid, cell.cell_id, q,
                )
                return "failed"

        try:
            with concurrent.futures.ThreadPoolExecutor(
                max_workers=n_workers,
                thread_name_prefix="p1-http",
            ) as ex:
                task_iter = iter(_iter_tasks())
                pending: set[concurrent.futures.Future] = set()
                window = max(n_workers, min(total_tasks, n_workers * 4))

                def _submit_next() -> bool:
                    if self._stop.is_set() or self._max_places_reached():
                        return False
                    try:
                        task = next(task_iter)
                    except StopIteration:
                        return False
                    pending.add(ex.submit(_do_one, task))
                    return True

                for _ in range(window):
                    if not _submit_next():
                        break
                while pending:
                    done, pending = concurrent.futures.wait(
                        pending,
                        return_when=concurrent.futures.FIRST_COMPLETED,
                    )
                    for fut in done:
                        fut.result()
                        completion_count += 1
                        completion_times.append(time.monotonic())
                        if completion_count % housekeep_every == 0:
                            _housekeep(force=False)
                        _log_batch_progress(force=False)
                        if self._stop.is_set() or self._max_places_reached():
                            for f in pending:
                                f.cancel()
                            pending.clear()
                            break
                        # Circuit breaker: if every key is permanently unusable
                        # (auth-disabled or daily-exhausted) — NOT merely cooling
                        # down from 429s — abort now instead of grinding every
                        # remaining cell into "failed" and flooding the logs.
                        if getattr(key_manager, "live_key_count", 1) == 0:
                            for f in pending:
                                f.cancel()
                            pending.clear()
                            logger.error(
                                "producer.all_keys_unavailable done=%d remaining=%d "
                                "— aborting Phase 1; restore keys and resume",
                                completion_count, max(0, total_tasks - completion_count),
                            )
                            raise AllKeysUnavailableError(
                                f"All {key_manager.key_count} Places API keys are unusable "
                                f"(auth-disabled or daily-exhausted) after {completion_count} cells"
                            )
                        _submit_next()
                    if self._stop.is_set() or self._max_places_reached():
                        for f in pending:
                            f.cancel()
                        pending.clear()
                        break
                    # fut.result() — exceptions already logged in _do_one
        finally:
            # Final flush so the billing-day ledger captures every call.
            _housekeep(force=True)
            if completion_count:
                _log_batch_progress(force=True)

    def _process_one_cell_places_api_sync(self, cell, q: str, worker_id: int,
                                          key_manager) -> str:
        """Sync per-cell HTTP call (Steps 1-3 of perf plan).

        Called by the ThreadPoolExecutor in _run_threadpool_places_api. The
        key manager paces each API key; there is no asyncio.to_thread
        roundtrip per HTTP request.
        discover_cell_places_api uses a thread-local requests.Session (Step 3)
        for TCP+TLS keep-alive — patched in places_api_discovery.py.
        """
        cell_id_var.set(cell.cell_id)
        # window — instantaneous burst far above Google's per-second token
        # Per-key slots in LocalAPIKeyManager do the pacing now; workers only
        # provide enough backlog to keep those slots full.
        self._stats.record_cell_started()
        status_for_stats = "failed"
        self._state.cells_set(cell.cell_id, q, status="in_progress")
        wid = f"p1-{worker_id}"
        self._stats.worker_set(wid, phase=1, state="busy",
                               task=f"cell {cell.cell_id}", query=q)
        t0 = time.monotonic()
        try:
            bbox = None
            if self._config.places_use_location_restriction:
                bbox = {
                    "north": cell.bbox_north, "south": cell.bbox_south,
                    "east": cell.bbox_east, "west": cell.bbox_west,
                }
            discovered, ok_count, fail_code = discover_cell_places_api(
                key_manager,
                q,
                cell.centroid_lat,
                cell.centroid_lon,
                cell.cell_id,
                self._config.grid_size_meters,
                self._config.places_tier,
                self._config.places_language_code,
                self._config.places_region_code,
                bbox,
            )
        except Exception as e:
            logger.exception("producer.places_api_cell_failed cell=%s query=%s worker=%d",
                             cell.cell_id, q, worker_id)
            self._state.cells_set(cell.cell_id, q, status="failed",
                                  error_msg=str(e)[:500])
            self._stats.record_cell_result(status_for_stats)
            self._stats.worker_set(wid, phase=1, state="idle", task="—", query="")
            return

        # Record API call cost. Per-key ledger flush + dashboard refresh is
        # debounced to once per ~50 completions in _run_threadpool_places_api,
        # so this hot path stays cheap.
        self._stats.record_api_calls(int(ok_count or 0), self._config.places_tier)

        # Batched append: ONE filelock acquire for the whole cell's discoveries
        # instead of one per place. With 330 workers and ~10 places per cell,
        # the per-row pattern was the actual cause of 47/330 worker busy ratio
        # (workers spent 80%+ of their time waiting for the OS filelock).
        if discovered:
            self._discovered_writer.append_many(
                {
                    "place_id": d.place_id,
                    "cell_id": cell.cell_id,
                    "query": q,
                    "form": d.place_id_form,
                    "lat": "" if d.lat is None else f"{d.lat:.7f}",
                    "lng": "" if d.lng is None else f"{d.lng:.7f}",
                    "discovered_at": "",
                }
                for d in discovered
            )

        pids = [d.place_id for d in discovered]
        survivors = self._dedup.filter(pids)
        self._enqueue_all(survivors)
        self._stats.discovered += len(survivors)
        elapsed = time.monotonic() - t0
        if fail_code == 429:
            logger.warning(
                "producer.places_api_cell_ratelimited cell=%s query=%s worker=%d found=%d elapsed=%.1fs",
                cell.cell_id, q, worker_id, len(survivors), elapsed,
            )
            self._state.cells_set(cell.cell_id, q, status="failed",
                                  place_count=len(survivors),
                                  error_msg="all key attempts rate-limited (HTTP 429)")
        elif fail_code is not None and ok_count == 0:
            # Zero successful pages AND a hard error (e.g. every key attempt hit
            # an auth 400/403). The cell got NO coverage — mark it "failed" so a
            # resume retries it. Marking "done" here (the old behavior) silently
            # skipped it forever, leaving a permanent coverage hole.
            status_for_stats = "failed"
            logger.warning(
                "producer.places_api_cell_no_coverage cell=%s query=%s worker=%d fail_code=%s elapsed=%.1fs",
                cell.cell_id, q, worker_id, fail_code, elapsed,
            )
            self._state.cells_set(cell.cell_id, q, status="failed",
                                  place_count=0,
                                  error_msg=f"No Places API coverage: HTTP {fail_code}")
        else:
            # fail_code is None (clean) OR a partial after >=1 successful page
            # (real coverage) — keep these as done.
            status_for_stats = "partial" if fail_code is not None else "done"
            error_msg = "" if fail_code is None else f"Partial Places API result: HTTP {fail_code}"
            logger.debug(
                "producer.places_api_cell_done cell=%s query=%s worker=%d found=%d new=%d api_pages=%d status=%s elapsed=%.1fs",
                cell.cell_id, q, worker_id, len(pids), len(survivors), ok_count,
                status_for_stats, elapsed,
            )
            self._state.cells_set(cell.cell_id, q, status="done",
                                  place_count=len(survivors),
                                  error_msg=error_msg)
        self._stats.worker_set(wid, phase=1, state="idle", task="—", query="")

        self._stats.record_cell_result(status_for_stats)
        return status_for_stats

    async def _process_one_cell(self, page, cell, q: str, worker_id: int) -> None:
        cell_id_var.set(cell.cell_id)
        # Token bucket is thread-safe; acquire in executor so we don't block the loop.
        await asyncio.get_event_loop().run_in_executor(None, self._limiter.acquire, 1)
        self._stats.record_cell_started()
        self._state.cells_set(cell.cell_id, q, status="in_progress")
        wid = f"p1-{worker_id}"
        self._stats.worker_set(wid, phase=1, state="busy",
                               task=f"cell {cell.cell_id}", query=q)
        t0 = time.monotonic()
        try:
            bbox = {
                "north": cell.bbox_north, "south": cell.bbox_south,
                "east": cell.bbox_east, "west": cell.bbox_west,
            }
            discovered = await discover_cell(
                page, q, cell.centroid_lat, cell.centroid_lon,
                cell_id=cell.cell_id, bbox=bbox,
                max_scroll=self._config.discovery_max_scroll,
                scroll_settle_ms=self._config.discovery_scroll_settle_ms,
            )
        except Exception as e:
            logger.exception("producer.cell_failed cell=%s query=%s worker=%d",
                             cell.cell_id, q, worker_id)
            self._state.cells_set(cell.cell_id, q, status="failed",
                                  error_msg=str(e)[:500])
            self._stats.record_cell_result("failed")
            self._stats.worker_set(wid, phase=1, state="idle", task="—", query="")
            return

        # Audit-trail write for every discovered placeId — batched.
        if discovered:
            self._discovered_writer.append_many(
                {
                    "place_id": d.place_id,
                    "cell_id": cell.cell_id,
                    "query": q,
                    "form": d.place_id_form,
                    "lat": "" if d.lat is None else f"{d.lat:.7f}",
                    "lng": "" if d.lng is None else f"{d.lng:.7f}",
                    "discovered_at": "",
                }
                for d in discovered
            )

        pids = [d.place_id for d in discovered]
        survivors = self._dedup.filter(pids)
        self._enqueue_all(survivors)
        self._stats.discovered += len(survivors)
        elapsed = time.monotonic() - t0
        logger.info("producer.cell_done cell=%s query=%s worker=%d found=%d new=%d elapsed=%.1fs",
                    cell.cell_id, q, worker_id, len(pids), len(survivors), elapsed)
        self._state.cells_set(cell.cell_id, q, status="done",
                              place_count=len(survivors))
        self._stats.record_cell_result("done")
        self._stats.worker_set(wid, phase=1, state="idle", task="—", query="")

    def _enqueue_all(self, place_ids: list[str]) -> None:
        # Decoupled hand-off: Phase 1 ONLY records discovered place_ids to
        # state_placeids.csv (status 'queued') and never touches the consumer
        # queue, so it never blocks on Phase 2's drain rate. The QueueRefiller
        # (sole queue producer) feeds the bounded queue from this durable
        # backlog. State CSV is the source of truth either way — so this also
        # covers sequential mode (phase2_workers == 0, no refiller/consumers):
        # ids are recorded now and a later run picks them up.
        for pid in place_ids:
            if self._stop.is_set():
                return
            self._state.placeids_set(pid, status="queued")

    def _max_places_reached(self) -> bool:
        cap = self._config.max_places
        if cap is None:
            return False
        return self._stats.discovered >= cap

    def _build_cells(self) -> list[GridCell]:
        if self._config.dry_run:
            # Fake 4 cells for dry-run smoke.
            return [
                GridCell(f"DRY_{i:04d}", 19.0 + 0.01 * i, 72.8 + 0.01 * i,
                         19.01 + 0.01 * i, 19.0 + 0.01 * i,
                         72.81 + 0.01 * i, 72.8 + 0.01 * i)
                for i in range(4)
            ]
        if self._config.boundary_polygon:
            boundary = boundary_from_polygon(self._config.boundary_polygon)
        else:
            boundary = fetch_boundary(self._config.osm_relation_id, self._config.osm_type)
        cells = generate_grid(
            boundary, self._config.grid_size_meters,
            city_prefix=self._config.city_name[:3].upper(),
            grid_type=self._config.grid_type,
        )
        return cells
