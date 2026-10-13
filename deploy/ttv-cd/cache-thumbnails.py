#!/usr/bin/env python3
"""Prepare _cd covers with the real music ArchiveMixin, away from panel clicks."""
from __future__ import annotations

import argparse
import asyncio
import contextlib
from datetime import datetime, timezone
import hashlib
import importlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import types
from urllib.parse import urlencode
from urllib.request import urlopen
import zipfile

MAX_IMAGE_BYTES = 2 * 1024 * 1024
VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}\Z")
SLUG = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
ARCHIVE_FILES = ("archive_staging.py", "archive_manifest.py", "archive_pipeline.py", "correspondencia.py", "validade_stream.py")


def archive_module():
    """Load unmodified music sources bundled in this standalone Termux kit."""
    folder = Path(__file__).resolve().parent / "archive"
    if not all((folder / name).is_file() for name in ARCHIVE_FILES):
        root = Path(__file__).resolve().parents[2]
        folder = root / "cogs/musica/runtime_telefone/agente"
    if not all((folder / name).is_file() for name in ARCHIVE_FILES):
        raise RuntimeError("kit incompleto: faltam os módulos do cache de música")
    namespace = "_ttv_cd_archive"
    package = types.ModuleType(namespace)
    package.__path__ = [str(folder)]
    sys.modules[namespace] = package
    return importlib.import_module(f"{namespace}.validade_stream")


def default_catalog() -> Path:
    bundled = Path(__file__).resolve().parent / "cd_song_videos.json"
    return bundled if bundled.is_file() else Path(__file__).resolve().parents[2] / "cogs/tts/cd_song_videos.json"


def atomic_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)


def read_catalog(path: Path) -> dict:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or raw.get("version") != 1 or not isinstance(raw.get("songs"), dict):
        raise ValueError("catálogo de capas inválido")
    if not 1 <= len(raw["songs"]) <= 500:
        raise ValueError("quantidade de capas inválida")
    for slug, item in raw["songs"].items():
        if not SLUG.fullmatch(slug) or not isinstance(item, dict) or not VIDEO_ID.fullmatch(str(item.get("video_id", ""))):
            raise ValueError("identificador de capa inválido")
    return raw


def _matches(item: dict, candidate: dict) -> bool:
    title = str(candidate.get("title", "")).casefold()
    prohibited = ("cover", "remix", "instrumental", "karaoke", "nightcore", "sped up", "slowed", "live", "concert")
    if not title or any(re.search(r"(?<!\w)" + re.escape(word) + r"(?!\w)", title) for word in prohibited):
        return False
    if any(str(term).casefold() in title for term in item.get("excluded_title_terms", []) if term):
        return False
    return any(str(alias).casefold() in title for alias in item.get("title_aliases", [item.get("title", "")]) if alias)


def choose_most_viewed(item: dict, candidates: list[dict]) -> dict | None:
    """One original-vocal version, highest count among the inspected matches."""
    matching = [candidate for candidate in candidates
                if VIDEO_ID.fullmatch(str(candidate.get("video_id", "")))
                and type(candidate.get("view_count")) is int and candidate["view_count"] >= 0
                and _matches(item, candidate)]
    return max(matching, key=lambda candidate: (candidate["view_count"], candidate["video_id"])) if matching else None


