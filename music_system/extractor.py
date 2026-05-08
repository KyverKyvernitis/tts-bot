from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import config

from .api_providers import ApiTrackBatch, ApiTrackCandidate, MusicApiProviders, compact_key, is_bad_match_title, normalize_text, title_quality_score
from .errors import MusicExtractionError
from .models import ExtractedBatch, MusicTrack
from .providers import (
    UrlProfile,
    clean_metadata_title,
    describe_url,
    fetch_metadata_title,
    fetch_public_metadata,
    looks_like_url,
    slug_search_terms,
    unique_queries,
)

logger = logging.getLogger(__name__)

_YOUTUBE_CLIENT_SETS: tuple[tuple[str, ...], ...] = (
    ("android", "web"),
    ("ios", "web"),
    ("web",),
)

MUSIC_METADATA_CACHE_TTL_SECONDS = max(0, int(getattr(config, "MUSIC_METADATA_CACHE_TTL_SECONDS", 300)))
MUSIC_STREAM_CACHE_TTL_SECONDS = max(30, int(getattr(config, "MUSIC_STREAM_CACHE_TTL_SECONDS", 480)))
MUSIC_EXTRACT_SOCKET_TIMEOUT_SECONDS = max(3.0, float(getattr(config, "MUSIC_EXTRACT_SOCKET_TIMEOUT_SECONDS", 8.0)))
MUSIC_YTDLP_RETRIES = max(0, int(getattr(config, "MUSIC_YTDLP_RETRIES", 1)))
MUSIC_FRAGMENT_RETRIES = max(0, int(getattr(config, "MUSIC_FRAGMENT_RETRIES", 1)))
MUSIC_EXTRACTOR_RETRIES = max(0, int(getattr(config, "MUSIC_EXTRACTOR_RETRIES", 1)))
MUSIC_PLAYLIST_LAZY_LOAD = bool(getattr(config, "MUSIC_PLAYLIST_LAZY_LOAD", True))
MUSIC_CACHE_MAX_ITEMS = max(20, int(getattr(config, "MUSIC_CACHE_MAX_ITEMS", 160)))
MUSIC_YTDLP_FORMAT = str(getattr(config, "MUSIC_YTDLP_FORMAT", "") or "bestaudio[vcodec=none]/bestaudio/best").strip()
MUSIC_HIGH_QUALITY_MAX_ABR = max(96, int(getattr(config, "MUSIC_HIGH_QUALITY_MAX_ABR", 256)))
MUSIC_MAX_AUDIO_BITRATE_STABLE = max(64, int(getattr(config, "MUSIC_MAX_AUDIO_BITRATE_STABLE", 160)))
MUSIC_HEAVY_LOAD_MAX_ABR = max(64, int(getattr(config, "MUSIC_HEAVY_LOAD_MAX_ABR", 128)))
MUSIC_MIN_LINK_METADATA_CONFIDENCE = str(getattr(config, "MUSIC_MIN_LINK_METADATA_CONFIDENCE", "medium") or "medium").strip().lower()
MUSIC_REJECT_WEAK_LINK_MATCHES = bool(getattr(config, "MUSIC_REJECT_WEAK_LINK_MATCHES", True))
MUSIC_MAX_DURATION_MISMATCH_SECONDS = max(0.0, float(getattr(config, "MUSIC_MAX_DURATION_MISMATCH_SECONDS", 45.0)))
MUSIC_MAX_DURATION_MISMATCH_RATIO = max(0.0, float(getattr(config, "MUSIC_MAX_DURATION_MISMATCH_RATIO", 0.25)))
MUSIC_MAX_GLOBAL_EXTRACTORS = max(1, int(getattr(config, "MUSIC_MAX_GLOBAL_EXTRACTORS", 1)))
_GLOBAL_YTDLP_SEMAPHORE: asyncio.Semaphore | None = None


def _metadata_confidence_rank(value: str) -> int:
    value = (value or "").strip().lower()
    if value == "high":
        return 3
    if value == "medium":
        return 2
    if value == "low":
        return 1
    return 0


def _metadata_confidence(candidate: ApiTrackCandidate | None) -> str:
    if candidate is None or not (candidate.title or "").strip():
        return "none"
    has_artist = bool((candidate.artist or "").strip())
    has_duration = candidate.duration is not None and float(candidate.duration or 0) > 0
    if has_artist and has_duration:
        return "high"
    if has_artist:
        return "medium"
    return "low"


def _duration_tolerance(expected: float | None) -> float:
    if not expected or expected <= 0:
        return 0.0
    return max(MUSIC_MAX_DURATION_MISMATCH_SECONDS, float(expected) * MUSIC_MAX_DURATION_MISMATCH_RATIO)


def _get_global_ytdlp_semaphore() -> asyncio.Semaphore:
    global _GLOBAL_YTDLP_SEMAPHORE
    if _GLOBAL_YTDLP_SEMAPHORE is None:
        _GLOBAL_YTDLP_SEMAPHORE = asyncio.Semaphore(MUSIC_MAX_GLOBAL_EXTRACTORS)
    return _GLOBAL_YTDLP_SEMAPHORE


