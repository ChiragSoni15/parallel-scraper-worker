"""Run a skeleton shard against an OpenRouter model, on the runner.

Same input as llm_batch_build.py -- a blob-hosted skeleton shard of
{key, prompt, images[], optional_images[], generation_config} -- but OpenRouter has no
Files API and no batch-job object, so there is nothing to submit and poll: the runner
materialises the images, POSTs each outlet synchronously, and writes the results itself.

Image fetch/resize/drop behaviour is imported from llm_batch_build so the two lanes
send byte-identical images and a model comparison is actually a model comparison.

usage:
  python tools/llm_openrouter_run.py --skeleton-url <blob url> --model qwen/qwen3.8-flash \
      --client kwality --dataset bakeoff --shard shard_001
  python tools/llm_openrouter_run.py --selftest      # no network
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from llm_batch_build import _fetch_or_none, signed, shard_prompt, row_prompt  # noqa: E402

API = "https://openrouter.ai/api/v1/chat/completions"
FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.I)


def build_messages(sk: dict, dim: int, pool: ThreadPoolExecutor,
                   template: str | None = None) -> tuple[list, int]:
    """OpenAI-style content blocks: the prompt, then every image as a data: URI."""
    urls = sk.get("images") or []
    optional = set(sk.get("optional_images") or [])
    imgs = [b for b in pool.map(lambda u: _fetch_or_none(u, dim, u in optional), urls)
            if b is not None]
    content = [{"type": "text", "text": row_prompt(sk, template)}]
    for b in imgs:
        b64 = base64.b64encode(b).decode("ascii")
        content.append({"type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{b64}"}})
    return [{"role": "user", "content": content}], len(imgs)


def extract_json(text: str) -> dict | None:
    """Tolerate a fenced or prose-wrapped object. Returns None if nothing parses."""
    if not text:
        return None
    t = FENCE.sub("", text.strip())
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        pass
    i, j = t.find("{"), t.rfind("}")
    if i == -1 or j <= i:
        return None
    try:
        return json.loads(t[i:j + 1])
    except json.JSONDecodeError:
        return None


def call(messages: list, model: str, key: str, gen: dict, tries: int = 4) -> dict:
    body = {"model": model, "messages": messages,
            "temperature": gen.get("temperature", 0), "top_p": gen.get("topP", 1)}
    # The skeleton disables thinking the Gemini way (thinkingConfig.thinkingBudget);
    # OpenRouter ignores that field, so translate it. This is not an optimisation:
    # Qwen3.8 Flash defaults to reasoning and will spend the WHOLE completion budget
    # thinking, returning content=None for every outlet while still billing. Measured:
    # default 60/60 tokens reasoning + no content; enabled=false 5 tokens + valid JSON,
    # 5.6x cheaper. "exclude" only hides the reasoning, it still burns it.
    if (gen.get("thinkingConfig") or {}).get("thinkingBudget") == 0:
        body["reasoning"] = {"enabled": False}
    if gen.get("maxOutputTokens"):
        body["max_tokens"] = gen["maxOutputTokens"]
    if gen.get("responseSchema"):        # honoured by models that enforce schemas
        body["response_format"] = {"type": "json_schema",
                                   "json_schema": {"name": "extraction", "strict": True,
                                                   "schema": gen["responseSchema"]}}
    elif gen.get("responseMimeType") == "application/json":
        body["response_format"] = {"type": "json_object"}

    last = None
    for attempt in range(tries):
        t0 = time.time()
        try:
            r = requests.post(API, timeout=180, json=body, headers={
                "Authorization": f"Bearer {key}", "Content-Type": "application/json"})
            ms = int((time.time() - t0) * 1000)
            if r.status_code == 200:
                d = r.json()
                txt = (d.get("choices") or [{}])[0].get("message", {}).get("content", "")
                return {"status": "ok", "latency_ms": ms, "raw": txt,
                        "result": extract_json(txt), "usage": d.get("usage") or {}}
            # 429 / 5xx are transient; a 4xx body tells us why and will not improve
            last = f"HTTP {r.status_code}: {r.text[:200]}"
            if r.status_code < 500 and r.status_code != 429:
                return {"status": "error", "latency_ms": ms, "error": last}
        except Exception as exc:                                   # noqa: BLE001
            last = str(exc)[:200]
        time.sleep(3 * (attempt + 1))
    return {"status": "error", "latency_ms": 0, "error": last}


def selftest() -> None:
    assert extract_json('{"a": 1}') == {"a": 1}
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json('here you go:\n{"a": {"b": 2}}\nhope that helps') == {"a": {"b": 2}}
    assert extract_json("no json here") is None
    assert extract_json("") is None

    class _Pool:                       # stands in for ThreadPoolExecutor.map
        @staticmethod
        def map(fn, it): return [b"\xff\xd8jpegbytes" for _ in it]

    msgs, n = build_messages(
        {"prompt": "P", "images": ["u1", "u2"], "optional_images": ["u2"]}, 1536, _Pool())
    assert n == 2, n
    blocks = msgs[0]["content"]
    assert blocks[0] == {"type": "text", "text": "P"}
    assert len(blocks) == 3 and all(b["type"] == "image_url" for b in blocks[1:])
    assert blocks[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    print("selftest ok")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skeleton-url")
    ap.add_argument("--client")
    ap.add_argument("--dataset")
    ap.add_argument("--shard")
    ap.add_argument("--model")
    ap.add_argument("--send-dim", type=int, default=1536)
    ap.add_argument("--workers", type=int, default=4, help="outlets in flight")
    ap.add_argument("--image-workers", type=int, default=16)
    ap.add_argument("--out-dir", default="batch_out")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()

    if a.selftest:
        selftest(); return
    for req in ("skeleton_url", "client", "dataset", "shard", "model"):
        if not getattr(a, req):
            raise SystemExit(f"--{req.replace('_', '-')} is required")

    key = os.environ.get("OPENROUTER_API_KEY", "")
    if not key:
        raise SystemExit("OPENROUTER_API_KEY missing")

    r = requests.get(signed(a.skeleton_url), timeout=120)
    r.raise_for_status()
    rows = [json.loads(ln) for ln in r.text.splitlines() if ln.strip()]
    template = None if all(x.get("prompt") for x in rows) else shard_prompt(a.skeleton_url)
    print(f"{a.shard}: {len(rows)} outlets -> {a.model}", flush=True)

    out = Path(a.out_dir); out.mkdir(parents=True, exist_ok=True)
    path = out / f"{a.client}-{a.dataset}-{a.shard}.results.jsonl"

    ok = bad = imgs_sent = 0
    t_start = time.time()
    with ThreadPoolExecutor(max_workers=a.image_workers) as images, \
         ThreadPoolExecutor(max_workers=a.workers) as outlets, \
         path.open("w", encoding="utf-8") as fh:

        def run(sk):
            msgs, n = build_messages(sk, a.send_dim, images, template)
            res = call(msgs, a.model, key, sk.get("generation_config") or {})
            return {"key": sk["key"], "model": a.model, "n_images": n, **res}

        for rec in outlets.map(run, rows):
            imgs_sent += rec["n_images"]
            if rec["status"] == "ok" and rec.get("result"):
                ok += 1
            else:
                bad += 1
                print(f"  {rec['key']}: {rec.get('error') or 'unparseable JSON'}", flush=True)
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")

    wall = time.time() - t_start
    u = {"prompt": 0, "completion": 0}
    for ln in path.read_text(encoding="utf-8").splitlines():
        us = (json.loads(ln).get("usage") or {})
        u["prompt"] += us.get("prompt_tokens", 0) or 0
        u["completion"] += us.get("completion_tokens", 0) or 0
    print(f"\n{a.shard}: ok {ok}, failed {bad}, images {imgs_sent}, "
          f"{wall:.0f}s wall ({wall / max(len(rows), 1):.1f}s/outlet)")
    print(f"tokens: {u['prompt']:,} in, {u['completion']:,} out -> {path}")


if __name__ == "__main__":
    main()
