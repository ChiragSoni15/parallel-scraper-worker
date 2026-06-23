"""JSON-backed query-preset store. One file at inputs/query_presets.json.

Schema:
  {
    "<preset_name>": [
      {"q": "shops", "label": "shops"},
      {"query": "restaurants", "label": "restaurants"},
      {"q": "restaurants", "label": "restaurants"}
    ],
    ...
  }
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Optional

from filelock import FileLock


_LOCK = threading.Lock()


def _ensure_path(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text("{}", encoding="utf-8")


def _read_all(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _write_all(path: Path, data: dict) -> None:
    _ensure_path(path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def list_presets(path: Path) -> dict:
    with FileLock(str(path) + ".lock"), _LOCK:
        return {
            name: _clean_queries(queries)
            for name, queries in _read_all(path).items()
        }


def get_preset(path: Path, name: str) -> Optional[list]:
    return list_presets(path).get(name)


def save_preset(path: Path, name: str, queries: list) -> None:
    """Upsert. `queries` is a list of {q, label} dicts."""
    cleaned = _clean_queries(queries)
    if not cleaned:
        raise ValueError("preset must have at least one query")
    with FileLock(str(path) + ".lock"), _LOCK:
        data = _read_all(path)
        data[name] = cleaned
        _write_all(path, data)


def _clean_queries(queries: list) -> list[dict]:
    cleaned = []
    for q in queries or []:
        if isinstance(q, str):
            qstr = q.strip()
            if qstr:
                cleaned.append({"q": qstr, "label": qstr})
            continue
        if not isinstance(q, dict):
            continue
        qstr = (q.get("q") or q.get("query") or "").strip()
        if not qstr:
            continue
        cleaned.append({"q": qstr, "label": (q.get("label") or qstr).strip()})
    return cleaned


def delete_preset(path: Path, name: str) -> bool:
    with FileLock(str(path) + ".lock"), _LOCK:
        data = _read_all(path)
        if name not in data:
            return False
        del data[name]
        _write_all(path, data)
        return True
