"""Cheap, coherent catalog snapshots shared by bot artifact readers.

The store owns immutable repository-shaped generations. A context pins one
generation across catalog lookup, cover attachment and preview lookup. Legacy
paths remain authoritative until the first valid runtime publication.
"""
from __future__ import annotations

from collections import OrderedDict
from contextlib import contextmanager
from contextvars import ContextVar
import json
import logging
import os
from pathlib import Path
import sqlite3
import threading
import time

_LOG = logging.getLogger(__name__)
_PIN = ContextVar("osaka_cd_catalog_snapshot", default=None)
_LOCK = threading.RLock()
_CACHE = None
_CACHE_AT = 0.0
_CONFIG = None
_LAST_GOOD = None
_VALIDATED = OrderedDict()
_VALIDATORS = []
_UNSUBSCRIBE = None
_MAX_GENERATIONS = 8


def invalidate(snapshot=None):
    """Safe from store worker threads; readers check the pointer on next use."""
    global _CACHE_AT
    with _LOCK:
        _CACHE_AT = 0.0


def register_validator(callback):
    """Register a synchronous bounded reader check before acknowledging data."""
    with _LOCK:
        if callback not in _VALIDATORS:
            _VALIDATORS.append(callback)
            _VALIDATED.clear()
            invalidate()


def _subscribe():
    global _UNSUBSCRIBE
    if _UNSUBSCRIBE is None:
        try:
            from .events import subscribe
            _UNSUBSCRIBE = subscribe(invalidate)
        except ImportError:
            pass


def _manifest_signature(root):
    result = []
    for relative in ("cogs/tts/cd_song_scores.json", "cogs/tts/cd_song_videos.json",
                     "cogs/tts/cd/previews/index.json"):
        path = root / relative
        stat = path.stat()
        if not 0 < stat.st_size <= 8 * 1024 * 1024:
            raise ValueError("invalid_runtime_catalog_size")
        result.append((stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size, stat.st_ino))
    return tuple(result)


def _check_manifest_shapes(root):
    scores = json.loads((root / "cogs/tts/cd_song_scores.json").read_text(encoding="utf-8"))
    videos = json.loads((root / "cogs/tts/cd_song_videos.json").read_text(encoding="utf-8"))
    previews = json.loads((root / "cogs/tts/cd/previews/index.json").read_text(encoding="utf-8"))
    if not isinstance(scores, list) or len(scores) > 500:
        raise ValueError("invalid_runtime_scores")
    if not isinstance(videos, dict) or not isinstance(videos.get("songs"), dict):
        raise ValueError("invalid_runtime_videos")
    if not isinstance(previews, dict):
        raise ValueError("invalid_runtime_previews")


def _validate_candidate(candidate):
    """Check one immutable generation before either reading or acknowledging it."""
    root = Path(candidate["root"])
    key = (str(root), candidate["generation"], _manifest_signature(root))
    if key not in _VALIDATED:
        _check_manifest_shapes(root)
        for validator in tuple(_VALIDATORS):
            validator(root)
        _VALIDATED[key] = True
        while len(_VALIDATED) > _MAX_GENERATIONS:
            _VALIDATED.popitem(last=False)
    else:
        _VALIDATED.move_to_end(key)


def snapshot():
    """Return the last usable snapshot; no database read in repeated hot calls."""
    global _CACHE, _CACHE_AT, _CONFIG, _LAST_GOOD
    pinned = _PIN.get()
    if pinned is not None:
        return pinned
    config = os.getenv("OSAKA_CD_DATA_ROOT", "").strip()
    with _LOCK:
        if config != _CONFIG:
            _CONFIG, _CACHE, _LAST_GOOD, _CACHE_AT = config, None, None, 0.0
            _VALIDATED.clear()
        now = time.monotonic()
        if _CACHE is not None and now < _CACHE_AT:
            return _CACHE
        _subscribe()
        try:
            from .store import runtime_snapshot
            candidate = dict(runtime_snapshot())
        except (ImportError, OSError, ValueError, sqlite3.Error) as exc:
            if _LAST_GOOD is not None:
                candidate = dict(_LAST_GOOD)
            else:
                candidate = {"generation": None, "initialized": False, "root": ""}
            _LOG.debug("[cd_catalog] runtime pointer unavailable: %s", type(exc).__name__)
        if candidate.get("initialized") and candidate.get("generation"):
            try:
                _validate_candidate(candidate)
                _LAST_GOOD = dict(candidate)
            except (OSError, ValueError, KeyError, TypeError, RecursionError, UnicodeError) as exc:
                _LOG.warning("[cd_catalog] runtime generation refused: %s", type(exc).__name__)
                candidate = dict(_LAST_GOOD) if _LAST_GOOD is not None else {
                    "generation": None, "initialized": False, "root": ""}
        _CACHE, _CACHE_AT = candidate, now + 0.5
        return _CACHE


@contextmanager
def pin_snapshot(value=None):
    """Nested operations inherit their caller's immutable generation."""
    current = _PIN.get()
    selected = current if current is not None else (dict(value) if value is not None else snapshot())
    token = _PIN.set(selected)
    try:
        yield selected
    finally:
        _PIN.reset(token)


@contextmanager
def unpin_snapshot():
    """Background tasks follow publications instead of inheriting a request."""
    token = _PIN.set(None)
    try:
        yield
    finally:
        _PIN.reset(token)


def data_root(fallback):
    selected = snapshot()
    return Path(selected["root"]) / "cogs/tts" if selected.get("initialized") else Path(fallback)


def acknowledge(selected=None):
    """Mark only the generation whose reader checks succeeded as activated."""
    chosen = selected or snapshot()
    if not chosen.get("initialized") or not chosen.get("generation"):
        return False
    try:
        # Explicit pins are used while panels upload their own attachments.
        # They must never bypass the checks required for an installed job.
        with _LOCK:
            _validate_candidate(chosen)
        from .store import get_store
        return bool(get_store().acknowledge(chosen["generation"]))
    except (ImportError, OSError, ValueError, KeyError, TypeError, RecursionError, UnicodeError, sqlite3.Error):
        return False
