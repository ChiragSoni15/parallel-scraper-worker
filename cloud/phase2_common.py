"""Phase 2 worker helpers shared by the direct-DB worker and the API-mode worker.

These helpers touch neither Azure SQL nor Google Drive — only stdlib + the scraper
engine. They live here (rather than in cloud/phase2_worker.py) so the PUBLIC worker
repo can ship cloud/phase2_worker_api.py without pulling in cloud/sql_state.py or
cloud/storage.py (which import DB/Drive and must stay private).
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass
class P2Result:
    done: int = 0
    failed: int = 0


def _default_session_factory(work_dir: str):
    # Imported INSIDE the factory on purpose: the dry-run test injects a fake
    # session, so importing PlaywrightSession (and Playwright) is deferred to
    # real runs only.
    import inspect

    from parallel_scraper.phase2_metadata import PlaywrightSession

    csv_path = str(Path(work_dir) / "phase2.csv")
    failures = str(Path(work_dir) / "phase2_failures.csv")
    want_shots = os.environ.get("PHASE2_CAPTURE_SCREENSHOTS", "1") != "0"
    kwargs = {}
    # Feature-detect: the screenshot capability lives in the delivery-branch
    # PlaywrightSession; only pass the arg when this build's class supports it.
    if want_shots and "capture_screenshots" in inspect.signature(PlaywrightSession.__init__).parameters:
        kwargs["capture_screenshots"] = True
    return PlaywrightSession(csv_path, failures, **kwargs)


def _split_image_urls(raw) -> list[str]:
    if not raw or raw == "N/A":
        return []
    return [u.strip() for u in str(raw).replace(",", ";").split(";") if u.strip()]
