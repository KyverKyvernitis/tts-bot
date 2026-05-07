from __future__ import annotations

import asyncio
import base64
import html
import json
import logging
import os
import re
import time
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Iterable
from urllib.parse import parse_qs, quote, urlencode, urlparse
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import config

logger = logging.getLogger(__name__)


def _env(name: str, default: str = "") -> str:
    return str(getattr(config, name, "") or os.getenv(name, default) or "").strip()


def _env_bool(name: str, default: bool = False) -> bool:
    raw = _env(name, "true" if default else "false").lower()
    return raw in {"1", "true", "yes", "sim", "on", "enabled", "ativo"}


def _env_float(name: str, default: float) -> float:
    try:
        return float(_env(name, str(default)))
    except Exception:
        return float(default)


def normalize_text(value: str) -> str:
    value = unicodedata.normalize("NFKD", value or "")
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    value = value.lower()
    value = re.sub(r"\([^)]*\)|\[[^]]*]", " ", value)
    value = re.sub(r"\b(official|music|video|audio|lyrics?|lyric|visualizer|remaster(?:ed)?|hd|hq|4k|mv|clip)\b", " ", value)
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def compact_key(value: str) -> str:
    return re.sub(r"\s+", " ", normalize_text(value)).strip()


BAD_VERSION_WORDS = {
    "slowed",
    "slow",
    "nightcore",
    "speed up",
    "sped up",
    "8d",
    "karaoke",
    "instrumental",
    "cover",
    "remix",
    "live",
    "lyrics",
    "lyric",
    "bass boosted",
    "extended",
    "edit",
    "reverb",
}

GOOD_VERSION_PHRASES = (
    "official audio",
    "official video",
    "official music video",
    "audio oficial",
    "video oficial",
)

OFFICIAL_CHANNEL_HINTS = ("official", "vevo", "topic", "records", "music")


def is_bad_match_title(title: str, *, query: str = "") -> bool:
    raw = re.sub(r"[^a-z0-9]+", " ", unicodedata.normalize("NFKD", title or "").lower())
    normalized = normalize_text(title)
    q_raw = re.sub(r"[^a-z0-9]+", " ", unicodedata.normalize("NFKD", query or "").lower())
    q = normalize_text(query)
    if not q and not q_raw:
        return any(word in normalized or word in raw for word in BAD_VERSION_WORDS)
    # Só penaliza versões alteradas quando o usuário não pediu essa versão.
    return any((word in normalized or word in raw) and word not in q and word not in q_raw for word in BAD_VERSION_WORDS)


def title_quality_score(title: str, *, query: str = "", channel: str = "") -> float:
    raw_title = re.sub(r"\s+", " ", (title or "").lower())
    raw_channel = re.sub(r"\s+", " ", (channel or "").lower())
    score = 0.0
    if any(phrase in raw_title for phrase in GOOD_VERSION_PHRASES):
        score += 16
    if any(hint in raw_channel for hint in OFFICIAL_CHANNEL_HINTS):
        score += 5
    if is_bad_match_title(title, query=query):
        score -= 26
    # Lyrics é aceitável como fallback, mas não deve vencer áudio/vídeo oficial.
    if "lyrics" in raw_title or "lyric" in raw_title:
        score -= 8
    return score


def parse_iso8601_duration(value: str) -> float | None:
    if not value:
        return None
    match = re.fullmatch(r"P(?:(?P<days>\d+)D)?(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?)?", value)
    if not match:
        return None
    days = int(match.group("days") or 0)
    hours = int(match.group("hours") or 0)
    minutes = int(match.group("minutes") or 0)
    seconds = int(match.group("seconds") or 0)
    return float(days * 86400 + hours * 3600 + minutes * 60 + seconds)


@dataclass(slots=True)
class ApiTrackCandidate:
    title: str
    artist: str = ""
    album: str = ""
    duration: float | None = None
    thumbnail: str = ""
    webpage_url: str = ""
    source: str = "api"
    provider: str = ""
    isrc: str = ""
    query: str = ""
    score: float = 0.0
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def search_query(self) -> str:
        if self.query:
            return self.query
        parts = [self.artist, self.title]
        return " ".join(part for part in parts if part).strip() or self.title

    @property
    def key(self) -> str:
        return compact_key(f"{self.artist} {self.title}")


@dataclass(slots=True)
class ApiTrackBatch:
    tracks: list[ApiTrackCandidate]
    title: str = ""
    is_playlist: bool = False
    truncated: bool = False
    source: str = ""


