"""Generic LLM-batch chunk builder (runs on a fleet runner).

Turns a *skeleton* shard (text-only requests + media URLs) into a full Gemini Batch input file
(images inlined as base64), uploads it to the Files API, and creates the batch job. Client-agnostic:
all prompt/schema logic lives with the client that wrote the skeleton.

Skeleton line (JSONL, one per request):
  {"key": "<id>", "prompt": "<full text prompt>", "images": ["<blob url>", ...],
   "generation_config": {...gemini generationConfig...}}

Job discovery: the batch job's display_name is "<client>/<dataset>/<shard>", so the submitting side
can reconstruct its ledger with client.batches.list() - no shared state store needed.

env: LLM_BATCH_GEMINI_KEY, MEDIA_BLOB_READ_SAS (query string or full container SAS URL)
usage: python tools/llm_batch_build.py --skeleton-url <blob url of shard jsonl> --client flora --dataset riyadh
         --shard shard_003 --model gemini-3.7-flash [--send-dim 1536] [--workers 16]
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path

import requests
from PIL import Image


def sas_query() -> str:
    sas = os.environ.get("MEDIA_BLOB_READ_SAS", "")
    if not sas:
        raise SystemExit("MEDIA_BLOB_READ_SAS missing")
    return sas.split("?", 1)[1] if "?" in sas else sas.lstrip("?")


def signed(url: str) -> str:
    return url if "sig=" in url else f"{url}?{sas_query()}"


def fetch_resized(url: str, dim: int) -> bytes:
    last = None
    for attempt in range(4):
        try:
            r = requests.get(signed(url), timeout=60)
            if r.status_code == 200:
                im = Image.open(BytesIO(r.content)).convert("RGB")
                im.thumbnail((dim, dim))
                buf = BytesIO()
                im.save(buf, "JPEG", quality=90)
                return buf.getvalue()
            last = f"HTTP {r.status_code}"
        except Exception as exc:  # noqa: BLE001
            last = str(exc)[:120]
        time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"fetch failed {url}: {last}")


def build_line(sk: dict, dim: int, pool: ThreadPoolExecutor) -> tuple[bytes, int]:
    parts = [{"text": sk["prompt"]}]
    imgs = list(pool.map(lambda u: fetch_resized(u, dim), sk.get("images") or []))
    for b in imgs:
        parts.append({"inlineData": {"mimeType": "image/jpeg", "data": base64.b64encode(b).decode("ascii")}})
    req = {"contents": [{"parts": parts}], "generationConfig": sk.get("generation_config") or {}}
    return (json.dumps({"key": sk["key"], "request": req}, ensure_ascii=False) + "\n").encode("utf-8"), len(imgs)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skeleton-url", required=True)
    ap.add_argument("--client", required=True)
    ap.add_argument("--dataset", required=True)
    ap.add_argument("--shard", required=True)
    ap.add_argument("--model", required=True)
    ap.add_argument("--send-dim", type=int, default=1536)
    ap.add_argument("--workers", type=int, default=16)
    ap.add_argument("--out-dir", default="batch_out")
    a = ap.parse_args()

    key = os.environ.get("LLM_BATCH_GEMINI_KEY")
    if not key:
        raise SystemExit("LLM_BATCH_GEMINI_KEY missing")
    from google import genai
    from google.genai import types

    t0 = time.time()
    r = requests.get(signed(a.skeleton_url), timeout=120)
    r.raise_for_status()
    skel = [json.loads(ln) for ln in r.text.splitlines() if ln.strip()]
    print(f"skeleton {a.shard}: {len(skel)} requests, {sum(len(s.get('images') or []) for s in skel)} images")

    out = Path(a.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{a.client}_{a.dataset}_{a.shard}.jsonl"
    n_img = 0
    with path.open("wb") as fh, ThreadPoolExecutor(max_workers=a.workers) as pool:
        for i, sk in enumerate(skel, 1):
            data, k = build_line(sk, a.send_dim, pool)
            fh.write(data)
            n_img += k
            if i % 25 == 0:
                print(f"  built {i}/{len(skel)} ({n_img} images, {path.stat().st_size / 1e6:.0f} MB, {time.time() - t0:.0f}s)", flush=True)
    size = path.stat().st_size
    print(f"built {path.name}: {size / 1e6:.0f} MB in {time.time() - t0:.0f}s")

    client = genai.Client(api_key=key)
    t1 = time.time()
    up = client.files.upload(file=str(path), config=types.UploadFileConfig(display_name=path.name, mime_type="jsonl"))
    print(f"uploaded {up.name} in {time.time() - t1:.0f}s")
    display = f"{a.client}/{a.dataset}/{a.shard}"
    job = client.batches.create(model=a.model, src=up.name, config=types.CreateBatchJobConfig(display_name=display))
    print(f"batch created {job.name} display_name={display} state={job.state}")
    stub = {"job": job.name, "display_name": display, "src_file": up.name, "input_bytes": size, "count": len(skel),
            "keys": [s["key"] for s in skel], "images": n_img, "built_s": round(t1 - t0), "model": a.model}
    (out / f"{a.shard}.job.json").write_text(json.dumps(stub), encoding="utf-8")
    print(json.dumps({k: v for k, v in stub.items() if k != "keys"}))


if __name__ == "__main__":
    sys.stdout.reconfigure(line_buffering=True)
    main()
