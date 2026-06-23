"""HTTP state backend for the PUBLIC Phase 2 worker.

Implements the small surface the worker needs (get_run / claim_place_ids / submit /
reclaim_place_ids) by calling the lease/submit API over HTTPS. Holds only
`WORKER_API_BASE` + `WORKER_TOKEN` — no DB creds, no Drive creds. Retries transient
network/5xx errors with backoff (lease/submit are idempotent on the server side)."""
from __future__ import annotations

import os
import random
import time

import requests


class HttpState:
    def __init__(self, base: str | None = None, token: str | None = None, timeout: float = 60.0):
        self.base = (base or os.environ["WORKER_API_BASE"]).rstrip("/")
        self.token = token or os.environ["WORKER_TOKEN"]
        self.timeout = timeout
        self._s = requests.Session()
        self._s.headers["Authorization"] = f"Bearer {self.token}"

    def _request(self, method: str, path: str, *, json=None, params=None):
        last = None
        for attempt in range(4):
            try:
                r = self._s.request(method, self.base + path, json=json, params=params, timeout=self.timeout)
                if r.status_code >= 500 and attempt < 3:
                    last = RuntimeError(f"{r.status_code}: {r.text[:200]}")
                    time.sleep((0.5 * (2 ** attempt)) + random.uniform(0, 0.3))
                    continue
                r.raise_for_status()
                return r.json()
            except requests.RequestException as exc:
                last = exc
                if attempt < 3:
                    time.sleep((0.5 * (2 ** attempt)) + random.uniform(0, 0.3))
                    continue
                raise
        if last is not None:
            raise last
        raise RuntimeError("unreachable")

    # --- surface the worker calls -------------------------------------------------
    def get_run(self, run_id: str):
        try:
            self._request("GET", "/phase2/progress", params={"run_id": run_id})
            return {"run_id": run_id}
        except requests.HTTPError as exc:
            if exc.response is not None and exc.response.status_code in (403, 404):
                return None
            raise

    def claim_place_ids(self, run_id: str, worker, n: int) -> list[dict]:
        out = self._request("POST", "/phase2/lease",
                            json={"run_id": run_id, "worker": str(worker), "n": int(n)})
        return out.get("pids", [])

    def submit(self, run_id: str, done: list[dict], failed: list[dict]) -> dict:
        return self._request("POST", "/phase2/results",
                            json={"run_id": run_id, "done": done, "failed": failed})

    def reclaim_place_ids(self, run_id: str, older_than_minutes: int = 30) -> int:
        out = self._request("POST", "/phase2/reclaim",
                            json={"run_id": run_id, "older_than_minutes": int(older_than_minutes)})
        return int(out.get("reclaimed", 0))

    def upload_screenshots(self, run_id: str, place_id: str, attempts: int, paths: dict) -> dict:
        """POST page-screenshot bytes (multipart) to the API, which uploads them to Drive.
        `paths` = {kind: local_png_path}. Carries the lease `attempts` so the server can
        reject stale uploads. Best-effort — the worker wraps this in try/except."""
        if not paths:
            return {"accepted": 0, "stale": 0, "kinds": []}
        handles = []
        try:
            files = []
            for kind, p in paths.items():
                fh = open(p, "rb")
                handles.append(fh)
                files.append((kind, (f"{place_id}_{kind}.png", fh, "image/png")))
            r = self._s.post(self.base + "/phase2/screenshots",
                             data={"run_id": run_id, "place_id": place_id, "attempts": int(attempts)},
                             files=files, timeout=self.timeout)
            r.raise_for_status()
            return r.json()
        finally:
            for fh in handles:
                try:
                    fh.close()
                except Exception:
                    pass