class MusicApiProviders:
    """Providers opcionais para melhorar metadata/ranking.

    As APIs aqui não substituem o yt-dlp/FFmpeg. Elas só ajudam a escolher o
    resultado certo e a responder mais rápido. Se nenhuma chave estiver no .env,
    o sistema continua funcionando com os fallbacks antigos.
    """

    def __init__(self, *, timeout: float | None = None) -> None:
        self.timeout = max(2.0, float(timeout if timeout is not None else _env_float("MUSIC_API_TIMEOUT_SECONDS", 5.0)))
        self.enabled = _env_bool("MUSIC_API_SEARCH_ENABLED", True)
        self.youtube_api_key = _env("YOUTUBE_API_KEY") or _env("GOOGLE_YOUTUBE_API_KEY")
        self.spotify_client_id = _env("SPOTIFY_CLIENT_ID")
        self.spotify_client_secret = _env("SPOTIFY_CLIENT_SECRET")
        self.spotify_refresh_token = _env("SPOTIFY_REFRESH_TOKEN")
        self.spotify_redirect_uri = _env("SPOTIFY_REDIRECT_URI", "http://127.0.0.1:8888/callback")
        self.spotify_market = (_env("SPOTIFY_MARKET", "BR") or "BR").upper()
        self.spotify_public_fallback_enabled = _env_bool("SPOTIFY_PUBLIC_FALLBACK_ENABLED", True)
        self.spotify_public_fallback_max_tracks = max(1, min(100, int(_env_float("SPOTIFY_PUBLIC_FALLBACK_MAX_TRACKS", 100))))
        self.deezer_enabled = _env_bool("DEEZER_API_ENABLED", True)
        self.soundcloud_enabled = _env_bool("SOUNDCLOUD_API_ENABLED", False)
        self.soundcloud_token = _env("SOUNDCLOUD_API_TOKEN")
        self.soundcloud_client_id = _env("SOUNDCLOUD_CLIENT_ID")
        self.soundcloud_base_url = _env("SOUNDCLOUD_API_BASE_URL", "https://api.soundcloud.com/tracks")
        self._spotify_token = ""
        self._spotify_token_expires_at = 0.0
        self._spotify_user_token = ""
        self._spotify_user_token_expires_at = 0.0
        self._spotify_public_token = ""
        self._spotify_public_token_expires_at = 0.0

    @property
    def has_any_provider(self) -> bool:
        return bool(
            self.enabled
            and (
                self.youtube_api_key
                or (self.spotify_client_id and self.spotify_client_secret)
                or self.spotify_public_fallback_enabled
                or self.deezer_enabled
                or (self.soundcloud_enabled and (self.soundcloud_token or self.soundcloud_client_id))
            )
        )

    @property
    def spotify_has_user_auth(self) -> bool:
        return bool(self.spotify_client_id and self.spotify_client_secret and self.spotify_refresh_token)

    async def metadata_from_url(self, url: str) -> ApiTrackCandidate | None:
        batch = await self.metadata_batch_from_url(url, limit=1)
        return batch.tracks[0] if batch and batch.tracks else None

    async def metadata_batch_from_url(self, url: str, *, limit: int = 25) -> ApiTrackBatch | None:
        """Lê metadata oficial de links de música/playlist quando houver provider.

        Não retorna stream de áudio. O extractor usa esses dados para buscar uma
        fonte tocável equivalente de forma correta e preguiçosa.
        """
        if not self.enabled:
            return None
        parsed = urlparse(url)
        host = (parsed.netloc or "").lower()
        path = parsed.path or ""
        if "open.spotify.com" in host:
            return await self.spotify_batch_from_url(url, limit=limit)
        if "deezer.com" in host:
            return await self.deezer_batch_from_url(url, limit=limit)
        if "soundcloud.com" in host and self.soundcloud_enabled and (self.soundcloud_token or self.soundcloud_client_id):
            return await self.soundcloud_batch_from_url(url, limit=limit)
        return None

    async def search(self, query: str, *, limit: int = 5, prefer_youtube: bool = True) -> list[ApiTrackCandidate]:
        if not self.enabled or not query.strip():
            return []
        limit = max(1, min(10, int(limit)))
        tasks: list[asyncio.Task[list[ApiTrackCandidate]]] = []
        if prefer_youtube and self.youtube_api_key:
            tasks.append(asyncio.create_task(self.youtube_search(query, limit=limit)))
        # Spotify/Deezer ajudam no ranking/metadata de busca textual, mesmo quando
        # o resultado final tocável vem do YouTube.
        if self.spotify_client_id and self.spotify_client_secret:
            tasks.append(asyncio.create_task(self.spotify_search(query, limit=min(limit, 5))))
        if self.deezer_enabled:
            tasks.append(asyncio.create_task(self.deezer_search(query, limit=min(limit, 5))))
        if self.soundcloud_enabled and (self.soundcloud_token or self.soundcloud_client_id):
            tasks.append(asyncio.create_task(self.soundcloud_search(query, limit=min(limit, 5))))
        if not tasks:
            return []

        results: list[ApiTrackCandidate] = []
        gathered = await asyncio.gather(*tasks, return_exceptions=True)
        for item in gathered:
            if isinstance(item, Exception):
                logger.debug("[music-api] provider search failed", exc_info=item)
                continue
            results.extend(item)
        return self.rank_and_dedupe(results, query=query, limit=limit)

    def rank_and_dedupe(self, candidates: Iterable[ApiTrackCandidate], *, query: str, limit: int = 5) -> list[ApiTrackCandidate]:
        q = normalize_text(query)
        seen: set[str] = set()
        ranked: list[ApiTrackCandidate] = []
        for candidate in candidates:
            title_key = candidate.key or compact_key(candidate.title)
            url_key = candidate.webpage_url.strip().lower()
            key = title_key or url_key
            if not key or key in seen or (url_key and url_key in seen):
                continue
            seen.add(key)
            if url_key:
                seen.add(url_key)
            candidate.score += self._candidate_score(candidate, q)
            ranked.append(candidate)
        ranked.sort(key=lambda item: item.score, reverse=True)
        return ranked[: max(1, int(limit))]

    def _candidate_score(self, candidate: ApiTrackCandidate, normalized_query: str) -> float:
        score = 0.0
        title_norm = normalize_text(candidate.title)
        channel_norm = normalize_text(candidate.artist)
        combined = normalize_text(f"{candidate.artist} {candidate.title}")
        if candidate.provider == "youtube":
            score += 32
        elif candidate.provider == "soundcloud":
            score += 14
        elif candidate.provider == "spotify":
            score += 12
        elif candidate.provider == "deezer":
            score += 10
        if normalized_query and normalized_query in combined:
            score += 38
        elif normalized_query:
            q_words = set(normalized_query.split())
            c_words = set(combined.split())
            if q_words:
                score += 28 * (len(q_words & c_words) / len(q_words))
        if channel_norm and any(word in channel_norm for word in ("official", "vevo", "topic")):
            score += 6
        if normalized_query and channel_norm:
            q_words = set(normalized_query.split())
            c_words = set(channel_norm.split())
            if q_words and len(q_words & c_words) >= 1:
                score += 4
        score += title_quality_score(candidate.title, query=normalized_query, channel=candidate.artist)
        if candidate.duration and 40 <= candidate.duration <= 900:
            score += 4
        elif candidate.duration and candidate.duration > 1200:
            score -= 8
        if candidate.thumbnail:
            score += 1
        if title_norm and normalized_query and title_norm == normalized_query:
            score += 4
        return score

    def _request_json(self, url: str, *, method: str = "GET", data: bytes | None = None, headers: dict[str, str] | None = None) -> dict[str, Any]:
        request = Request(url, data=data, method=method, headers={
            "User-Agent": "DiscordMusicBot/1.0",
            "Accept": "application/json",
            **(headers or {}),
        })
        with urlopen(request, timeout=self.timeout) as response:  # noqa: S310 - URLs fixas dos providers configurados
            raw = response.read(2_000_000)
        return json.loads(raw.decode("utf-8", errors="ignore") or "{}")

    async def _to_thread_json(self, url: str, *, method: str = "GET", data: bytes | None = None, headers: dict[str, str] | None = None) -> dict[str, Any]:
        return await asyncio.to_thread(self._request_json, url, method=method, data=data, headers=headers)

    async def youtube_search(self, query: str, *, limit: int = 5) -> list[ApiTrackCandidate]:
        if not self.youtube_api_key:
            return []
        params = urlencode({
            "part": "snippet",
            "type": "video",
            "maxResults": max(1, min(10, int(limit))),
            "q": query,
            "key": self.youtube_api_key,
            "safeSearch": "none",
            "videoEmbeddable": "true",
        })
        data = await self._to_thread_json(f"https://www.googleapis.com/youtube/v3/search?{params}")
        items = data.get("items") or []
        video_ids = []
        base: dict[str, ApiTrackCandidate] = {}
        for item in items:
            video_id = str(((item.get("id") or {}).get("videoId")) or "").strip()
            snippet = item.get("snippet") or {}
            if not video_id:
                continue
            thumbnails = snippet.get("thumbnails") or {}
            thumb = ((thumbnails.get("medium") or thumbnails.get("default") or thumbnails.get("high") or {}).get("url") or "")
            candidate = ApiTrackCandidate(
                title=str(snippet.get("title") or "").strip(),
                artist=str(snippet.get("channelTitle") or "").strip(),
                thumbnail=thumb,
                webpage_url=f"https://www.youtube.com/watch?v={video_id}",
                provider="youtube",
                source="YouTube API",
                query=query,
                score=40,
            )
            video_ids.append(video_id)
            base[video_id] = candidate
        if video_ids:
            duration_params = urlencode({
                "part": "contentDetails,status",
                "id": ",".join(video_ids),
                "key": self.youtube_api_key,
            })
            with_duration = await self._to_thread_json(f"https://www.googleapis.com/youtube/v3/videos?{duration_params}")
            for item in with_duration.get("items") or []:
                video_id = str(item.get("id") or "")
                candidate = base.get(video_id)
                if not candidate:
                    continue
                candidate.duration = parse_iso8601_duration(str((item.get("contentDetails") or {}).get("duration") or ""))
                status = item.get("status") or {}
                if str(status.get("embeddable", "true")).lower() == "false":
                    candidate.score -= 10
        return list(base.values())

    async def spotify_token(self, *, user: bool = False) -> str:
        """Retorna token Spotify.

        - user=False: Client Credentials para busca/faixas públicas.
        - user=True: Refresh Token de usuário para playlists privadas/colaborativas
          e endpoints que retornam 403 com token de app.
        """
        if not (self.spotify_client_id and self.spotify_client_secret):
            return ""
        if user:
            return await self.spotify_user_token()
        if self._spotify_token and time.monotonic() < self._spotify_token_expires_at - 30:
            return self._spotify_token
        auth = base64.b64encode(f"{self.spotify_client_id}:{self.spotify_client_secret}".encode()).decode()
        data = await self._to_thread_json(
            "https://accounts.spotify.com/api/token",
            method="POST",
            data=b"grant_type=client_credentials",
            headers={"Authorization": f"Basic {auth}", "Content-Type": "application/x-www-form-urlencoded"},
        )
        token = str(data.get("access_token") or "")
        if token:
            self._spotify_token = token
            self._spotify_token_expires_at = time.monotonic() + float(data.get("expires_in") or 3600)
        return token

    async def spotify_user_token(self) -> str:
        if not (self.spotify_client_id and self.spotify_client_secret and self.spotify_refresh_token):
            return ""
        if self._spotify_user_token and time.monotonic() < self._spotify_user_token_expires_at - 30:
            return self._spotify_user_token
        auth = base64.b64encode(f"{self.spotify_client_id}:{self.spotify_client_secret}".encode()).decode()
        payload = urlencode({"grant_type": "refresh_token", "refresh_token": self.spotify_refresh_token}).encode()
        data = await self._to_thread_json(
            "https://accounts.spotify.com/api/token",
            method="POST",
            data=payload,
            headers={"Authorization": f"Basic {auth}", "Content-Type": "application/x-www-form-urlencoded"},
        )
        token = str(data.get("access_token") or "")
        if token:
            self._spotify_user_token = token
            self._spotify_user_token_expires_at = time.monotonic() + float(data.get("expires_in") or 3600)
        return token

    def _spotify_headers(self, token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}"}

    def _request_text(self, url: str, *, headers: dict[str, str] | None = None, max_bytes: int = 5_000_000) -> str:
        request = Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/json;q=0.8,*/*;q=0.7",
            "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
            **(headers or {}),
        })
        with urlopen(request, timeout=self.timeout) as response:  # noqa: S310 - URLs públicas dos providers
            raw = response.read(max_bytes)
        return raw.decode("utf-8", errors="ignore")

    async def _to_thread_text(self, url: str, *, headers: dict[str, str] | None = None, max_bytes: int = 5_000_000) -> str:
        return await asyncio.to_thread(self._request_text, url, headers=headers, max_bytes=max_bytes)

    async def spotify_public_token(self) -> str:
        """Token anônimo do web player usado como fallback público.

        Esse fallback é propositalmente opcional: ele tenta ler metadata pública
        que o Spotify já expõe no web player quando a Web API oficial do app
        responde 403 para playlists públicas. Não é usado para tocar áudio.
        """
        if not self.spotify_public_fallback_enabled:
            return ""
        if self._spotify_public_token and time.monotonic() < self._spotify_public_token_expires_at - 30:
            return self._spotify_public_token

        urls = (
            "https://open.spotify.com/get_access_token?reason=transport&productType=web_player",
            "https://open.spotify.com/get_access_token?reason=init&productType=web_player",
            "https://open.spotify.com/get_access_token?reason=transport&productType=web-player",
            "https://open.spotify.com/get_access_token?reason=init&productType=web-player",
        )
        last_error: Exception | None = None
        for url in urls:
            try:
                data = await self._to_thread_json(url, headers={
                    "Origin": "https://open.spotify.com",
                    "Referer": "https://open.spotify.com/",
                    "App-Platform": "WebPlayer",
                })
                token = str(data.get("accessToken") or data.get("access_token") or "").strip()
                if not token:
                    continue
                expires = data.get("accessTokenExpirationTimestampMs") or data.get("expires_in") or 3600
                try:
                    expires_float = float(expires)
                    if expires_float > 10_000_000_000:
                        self._spotify_public_token_expires_at = time.monotonic() + max(60.0, (expires_float / 1000.0) - time.time())
                    else:
                        self._spotify_public_token_expires_at = time.monotonic() + max(60.0, expires_float)
                except Exception:
                    self._spotify_public_token_expires_at = time.monotonic() + 3600.0
                self._spotify_public_token = token
                return token
            except Exception as exc:
                last_error = exc
                logger.debug("[music-api] token público Spotify falhou | url=%s", url, exc_info=True)
        if last_error:
            logger.debug("[music-api] nenhum token público Spotify disponível", exc_info=last_error)
        return ""

    async def _spotify_public_json(self, path: str) -> dict[str, Any]:
        token = await self.spotify_public_token()
        if not token:
            return {}
        url = "https://api.spotify.com/v1/" + path.lstrip("/")
        return await self._to_thread_json(url, headers={
            "Authorization": f"Bearer {token}",
            "Origin": "https://open.spotify.com",
            "Referer": "https://open.spotify.com/",
            "App-Platform": "WebPlayer",
        })

    def _spotify_public_urls(self, kind: str, item_id: str) -> list[str]:
        item_id = quote(item_id)
        if kind == "track":
            return [f"https://open.spotify.com/track/{item_id}", f"https://open.spotify.com/embed/track/{item_id}"]
        if kind == "album":
            return [f"https://open.spotify.com/album/{item_id}", f"https://open.spotify.com/embed/album/{item_id}"]
        if kind == "playlist":
            return [f"https://open.spotify.com/playlist/{item_id}", f"https://open.spotify.com/embed/playlist/{item_id}"]
        return []

    def _spotify_public_duration(self, value: Any) -> float | None:
        if value is None:
            return None
        if isinstance(value, (int, float)):
            number = float(value)
            # Spotify geralmente usa duration_ms.
            return number / 1000.0 if number > 10_000 else number
        text = str(value).strip()
        if not text:
            return None
        parsed = parse_iso8601_duration(text)
        if parsed:
            return parsed
        try:
            number = float(text)
            return number / 1000.0 if number > 10_000 else number
        except Exception:
            return None

    def _spotify_public_images(self, value: Any) -> str:
        if isinstance(value, str):
            return value
        if isinstance(value, dict):
            for key in ("url", "src"):
                if value.get(key):
                    return str(value.get(key) or "")
            for key in ("images", "sources"):
                found = self._spotify_public_images(value.get(key))
                if found:
                    return found
        if isinstance(value, list):
            for item in value:
                found = self._spotify_public_images(item)
                if found:
                    return found
        return ""

    def _spotify_public_artist_names(self, data: dict[str, Any]) -> str:
        def names_from(value: Any) -> list[str]:
            names: list[str] = []
            if isinstance(value, str):
                clean = value.strip()
                if clean and clean.lower() not in {"spotify"}:
                    names.append(clean)
            elif isinstance(value, dict):
                for key in ("name", "title", "text"):
                    clean = str(value.get(key) or "").strip()
                    if clean:
                        names.append(clean)
                        break
            elif isinstance(value, list):
                for item in value:
                    names.extend(names_from(item))
            return names

        for key in ("artists", "artist", "byArtist", "creator", "authors", "owner"):
            result = names_from(data.get(key))
            if result:
                # Remove duplicatas mantendo a ordem.
                seen: set[str] = set()
                unique: list[str] = []
                for name in result:
                    marker = normalize_text(name)
                    if marker and marker not in seen:
                        seen.add(marker)
                        unique.append(name)
                if unique:
                    return ", ".join(unique[:4])
        return ""

    def _spotify_public_candidate_from_obj(self, data: dict[str, Any], *, url: str = "") -> ApiTrackCandidate | None:
        if not isinstance(data, dict):
            return None
        uri = str(data.get("uri") or data.get("playableUri") or "")
        item_type = str(data.get("type") or data.get("__typename") or data.get("contentType") or "").lower()
        is_trackish = (
            uri.startswith("spotify:track:")
            or item_type in {"track", "trackresponsewrapper", "playlisttrack"}
            or bool(data.get("duration_ms") or data.get("durationMs") or data.get("duration")) and bool(data.get("artists") or data.get("artist") or data.get("byArtist"))
        )
        if not is_trackish:
            return None

        title = str(data.get("name") or data.get("title") or data.get("trackName") or "").strip()
        if not title and isinstance(data.get("track"), dict):
            return self._spotify_public_candidate_from_obj(data["track"], url=url)
        if not title:
            return None
        artist = self._spotify_public_artist_names(data)
        album_data = data.get("album") if isinstance(data.get("album"), dict) else {}
        album = str((album_data or {}).get("name") or (album_data or {}).get("title") or "")
        images = data.get("images") or data.get("image") or data.get("coverArt") or data.get("albumOfTrack") or album_data
        thumbnail = self._spotify_public_images(images)
        duration = self._spotify_public_duration(data.get("duration_ms") or data.get("durationMs") or data.get("duration"))
        external_urls = data.get("external_urls") or data.get("externalUrls") or {}
        webpage_url = ""
        if isinstance(external_urls, dict):
            webpage_url = str(external_urls.get("spotify") or external_urls.get("url") or "")
        if not webpage_url:
            item_id = str(data.get("id") or data.get("trackId") or "").strip()
            if uri.startswith("spotify:track:"):
                item_id = uri.split(":")[-1]
            if item_id:
                webpage_url = f"https://open.spotify.com/track/{item_id}"
        if not webpage_url:
            webpage_url = url
        isrc = ""
        external_ids = data.get("external_ids") or data.get("externalIds") or {}
        if isinstance(external_ids, dict):
            isrc = str(external_ids.get("isrc") or external_ids.get("ISRC") or "")
        return ApiTrackCandidate(
            title=title,
            artist=artist,
            album=album,
            duration=duration,
            thumbnail=thumbnail,
            webpage_url=webpage_url,
            source="Spotify público",
            provider="spotify",
            isrc=isrc,
            query=" ".join(part for part in (artist, title, "official audio") if part),
            score=30,
        )

    def _spotify_public_candidates_from_json(self, data: Any, *, url: str, limit: int) -> list[ApiTrackCandidate]:
        results: list[ApiTrackCandidate] = []
        seen: set[str] = set()

        def add(candidate: ApiTrackCandidate | None) -> None:
            if not candidate:
                return
            key = compact_key(f"{candidate.artist} {candidate.title}") or candidate.webpage_url.lower()
            if not key or key in seen:
                return
            seen.add(key)
            results.append(candidate)

        def walk(value: Any, depth: int = 0) -> None:
            if len(results) >= limit or depth > 18:
                return
            if isinstance(value, dict):
                # Muitos payloads usam wrappers {track: {...}} ou {item: {...}}.
                for key in ("track", "item", "data"):
                    nested = value.get(key)
                    if isinstance(nested, dict):
                        add(self._spotify_public_candidate_from_obj(nested, url=url))
                add(self._spotify_public_candidate_from_obj(value, url=url))
                for nested in value.values():
                    walk(nested, depth + 1)
            elif isinstance(value, list):
                for item in value:
                    walk(item, depth + 1)
                    if len(results) >= limit:
                        break

        walk(data)
        return results[:limit]

    async def _spotify_public_page_batch(self, kind: str, item_id: str, *, limit: int, original_url: str) -> ApiTrackBatch | None:
        if not self.spotify_public_fallback_enabled:
            return None
        limit = max(1, min(self.spotify_public_fallback_max_tracks, int(limit)))
        last_title = ""
        for url in self._spotify_public_urls(kind, item_id):
            try:
                content = await self._to_thread_text(url, max_bytes=6_000_000)
            except Exception:
                logger.debug("[music-api] fallback público Spotify HTML falhou | url=%s", url, exc_info=True)
                continue

            # Título amigável da página como fallback para nome de playlist/álbum.
            title_match = re.search(r'<title[^>]*>(.*?)</title>', content, re.IGNORECASE | re.DOTALL)
            if title_match:
                last_title = html.unescape(re.sub(r"\s+", " ", title_match.group(1))).replace(" | Spotify", "").strip()

            json_blobs: list[Any] = []
            for match in re.finditer(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', content, re.IGNORECASE | re.DOTALL):
                try:
                    json_blobs.append(json.loads(html.unescape(match.group(1))))
                except Exception:
                    pass
            match = re.search(r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>', content, re.IGNORECASE | re.DOTALL)
            if match:
                try:
                    json_blobs.append(json.loads(html.unescape(match.group(1))))
                except Exception:
                    pass
            # Fallback genérico: captura objetos com spotify:track dentro de scripts/RSC.
            for raw_match in re.finditer(r'\{[^{}]{0,2500}spotify:track:[^{}]{0,2500}\}', content):
                raw = html.unescape(raw_match.group(0))
                try:
                    json_blobs.append(json.loads(raw))
                except Exception:
                    # Em payload RSC pode haver aspas escapadas dentro de strings maiores.
                    pass

            tracks: list[ApiTrackCandidate] = []
            for blob in json_blobs:
                tracks.extend(self._spotify_public_candidates_from_json(blob, url=original_url or url, limit=limit - len(tracks)))
                if len(tracks) >= limit:
                    break
            if tracks:
                return ApiTrackBatch(
                    tracks=self.rank_and_dedupe(tracks, query=last_title or original_url, limit=limit),
                    title=last_title or (tracks[0].album if kind == "album" else "Spotify"),
                    is_playlist=kind in {"album", "playlist"} or len(tracks) > 1,
                    truncated=len(tracks) >= limit,
                    source="Spotify público",
                )
        return None

    async def spotify_public_batch_from_url(self, url: str, *, limit: int = 25) -> ApiTrackBatch | None:
        kind, item_id = self._spotify_resource(url)
        if not item_id or not self.spotify_public_fallback_enabled:
            return None
        limit = max(1, min(self.spotify_public_fallback_max_tracks, int(limit)))
        quoted_id = quote(item_id)
        last_error: Exception | None = None
        try:
            if kind == "track":
                data = await self._spotify_public_json(f"tracks/{quoted_id}?market={quote(self.spotify_market)}")
                candidate = self._spotify_candidate(data, url=url)
                if candidate:
                    candidate.source = "Spotify público"
                    return ApiTrackBatch(tracks=[candidate], title=candidate.title, is_playlist=False, source="Spotify público")
            elif kind == "album":
                data = await self._spotify_public_json(f"albums/{quoted_id}?market={quote(self.spotify_market)}")
                if data:
                    album_title = str(data.get("name") or "Álbum Spotify")
                    images = data.get("images") or []
                    album_image = str((images[0] or {}).get("url") or "") if images else ""
                    album_artists = data.get("artists") or []
                    default_artist = ", ".join(str(a.get("name") or "").strip() for a in album_artists if a.get("name"))
                    tracks: list[ApiTrackCandidate] = []
                    for item in (((data.get("tracks") or {}).get("items")) or [])[:limit]:
                        candidate = self._spotify_candidate({**item, "album": data}, url=str(((item.get("external_urls") or {}).get("spotify")) or url))
                        if candidate:
                            candidate.source = "Spotify público"
                            if not candidate.artist:
                                candidate.artist = default_artist
                            if not candidate.thumbnail:
                                candidate.thumbnail = album_image
                            candidate.album = album_title
                            tracks.append(candidate)
                    total = int(((data.get("tracks") or {}).get("total")) or len(tracks))
                    if tracks:
                        return ApiTrackBatch(tracks=tracks, title=album_title, is_playlist=True, truncated=total > len(tracks), source="Spotify público")
            elif kind == "playlist":
                tracks: list[ApiTrackCandidate] = []
                offset = 0
                total = 0
                playlist_title = "Playlist Spotify"
                try:
                    meta = await self._spotify_public_json(
                        f"playlists/{quoted_id}?fields=name,tracks.total&market={quote(self.spotify_market)}"
                    )
                    playlist_title = str(meta.get("name") or playlist_title)
                    total = int(((meta.get("tracks") or {}).get("total")) or 0)
                except Exception as exc:
                    last_error = exc
                    logger.debug("[music-api] fallback público Spotify meta playlist falhou", exc_info=True)
                while len(tracks) < limit:
                    page_limit = min(50, limit - len(tracks))
                    fields = "items(track(name,artists(name),album(name,images),duration_ms,external_ids,external_urls,is_local,type)),next,total"
                    params = urlencode({
                        "limit": page_limit,
                        "offset": offset,
                        "fields": fields,
                        "market": self.spotify_market,
                        "additional_types": "track",
                    })
                    data = await self._spotify_public_json(f"playlists/{quoted_id}/tracks?{params}")
                    items = data.get("items") or []
                    if not items:
                        break
                    for row in items:
                        track_data = row.get("track") or {}
                        if not track_data or track_data.get("is_local") or str(track_data.get("type") or "track") != "track":
                            continue
                        candidate = self._spotify_candidate(track_data)
                        if candidate:
                            candidate.source = "Spotify público"
                            tracks.append(candidate)
                            if len(tracks) >= limit:
                                break
                    total = total or int(data.get("total") or 0)
                    if not data.get("next"):
                        break
                    offset += len(items)
                if tracks:
                    return ApiTrackBatch(tracks=tracks, title=playlist_title, is_playlist=True, truncated=bool((total or 0) and total > len(tracks)), source="Spotify público")
        except Exception as exc:
            last_error = exc
            logger.debug("[music-api] fallback público Spotify API falhou | kind=%s id=%s", kind, item_id, exc_info=True)

        page_batch = await self._spotify_public_page_batch(kind, item_id, limit=limit, original_url=url)
        if page_batch and page_batch.tracks:
            return page_batch
        if last_error:
            logger.debug("[music-api] fallback público Spotify sem resultado", exc_info=last_error)
        return None

    def _spotify_track_id(self, url: str) -> str:
        kind, item_id = self._spotify_resource(url)
        return item_id if kind == "track" else ""

    def _spotify_resource(self, url: str) -> tuple[str, str]:
        parsed = urlparse(url)
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) >= 2 and parts[0] in {"track", "album", "playlist"}:
            return parts[0], parts[1]
        return "", ""

    async def _spotify_token_candidates(self, *, prefer_user: bool = False) -> list[tuple[str, str]]:
        candidates: list[tuple[str, str]] = []
        if prefer_user and self.spotify_has_user_auth:
            try:
                user_token = await self.spotify_token(user=True)
                if user_token:
                    candidates.append(("user", user_token))
            except Exception:
                logger.debug("[music-api] falha ao gerar token Spotify de usuário", exc_info=True)
        try:
            app_token = await self.spotify_token(user=False)
            if app_token and all(token != app_token for _, token in candidates):
                candidates.append(("app", app_token))
        except Exception:
            logger.debug("[music-api] falha ao gerar token Spotify de app", exc_info=True)
        return candidates

    async def _spotify_json_first_ok(self, urls: Iterable[str], *, headers: dict[str, str]) -> dict[str, Any]:
        last_error: Exception | None = None
        for url in urls:
            try:
                return await self._to_thread_json(url, headers=headers)
            except HTTPError as exc:
                last_error = exc
                # Tenta variantes sem market/additional_types antes de desistir.
                if exc.code in {400, 403, 404}:
                    continue
                raise
            except Exception as exc:
                last_error = exc
                break
        if last_error:
            raise last_error
        return {}

    def _spotify_playlist_meta_urls(self, playlist_id: str) -> list[str]:
        quoted_id = quote(playlist_id)
        fields = quote("name,tracks.total", safe=",.")
        market = quote(self.spotify_market)
        return [
            f"https://api.spotify.com/v1/playlists/{quoted_id}?fields={fields}&market={market}",
            f"https://api.spotify.com/v1/playlists/{quoted_id}?fields={fields}",
        ]

    def _spotify_playlist_tracks_urls(self, playlist_id: str, *, limit: int, offset: int) -> list[str]:
        quoted_id = quote(playlist_id)
        fields = "items(track(name,artists(name),album(name,images),duration_ms,external_ids,external_urls,is_local,type)),next,total"
        base = {
            "limit": max(1, min(50, int(limit))),
            "offset": max(0, int(offset)),
            "fields": fields,
        }
        market = self.spotify_market
        variants = [
            {**base, "market": market, "additional_types": "track"},
            {**base, "market": market},
            {**base, "additional_types": "track"},
            base,
        ]
        return [f"https://api.spotify.com/v1/playlists/{quoted_id}/tracks?{urlencode(params)}" for params in variants]

    async def spotify_batch_from_url(self, url: str, *, limit: int = 25) -> ApiTrackBatch | None:
        kind, item_id = self._spotify_resource(url)
        if not item_id:
            return None
        limit = max(1, min(100, int(limit)))

        last_error: Exception | None = None

        if kind == "track":
            if self.spotify_client_id and self.spotify_client_secret:
                try:
                    token = await self.spotify_token()
                    if token:
                        data = await self._to_thread_json(
                            f"https://api.spotify.com/v1/tracks/{quote(item_id)}?market={quote(self.spotify_market)}",
                            headers=self._spotify_headers(token),
                        )
                        candidate = self._spotify_candidate(data, url=url)
                        if candidate:
                            return ApiTrackBatch(tracks=[candidate], title=candidate.title, is_playlist=False, source="Spotify API")
                except Exception as exc:
                    last_error = exc
                    logger.debug("[music-api] Spotify API track falhou, tentando fallback público | url=%s", url, exc_info=True)
            public_batch = await self.spotify_public_batch_from_url(url, limit=1)
            if public_batch and public_batch.tracks:
                return public_batch
            if last_error:
                raise last_error
            return None

        if kind == "album":
            if self.spotify_client_id and self.spotify_client_secret:
                try:
                    token = await self.spotify_token()
                    if token:
                        data = await self._to_thread_json(
                            f"https://api.spotify.com/v1/albums/{quote(item_id)}?market={quote(self.spotify_market)}",
                            headers=self._spotify_headers(token),
                        )
                        album_title = str(data.get("name") or "Álbum Spotify")
                        images = data.get("images") or []
                        album_image = str((images[0] or {}).get("url") or "") if images else ""
                        album_artists = data.get("artists") or []
                        default_artist = ", ".join(str(a.get("name") or "").strip() for a in album_artists if a.get("name"))
                        items = (((data.get("tracks") or {}).get("items")) or [])[:limit]
                        tracks: list[ApiTrackCandidate] = []
                        for item in items:
                            candidate = self._spotify_candidate({**item, "album": data}, url=str(((item.get("external_urls") or {}).get("spotify")) or url))
                            if candidate:
                                if not candidate.artist:
                                    candidate.artist = default_artist
                                if not candidate.thumbnail:
                                    candidate.thumbnail = album_image
                                candidate.album = album_title
                                tracks.append(candidate)
                        total = int(((data.get("tracks") or {}).get("total")) or len(tracks))
                        if tracks:
                            return ApiTrackBatch(tracks=tracks, title=album_title, is_playlist=True, truncated=total > len(tracks), source="Spotify API")
                except Exception as exc:
                    last_error = exc
                    logger.debug("[music-api] Spotify API album falhou, tentando fallback público | url=%s", url, exc_info=True)
            public_batch = await self.spotify_public_batch_from_url(url, limit=limit)
            if public_batch and public_batch.tracks:
                return public_batch
            if last_error:
                raise last_error
            return None

        if kind == "playlist":
            if self.spotify_client_id and self.spotify_client_secret:
                token_candidates = await self._spotify_token_candidates(prefer_user=True)
            else:
                token_candidates = []

            for token_kind, token in token_candidates:
                headers = self._spotify_headers(token)
                playlist_title = "Playlist Spotify"
                total = 0
                try:
                    meta = await self._spotify_json_first_ok(self._spotify_playlist_meta_urls(item_id), headers=headers)
                    playlist_title = str(meta.get("name") or playlist_title)
                    total = int(((meta.get("tracks") or {}).get("total")) or 0)
                except HTTPError as exc:
                    last_error = exc
                    # Algumas contas/apps conseguem ler /tracks mesmo quando o endpoint
                    # de metadata recusa fields/market. Segue tentando os itens.
                    if exc.code not in {400, 403, 404}:
                        raise
                except Exception as exc:
                    last_error = exc
                    continue

                tracks: list[ApiTrackCandidate] = []
                offset = 0
                try:
                    while len(tracks) < limit:
                        page_limit = min(50, limit - len(tracks))
                        data = await self._spotify_json_first_ok(
                            self._spotify_playlist_tracks_urls(item_id, limit=page_limit, offset=offset),
                            headers=headers,
                        )
                        items = data.get("items") or []
                        if not items:
                            break
                        for row in items:
                            track_data = row.get("track") or {}
                            if not track_data or track_data.get("is_local") or str(track_data.get("type") or "track") != "track":
                                continue
                            candidate = self._spotify_candidate(track_data)
                            if candidate:
                                tracks.append(candidate)
                                if len(tracks) >= limit:
                                    break
                        if not data.get("next"):
                            break
                        offset += len(items)
                        total = total or int(data.get("total") or 0)
                    if tracks:
                        return ApiTrackBatch(
                            tracks=tracks,
                            title=playlist_title,
                            is_playlist=True,
                            truncated=bool((total or 0) and total > len(tracks)),
                            source=f"Spotify API ({token_kind})",
                        )
                except HTTPError as exc:
                    last_error = exc
                    if exc.code not in {400, 403, 404}:
                        raise
                    # Tenta próximo token e depois fallback público.
                    continue
                except Exception as exc:
                    last_error = exc
                    continue

            # Fallback público estilo spotify-url-info: ajuda principalmente em
            # playlists públicas quando apps novos recebem 403 na Web API.
            public_batch = await self.spotify_public_batch_from_url(url, limit=limit)
            if public_batch and public_batch.tracks:
                return public_batch
            if last_error:
                raise last_error
            return None
        return None

    async def spotify_track_from_url(self, url: str) -> ApiTrackCandidate | None:
        track_id = self._spotify_track_id(url)
        if not track_id or not (self.spotify_client_id and self.spotify_client_secret):
            return None
        token = await self.spotify_token()
        if not token:
            return None
        data = await self._to_thread_json(f"https://api.spotify.com/v1/tracks/{quote(track_id)}?market={quote(self.spotify_market)}", headers=self._spotify_headers(token))
        return self._spotify_candidate(data, url=url)

    async def spotify_search(self, query: str, *, limit: int = 5) -> list[ApiTrackCandidate]:
        token = await self.spotify_token()
        if not token:
            return []
        params = urlencode({"q": query, "type": "track", "limit": max(1, min(10, int(limit))), "market": self.spotify_market})
        data = await self._to_thread_json(f"https://api.spotify.com/v1/search?{params}", headers=self._spotify_headers(token))
        items = (((data.get("tracks") or {}).get("items")) or [])
        return [cand for cand in (self._spotify_candidate(item) for item in items) if cand]

    def _spotify_candidate(self, data: dict[str, Any], *, url: str = "") -> ApiTrackCandidate | None:
        if not data:
            return None
        title = str(data.get("name") or "").strip()
        artists = data.get("artists") or []
        artist = ", ".join(str(a.get("name") or "").strip() for a in artists if a.get("name"))
        album_data = data.get("album") or {}
        images = album_data.get("images") or []
        image = str((images[0] or {}).get("url") or "") if images else ""
        duration_ms = data.get("duration_ms")
        external_ids = data.get("external_ids") or {}
        external_urls = data.get("external_urls") or {}
        webpage_url = url or str(external_urls.get("spotify") or "")
        if not title:
            return None
        return ApiTrackCandidate(
            title=title,
            artist=artist,
            album=str(album_data.get("name") or ""),
            duration=(float(duration_ms) / 1000.0) if duration_ms else None,
            thumbnail=image,
            webpage_url=webpage_url,
            source="Spotify API",
            provider="spotify",
            isrc=str(external_ids.get("isrc") or ""),
            query=" ".join(part for part in (artist, title, "official audio") if part),
            score=35,
        )

    def _deezer_resource(self, url: str) -> tuple[str, str]:
        parsed = urlparse(url)
        parts = [part for part in parsed.path.split("/") if part]
        for kind in ("track", "album", "playlist"):
            if kind in parts:
                idx = parts.index(kind)
                if idx + 1 < len(parts):
                    return kind, parts[idx + 1]
        return "", ""

    async def deezer_batch_from_url(self, url: str, *, limit: int = 25) -> ApiTrackBatch | None:
        if not self.deezer_enabled:
            return None
        kind, item_id = self._deezer_resource(url)
        if not item_id:
            return None
        limit = max(1, min(100, int(limit)))
        if kind == "track":
            data = await self._to_thread_json(f"https://api.deezer.com/track/{quote(item_id)}")
            candidate = self._deezer_candidate(data, url=url)
            return ApiTrackBatch(tracks=[candidate] if candidate else [], title=candidate.title if candidate else "", is_playlist=False, source="Deezer API")
        if kind == "album":
            data = await self._to_thread_json(f"https://api.deezer.com/album/{quote(item_id)}")
            title = str(data.get("title") or "Álbum Deezer")
            artist_data = data.get("artist") or {}
            album_data = {"title": title, "cover_medium": data.get("cover_medium") or data.get("cover") or ""}
            tracks: list[ApiTrackCandidate] = []
            for item in ((data.get("tracks") or {}).get("data") or [])[:limit]:
                candidate = self._deezer_candidate({**item, "artist": item.get("artist") or artist_data, "album": album_data})
                if candidate:
                    candidate.album = title
                    tracks.append(candidate)
            total = int(((data.get("tracks") or {}).get("total")) or len(tracks))
            return ApiTrackBatch(tracks=tracks, title=title, is_playlist=True, truncated=total > len(tracks), source="Deezer API")
        if kind == "playlist":
            data = await self._to_thread_json(f"https://api.deezer.com/playlist/{quote(item_id)}")
            title = str(data.get("title") or "Playlist Deezer")
            tracks: list[ApiTrackCandidate] = []
            next_url = ""
            for item in ((data.get("tracks") or {}).get("data") or []):
                candidate = self._deezer_candidate(item)
                if candidate:
                    tracks.append(candidate)
                    if len(tracks) >= limit:
                        break
            next_url = str(((data.get("tracks") or {}).get("next")) or "")
            while next_url and len(tracks) < limit:
                page = await self._to_thread_json(next_url)
                for item in page.get("data") or []:
                    candidate = self._deezer_candidate(item)
                    if candidate:
                        tracks.append(candidate)
                        if len(tracks) >= limit:
                            break
                next_url = str(page.get("next") or "")
            total = int(((data.get("tracks") or {}).get("total")) or len(tracks))
            return ApiTrackBatch(tracks=tracks, title=title, is_playlist=True, truncated=total > len(tracks), source="Deezer API")
        return None

    async def deezer_track_from_url(self, url: str) -> ApiTrackCandidate | None:
        parsed = urlparse(url)
        parts = [part for part in parsed.path.split("/") if part]
        track_id = ""
        if "track" in parts:
            idx = parts.index("track")
            if idx + 1 < len(parts):
                track_id = parts[idx + 1]
        if not track_id:
            return None
        data = await self._to_thread_json(f"https://api.deezer.com/track/{quote(track_id)}")
        return self._deezer_candidate(data, url=url)

    async def deezer_search(self, query: str, *, limit: int = 5) -> list[ApiTrackCandidate]:
        if not self.deezer_enabled:
            return []
        params = urlencode({"q": query, "limit": max(1, min(10, int(limit)))})
        data = await self._to_thread_json(f"https://api.deezer.com/search/track?{params}")
        items = data.get("data") or []
        return [cand for cand in (self._deezer_candidate(item) for item in items) if cand]

    def _deezer_candidate(self, data: dict[str, Any], *, url: str = "") -> ApiTrackCandidate | None:
        if not data:
            return None
        title = str(data.get("title") or data.get("title_short") or "").strip()
        artist_data = data.get("artist") or {}
        album_data = data.get("album") or {}
        artist = str(artist_data.get("name") or "").strip()
        if not title:
            return None
        return ApiTrackCandidate(
            title=title,
            artist=artist,
            album=str(album_data.get("title") or ""),
            duration=float(data.get("duration") or 0) or None,
            thumbnail=str(album_data.get("cover_medium") or album_data.get("cover") or ""),
            webpage_url=url or str(data.get("link") or ""),
            source="Deezer API",
            provider="deezer",
            isrc=str(data.get("isrc") or ""),
            query=" ".join(part for part in (artist, title, "official audio") if part),
            score=25,
        )

    def _soundcloud_auth(self, params: dict[str, Any] | None = None, headers: dict[str, str] | None = None) -> tuple[dict[str, Any], dict[str, str]]:
        params = dict(params or {})
        headers = dict(headers or {})
        if self.soundcloud_token:
            headers["Authorization"] = f"OAuth {self.soundcloud_token}"
        elif self.soundcloud_client_id:
            params["client_id"] = self.soundcloud_client_id
        return params, headers

    async def soundcloud_batch_from_url(self, url: str, *, limit: int = 25) -> ApiTrackBatch | None:
        if not (self.soundcloud_enabled and (self.soundcloud_token or self.soundcloud_client_id)):
            return None
        params, headers = self._soundcloud_auth({"url": url})
        data = await self._to_thread_json("https://api.soundcloud.com/resolve?" + urlencode(params), headers=headers)
        if not data:
            return None
        kind = str(data.get("kind") or "").lower()
        if kind == "track":
            candidate = self._soundcloud_candidate(data, fallback_url=url)
            return ApiTrackBatch(tracks=[candidate] if candidate else [], title=candidate.title if candidate else "", is_playlist=False, source="SoundCloud API")
        if kind in {"playlist", "system-playlist"} or data.get("tracks"):
            title = str(data.get("title") or "Playlist SoundCloud")
            tracks: list[ApiTrackCandidate] = []
            for item in (data.get("tracks") or [])[: max(1, int(limit))]:
                candidate = self._soundcloud_candidate(item, fallback_url="")
                if candidate:
                    tracks.append(candidate)
            total = int(data.get("track_count") or len(data.get("tracks") or []) or len(tracks))
            return ApiTrackBatch(tracks=tracks, title=title, is_playlist=True, truncated=total > len(tracks), source="SoundCloud API")
        return None

    def _soundcloud_candidate(self, item: dict[str, Any], *, fallback_url: str = "") -> ApiTrackCandidate | None:
        title = str(item.get("title") or "").strip()
        if not title:
            return None
        user = item.get("user") or {}
        duration = item.get("duration")
        return ApiTrackCandidate(
            title=title,
            artist=str(user.get("username") or user.get("full_name") or ""),
            duration=(float(duration) / 1000.0) if duration else None,
            thumbnail=str(item.get("artwork_url") or item.get("waveform_url") or ""),
            webpage_url=str(item.get("permalink_url") or fallback_url or ""),
            source="SoundCloud API",
            provider="soundcloud",
            query=" ".join(part for part in (str(user.get("username") or ""), title) if part),
            score=30,
        )

    async def soundcloud_search(self, query: str, *, limit: int = 5) -> list[ApiTrackCandidate]:
        if not (self.soundcloud_enabled and (self.soundcloud_token or self.soundcloud_client_id)):
            return []
        params, headers = self._soundcloud_auth({"q": query, "limit": max(1, min(10, int(limit)))})
        url = self.soundcloud_base_url.rstrip("?") + "?" + urlencode(params)
        data = await self._to_thread_json(url, headers=headers)
        items = data if isinstance(data, list) else (data.get("collection") or data.get("data") or [])
        candidates: list[ApiTrackCandidate] = []
        for item in items:
            candidate = self._soundcloud_candidate(item)
            if candidate:
                candidate.query = query
                candidates.append(candidate)
        return candidates