def youtube_api_candidates(item: dict, key: str) -> list[dict]:
    # Credentials stay in the process; neither URLs nor exceptions enter reports.
    def fetch(endpoint: str, params: dict) -> dict:
        params = dict(params, key=key)
        with urlopen(f"https://www.googleapis.com/youtube/v3/{endpoint}?{urlencode(params)}", timeout=15) as response:
            payload = response.read(2 * 1024 * 1024 + 1)
        if len(payload) > 2 * 1024 * 1024:
            raise ValueError("resposta de metadados grande demais")
        return json.loads(payload)

    found = fetch("search", {"part": "snippet", "type": "video", "q": item["search_query"],
                            "order": "viewCount", "maxResults": 25, "safeSearch": "none"})
    ids = [str((entry.get("id") or {}).get("videoId", "")) for entry in found.get("items", [])]
    ids.extend(str(entry.get("video_id", "")) for entry in item.get("candidates", []))
    ids.append(item["video_id"])
    ids = list(dict.fromkeys(identifier for identifier in ids if VIDEO_ID.fullmatch(identifier)))[:50]
    if not ids:
        return []
    detailed = fetch("videos", {"part": "snippet,statistics", "id": ",".join(ids)})
    candidates = []
    for entry in detailed.get("items", []):
        with contextlib.suppress(ValueError, TypeError):
            candidates.append({"video_id": entry["id"], "title": entry["snippet"]["title"],
                               "channel": entry["snippet"]["channelTitle"],
                               "view_count": int(entry["statistics"]["viewCount"])})
    return candidates


def yt_dlp_candidates(item: dict) -> list[dict]:
    import yt_dlp
    options = {"quiet": True, "no_warnings": True, "skip_download": True, "extract_flat": True,
               "socket_timeout": 12, "retries": 1, "noplaylist": True, "ignoreerrors": True}
    proxy = os.getenv("HTTPS_PROXY") or os.getenv("https_proxy")
    if proxy:
        options["proxy"] = proxy
    with yt_dlp.YoutubeDL(options) as extractor:
        found = extractor.extract_info(f"ytsearch25:{item['search_query']}", download=False) or {}
    return [{"video_id": str(entry.get("id", "")), "title": str(entry.get("title", "")),
             "channel": str(entry.get("channel") or entry.get("uploader") or ""),
             "view_count": entry.get("view_count")}
            for entry in found.get("entries", []) if isinstance(entry, dict)]


