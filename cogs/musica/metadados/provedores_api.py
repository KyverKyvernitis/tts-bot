from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, Iterable
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from cogs.musica import configuracao as config

from .modelos import ApiTrackBatch, ApiTrackCandidate
from .normalizacao import compact_key, is_bad_match_title, normalize_text, title_quality_score
from .fontes.deezer import ProvedorDeezerMixin
from .fontes.soundcloud import ProvedorSoundCloudMixin
from .fontes.spotify import ProvedorSpotifyMixin
from .fontes.youtube import ProvedorYouTubeMixin

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


class MusicApiProviders(ProvedorSpotifyMixin, ProvedorYouTubeMixin, ProvedorDeezerMixin, ProvedorSoundCloudMixin):
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

    async def search_sources(self, query: str, *, limit: int = 5, prefer_youtube: bool = True) -> list[ApiTrackCandidate]:
        """Retorna candidatos crus das fontes disponíveis, preservando a origem.

        A deduplicação cross-provider fica para a camada de busca inteligente,
        que consegue fundir por identidade/duração/ISRC junto dos resultados do
        Phone Worker. O método ``search`` mantém o contrato legado ranqueado.
        """
        if not self.enabled or not query.strip():
            return []
        limit = max(1, min(10, int(limit)))
        tasks: list[asyncio.Task[list[ApiTrackCandidate]]] = []
        if prefer_youtube and self.youtube_api_key:
            tasks.append(asyncio.create_task(self.youtube_search(query, limit=limit)))
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
        return results

    async def search(self, query: str, *, limit: int = 5, prefer_youtube: bool = True) -> list[ApiTrackCandidate]:
        results = await self.search_sources(query, limit=limit, prefer_youtube=prefer_youtube)
        if not results:
            return []
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