class MusicExtractor:
    """Extração modular de música.

    Estratégia:
    - links diretos de áudio entram sem yt-dlp;
    - busca textual usa metadata leve primeiro para responder rápido;
    - playlists são lidas de forma limitada e preguiçosa;
    - Spotify/Apple/Deezer viram metadata + busca tocável, sem API paga;
    - YouTube tem fallback por URL canônica, clients alternativos e busca pelo título/id.
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
        self.cookies_file = self._resolve_cookies_file()
        self.api = MusicApiProviders(timeout=min(8.0, self.timeout_seconds))
        self._search_cache: dict[str, tuple[float, list[MusicTrack]]] = {}
        self._single_cache: dict[str, tuple[float, MusicTrack]] = {}
        self._stream_cache: dict[str, tuple[float, MusicTrack]] = {}

    def looks_like_url(self, query: str) -> bool:
        return looks_like_url(query)

    def is_metadata_only_platform(self, query: str) -> bool:
        return describe_url(query).is_metadata_only

    def _clone_track(self, track: MusicTrack, *, requester_id: int | None = None, requester_name: str | None = None, original_url: str | None = None) -> MusicTrack:
        clone = MusicTrack(
            title=track.title,
            webpage_url=track.webpage_url,
            requester_id=int(track.requester_id if requester_id is None else requester_id),
            requester_name=track.requester_name if requester_name is None else requester_name,
            stream_url=track.stream_url,
            duration=track.duration,
            uploader=track.uploader,
            thumbnail=track.thumbnail,
            source=track.source,
            original_url=track.original_url if original_url is None else original_url,
            extractor=track.extractor,
            is_live=track.is_live,
        )
        clone.resolved_at_monotonic = track.resolved_at_monotonic
        clone.resolved_audio_max_abr = int(getattr(track, "resolved_audio_max_abr", 0) or 0)
        return clone

    def _cache_get_tracks(self, cache: dict[str, tuple[float, list[MusicTrack]]], key: str, *, requester_id: int, requester_name: str, original_url: str = "") -> list[MusicTrack] | None:
        if MUSIC_METADATA_CACHE_TTL_SECONDS <= 0:
            return None
        item = cache.get(key)
        if not item:
            return None
        created, tracks = item
        if time.monotonic() - created > MUSIC_METADATA_CACHE_TTL_SECONDS:
            cache.pop(key, None)
            return None
        return [self._clone_track(track, requester_id=requester_id, requester_name=requester_name, original_url=original_url or track.original_url) for track in tracks]

    def _cache_put_tracks(self, cache: dict[str, tuple[float, list[MusicTrack]]], key: str, tracks: list[MusicTrack]) -> None:
        if MUSIC_METADATA_CACHE_TTL_SECONDS <= 0 or not key or not tracks:
            return
        if len(cache) >= MUSIC_CACHE_MAX_ITEMS:
            oldest = sorted(cache.items(), key=lambda item: item[1][0])[: max(1, len(cache) // 8)]
            for old_key, _ in oldest:
                cache.pop(old_key, None)
        cache[key] = (time.monotonic(), [self._clone_track(track, requester_id=track.requester_id, requester_name=track.requester_name) for track in tracks])

    def _cache_get_single(self, key: str, *, requester_id: int, requester_name: str, original_url: str = "") -> MusicTrack | None:
        item = self._single_cache.get(key)
        if not item or MUSIC_METADATA_CACHE_TTL_SECONDS <= 0:
            return None
        created, track = item
        if time.monotonic() - created > MUSIC_METADATA_CACHE_TTL_SECONDS:
            self._single_cache.pop(key, None)
            return None
        return self._clone_track(track, requester_id=requester_id, requester_name=requester_name, original_url=original_url or track.original_url)

    def _cache_put_single(self, key: str, track: MusicTrack) -> None:
        if MUSIC_METADATA_CACHE_TTL_SECONDS <= 0 or not key:
            return
        if len(self._single_cache) >= MUSIC_CACHE_MAX_ITEMS:
            oldest = sorted(self._single_cache.items(), key=lambda item: item[1][0])[: max(1, len(self._single_cache) // 8)]
            for old_key, _ in oldest:
                self._single_cache.pop(old_key, None)
        self._single_cache[key] = (time.monotonic(), self._clone_track(track, requester_id=track.requester_id, requester_name=track.requester_name))

    def _stream_cache_key(self, source: str, audio_max_abr: int | None = None) -> str:
        base = (source or "").strip().lower()
        try:
            max_abr = int(audio_max_abr or 0)
        except Exception:
            max_abr = 0
        return f"{base}|abr:{max_abr}" if max_abr > 0 else base

    def _apply_stream_cache(self, track: MusicTrack, key: str) -> bool:
        item = self._stream_cache.get(key)
        if not item:
            return False
        created, cached = item
        if time.monotonic() - created > MUSIC_STREAM_CACHE_TTL_SECONDS:
            self._stream_cache.pop(key, None)
            return False
        if not cached.stream_url:
            return False
        self._copy_resolved_fields(track, cached)
        return True

    def _put_stream_cache(self, key: str, track: MusicTrack) -> None:
        if not key or not track.stream_url:
            return
        if len(self._stream_cache) >= MUSIC_CACHE_MAX_ITEMS:
            oldest = sorted(self._stream_cache.items(), key=lambda item: item[1][0])[: max(1, len(self._stream_cache) // 8)]
            for old_key, _ in oldest:
                self._stream_cache.pop(old_key, None)
        self._stream_cache[key] = (time.monotonic(), self._clone_track(track, requester_id=track.requester_id, requester_name=track.requester_name))

    def _score_candidate_against_metadata(self, candidate: ApiTrackCandidate, meta: ApiTrackCandidate) -> float:
        wanted = normalize_text(f"{meta.artist} {meta.title}")
        combined = normalize_text(f"{candidate.artist} {candidate.title}")
        score = float(candidate.score)
        if wanted and wanted in combined:
            score += 50
        elif wanted:
            wanted_words = set(wanted.split())
            candidate_words = set(combined.split())
            if wanted_words:
                score += 35 * (len(wanted_words & candidate_words) / len(wanted_words))
        score += title_quality_score(candidate.title, query=wanted, channel=candidate.artist)
        if meta.duration and candidate.duration:
            diff = abs(float(meta.duration) - float(candidate.duration))
            if diff <= 2:
                score += 24
            elif diff <= 5:
                score += 18
            elif diff <= 10:
                score += 10
            elif diff > _duration_tolerance(float(meta.duration)):
                score -= 40
        return score

    def _metadata_is_safe_for_autosearch(self, meta: ApiTrackCandidate | None) -> bool:
        if not MUSIC_REJECT_WEAK_LINK_MATCHES:
            return bool(meta and meta.title)
        return _metadata_confidence_rank(_metadata_confidence(meta)) >= _metadata_confidence_rank(MUSIC_MIN_LINK_METADATA_CONFIDENCE)

    def _metadata_error_message(self, platform: str, meta: ApiTrackCandidate | None = None) -> str:
        title = (getattr(meta, "title", "") or "").strip()
        platform_label = {
            "spotify": "Spotify",
            "deezer": "Deezer",
            "apple": "Apple Music",
            "soundcloud": "SoundCloud",
        }.get((platform or "").lower(), "link")
        if title:
            return (
                f"Não consegui confirmar essa faixa do {platform_label}. O link só retornou `{title}` sem artista/duração confiável. "
                "Para evitar tocar uma música errada, não adicionei nada ao queue."
            )
        return (
            f"Não consegui ler artista/duração confiável desse link do {platform_label}. "
            "Tente configurar a API da plataforma ou pesquise por `nome da música artista`."
        )

    def _candidate_matches_metadata(self, candidate: ApiTrackCandidate, meta: ApiTrackCandidate) -> bool:
        if not (meta.title or "").strip():
            return False
        title_words = set(normalize_text(meta.title).split())
        artist_words = set(normalize_text(meta.artist).split())
        combined_words = set(normalize_text(f"{candidate.artist} {candidate.title}").split())

        if title_words:
            title_hits = len(title_words & combined_words) / max(1, len(title_words))
            if title_hits < 0.65:
                return False

        if artist_words:
            artist_hits = len(artist_words & combined_words) / max(1, len(artist_words))
            if artist_hits < 0.45:
                return False
        elif MUSIC_REJECT_WEAK_LINK_MATCHES:
            return False

        if meta.duration and candidate.duration:
            diff = abs(float(meta.duration) - float(candidate.duration))
            if diff > _duration_tolerance(float(meta.duration)):
                return False
        elif not meta.duration and candidate.duration and candidate.duration > 600:
            title_norm = normalize_text(meta.title)
            if not any(word in title_norm for word in ("mix", "live", "extended", "set", "podcast")):
                return False

        wanted_query = normalize_text(f"{meta.artist} {meta.title}")
        if is_bad_match_title(candidate.title, query=wanted_query):
            return False
        return True

    async def _ytdlp_candidates_for_metadata(self, query: str, *, limit: int, requester_id: int, requester_name: str, original_url: str) -> list[ApiTrackCandidate]:
        candidates: list[ApiTrackCandidate] = []
        for flat in (True, False):
            try:
                info = await self._run_extract(f"ytsearch{max(1, min(8, int(limit)))}:{query}", extract_flat=flat, playlist=True)
            except Exception:
                logger.debug("[music] busca validada via yt-dlp falhou | query=%r flat=%s", query, flat, exc_info=True)
                continue
            for entry in (info.get("entries") or []):
                if not entry:
                    continue
                track = self._track_from_info(entry, requester_id=requester_id, requester_name=requester_name, original_url=original_url or query)
                if not (track.webpage_url or track.stream_url):
                    continue
                candidates.append(ApiTrackCandidate(
                    title=track.title,
                    artist=track.uploader,
                    duration=track.duration,
                    thumbnail=track.thumbnail,
                    webpage_url=track.webpage_url or track.stream_url,
                    source=track.source or "yt-dlp",
                    provider="youtube" if "youtu" in (track.webpage_url or "") or "youtube" in (track.extractor or "").lower() else "yt-dlp",
                    query=query,
                    score=20 + title_quality_score(track.title, query=query, channel=track.uploader),
                ))
            if candidates:
                break
        return candidates

    async def _search_one_for_metadata(self, meta: ApiTrackCandidate, *, requester_id: int, requester_name: str = "", original_url: str = "") -> MusicTrack:
        if not self._metadata_is_safe_for_autosearch(meta):
            raise MusicExtractionError(self._metadata_error_message("link", meta))
        query = " ".join(part for part in (meta.artist, meta.title, "official audio") if part).strip()
        if not query:
            raise MusicExtractionError("Metadata vazia.")

        candidates: list[ApiTrackCandidate] = []
        try:
            candidates.extend(await self.api.search(query, limit=max(5, self.search_results), prefer_youtube=True))
        except Exception:
            logger.debug("[music] busca API validada falhou | query=%r", query, exc_info=True)

        playable = [candidate for candidate in candidates if self._candidate_is_playable_entry(candidate)]
        valid = [candidate for candidate in playable if self._candidate_matches_metadata(candidate, meta)]
        if not valid:
            valid = [candidate for candidate in await self._ytdlp_candidates_for_metadata(
                query,
                limit=max(5, self.search_results),
                requester_id=requester_id,
                requester_name=requester_name,
                original_url=original_url or query,
            ) if self._candidate_matches_metadata(candidate, meta)]
        if not valid:
            raise MusicExtractionError("Encontrei resultados parecidos, mas nenhum confirmou essa música. Não adicionei nada para evitar tocar uma versão errada.")

        valid.sort(key=lambda candidate: self._score_candidate_against_metadata(candidate, meta), reverse=True)
        track = self._track_from_api_candidate(valid[0], requester_id=requester_id, requester_name=requester_name, original_url=original_url or query)
        track.title = self._prefer_clean_title(track.title, meta)
        track.duration = meta.duration or track.duration
        track.thumbnail = meta.thumbnail or track.thumbnail
        track.uploader = meta.artist or track.uploader
        track.source = f"{meta.source} → {track.source or 'YouTube'}"
        return track

    def _resolve_cookies_file(self) -> str:
        value = str(
            getattr(config, "MUSIC_YTDLP_COOKIES_FILE", "")
            or getattr(config, "YTDLP_COOKIES_FILE", "")
            or os.getenv("MUSIC_YTDLP_COOKIES_FILE", "")
            or os.getenv("YTDLP_COOKIES_FILE", "")
            or ""
        ).strip()
        if not value:
            return ""
        try:
            path = Path(value).expanduser()
            return str(path) if path.is_file() else ""
        except Exception:
            return ""

    def _format_for_quality(self, audio_max_abr: int | None = None) -> str:
        """Formato yt-dlp adaptado à carga atual do player.

        `audio_max_abr` limita bitrate quando há vários servidores tocando.
        Com um único servidor, o router passa None/0 para usar o melhor áudio-only
        disponível, sem teto de abr.
        """
        try:
            max_abr = int(audio_max_abr or 0)
        except Exception:
            max_abr = 0
        if max_abr <= 0:
            return MUSIC_YTDLP_FORMAT
        return (
            f"bestaudio[vcodec=none][abr<={max_abr}][acodec=opus][asr=48000]/"
            f"bestaudio[vcodec=none][abr<={max_abr}][acodec=opus]/"
            f"bestaudio[vcodec=none][abr<={max_abr}][ext=m4a]/"
            f"bestaudio[vcodec=none][abr<={max_abr}]/"
            "bestaudio[vcodec=none][acodec=opus][asr=48000]/bestaudio[vcodec=none][acodec=opus]/bestaudio[vcodec=none][ext=m4a]/bestaudio[vcodec=none]/bestaudio"
        )

    def _base_opts(self, *, extract_flat: bool | str = False, playlist: bool = False, youtube_clients: tuple[str, ...] | None = None, audio_max_abr: int | None = None) -> dict[str, Any]:
        opts: dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "noplaylist": not playlist,
            "extract_flat": extract_flat,
            "default_search": "ytsearch",
            "source_address": "0.0.0.0",
            "socket_timeout": min(self.timeout_seconds, MUSIC_EXTRACT_SOCKET_TIMEOUT_SECONDS),
            "retries": MUSIC_YTDLP_RETRIES,
            "fragment_retries": MUSIC_FRAGMENT_RETRIES,
            "extractor_retries": MUSIC_EXTRACTOR_RETRIES,
            "ignoreerrors": playlist,
            "nocheckcertificate": True,
            "geo_bypass": True,
            "cachedir": False,
            "playlistend": self.max_playlist_items if playlist else None,
            "lazy_playlist": MUSIC_PLAYLIST_LAZY_LOAD and bool(playlist),
            "concurrent_fragment_downloads": 1,
            "format": self._format_for_quality(audio_max_abr),
            "http_headers": {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
                "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
            },
        }
        if opts["playlistend"] is None:
            opts.pop("playlistend", None)
        try:
            capped_abr = int(audio_max_abr or 0)
        except Exception:
            capped_abr = 0
        if capped_abr > 0:
            # Modo econômico/estável: prefere Opus/WebM quando estiver dentro do teto,
            # porque costuma ser leve para stream. Alta qualidade não define sort
            # customizado para deixar o yt-dlp escolher o melhor áudio-only real.
            opts["format_sort"] = ["acodec:opus", "asr:48000", "ext:webm:m4a", "abr", "proto"]
        if self.cookies_file:
            opts["cookiefile"] = self.cookies_file
        if youtube_clients:
            opts["extractor_args"] = {"youtube": {"player_client": list(youtube_clients)}}
        return opts

    async def extract(self, query: str, *, requester_id: int, requester_name: str = "") -> ExtractedBatch:
        query = (query or "").strip()
        if not query:
            raise MusicExtractionError("Envie um link ou texto para pesquisar.")

        profile = describe_url(query)
        if profile.is_direct_audio:
            return ExtractedBatch(
                tracks=[self._direct_audio_track(profile, requester_id=requester_id, requester_name=requester_name)],
                query=query,
                is_playlist=False,
            )
        if profile.is_metadata_only:
            return await self._extract_metadata_only(profile, requester_id=requester_id, requester_name=requester_name)
        if profile.is_youtube:
            return await self._extract_youtube(profile, requester_id=requester_id, requester_name=requester_name)
        if profile.is_url:
            return await self._extract_generic_url(profile, requester_id=requester_id, requester_name=requester_name)
        return await self.search(query, requester_id=requester_id, requester_name=requester_name)

    async def search(self, query: str, *, requester_id: int, requester_name: str = "") -> ExtractedBatch:
        query = re.sub(r"\s+", " ", (query or "").strip())
        if not query:
            raise MusicExtractionError("Pesquisa vazia.")
        if len(query) == 1:
            # Uma letra só costuma trazer resultado ruim; ainda tentamos, mas com mensagem melhor se falhar.
            logger.debug("[music] busca muito curta: %r", query)

        cache_key = "search:" + compact_key(query)
        cached = self._cache_get_tracks(self._search_cache, cache_key, requester_id=requester_id, requester_name=requester_name, original_url=query)
        if cached:
            return ExtractedBatch(tracks=cached[: self.search_results], query=query, is_playlist=False)

        # APIs opcionais entram antes do yt-dlp para busca manual: YouTube API
        # devolve URLs de vídeo tocáveis de forma leve; Spotify/Deezer ajudam a
        # ranquear metadata quando configurados. Se não houver key, cai no fluxo antigo.
        try:
            api_candidates = await self.api.search(query, limit=self.search_results, prefer_youtube=True)
            playable = [candidate for candidate in api_candidates if self._candidate_is_playable_entry(candidate)]
            if playable:
                tracks = [
                    self._track_from_api_candidate(candidate, requester_id=requester_id, requester_name=requester_name, original_url=query)
                    for candidate in playable[: self.search_results]
                ]
                tracks = self._dedupe_tracks(tracks)
                self._cache_put_tracks(self._search_cache, cache_key, tracks)
                return ExtractedBatch(tracks=tracks, query=query, is_playlist=False)
        except Exception:
            logger.debug("[music] busca via API falhou | query=%r", query, exc_info=True)

        last_error: Exception | None = None
        for extractor_query, flat in (
            (f"ytsearch{self.search_results}:{query}", True),
            (f"ytsearch{self.search_results}:{query}", False),
            (f"ytsearch1:{query}", True),
        ):
            try:
                info = await self._run_extract(extractor_query, extract_flat=flat, playlist=True)
                entries = [entry for entry in (info.get("entries") or []) if entry]
                tracks = [
                    self._track_from_info(entry, requester_id=requester_id, requester_name=requester_name, original_url=query)
                    for entry in entries
                ]
                tracks = self._dedupe_tracks([track for track in tracks if track.webpage_url or track.stream_url])
                if tracks:
                    tracks = tracks[: self.search_results]
                    self._cache_put_tracks(self._search_cache, cache_key, tracks)
                    return ExtractedBatch(tracks=tracks, query=query, is_playlist=False)
            except Exception as exc:
                last_error = exc
                logger.debug("[music] busca falhou | query=%r flat=%s", query, flat, exc_info=True)

        if last_error:
            raise MusicExtractionError(
                "Não encontrei resultados tocáveis. Tente pesquisar com nome e artista.",
                detail=str(last_error),
            ) from last_error
        raise MusicExtractionError("Não encontrei resultados tocáveis. Tente pesquisar com nome e artista.")

    async def search_one(self, query: str, *, requester_id: int, requester_name: str = "", original_url: str = "") -> MusicTrack:
        """Busca uma única faixa. Usado por links de track Spotify/Deezer/Apple.

        Link de faixa única NUNCA deve enfileirar vários resultados alternativos.
        """
        query = re.sub(r"\s+", " ", (query or "").strip())
        if not query:
            raise MusicExtractionError("Pesquisa vazia.")

        cache_key = "one:" + compact_key(query)
        cached_one = self._cache_get_single(cache_key, requester_id=requester_id, requester_name=requester_name, original_url=original_url or query)
        if cached_one is not None:
            return cached_one

        try:
            api_candidates = await self.api.search(query, limit=max(3, self.search_results), prefer_youtube=True)
            playable = [candidate for candidate in api_candidates if self._candidate_is_playable_entry(candidate)]
            if playable:
                candidate = playable[0]
                track = self._track_from_api_candidate(candidate, requester_id=requester_id, requester_name=requester_name, original_url=original_url or query)
                self._cache_put_single(cache_key, track)
                return track
        except Exception:
            logger.debug("[music] search_one via API falhou | query=%r", query, exc_info=True)

        last_error: Exception | None = None
        for extractor_query, flat in ((f"ytsearch1:{query}", True), (f"ytsearch1:{query}", False)):
            try:
                info = await self._run_extract(extractor_query, extract_flat=flat, playlist=True)
                first = next((entry for entry in (info.get("entries") or []) if entry), None)
                if first:
                    track = self._track_from_info(first, requester_id=requester_id, requester_name=requester_name, original_url=original_url or query)
                    if track.webpage_url or track.stream_url:
                        self._cache_put_single(cache_key, track)
                        return track
            except Exception as exc:
                last_error = exc
                logger.debug("[music] search_one falhou | query=%r flat=%s", query, flat, exc_info=True)
        raise MusicExtractionError("Não encontrei uma alternativa tocável para essa música.", detail=str(last_error or ""))

    async def _extract_metadata_only(self, profile: UrlProfile, *, requester_id: int, requester_name: str = "") -> ExtractedBatch:
        """Lê links Spotify/Deezer/Apple como metadata oficial.

        Faixa única retorna 1 item. Playlist/álbum retorna vários itens leves e
        deixa a resolução do stream para o momento da reprodução. Se a metadata
        do link for fraca demais, o bot recusa em vez de tocar uma música aleatória.
        """
        api_batch: ApiTrackBatch | None = None
        api_error = ""
        try:
            api_batch = await self.api.metadata_batch_from_url(profile.canonical, limit=self.max_playlist_items)
        except Exception as exc:
            api_error = str(exc)
            logger.debug("[music] metadata batch API falhou | url=%s", profile.raw, exc_info=True)

        if api_batch and api_batch.tracks:
            tracks = [
                self._metadata_track_from_candidate(candidate, requester_id=requester_id, requester_name=requester_name, original_url=profile.raw)
                for candidate in api_batch.tracks[: self.max_playlist_items]
                if self._metadata_is_safe_for_autosearch(candidate)
            ]
            tracks = self._dedupe_tracks(tracks)
            if not tracks:
                if api_batch.is_playlist:
                    raise MusicExtractionError("Consegui ler a playlist, mas nenhuma música veio com artista/duração confiável o suficiente para tocar com segurança.")
                raise MusicExtractionError("Li esse link, mas nenhum item trouxe artista/duração confiável o suficiente.")
            return ExtractedBatch(
                tracks=tracks,
                query=profile.raw,
                is_playlist=bool(api_batch.is_playlist or len(tracks) > 1),
                playlist_title=api_batch.title,
                truncated=bool(api_batch.truncated),
            )

        if profile.platform == "spotify" and profile.resource_type == "playlist":
            if not getattr(self.api, "spotify_has_user_auth", False):
                raise MusicExtractionError(
                    "Não consegui abrir essa playlist do Spotify. A API do app está configurada, mas playlists exigem autorização de usuário. "
                    "Gere e configure SPOTIFY_REFRESH_TOKEN ou envie uma música única.",
                )
            if "403" in api_error or "Forbidden" in api_error:
                raise MusicExtractionError(
                    "A Spotify API recusou essa playlist. Gere novamente o SPOTIFY_REFRESH_TOKEN com a conta que tem acesso à playlist "
                    "e com os escopos playlist-read-private, playlist-read-collaborative e user-read-private.",
                    detail=api_error,
                )
            raise MusicExtractionError(
                "Não consegui ler essa playlist do Spotify mesmo com autorização. Verifique se o link é válido e se a conta autorizada tem acesso.",
                detail=api_error,
            )

        if profile.platform in {"spotify", "deezer"} and profile.resource_type in {"playlist", "album"}:
            raise MusicExtractionError(
                "Não consegui ler essa playlist/álbum. Configure a API da plataforma ou envie uma pesquisa/link de música única.",
                detail=api_error,
            )

        # Fallback de faixa única: tenta metadata pública/oEmbed. Se vier só o
        # título, não pesquisa automaticamente para evitar resultados aleatórios.
        public_meta: ApiTrackCandidate | None = None
        try:
            raw_meta = await asyncio.wait_for(
                asyncio.to_thread(fetch_public_metadata, profile.canonical, timeout=min(6.0, self.timeout_seconds)),
                timeout=min(8.0, self.timeout_seconds + 1.0),
            )
            if raw_meta.get("title"):
                title = raw_meta.get("title", "")
                artist = raw_meta.get("artist", "")
                # Se o título público vier no formato "Artista - Música", usa isso
                # como metadata média mesmo sem API oficial.
                if not artist and " - " in title:
                    left, right = title.split(" - ", 1)
                    if left.strip() and right.strip():
                        artist, title = left.strip(), right.strip()
                public_meta = ApiTrackCandidate(
                    title=title,
                    artist=artist,
                    thumbnail=raw_meta.get("thumbnail", ""),
                    webpage_url=profile.raw,
                    source=f"{profile.platform or 'link'} metadata pública",
                    provider=profile.platform or "metadata",
                    query=" ".join(part for part in (artist, title, "official audio") if part),
                    score=25,
                )
        except Exception:
            logger.debug("[music] metadata pública falhou | url=%s", profile.raw, exc_info=True)

        if public_meta is not None:
            if not self._metadata_is_safe_for_autosearch(public_meta):
                raise MusicExtractionError(self._metadata_error_message(profile.platform, public_meta))
            track = self._metadata_track_from_candidate(public_meta, requester_id=requester_id, requester_name=requester_name, original_url=profile.raw)
            return ExtractedBatch(tracks=[track], query=public_meta.search_query, is_playlist=False)

        # Última tentativa: metadata API unitária, se alguma integração conseguir responder.
        api_meta: ApiTrackCandidate | None = None
        try:
            api_meta = await self.api.metadata_from_url(profile.canonical)
        except Exception:
            logger.debug("[music] metadata API unitária falhou | url=%s", profile.raw, exc_info=True)
        if api_meta is not None:
            if not self._metadata_is_safe_for_autosearch(api_meta):
                raise MusicExtractionError(self._metadata_error_message(profile.platform, api_meta))
            track = self._metadata_track_from_candidate(api_meta, requester_id=requester_id, requester_name=requester_name, original_url=profile.raw)
            return ExtractedBatch(tracks=[track], query=api_meta.search_query, is_playlist=False)

        raise MusicExtractionError(self._metadata_error_message(profile.platform, None))

    async def _extract_youtube(self, profile: UrlProfile, *, requester_id: int, requester_name: str = "") -> ExtractedBatch:
        errors: list[str] = []
        candidates = unique_queries(profile.canonical, profile.raw)

        if profile.resource_type == "playlist":
            try:
                return await self._extract_url(profile.canonical, requester_id=requester_id, requester_name=requester_name, youtube=True)
            except Exception as exc:
                errors.append(str(exc))
                logger.debug("[music] youtube playlist extraction failed | url=%s", profile.raw, exc_info=True)

        # Para _play <url do YouTube>, responda rápido: pegue apenas metadata leve
        # e deixe a resolução pesada do stream para o momento real do playback.
        for url in candidates:
            metadata = await self._try_extract_metadata(url, requester_id=requester_id, requester_name=requester_name)
            if metadata and metadata.tracks:
                for track in metadata.tracks:
                    track.original_url = profile.raw
                    if not track.webpage_url:
                        track.webpage_url = profile.canonical
                return metadata

        for url in candidates:
            try:
                return await self._extract_url(url, requester_id=requester_id, requester_name=requester_name, youtube=True)
            except Exception as exc:
                errors.append(str(exc))
                logger.debug("[music] youtube direct extraction failed | url=%s", url, exc_info=True)

        # Fallback leve: pega título público da página ou usa o ID como busca.
        # URL de música única deve retornar apenas um resultado alternativo.
        title = await self._metadata_title(profile.canonical)
        search_queries = unique_queries(title, profile.youtube_video_id)
        for query in search_queries:
            try:
                track = await self.search_one(query, requester_id=requester_id, requester_name=requester_name, original_url=profile.raw)
                track.original_url = profile.raw
                return ExtractedBatch(tracks=[track], query=query, is_playlist=False)
            except Exception as exc:
                errors.append(str(exc))

        detail = " | ".join(e for e in errors if e)[-500:]
        raise MusicExtractionError(
            "Não consegui ler esse link do YouTube. Atualize o yt-dlp e, se o vídeo tiver restrição, configure cookies do navegador.",
            detail=detail,
        )

    async def _extract_generic_url(self, profile: UrlProfile, *, requester_id: int, requester_name: str = "") -> ExtractedBatch:
        if profile.platform == "soundcloud":
            try:
                api_batch = await self.api.metadata_batch_from_url(profile.canonical, limit=self.max_playlist_items)
                if api_batch and api_batch.tracks:
                    tracks = [
                        self._track_from_api_candidate(candidate, requester_id=requester_id, requester_name=requester_name, original_url=profile.raw)
                        for candidate in api_batch.tracks[: self.max_playlist_items]
                    ]
                    tracks = self._dedupe_tracks([track for track in tracks if track.webpage_url or track.stream_url])
                    if tracks:
                        return ExtractedBatch(
                            tracks=tracks,
                            query=profile.raw,
                            is_playlist=bool(api_batch.is_playlist or len(tracks) > 1),
                            playlist_title=api_batch.title,
                            truncated=bool(api_batch.truncated),
                        )
            except Exception:
                logger.debug("[music] SoundCloud API metadata falhou | url=%s", profile.raw, exc_info=True)
        try:
            return await self._extract_url(profile.canonical, requester_id=requester_id, requester_name=requester_name)
        except Exception as exc:
            logger.debug("[music] generic URL direct extraction failed | url=%s", profile.raw, exc_info=True)
            title = await self._metadata_title(profile.canonical)
            slug = slug_search_terms(profile.canonical)
            for query in unique_queries(title, slug):
                try:
                    track = await self.search_one(query, requester_id=requester_id, requester_name=requester_name, original_url=profile.raw)
                    track.original_url = profile.raw
                    return ExtractedBatch(tracks=[track], query=query, is_playlist=False)
                except Exception:
                    logger.debug("[music] generic URL search fallback failed | query=%r", query, exc_info=True)
            raise MusicExtractionError("Não consegui ler esse link nem encontrar uma fonte tocável equivalente.", detail=str(exc)) from exc

    async def _extract_url(self, url: str, *, requester_id: int, requester_name: str = "", youtube: bool = False) -> ExtractedBatch:
        profile = describe_url(url)
        if profile.is_direct_audio:
            return ExtractedBatch(
                tracks=[self._direct_audio_track(profile, requester_id=requester_id, requester_name=requester_name)],
                query=url,
                is_playlist=False,
            )

        # Primeiro tenta metadata/playlist leve. Para música única do YouTube, isso evita erros comuns de player.
        info: dict[str, Any] | None = None
        last_error: Exception | None = None
        runs: list[tuple[bool | str, bool, tuple[str, ...] | None]] = [("in_playlist", True, None), (False, True, None)]
        if youtube:
            runs.extend((False, False, clients) for clients in _YOUTUBE_CLIENT_SETS)
        else:
            runs.append((False, False, None))

        for flat, playlist, clients in runs:
            try:
                info = await self._run_extract(url, extract_flat=flat, playlist=playlist, youtube_clients=clients)
                if info:
                    break
            except Exception as exc:
                last_error = exc
                logger.debug("[music] URL extract failed | url=%s flat=%s playlist=%s clients=%s", url, flat, playlist, clients, exc_info=True)

        if not info:
            raise MusicExtractionError("Não consegui ler esse link.", detail=str(last_error or ""))

        entries = info.get("entries")
        if entries is not None:
            raw_entries = [entry for entry in entries if entry]
            limited = raw_entries[: self.max_playlist_items]
            tracks = [
                self._track_from_info(entry, requester_id=requester_id, requester_name=requester_name, original_url=url)
                for entry in limited
            ]
            tracks = [track for track in tracks if track.webpage_url or track.stream_url]
            if not tracks:
                raise MusicExtractionError("A playlist/link não trouxe nenhum item tocável.")
            return ExtractedBatch(
                tracks=tracks,
                query=url,
                is_playlist=len(tracks) > 1 or bool(info.get("playlist_count")),
                playlist_title=str(info.get("title") or "Playlist"),
                truncated=bool(info.get("playlist_count") and int(info.get("playlist_count") or 0) > len(limited)),
            )

        track = self._track_from_info(info, requester_id=requester_id, requester_name=requester_name, original_url=url)
        if not (track.webpage_url or track.stream_url):
            raise MusicExtractionError("O link foi lido, mas não retornou áudio tocável.")
        return ExtractedBatch(tracks=[track], query=url, is_playlist=False)

    async def resolve_stream(self, track: MusicTrack, *, force: bool = False, audio_max_abr: int | None = None) -> MusicTrack:
        age = time.monotonic() - float(track.resolved_at_monotonic or 0.0)
        try:
            requested_abr = int(audio_max_abr or 0)
        except Exception:
            requested_abr = 0
        resolved_abr = int(getattr(track, "resolved_audio_max_abr", 0) or 0)
        if track.stream_url and not self._is_probably_direct_stream_url(track.stream_url):
            # Flat mode do yt-dlp pode colocar a página do YouTube em info["url"].
            # FFmpeg não consegue tocar página HTML; ele precisa do stream real.
            track.stream_url = ""
            track.resolved_at_monotonic = 0.0
        if track.stream_url and not force and age < MUSIC_STREAM_CACHE_TTL_SECONDS:
            # Reusa stream só quando a qualidade resolvida combina com a qualidade atual.
            # Com 1 servidor, requested_abr=0 significa alta qualidade sem teto; nesse
            # caso não pode reaproveitar stream capado resolvido antes em modo econômico.
            is_direct_track = str(track.extractor or "").lower() == "direct"
            if is_direct_track or (not requested_abr and not resolved_abr) or (requested_abr and resolved_abr == requested_abr):
                return track

        if str(track.extractor or "").lower() == "metadata":
            return await self._resolve_metadata_track(track, force=force, audio_max_abr=audio_max_abr)

        source = track.webpage_url or (track.original_url if looks_like_url(track.original_url) else "") or track.stream_url
        if not source:
            return await self._resolve_metadata_track(track, force=force, audio_max_abr=audio_max_abr)

        profile = describe_url(source)
        if profile.is_metadata_only:
            return await self._resolve_metadata_track(track, force=force, audio_max_abr=audio_max_abr)

        stream_cache_key = self._stream_cache_key(source, audio_max_abr)
        if not force and self._apply_stream_cache(track, stream_cache_key):
            return track

        if profile.is_direct_audio:
            track.stream_url = profile.canonical
            if not track.webpage_url:
                track.webpage_url = profile.canonical
            track.resolved_at_monotonic = time.monotonic()
            track.resolved_audio_max_abr = 0
            self._put_stream_cache(stream_cache_key, track)
            return track

        errors: list[str] = []
        runs: list[tuple[bool | str, bool, tuple[str, ...] | None]] = [(False, False, None)]
        if profile.is_youtube:
            runs.extend((False, False, clients) for clients in _YOUTUBE_CLIENT_SETS)

        for flat, playlist, clients in runs:
            try:
                info = await self._run_extract(source, extract_flat=flat, playlist=playlist, youtube_clients=clients, audio_max_abr=audio_max_abr)
                if info.get("entries"):
                    first = next((entry for entry in info.get("entries") or [] if entry), None)
                    if first:
                        info = first
                updated = self._track_from_info(
                    info,
                    requester_id=track.requester_id,
                    requester_name=track.requester_name,
                    original_url=track.original_url or source,
                )
                if updated.stream_url:
                    updated.resolved_audio_max_abr = requested_abr
                    self._copy_resolved_fields(track, updated)
                    self._put_stream_cache(stream_cache_key, track)
                    return track
            except Exception as exc:
                errors.append(str(exc))
                logger.debug("[music] stream resolve failed | source=%s clients=%s", source, clients, exc_info=True)

        # Último fallback: busca pelo título atual e resolve o melhor resultado apenas uma vez.
        if track.title and not profile.is_metadata_only:
            try:
                candidate = await self.search_one(track.title, requester_id=track.requester_id, requester_name=track.requester_name, original_url=track.original_url or source)
                if candidate.webpage_url != track.webpage_url:
                    await self.resolve_stream(candidate, force=True, audio_max_abr=audio_max_abr)
                    if candidate.stream_url:
                        self._copy_resolved_fields(track, candidate)
                        self._put_stream_cache(stream_cache_key, track)
                        return track
            except Exception as exc:
                errors.append(str(exc))

        detail = " | ".join(errors)[-500:]
        raise MusicExtractionError("Não consegui obter o stream de áudio dessa música.", detail=detail)

    async def _try_extract_metadata(self, url: str, *, requester_id: int, requester_name: str = "") -> ExtractedBatch | None:
        try:
            info = await self._run_extract(url, extract_flat=True, playlist=False)
            if not info:
                return None
            if info.get("entries"):
                tracks = [
                    self._track_from_info(entry, requester_id=requester_id, requester_name=requester_name, original_url=url)
                    for entry in (info.get("entries") or [])
                    if entry
                ]
            else:
                tracks = [self._track_from_info(info, requester_id=requester_id, requester_name=requester_name, original_url=url)]
            tracks = [track for track in tracks if track.title]
            return ExtractedBatch(tracks=tracks, query=url, is_playlist=len(tracks) > 1) if tracks else None
        except Exception:
            return None

    async def _metadata_title(self, url: str) -> str:
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(fetch_metadata_title, url, timeout=min(6.0, self.timeout_seconds)),
                timeout=min(8.0, self.timeout_seconds + 1.0),
            )
        except Exception:
            return ""

    async def _run_extract(
        self,
        query: str,
        *,
        extract_flat: bool | str = False,
        playlist: bool = False,
        youtube_clients: tuple[str, ...] | None = None,
        audio_max_abr: int | None = None,
    ) -> dict[str, Any]:
        try:
            import yt_dlp  # import tardio para não pesar o boot quando música não é usada
        except Exception as exc:  # pragma: no cover
            raise MusicExtractionError("Dependência yt-dlp não está instalada no venv.") from exc

        opts = self._base_opts(extract_flat=extract_flat, playlist=playlist, youtube_clients=youtube_clients, audio_max_abr=audio_max_abr)

        def _work() -> dict[str, Any]:
            with yt_dlp.YoutubeDL(opts) as ydl:
                result = ydl.extract_info(query, download=False)
                return result or {}

        try:
            async with _get_global_ytdlp_semaphore():
                return await asyncio.wait_for(asyncio.to_thread(_work), timeout=self.timeout_seconds + 5.0)
        except asyncio.TimeoutError as exc:
            raise MusicExtractionError("A extração demorou demais e foi cancelada.") from exc
        except MusicExtractionError:
            raise
        except Exception as exc:
            logger.debug("yt-dlp extract failed for %r", query, exc_info=True)
            raise MusicExtractionError(self._friendly_yt_dlp_error(exc), detail=str(exc)) from exc

    def _friendly_yt_dlp_error(self, exc: Exception) -> str:
        text = str(exc)
        lower = text.lower()
        if "drm" in lower:
            return "Essa fonte usa DRM e não pode ser tocada diretamente. Tentei buscar uma alternativa tocável, mas essa tentativa falhou."
        if "unsupported url" in lower:
            return "Essa plataforma/link não foi reconhecida pelo yt-dlp. Vou precisar de outro link ou pesquisa por nome."
        if "private" in lower:
            return "Esse conteúdo parece privado ou sem permissão pública."
        if "sign in" in lower or "login" in lower or "cookies" in lower:
            return "Esse conteúdo precisa de login/cookies para ser lido."
        if "copyright" in lower or "blocked" in lower or "unavailable" in lower:
            return "Esse conteúdo está indisponível, bloqueado ou removido para o bot."
        if "timed out" in lower or "timeout" in lower:
            return "A plataforma demorou demais para responder."
        return f"Não consegui extrair áudio desse link/pesquisa: {text[:180]}"

    def _candidate_is_playable_entry(self, candidate: ApiTrackCandidate) -> bool:
        url = (candidate.webpage_url or "").strip()
        if not url:
            return False
        profile = describe_url(url)
        if profile.is_metadata_only:
            return False
        return profile.is_youtube or profile.is_direct_audio or candidate.provider == "soundcloud"

    def _metadata_track_from_candidate(
        self,
        candidate: ApiTrackCandidate,
        *,
        requester_id: int,
        requester_name: str = "",
        original_url: str = "",
    ) -> MusicTrack:
        title = candidate.title or candidate.search_query or "Música sem título"
        artist = candidate.artist or ""
        display_title = " - ".join(part for part in (artist, title) if part).strip() or title
        track = MusicTrack(
            title=display_title,
            webpage_url="",
            requester_id=int(requester_id),
            requester_name=requester_name,
            duration=candidate.duration,
            uploader=artist,
            thumbnail=candidate.thumbnail,
            source=candidate.source or candidate.provider or "metadata",
            original_url=candidate.webpage_url or original_url or candidate.search_query,
            extractor="metadata",
        )
        return track

    def _metadata_candidate_from_track(self, track: MusicTrack) -> ApiTrackCandidate:
        title = (track.title or "").strip()
        artist = (track.uploader or "").strip()
        if artist:
            prefix = artist.lower() + " - "
            if title.lower().startswith(prefix):
                title = title[len(prefix):].strip()
        elif " - " in title:
            maybe_artist, maybe_title = title.split(" - ", 1)
            if maybe_artist and maybe_title:
                artist, title = maybe_artist.strip(), maybe_title.strip()
        return ApiTrackCandidate(
            title=title or track.title or "Música sem título",
            artist=artist,
            duration=track.duration,
            thumbnail=track.thumbnail,
            webpage_url=track.original_url,
            source=track.source or "metadata",
            provider="metadata",
            query=" ".join(part for part in (artist, title, "official audio") if part),
            score=35,
        )

    async def _resolve_metadata_track(self, track: MusicTrack, *, force: bool = False, audio_max_abr: int | None = None) -> MusicTrack:
        meta = self._metadata_candidate_from_track(track)
        candidate = await self._search_one_for_metadata(
            meta,
            requester_id=track.requester_id,
            requester_name=track.requester_name,
            original_url=track.original_url or meta.search_query,
        )
        await self.resolve_stream(candidate, force=force, audio_max_abr=audio_max_abr)
        if candidate.stream_url:
            official_title = " - ".join(part for part in (meta.artist, meta.title) if part).strip()
            self._copy_resolved_fields(track, candidate)
            if official_title:
                track.title = official_title
            track.duration = meta.duration or track.duration
            track.uploader = meta.artist or track.uploader
            track.thumbnail = meta.thumbnail or track.thumbnail
            track.original_url = track.original_url or candidate.original_url
            track.extractor = candidate.extractor or track.extractor
            return track
        raise MusicExtractionError("Não consegui resolver uma fonte tocável para essa música.")

    def _track_from_api_candidate(
        self,
        candidate: ApiTrackCandidate,
        *,
        requester_id: int,
        requester_name: str = "",
        original_url: str = "",
    ) -> MusicTrack:
        track = MusicTrack(
            title=candidate.title or candidate.search_query or "Música sem título",
            webpage_url=candidate.webpage_url,
            requester_id=int(requester_id),
            requester_name=requester_name,
            duration=candidate.duration,
            uploader=candidate.artist,
            thumbnail=candidate.thumbnail,
            source=candidate.source or candidate.provider or "API",
            original_url=original_url or candidate.webpage_url,
            extractor=candidate.provider or "api",
        )
        return track

    def _prefer_clean_title(self, current_title: str, api_meta: ApiTrackCandidate) -> str:
        official = " - ".join(part for part in (api_meta.artist, api_meta.title) if part).strip()
        if not official:
            return current_title
        # Se o resultado tocável veio como Lyrics/Slowed/Cover, mostra o nome oficial
        # do Spotify/Deezer em vez de poluir o painel/fila com a versão alternativa.
        if is_bad_match_title(current_title, query=official):
            return official
        return official if len(official) <= len(current_title or "") + 15 else current_title

    def _dedupe_tracks(self, tracks: list[MusicTrack]) -> list[MusicTrack]:
        seen: set[str] = set()
        out: list[MusicTrack] = []
        for track in tracks:
            title_key = compact_key(track.title)
            duration_bucket = ""
            if track.duration is not None:
                duration_bucket = str(int(max(0, track.duration) // 8))
            url_key = (track.webpage_url or track.original_url or "").strip().lower()
            keys = [key for key in (url_key, f"{title_key}:{duration_bucket}" if title_key else "") if key]
            if any(key in seen for key in keys):
                continue
            seen.update(keys)
            out.append(track)
        return out

    def _direct_audio_track(self, profile: UrlProfile, *, requester_id: int, requester_name: str) -> MusicTrack:
        parsed = urlparse(profile.canonical)
        raw_name = unquote((parsed.path.rsplit("/", 1)[-1] or "stream").strip())
        title = re.sub(r"\.(mp3|m4a|aac|ogg|opus|wav|flac|webm)$", "", raw_name, flags=re.IGNORECASE)
        title = clean_metadata_title(title.replace("_", " ").replace("-", " ")) or "Stream de áudio"
        track = MusicTrack(
            title=title,
            webpage_url=profile.canonical,
            requester_id=int(requester_id),
            requester_name=requester_name,
            stream_url=profile.canonical,
            source=profile.host or "link direto",
            original_url=profile.raw,
            extractor="direct",
        )
        track.resolved_at_monotonic = time.monotonic()
        return track

    def _track_from_info(self, info: dict[str, Any], *, requester_id: int, requester_name: str = "", original_url: str = "") -> MusicTrack:
        title = str(info.get("title") or info.get("fulltitle") or info.get("alt_title") or info.get("id") or "Música sem título").strip()
        raw_info_url = str(info.get("url") or "").strip()
        raw_webpage_url = str(info.get("webpage_url") or info.get("original_url") or "").strip()

        webpage_url = raw_webpage_url
        if not webpage_url and raw_info_url and not self._is_probably_direct_stream_url(raw_info_url, info):
            # Resultado flat costuma guardar a página/ID em "url". Use isso como
            # origem para resolver depois, não como stream do FFmpeg.
            webpage_url = raw_info_url
        if not webpage_url and looks_like_url(original_url):
            webpage_url = original_url
        if webpage_url and not looks_like_url(webpage_url):
            # Flat entries do YouTube às vezes trazem só id/url parcial.
            extractor_key = str(info.get("extractor_key") or info.get("ie_key") or "").lower()
            if "youtube" in extractor_key or re.fullmatch(r"[A-Za-z0-9_-]{6,}", webpage_url):
                webpage_url = f"https://www.youtube.com/watch?v={webpage_url}"
            else:
                webpage_url = ""

        stream_url = raw_info_url if self._is_probably_direct_stream_url(raw_info_url, info) else ""
        duration_raw = info.get("duration")
        try:
            duration = float(duration_raw) if duration_raw is not None else None
        except Exception:
            duration = None
        uploader = str(info.get("uploader") or info.get("channel") or info.get("creator") or info.get("artist") or "")
        thumbnail = str(info.get("thumbnail") or "")
        extractor = str(info.get("extractor_key") or info.get("extractor") or info.get("ie_key") or "")
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
            track.resolved_audio_max_abr = 0
        return track

    def _is_known_webpage_url(self, url: str) -> bool:
        if not looks_like_url(url):
            return False
        profile = describe_url(url)
        if profile.is_direct_audio:
            return False
        if profile.is_youtube or profile.is_metadata_only:
            return True
        parsed = urlparse(url)
        host = (parsed.netloc or "").lower().removeprefix("www.")
        webpage_domains = (
            "soundcloud.com",
            "bandcamp.com",
            "vimeo.com",
            "dailymotion.com",
            "twitch.tv",
            "twitter.com",
            "x.com",
            "facebook.com",
            "instagram.com",
            "tiktok.com",
            "reddit.com",
        )
        return any(host == domain or host.endswith("." + domain) for domain in webpage_domains)

    def _is_probably_direct_stream_url(self, url: str, info: dict[str, Any] | None = None) -> bool:
        url = (url or "").strip()
        if not looks_like_url(url):
            return False
        if self._is_known_webpage_url(url):
            return False

        info = info or {}
        parsed = urlparse(url)
        host = (parsed.netloc or "").lower()
        path = (parsed.path or "").lower()
        protocol = str(info.get("protocol") or "").lower()
        ext = str(info.get("ext") or "").lower()
        acodec = str(info.get("acodec") or "").lower()

        direct_protocols = {"http", "https", "m3u8", "m3u8_native", "http_dash_segments", "mhtml"}
        stream_hosts = ("googlevideo.com", "sndcdn.com", "fbcdn.net", "cdninstagram.com", "akamaized.net", "cloudfront.net")
        if any(token in host for token in stream_hosts):
            return True
        if path.endswith((".mp3", ".m4a", ".aac", ".ogg", ".opus", ".wav", ".flac", ".webm", ".m3u8")):
            return True
        if protocol in direct_protocols and (acodec and acodec != "none"):
            return True
        if protocol in direct_protocols and ext in {"mp3", "m4a", "aac", "ogg", "opus", "wav", "flac", "webm", "m3u8"}:
            return True
        if protocol in direct_protocols and info.get("format_id") and not self._is_known_webpage_url(url):
            return True
        return False

    def _copy_resolved_fields(self, target: MusicTrack, source: MusicTrack) -> None:
        target.stream_url = source.stream_url or target.stream_url
        target.webpage_url = source.webpage_url or target.webpage_url
        target.duration = source.duration if source.duration is not None else target.duration
        target.uploader = source.uploader or target.uploader
        target.thumbnail = source.thumbnail or target.thumbnail
        target.source = source.source or target.source
        target.extractor = source.extractor or target.extractor
        target.is_live = bool(source.is_live)
        target.resolved_at_monotonic = time.monotonic()
        target.resolved_audio_max_abr = int(getattr(source, "resolved_audio_max_abr", 0) or 0)
