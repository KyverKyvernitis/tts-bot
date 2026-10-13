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
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_MAX_BYTES = 2 * 1024 * 1024
_VIDEO_SNAPSHOTS: dict[Path, tuple[tuple[int, ...], dict[str, dict]]] = {}
_VIDEO_FAILED_SIGNATURES: dict[Path, tuple[int, ...]] = {}


def video_catalog() -> dict[str, dict]:
    """Keep the last complete metadata snapshot during interrupted publication."""
    path = _DATA / "cd_song_videos.json"
    previous = _VIDEO_SNAPSHOTS.get(path)
    signature = (0, 0, 0, 0)
    try:
        stat = path.stat()
        signature = (stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size, stat.st_ino)
        if previous is not None and previous[0] == signature:
            return previous[1]
        if _VIDEO_FAILED_SIGNATURES.get(path) == signature:
            return previous[1] if previous is not None else {}
        if not 0 < stat.st_size <= 8 * 1024 * 1024:
            raise ValueError("invalid_cd_video_catalog_size")
        raw = json.loads(path.read_text(encoding="utf-8"))
        songs = raw.get("songs") if isinstance(raw, dict) else None
        if not isinstance(songs, dict) or len(songs) > 500:
            raise ValueError("invalid_cd_video_catalog")
        for identity, item in songs.items():
            if (not isinstance(identity, str) or len(identity) > 90 or not _MELODY_ID.fullmatch(identity)
                    or not isinstance(item, dict) or not isinstance(item.get("video_id"), str)
                    or not _VIDEO_ID.fullmatch(item["video_id"])):
                raise ValueError("invalid_cd_video_entry")
            digest = item.get("thumbnail_sha256")
            if digest is not None and (not isinstance(digest, str) or not _SHA256.fullmatch(digest)):
                raise ValueError("invalid_cd_thumbnail_hash")
        _VIDEO_SNAPSHOTS[path] = (signature, songs)
        _VIDEO_FAILED_SIGNATURES.pop(path, None)
        return songs
    except (OSError, ValueError, TypeError):
        _VIDEO_FAILED_SIGNATURES[path] = signature
        return previous[1] if previous is not None else {}


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
    # Remote provenance can exist before a local cover. Unbound JPEGs do not
    # supply a color or emoji until their exact bytes are in the manifest.
    if not isinstance(expected, str) or not _SHA256.fullmatch(expected):
        return None
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
        if hashlib.sha256(data).hexdigest() != expected:
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


def cover_revision(melody_id: str) -> str:
    """Version an app emoji by its actual bytes, not just the CD id."""
    data = _cover_bytes(melody_id)
    return hashlib.sha256(data).hexdigest()[:12] if data else ""
