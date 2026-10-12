"""Local _cd thumbnails; opening the Discord panel never performs HTTP."""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
from pathlib import Path

import discord

_DATA = Path(__file__).parent
_VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}\Z")
_MELODY_ID = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
_MAX_BYTES = 2 * 1024 * 1024


def video_catalog() -> dict[str, dict]:
    """Read public provenance, independent of the singing score catalogue."""
    try:
        raw = json.loads((_DATA / "cd_song_videos.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    songs = raw.get("songs") if isinstance(raw, dict) else None
    return songs if isinstance(songs, dict) else {}


def _cover_bytes(melody_id: str) -> bytes | None:
    if not isinstance(melody_id, str) or not _MELODY_ID.fullmatch(melody_id):
        return None
    item = video_catalog().get(melody_id)
    if not isinstance(item, dict):
        return None
    video_id = item.get("video_id", "")
    if not isinstance(video_id, str) or not _VIDEO_ID.fullmatch(video_id):
        return None
    roots = [_DATA / "cd" / "thumbnails"]
    configured = os.getenv("TTV_CD_THUMBNAIL_CACHE_DIR", "").strip()
    if configured:
        roots.insert(0, Path(configured).expanduser())
    expected = item.get("thumbnail_sha256")
    for root in roots:
        path = root / f"{video_id}.jpg"
        try:
            # An installed cover may not redirect reading outside its cache.
            if path.is_symlink() or not path.is_file() or not 0 < path.stat().st_size <= _MAX_BYTES:
                continue
            data = path.read_bytes()
        except OSError:
            continue
        if not data.startswith(b"\xff\xd8\xff") or not data.endswith(b"\xff\xd9"):
            continue
        if expected and (not isinstance(expected, str) or hashlib.sha256(data).hexdigest() != expected):
            continue
        try:
            # Pillow is already a bot dependency. Only bounded JPEGs reach it.
            from PIL import Image
            with Image.open(io.BytesIO(data)) as picture:
                if picture.format != "JPEG" or not (0 < picture.width <= 768 and 0 < picture.height <= 768):
                    continue
                picture.load()
        except ImportError:
            continue
        except (OSError, ValueError, Image.DecompressionBombError):
            continue
        return data
    return None


def cover_attachment_url(melody_id: str) -> str:
    return f"attachment://ttv-cd-{melody_id}.jpg" if _cover_bytes(melody_id) is not None else ""


def remote_thumbnail_url(melody_id: str) -> str:
    """Pinned public URL; this function never searches or downloads anything."""
    item = video_catalog().get(melody_id)
    video_id = item.get("video_id", "") if isinstance(item, dict) else ""
    return f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg" if isinstance(video_id, str) and _VIDEO_ID.fullmatch(video_id) else ""


def cached_cover_file(melody_id: str) -> discord.File | None:
    """Each send/edit owns a fresh file; no expiring CDN URL is retained."""
    data = _cover_bytes(melody_id)
    if data is None:
        return None
    return discord.File(io.BytesIO(data), filename=f"ttv-cd-{melody_id}.jpg")