async def prepare(options) -> dict:
    raw = read_catalog(options.catalog)
    archive = archive_module()
    import aiohttp
    # The existing converter is reused unchanged. Respect cloud/user proxies.
    def session(**kwargs):
        return aiohttp.ClientSession(**dict(kwargs, trust_env=True))
    archive.aiohttp = types.SimpleNamespace(ClientTimeout=aiohttp.ClientTimeout, ClientSession=session,
                                          ClientError=aiohttp.ClientError)

    class CoverCache(archive.ArchiveMixin):
        def __init__(self):
            self.ffmpeg_executable = shutil.which("ffmpeg")
            self.ffprobe_executable = shutil.which("ffprobe")
            self.states = {}
            self._active_resolve_tasks = {}
            self._archive_foreground_until = {}
            if not self.ffmpeg_executable or not self.ffprobe_executable:
                raise RuntimeError("FFmpeg/FFprobe ausentes; instale com pkg install ffmpeg")

    cache = CoverCache()
    options.cache_dir.mkdir(parents=True, exist_ok=True)
    prepared, failures = [], []
    key = os.getenv("YOUTUBE_API_KEY") or os.getenv("GOOGLE_YOUTUBE_API_KEY")
    for slug, item in raw["songs"].items():
        if options.refresh_videos:
            try:
                candidates = await asyncio.to_thread(youtube_api_candidates if key else yt_dlp_candidates, item, *([key] if key else []))
                chosen = choose_most_viewed(item, candidates)
                if chosen:
                    item.update(chosen, selection_basis="highest-view-count-among-inspected-original-vocal-matches",
                                checked_at=datetime.now(timezone.utc).isoformat(), inspected_candidates=len(candidates))
                    item["candidates"] = candidates
                    item["webpage_url"] = f"https://www.youtube.com/watch?v={chosen['video_id']}"
                    item["thumbnail_url"] = f"https://i.ytimg.com/vi/{chosen['video_id']}/hqdefault.jpg"
                    item["source_url"] = item["webpage_url"]
                    item["view_count_source"] = "youtube-data-api" if key else "yt-dlp-public-metadata"
                else:
                    item["refresh_status"] = "no-matching-view-count; preserved-curated-video"
            except Exception as error:
                item["refresh_status"] = f"unavailable-{type(error).__name__}; preserved-curated-video"
        video_id = item["video_id"]
        target = options.cache_dir / f"{video_id}.jpg"
        try:
            with tempfile.TemporaryDirectory(prefix=".cd-cover-", dir=options.cache_dir) as temporary:
                folder = Path(temporary)
                cover = None
                cached_data = None
                expected = item.get("thumbnail_sha256")
                if target.is_file() and not target.is_symlink() and 0 < target.stat().st_size <= MAX_IMAGE_BYTES:
                    data = target.read_bytes()
                    # _archive_jpeg may normalize PNG/WebP into JPEG. Only reuse the
                    # original bytes when the cached file already is JPEG; never
                    # publish mislabeled bytes as a .jpg after validating a conversion.
                    if (data.startswith(b"\xff\xd8\xff") and data.endswith(b"\xff\xd9")
                            and (not expected or hashlib.sha256(data).hexdigest() == expected)):
                        cover = await cache._archive_jpeg(target, folder / "validated.jpg")
                        if cover is not None:
                            cached_data = data
                if cover is None:
                    track = {"display_thumbnail": f"https://i.ytimg.com/vi/{video_id}/maxresdefault.jpg",
                             "webpage_url": f"https://www.youtube.com/watch?v={video_id}"}
                    cover = await cache._archive_cover_for_track(track, folder)
                if cover is None:
                    raise ValueError("thumbnail indisponível ou imagem inválida")
                data = cached_data if cached_data is not None else cover.read_bytes()
                atomic_bytes(target, data)
                item["thumbnail_sha256"] = hashlib.sha256(data).hexdigest()
                item["thumbnail_bytes"] = len(data)
                prepared.append({"melody_id": slug, "video_id": video_id, "bytes": len(data), "path": str(target)})
        except Exception as error:
            failures.append({"melody_id": slug, "error": type(error).__name__})
    saved_catalog = options.cache_dir / "cd_song_videos.json"
    atomic_bytes(saved_catalog, json.dumps(raw, ensure_ascii=False, indent=2).encode())
    report = {"ok": not failures, "cache_directory": str(options.cache_dir), "prepared": prepared, "failures": failures,
              "songs": len(raw["songs"]), "selection_note": "Contagens são um snapshot dos candidatos conferidos; não garantem o maior vídeo de todo o YouTube.",
              "panel_network_requests": 0, "worker_runtime_changed": False}
    if options.export_zip and prepared:
        options.export_zip.parent.mkdir(parents=True, exist_ok=True)
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as zipped:
            zipped.writestr("cogs/tts/cd_song_videos.json", saved_catalog.read_bytes())
            for item in prepared:
                zipped.writestr(f"cogs/tts/cd/thumbnails/{item['video_id']}.jpg", Path(item["path"]).read_bytes())
        atomic_bytes(options.export_zip, buffer.getvalue())
        report["bot_cover_patch"] = str(options.export_zip)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, default=default_catalog())
    parser.add_argument("--cache-dir", type=Path, default=Path.home() / ".cache/core-worker/ttv/cd/thumbnails")
    parser.add_argument("--refresh-videos", action="store_true", help="compare metadados novos via chave YouTube existente ou yt-dlp instalado")
    parser.add_argument("--export-zip", type=Path, help="ZIP só das capas para aplicar no atualizador do bot")
    parser.add_argument("--report", type=Path)
    options = parser.parse_args()
    try:
        report = asyncio.run(prepare(options))
    except Exception as error:
        report = {"ok": False, "error": type(error).__name__, "message": "Não foi possível preparar as capas; confira o kit e FFmpeg/Discord/aiohttp instalados."}
    if options.report:
        atomic_bytes(options.report, json.dumps(report, ensure_ascii=False, indent=2).encode())
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
