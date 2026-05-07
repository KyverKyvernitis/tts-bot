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

from .errors import MusicExtractionError
from .models import ExtractedBatch, MusicTrack
from .providers import (
    UrlProfile,
    clean_metadata_title,
    describe_url,
    fetch_metadata_title,
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

    def looks_like_url(self, query: str) -> bool:
        return looks_like_url(query)

    def is_metadata_only_platform(self, query: str) -> bool:
        return describe_url(query).is_metadata_only

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

    def _base_opts(self, *, extract_flat: bool | str = False, playlist: bool = False, youtube_clients: tuple[str, ...] | None = None) -> dict[str, Any]:
        opts: dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "noplaylist": not playlist,
            "extract_flat": extract_flat,
            "default_search": "ytsearch",
            "source_address": "0.0.0.0",
            "socket_timeout": self.timeout_seconds,
            "retries": 3,
            "fragment_retries": 3,
            "extractor_retries": 2,
            "ignoreerrors": playlist,
            "nocheckcertificate": True,
            "geo_bypass": True,
            "cachedir": False,
            "playlistend": self.max_playlist_items if playlist else None,
            "format": "bestaudio[acodec=opus]/bestaudio[ext=webm]/bestaudio/best",
            "format_sort": ["acodec:opus", "ext:webm:m4a", "abr", "asr", "proto"],
            "http_headers": {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
                "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
            },
        }
        if opts["playlistend"] is None:
            opts.pop("playlistend", None)
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
                tracks = [track for track in tracks if track.webpage_url or track.stream_url]
                if tracks:
                    return ExtractedBatch(tracks=tracks[: self.search_results], query=query, is_playlist=False)
            except Exception as exc:
                last_error = exc
                logger.debug("[music] busca falhou | query=%r flat=%s", query, flat, exc_info=True)

        if last_error:
            raise MusicExtractionError(
                "Não encontrei resultados tocáveis. Tente pesquisar com nome e artista.",
                detail=str(last_error),
            ) from last_error
        raise MusicExtractionError("Não encontrei resultados tocáveis. Tente pesquisar com nome e artista.")

    async def _extract_metadata_only(self, profile: UrlProfile, *, requester_id: int, requester_name: str = "") -> ExtractedBatch:
        # Spotify/Apple/Deezer não são tocados diretamente: vira metadata + busca.
        # Não chamamos yt-dlp nesses links aqui, porque algumas plataformas disparam
        # erro de DRM mesmo quando só queremos título público. Isso polui a log e
        # não ajuda na reprodução.
        queries: list[str] = []

        title = await self._metadata_title(profile.canonical)
        queries.append(title)
        queries.append(slug_search_terms(profile.canonical))

        for query in unique_queries(*queries):
            try:
                batch = await self.search(query, requester_id=requester_id, requester_name=requester_name)
                # Mantém URL original para rastreabilidade.
                for track in batch.tracks:
                    track.original_url = profile.raw
                return batch
            except Exception:
                logger.debug("[music] fallback metadata-only falhou | query=%r url=%s", query, profile.raw, exc_info=True)

        raise MusicExtractionError(
            "Não consegui transformar esse link em uma música tocável. Spotify/Apple/Deezer entram por busca de título/artista; tente `_play nome da música artista`.",
        )

    async def _extract_youtube(self, profile: UrlProfile, *, requester_id: int, requester_name: str = "") -> ExtractedBatch:
        errors: list[str] = []
        candidates = unique_queries(profile.canonical, profile.raw)

        for url in candidates:
            try:
                return await self._extract_url(url, requester_id=requester_id, requester_name=requester_name, youtube=True)
            except Exception as exc:
                errors.append(str(exc))
                logger.debug("[music] youtube direct extraction failed | url=%s", url, exc_info=True)

        # Fallback leve: pega título público da página ou usa o ID como busca.
        title = await self._metadata_title(profile.canonical)
        search_queries = unique_queries(title, profile.youtube_video_id)
        for query in search_queries:
            try:
                batch = await self.search(query, requester_id=requester_id, requester_name=requester_name)
                for track in batch.tracks:
                    track.original_url = profile.raw
                return batch
            except Exception as exc:
                errors.append(str(exc))

        detail = " | ".join(e for e in errors if e)[-500:]
        raise MusicExtractionError(
            "Não consegui ler esse link do YouTube. Atualize o yt-dlp e, se o vídeo tiver restrição, configure cookies do navegador.",
            detail=detail,
        )

    async def _extract_generic_url(self, profile: UrlProfile, *, requester_id: int, requester_name: str = "") -> ExtractedBatch:
        try:
            return await self._extract_url(profile.canonical, requester_id=requester_id, requester_name=requester_name)
        except Exception as exc:
            logger.debug("[music] generic URL direct extraction failed | url=%s", profile.raw, exc_info=True)
            title = await self._metadata_title(profile.canonical)
            slug = slug_search_terms(profile.canonical)
            for query in unique_queries(title, slug):
                try:
                    batch = await self.search(query, requester_id=requester_id, requester_name=requester_name)
                    for track in batch.tracks:
                        track.original_url = profile.raw
                    return batch
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

    async def resolve_stream(self, track: MusicTrack, *, force: bool = False) -> MusicTrack:
        age = time.monotonic() - float(track.resolved_at_monotonic or 0.0)
        if track.stream_url and not self._is_probably_direct_stream_url(track.stream_url):
            # Flat mode do yt-dlp pode colocar a página do YouTube em info["url"].
            # FFmpeg não consegue tocar página HTML; ele precisa do stream real.
            track.stream_url = ""
            track.resolved_at_monotonic = 0.0
        if track.stream_url and not force and age < 20 * 60:
            return track

        source = track.webpage_url or (track.original_url if looks_like_url(track.original_url) else "") or track.stream_url
        if not source:
            raise MusicExtractionError("Música sem URL de origem.")

        profile = describe_url(source)
        if profile.is_direct_audio:
            track.stream_url = profile.canonical
            if not track.webpage_url:
                track.webpage_url = profile.canonical
            track.resolved_at_monotonic = time.monotonic()
            return track

        errors: list[str] = []
        runs: list[tuple[bool | str, bool, tuple[str, ...] | None]] = [(False, False, None)]
        if profile.is_youtube:
            runs.extend((False, False, clients) for clients in _YOUTUBE_CLIENT_SETS)

        for flat, playlist, clients in runs:
            try:
                info = await self._run_extract(source, extract_flat=flat, playlist=playlist, youtube_clients=clients)
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
                    self._copy_resolved_fields(track, updated)
                    return track
            except Exception as exc:
                errors.append(str(exc))
                logger.debug("[music] stream resolve failed | source=%s clients=%s", source, clients, exc_info=True)

        # Último fallback: busca pelo título atual e resolve o primeiro resultado.
        if track.title and not profile.is_metadata_only:
            try:
                batch = await self.search(track.title, requester_id=track.requester_id, requester_name=track.requester_name)
                for candidate in batch.tracks[:3]:
                    if candidate.webpage_url == track.webpage_url:
                        continue
                    try:
                        await self.resolve_stream(candidate, force=True)
                        if candidate.stream_url:
                            self._copy_resolved_fields(track, candidate)
                            return track
                    except Exception:
                        continue
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
    ) -> dict[str, Any]:
        try:
            import yt_dlp  # import tardio para não pesar o boot quando música não é usada
        except Exception as exc:  # pragma: no cover
            raise MusicExtractionError("Dependência yt-dlp não está instalada no venv.") from exc

        opts = self._base_opts(extract_flat=extract_flat, playlist=playlist, youtube_clients=youtube_clients)

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
