"""Generic LLM-batch collector (runs on a fleet runner, on a cron).

For every Gemini batch job in this project whose display_name looks like "<client>/<dataset>/<shard>":
  * write/refresh a status stub   -> blob llm-batch/<client>/<dataset>/status/<shard>.json
  * if SUCCEEDED and not yet collected: download results -> blob .../results/<shard>.jsonl,
    then DELETE the Files-API input (frees the 20 GB per-project budget for the next wave)
  * if FAILED / EXPIRED / CANCELLED: stub carries the error; input file deleted too
Idempotent: "collected" == results blob exists (HEAD with the read SAS). Safe to run every few minutes.

env: LLM_BATCH_GEMINI_KEY, MEDIA_BLOB_READ_SAS, MEDIA_BLOB_WRITE_SAS (falls back to PHASE2_SHOT_BLOB_SAS)
usage: python tools/llm_batch_collect.py [--client flora] [--dataset riyadh_p1] [--keep-inputs]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone

import requests

CONTAINER = "https://micromarket.blob.core.windows.net/scraper-media"
TERMINAL_BAD = {"JOB_STATE_FAILED", "JOB_STATE_EXPIRED", "JOB_STATE_CANCELLED"}


def _q(var: str, *fallbacks: str) -> str:
    for v in (var, *fallbacks):
        sas = os.environ.get(v, "")
        if "sig=" in sas:
            return sas.split("?", 1)[1] if "?" in sas else sas.lstrip("?")
    raise SystemExit(f"{var} missing")


def blob_exists(url: str, rq: str) -> bool:
    return requests.head(f"{url}?{rq}", timeout=30).status_code == 200


def blob_put(url: str, data: bytes, wq: str, ct: str = "application/json") -> None:
    r = requests.put(f"{url}?{wq}", data=data, headers={"x-ms-blob-type": "BlockBlob", "Content-Type": ct}, timeout=600)
    if r.status_code not in (200, 201):
        raise RuntimeError(f"PUT {url.rsplit('/', 1)[-1]}: HTTP {r.status_code} {r.text[:160]}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--client")
    ap.add_argument("--dataset")
    ap.add_argument("--keep-inputs", action="store_true", help="do not delete Files-API inputs after collect")
    a = ap.parse_args()
    key = os.environ.get("LLM_BATCH_GEMINI_KEY") or sys.exit("LLM_BATCH_GEMINI_KEY missing")
    rq, wq = _q("MEDIA_BLOB_READ_SAS"), _q("MEDIA_BLOB_WRITE_SAS", "PHASE2_SHOT_BLOB_SAS")
    from google import genai
    client = genai.Client(api_key=key)

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    seen = collected = failed = 0
    for job in client.batches.list():
        dn = getattr(job, "display_name", "") or ""
        parts = dn.split("/")
        if len(parts) != 3:
            continue
        cl, ds, shard = parts
        if (a.client and cl != a.client) or (a.dataset and ds != a.dataset):
            continue
        seen += 1
        base = f"{CONTAINER}/llm-batch/{cl}/{ds}"
        results_url = f"{base}/results/{shard}.jsonl"
        status_url = f"{base}/status/{shard}.json"
        state = getattr(job.state, "name", str(job.state))
        src = getattr(getattr(job, "src", None), "file_name", None)
        stats = getattr(job, "batch_stats", None)
        stub = {"client": cl, "dataset": ds, "shard": shard, "job": job.name, "display_name": dn, "state": state,
                "model": getattr(job, "model", None), "src_file": src,
                "create_time": str(getattr(job, "create_time", "") or ""),
                "update_time": str(getattr(job, "update_time", "") or ""),
                "request_count": getattr(stats, "request_count", None) if stats else None,
                "pending_request_count": getattr(stats, "pending_request_count", None) if stats else None,
                "checked_at": now, "results_url": None, "collected_at": None, "input_deleted": False, "error": None}
        already = blob_exists(results_url, rq)
        if state == "JOB_STATE_SUCCEEDED":
            if not already:
                dest = job.dest
                fname = getattr(dest, "file_name", None)
                if fname:
                    data = client.files.download(file=fname)
                    text = data.decode("utf-8") if isinstance(data, bytes) else str(data)
                else:
                    text = "\n".join(json.dumps(r.to_json_dict() if hasattr(r, "to_json_dict") else r)
                                     for r in (getattr(dest, "inlined_responses", None) or []))
                blob_put(results_url, text.encode("utf-8"), wq, "application/x-ndjson")
                collected += 1
                print(f"collected {dn} -> {results_url} ({len(text) / 1e6:.1f} MB)")
            stub["results_url"] = results_url
            stub["collected_at"] = now
        elif state in TERMINAL_BAD:
            err = getattr(job, "error", None)
            stub["error"] = str(err)[:500] if err else state
            failed += 1
        if (state == "JOB_STATE_SUCCEEDED" or state in TERMINAL_BAD) and src and not a.keep_inputs:
            try:
                client.files.delete(name=src)
                stub["input_deleted"] = True
            except Exception as exc:  # noqa: BLE001
                gone = "404" in str(exc) or "not found" in str(exc).lower()
                stub["input_deleted"] = gone
                if not gone:
                    stub["input_delete_error"] = str(exc)[:300]
                    print(f"  files.delete({src}) failed: {str(exc)[:200]}")
        blob_put(status_url, json.dumps(stub).encode("utf-8"), wq)
        print(f"{dn:48} {state:24} pending={stub['pending_request_count']} collected={'yes' if stub['results_url'] else 'no'} input_deleted={stub['input_deleted']}")
    print(json.dumps({"jobs_seen": seen, "newly_collected": collected, "failed": failed, "checked_at": now}))


if __name__ == "__main__":
    sys.stdout.reconfigure(line_buffering=True)
    main()
