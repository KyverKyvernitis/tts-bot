from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import time
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Iterable
from urllib.parse import parse_qs, quote, urlencode, urlparse
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
        self.deezer_enabled = _env_bool("DEEZER_API_ENABLED", True)
        self.soundcloud_enabled = _env_bool("SOUNDCLOUD_API_ENABLED", False)
        self.soundcloud_token = _env("SOUNDCLOUD_API_TOKEN")
        self.soundcloud_client_id = _env("SOUNDCLOUD_CLIENT_ID")
        self.soundcloud_base_url = _env("SOUNDCLOUD_API_BASE_URL", "https://api.soundcloud.com/tracks")
        self._spotify_token = ""
        self._spotify_token_expires_at = 0.0

    @property
    def has_any_provider(self) -> bool:
        return bool(
            self.enabled
            and (
                self.youtube_api_key
                or (self.spotify_client_id and self.spotify_client_secret)
                or self.deezer_enabled
                or (self.soundcloud_enabled and (self.soundcloud_token or self.soundcloud_client_id))
            )
        )

    async def metadata_from_url(self, url: str) -> ApiTrackCandidate | None:
        if not self.enabled:
            return None
        host = (urlparse(url).netloc or "").lower()
        path = urlparse(url).path or ""
        if "open.spotify.com" in host and "/track/" in path:
            return await self.spotify_track_from_url(url)
        if "deezer.com" in host and "/track/" in path:
            return await self.deezer_track_from_url(url)
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

    async def spotify_token(self) -> str:
        if not (self.spotify_client_id and self.spotify_client_secret):
            return ""
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

    def _spotify_headers(self, token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}"}

    def _spotify_track_id(self, url: str) -> str:
        parsed = urlparse(url)
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) >= 2 and parts[0] == "track":
            return parts[1]
        return ""

    async def spotify_track_from_url(self, url: str) -> ApiTrackCandidate | None:
        track_id = self._spotify_track_id(url)
        if not track_id or not (self.spotify_client_id and self.spotify_client_secret):
            return None
        token = await self.spotify_token()
        if not token:
            return None
        data = await self._to_thread_json(f"https://api.spotify.com/v1/tracks/{quote(track_id)}", headers=self._spotify_headers(token))
        return self._spotify_candidate(data, url=url)

    async def spotify_search(self, query: str, *, limit: int = 5) -> list[ApiTrackCandidate]:
        token = await self.spotify_token()
        if not token:
            return []
        params = urlencode({"q": query, "type": "track", "limit": max(1, min(10, int(limit)))})
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

    async def soundcloud_search(self, query: str, *, limit: int = 5) -> list[ApiTrackCandidate]:
        if not (self.soundcloud_enabled and (self.soundcloud_token or self.soundcloud_client_id)):
            return []
        params = {"q": query, "limit": max(1, min(10, int(limit)))}
        headers: dict[str, str] = {}
        if self.soundcloud_token:
            headers["Authorization"] = f"OAuth {self.soundcloud_token}"
        elif self.soundcloud_client_id:
            params["client_id"] = self.soundcloud_client_id
        url = self.soundcloud_base_url.rstrip("?") + "?" + urlencode(params)
        data = await self._to_thread_json(url, headers=headers)
        items = data if isinstance(data, list) else (data.get("collection") or data.get("data") or [])
        candidates: list[ApiTrackCandidate] = []
        for item in items:
            title = str(item.get("title") or "").strip()
            user = item.get("user") or {}
            duration = item.get("duration")
            if not title:
                continue
            candidates.append(ApiTrackCandidate(
                title=title,
                artist=str(user.get("username") or user.get("full_name") or ""),
                duration=(float(duration) / 1000.0) if duration else None,
                thumbnail=str(item.get("artwork_url") or item.get("waveform_url") or ""),
                webpage_url=str(item.get("permalink_url") or ""),
                source="SoundCloud API",
                provider="soundcloud",
                query=query,
                score=28,
            ))
        return candidates
