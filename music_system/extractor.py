from __future__ import annotations

import asyncio
import functools
import logging
import re
import time
from typing import Any
from urllib.parse import urlparse

from .models import ExtractedBatch, MusicTrack

logger = logging.getLogger(__name__)

_URL_RE = re.compile(r"https?://", re.IGNORECASE)
_SPOTIFY_RE = re.compile(r"https?://open\.spotify\.com/(track|album|playlist|episode)/", re.IGNORECASE)
_APPLE_RE = re.compile(r"https?://music\.apple\.com/", re.IGNORECASE)
_DEEZER_RE = re.compile(r"https?://www\.deezer\.com/", re.IGNORECASE)


class MusicExtractionError(RuntimeError):
    pass


class MusicExtractor:
    """Camada isolada de yt-dlp.

    Mantém a cog limpa e deixa a troca de provider/estratégia em um lugar só.
    """

    def __init__(
        self,
        *,
        max_playlist_items: int = 25,
        search_results: int = 5,
        timeout_seconds: float = 20.0,
    ) -> None:
        self.max_playlist_items = max(1, int(max_playlist_items))
        self.search_results = max(1, min(10, int(search_results)))
        self.timeout_seconds = max(5.0, float(timeout_seconds))

    def looks_like_url(self, query: str) -> bool:
        return bool(_URL_RE.search(query or ""))

    def is_metadata_only_platform(self, query: str) -> bool:
        q = query or ""
        return bool(_SPOTIFY_RE.search(q) or _APPLE_RE.search(q) or _DEEZER_RE.search(q))

    def _base_opts(self, *, extract_flat: bool = False) -> dict[str, Any]:
        return {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "noplaylist": False,
            "extract_flat": extract_flat,
            "default_search": "ytsearch",
            "source_address": "0.0.0.0",
            "socket_timeout": self.timeout_seconds,
            "retries": 2,
            "fragment_retries": 2,
            "ignoreerrors": True,
            "nocheckcertificate": True,
            # Qualidade boa sem forçar vídeo. Opus/WebM costuma ser o melhor caminho para Discord.
            "format": "bestaudio[acodec=opus]/bestaudio/best",
            "format_sort": ["acodec:opus", "abr", "asr", "proto"],
        }

    async def extract(self, query: str, *, requester_id: int, requester_name: str = "") -> ExtractedBatch:
        query = (query or "").strip()
        if not query:
            raise MusicExtractionError("Envie um link ou texto para pesquisar.")

        if self.is_metadata_only_platform(query):
            # Sem API paga: tentamos ler metadata; se o provider não entregar áudio
            # tocável, pesquisamos por título/artista em uma fonte compatível.
            try:
                batch = await self._extract_url(query, requester_id=requester_id, requester_name=requester_name)
                if any(track.stream_url for track in batch.tracks):
                    return batch
                first = batch.tracks[0] if batch.tracks else None
                metadata_query = " ".join(part for part in (getattr(first, "title", ""), getattr(first, "uploader", "")) if part).strip()
                if metadata_query:
                    return await self.search(metadata_query, requester_id=requester_id, requester_name=requester_name)
            except Exception:
                metadata_query = await self._best_effort_metadata_query(query)
                if metadata_query:
                    return await self.search(metadata_query, requester_id=requester_id, requester_name=requester_name)
            raise MusicExtractionError("Não consegui transformar esse link em uma música tocável.")

        if self.looks_like_url(query):
            return await self._extract_url(query, requester_id=requester_id, requester_name=requester_name)
        return await self.search(query, requester_id=requester_id, requester_name=requester_name)

    async def search(self, query: str, *, requester_id: int, requester_name: str = "") -> ExtractedBatch:
        q = f"ytsearch{self.search_results}:{query}"
        info = await self._run_extract(q, extract_flat=False)
        entries = [e for e in (info.get("entries") or []) if e]
        tracks = [self._track_from_info(e, requester_id=requester_id, requester_name=requester_name, original_url=query) for e in entries]
        if not tracks:
            raise MusicExtractionError("Não encontrei resultados tocáveis.")
        return ExtractedBatch(tracks=tracks, query=query, is_playlist=False)

    async def _extract_url(self, url: str, *, requester_id: int, requester_name: str = "") -> ExtractedBatch:
        info = await self._run_extract(url, extract_flat=False)
        if not info:
            raise MusicExtractionError("Não consegui ler esse link.")

        entries = info.get("entries")
        if entries is not None:
            raw_entries = [e for e in entries if e]
            limited = raw_entries[: self.max_playlist_items]
            tracks = [
                self._track_from_info(e, requester_id=requester_id, requester_name=requester_name, original_url=url)
                for e in limited
            ]
            if not tracks:
                raise MusicExtractionError("A playlist/link não trouxe nenhum item tocável.")
            return ExtractedBatch(
                tracks=tracks,
                query=url,
                is_playlist=True,
                playlist_title=str(info.get("title") or "Playlist"),
                truncated=len(raw_entries) > len(limited),
            )

        track = self._track_from_info(info, requester_id=requester_id, requester_name=requester_name, original_url=url)
        return ExtractedBatch(tracks=[track], query=url, is_playlist=False)

    async def resolve_stream(self, track: MusicTrack, *, force: bool = False) -> MusicTrack:
        # URLs de stream podem expirar. Revalida se estiver velho ou vazio.
        age = time.monotonic() - float(track.resolved_at_monotonic or 0.0)
        if track.stream_url and not force and age < 20 * 60:
            return track

        source = track.webpage_url or track.original_url or track.stream_url
        if not source:
            raise MusicExtractionError("Música sem URL de origem.")

        info = await self._run_extract(source, extract_flat=False)
        if info.get("entries"):
            first = next((e for e in info.get("entries") or [] if e), None)
            if first:
                info = first
        updated = self._track_from_info(
            info,
            requester_id=track.requester_id,
            requester_name=track.requester_name,
            original_url=track.original_url or source,
        )
        track.stream_url = updated.stream_url
        track.webpage_url = updated.webpage_url or track.webpage_url
        track.duration = updated.duration if updated.duration is not None else track.duration
        track.uploader = updated.uploader or track.uploader
        track.thumbnail = updated.thumbnail or track.thumbnail
        track.source = updated.source or track.source
        track.extractor = updated.extractor or track.extractor
        track.is_live = bool(updated.is_live)
        track.resolved_at_monotonic = time.monotonic()
        return track

    async def _run_extract(self, query: str, *, extract_flat: bool = False) -> dict[str, Any]:
        try:
            import yt_dlp  # import tardio para não pesar boot quando música não é usada
        except Exception as exc:  # pragma: no cover
            raise MusicExtractionError("Dependência yt-dlp não está instalada no venv.") from exc

        opts = self._base_opts(extract_flat=extract_flat)

        def _work() -> dict[str, Any]:
            with yt_dlp.YoutubeDL(opts) as ydl:
                result = ydl.extract_info(query, download=False)
                return result or {}

        try:
            return await asyncio.wait_for(asyncio.to_thread(_work), timeout=self.timeout_seconds + 5.0)
        except asyncio.TimeoutError as exc:
            raise MusicExtractionError("A extração demorou demais e foi cancelada.") from exc
        except MusicExtractionError:
            raise
        except Exception as exc:
            logger.debug("yt-dlp extract failed for %r", query, exc_info=True)
            raise MusicExtractionError(f"Não consegui extrair áudio desse link/pesquisa: {exc}") from exc

    async def _best_effort_metadata_query(self, url: str) -> str:
        # Sem credenciais externas: usa slug da URL como busca decente.
        parsed = urlparse(url)
        parts = [p for p in parsed.path.split("/") if p and not p.isdigit()]
        if not parts:
            return ""
        candidate = parts[-1].split("?")[0]
        candidate = re.sub(r"[-_]+", " ", candidate).strip()
        candidate = re.sub(r"\s+", " ", candidate)
        return candidate

    def _track_from_info(self, info: dict[str, Any], *, requester_id: int, requester_name: str = "", original_url: str = "") -> MusicTrack:
        title = str(info.get("title") or info.get("fulltitle") or info.get("id") or "Música sem título").strip()
        webpage_url = str(info.get("webpage_url") or info.get("original_url") or info.get("url") or original_url or "")
        stream_url = str(info.get("url") or "")
        duration_raw = info.get("duration")
        try:
            duration = float(duration_raw) if duration_raw is not None else None
        except Exception:
            duration = None
        uploader = str(info.get("uploader") or info.get("channel") or info.get("creator") or "")
        thumbnail = str(info.get("thumbnail") or "")
        extractor = str(info.get("extractor_key") or info.get("extractor") or "")
        source = extractor or (urlparse(webpage_url).netloc if webpage_url else "")
        is_live = bool(info.get("is_live") or info.get("live_status") == "is_live")
        track = MusicTrack(
            title=title,
            webpage_url=webpage_url,
            requester_id=int(requester_id),
            requester_name=requester_name,
            stream_url=stream_url,
            duration=duration,
            uploader=uploader,
            thumbnail=thumbnail,
            source=source,
            original_url=original_url,
            extractor=extractor,
            is_live=is_live,
        )
        if stream_url:
            track.resolved_at_monotonic = time.monotonic()
        return track
