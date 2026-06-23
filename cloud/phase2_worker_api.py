"""Phase 2 worker — API mode (for the PUBLIC worker repo).

Same scrape loop as cloud/phase2_worker.py, but the state backend is the lease/submit
API (cloud/http_state.HttpState) instead of direct Azure SQL. It carries each pid's
`attempts` (the lease version) from /lease through to /results so the server can reject
stale submissions. Holds no DB/Drive creds — only WORKER_API_BASE + WORKER_TOKEN.

    python -m cloud.phase2_worker_api --run-id kwality_jaipur --shard 0 --max-minutes 300
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import tempfile
import time
from pathlib import Path

from parallel_scraper.logging_setup import phase_var, run_id_var, setup_logging

from cloud.http_state import HttpState
from cloud.phase2_common import P2Result, _default_session_factory, _split_image_urls

logger = logging.getLogger(__name__)


def run_worker(run_id, shard, max_minutes=300, batch=20, state=None,
               session_factory=None, flush_interval_s=None, max_places=None) -> P2Result:
    state = state if state is not None else HttpState()
    flush_interval_s = (
        float(os.environ.get("PHASE2_FLUSH_INTERVAL_S", "20"))
        if flush_interval_s is None else flush_interval_s
    )
    phase_var.set("phase2")
    run_id_var.set(run_id)
    if state.get_run(run_id) is None:
        raise RuntimeError(f"run_id not found / not allowed: {run_id}")

    if str(shard) == "0":
        try:
            logger.info("phase2.reclaim run_id=%s reclaimed=%d", run_id, state.reclaim_place_ids(run_id, 30))
        except Exception:
            logger.warning("phase2.reclaim_failed", exc_info=True)

    work_dir = tempfile.mkdtemp(prefix="p2api_")
    session = (session_factory or (lambda **k: _default_session_factory(work_dir)))(work_dir=work_dir)

    result = P2Result()
    started = time.monotonic()
    last_flush = started
    stop_after = max(0.0, float(max_minutes) * 60.0 - 300.0)
    recycle_every = int(os.environ.get("PHASE2_RECYCLE_EVERY", "50"))
    pend_done: list[dict] = []
    pend_failed: list[dict] = []

    def flush():
        nonlocal last_flush
        if pend_done or pend_failed:
            out = state.submit(run_id, list(pend_done), list(pend_failed))
            acc = int(out.get("accepted", 0))
            stale = int(out.get("stale", 0))
            # accepted spans both done+failed; count locally for logging only
            result.done += len([d for d in pend_done])
            result.failed += len([f for f in pend_failed])
            logger.info("phase2.flush submitted=%d accepted=%d stale=%d done_total=%d failed_total=%d rps=%.3f",
                        len(pend_done) + len(pend_failed), acc, stale, result.done, result.failed,
                        (result.done + result.failed) / max(0.001, time.monotonic() - started))
        pend_done.clear()
        pend_failed.clear()
        last_flush = time.monotonic()

    n_since_recycle = 0
    try:
        while True:
            if stop_after and (time.monotonic() - started) > stop_after:
                logger.info("phase2.time_budget_exit run_id=%s shard=%s", run_id, shard)
                break
            if max_places is not None and (result.done + result.failed + len(pend_done) + len(pend_failed)) >= max_places:
                break
            leased = state.claim_place_ids(run_id, str(shard), int(batch))
            if not leased:
                break
            for rec in leased:
                pid = str(rec["place_id"])
                attempts = int(rec["attempts"])
                try:
                    row = session.scrape(pid)
                    if not row or (row.get("name") or "") in ("", "FAILED"):
                        raise RuntimeError("blank-after-warmup / no data")
                    urls = _split_image_urls(row.get("image_urls"))
                    pend_done.append({"place_id": pid, "attempts": attempts,
                                      "data": json.dumps(row, default=str), "image_urls": urls})
                except Exception as exc:
                    logger.warning("phase2.place_failed run_id=%s pid=%s", run_id, pid, exc_info=True)
                    pend_failed.append({"place_id": pid, "attempts": attempts, "error": str(exc)})
                n_since_recycle += 1
                if n_since_recycle >= recycle_every:
                    try:
                        session.recycle()
                    except Exception:
                        logger.debug("recycle_failed", exc_info=True)
                    n_since_recycle = 0
                if (time.monotonic() - last_flush) >= flush_interval_s:
                    flush()
        flush()
    finally:
        try:
            session.close()
        except Exception:
            pass
    logger.info("phase2.complete run_id=%s shard=%s done=%d failed=%d", run_id, shard, result.done, result.failed)
    return result


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--shard", required=True)
    ap.add_argument("--max-minutes", type=float, default=300)
    ap.add_argument("--batch", type=int, default=20)
    ap.add_argument("--max-places", type=int)
    args = ap.parse_args(argv)
    setup_logging(None, level="INFO")
    run_worker(args.run_id, args.shard, max_minutes=args.max_minutes,
               batch=args.batch, max_places=args.max_places)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
