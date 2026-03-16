"""Caching for research briefs and rendered scene outputs.

Stores cached artifacts in a local directory with a JSON index.
Keys are hashed from relevant inputs.
"""

import hashlib
import json
import shutil
import time
from pathlib import Path

CACHE_DIR = Path(__file__).resolve().parent.parent / "manim_cache"
INDEX_PATH = CACHE_DIR / "index.json"

RESEARCH_TTL = 86400  # 24 hours


def _ensure_cache() -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    if not INDEX_PATH.exists():
        INDEX_PATH.write_text("{}", encoding="utf-8")


def _load_index() -> dict:
    _ensure_cache()
    try:
        return json.loads(INDEX_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, FileNotFoundError):
        return {}


def _save_index(index: dict) -> None:
    _ensure_cache()
    INDEX_PATH.write_text(json.dumps(index, indent=2), encoding="utf-8")


def _hash_key(*parts: str) -> str:
    combined = "|".join(parts)
    return hashlib.sha256(combined.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Research brief cache
# ---------------------------------------------------------------------------

def cache_research(topic: str, brief: dict) -> str:
    key = _hash_key("research", topic.lower().strip())
    index = _load_index()

    entry_dir = CACHE_DIR / "research" / key
    entry_dir.mkdir(parents=True, exist_ok=True)
    (entry_dir / "brief.json").write_text(json.dumps(brief, indent=2), encoding="utf-8")

    index[f"research:{key}"] = {
        "type": "research",
        "topic": topic,
        "created_at": time.time(),
        "path": str(entry_dir / "brief.json"),
    }
    _save_index(index)
    return key


def get_cached_research(topic: str) -> dict | None:
    key = _hash_key("research", topic.lower().strip())
    index = _load_index()
    entry = index.get(f"research:{key}")
    if not entry:
        return None

    if time.time() - entry.get("created_at", 0) > RESEARCH_TTL:
        return None

    path = Path(entry["path"])
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None
    return None


# ---------------------------------------------------------------------------
# Rendered scene cache
# ---------------------------------------------------------------------------

def cache_render(scene_id: str, code_hash: str, mp4_path: str) -> str:
    key = _hash_key("render", scene_id, code_hash)
    index = _load_index()

    entry_dir = CACHE_DIR / "renders" / key
    entry_dir.mkdir(parents=True, exist_ok=True)
    dest = entry_dir / Path(mp4_path).name
    shutil.copy2(mp4_path, dest)

    index[f"render:{key}"] = {
        "type": "render",
        "scene_id": scene_id,
        "code_hash": code_hash,
        "created_at": time.time(),
        "path": str(dest),
    }
    _save_index(index)
    return key


def get_cached_render(scene_id: str, code_hash: str) -> str | None:
    key = _hash_key("render", scene_id, code_hash)
    index = _load_index()
    entry = index.get(f"render:{key}")
    if not entry:
        return None
    path = Path(entry["path"])
    if path.exists():
        return str(path)
    return None


# ---------------------------------------------------------------------------
# Context processing cache (PDF/notes/URL)
# ---------------------------------------------------------------------------

def cache_context(input_hash: str, context_data: dict) -> str:
    key = _hash_key("context", input_hash)
    index = _load_index()

    entry_dir = CACHE_DIR / "context" / key
    entry_dir.mkdir(parents=True, exist_ok=True)
    (entry_dir / "context.json").write_text(
        json.dumps(context_data, indent=2), encoding="utf-8"
    )

    index[f"context:{key}"] = {
        "type": "context",
        "created_at": time.time(),
        "path": str(entry_dir / "context.json"),
    }
    _save_index(index)
    return key


def get_cached_context(input_hash: str) -> dict | None:
    key = _hash_key("context", input_hash)
    index = _load_index()
    entry = index.get(f"context:{key}")
    if not entry:
        return None
    if time.time() - entry.get("created_at", 0) > RESEARCH_TTL:
        return None
    path = Path(entry["path"])
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None
    return None
