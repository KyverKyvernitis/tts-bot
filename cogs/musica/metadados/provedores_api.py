from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, Iterable
from urllib.error import HTTPError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

import aiohttp

from cogs.musica import configuracao as config

from .modelos import ApiTrackBatch, ApiTrackCandidate
from .normalizacao import compact_key, is_bad_match_title, normalize_text, title_quality_score
from .fontes.deezer import ProvedorDeezerMixin
from .fontes.soundcloud import ProvedorSoundCloudMixin
from .fontes.spotify import ProvedorSpotifyMixin
from .fontes.youtube import ProvedorYouTubeMixin
from .resiliencia import executar_provider_resiliente
from .quota_youtube import consumir_busca_youtube

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
        self._http_session: aiohttp.ClientSession | None = None
        self._http_loop: asyncio.AbstractEventLoop | None = None

    def _consumir_busca_youtube(self) -> bool:
        return consumir_busca_youtube(
            limite_diario=int(getattr(config, "MUSIC_SEARCH_YOUTUBE_API_DAILY_SOFT_CALLS", 80) or 0),
            habilitado=bool(getattr(config, "MUSIC_SEARCH_YOUTUBE_API_QUOTA_GUARD_ENABLED", True)),
        )

    async def _youtube_search_com_quota(
        self,
        query: str,
        *,
        limit: int,
        include_details: bool = False,
    ) -> list[ApiTrackCandidate]:
        if not self._consumir_busca_youtube():
            logger.debug("[music/search] youtube api pulada pelo guard local de quota")
            return []
        return await self.youtube_search(query, limit=limit, include_details=include_details)

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
            # Playback não depende da Spotify Web API/OAuth. Links Spotify são
            # direct play e usam somente metadata pública do web player/embed.
            return await self.spotify_public_batch_from_url(url, limit=limit)
        if "deezer.com" in host:
            return await self.deezer_batch_from_url(url, limit=limit)
        if "soundcloud.com" in host and self.soundcloud_enabled and (self.soundcloud_token or self.soundcloud_client_id):
            return await self.soundcloud_batch_from_url(url, limit=limit)
        return None

    async def search_sources(self, query: str, *, limit: int = 3, prefer_youtube: bool = True, total_budget_seconds: float | None = None, provider_order: tuple[str, ...] | None = None, max_providers: int | None = None) -> list[ApiTrackCandidate]:
        """Retorna candidatos crus das fontes disponíveis, preservando a origem.

        A deduplicação cross-provider fica para a camada de busca inteligente,
        que consegue fundir por identidade/duração/ISRC junto dos resultados do
        Phone Worker. O método ``search`` mantém o contrato legado ranqueado.
        """
        if not self.enabled or not query.strip():
            return []
        limit = max(1, min(10, int(limit)))
        timeout_provider = max(
            0.2,
            float(getattr(config, "MUSIC_SEARCH_PROVIDER_TIMEOUT_SECONDS", 1.5) or 1.5),
        )
        falhas_para_abrir = max(
            1,
            int(getattr(config, "MUSIC_SEARCH_PROVIDER_CIRCUIT_FAILURES", 2) or 2),
        )
        cooldown = max(
            1.0,
            float(getattr(config, "MUSIC_SEARCH_PROVIDER_CIRCUIT_COOLDOWN_SECONDS", 30.0) or 30.0),
        )

        tasks: list[asyncio.Task[list[ApiTrackCandidate]]] = []

        def _agendar(nome: str, func, provider_limit: int, *, kwargs: dict[str, Any] | None = None) -> None:
            async def _operacao() -> list[ApiTrackCandidate]:
                return await func(query, limit=provider_limit, **(kwargs or {}))

            tasks.append(
                asyncio.create_task(
                    executar_provider_resiliente(
                        nome,
                        _operacao,
                        timeout_seconds=timeout_provider,
                        falhas_para_abrir=falhas_para_abrir,
                        cooldown_seconds=cooldown,
                        fallback=[],
                    )
                )
            )

        ordem = provider_order or ("youtube", "spotify", "deezer", "soundcloud")
        limite_fontes = None if max_providers is None else max(0, int(max_providers))
        agendadas = 0
        vistos: set[str] = set()
        for nome in ordem:
            nome = str(nome or "").strip().lower()
            if not nome or nome in vistos:
                continue
            vistos.add(nome)
            if limite_fontes is not None and agendadas >= limite_fontes:
                break
            if nome == "youtube":
                if not prefer_youtube or not self.youtube_api_key:
                    continue
                _agendar("youtube", self._youtube_search_com_quota, limit, kwargs={"include_details": False})
            elif nome == "spotify":
                if not (self.spotify_client_id and self.spotify_client_secret):
                    continue
                _agendar("spotify", self.spotify_search, min(limit, 5))
            elif nome == "deezer":
                if not self.deezer_enabled:
                    continue
                _agendar("deezer", self.deezer_search, min(limit, 5))
            elif nome == "soundcloud":
                if not (self.soundcloud_enabled and (self.soundcloud_token or self.soundcloud_client_id)):
                    continue
                _agendar("soundcloud", self.soundcloud_search, min(limit, 5))
            else:
                continue
            agendadas += 1

        if not tasks:
            return []

        results: list[ApiTrackCandidate] = []
        budget = (
            max(0.05, float(total_budget_seconds))
            if total_budget_seconds is not None
            else max(0.05, float(getattr(config, "MUSIC_SEARCH_PROVIDER_FAST_BUDGET_SECONDS", 0.65) or 0.65))
        )
        done, pending = await asyncio.wait(tasks, timeout=budget)
        # asyncio.wait devolve sets; iterar a lista original preserva a prioridade
        # deterministica dos providers mesmo quando varios concluem juntos.
        for task in tasks:
            if task not in done:
                continue
            try:
                results.extend(task.result())
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.debug("[music/search] provider descartado apos falha | erro=%s", exc)
        if pending:
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            logger.debug(
                "[music/search] budget de providers atingido | budget_ms=%.0f concluidos=%s pendentes=%s",
                budget * 1000.0,
                len(done),
                len(pending),
            )
        return results

    async def search(self, query: str, *, limit: int = 3, prefer_youtube: bool = True) -> list[ApiTrackCandidate]:
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
        # Mantido como fallback/test seam para caminhos legados síncronos. A busca
        # normal usa ``_to_thread_json`` abaixo, que agora reutiliza ClientSession.
        request = Request(url, data=data, method=method, headers={
            "User-Agent": "DiscordMusicBot/1.0",
            "Accept": "application/json",
            **(headers or {}),
        })
        with urlopen(request, timeout=self.timeout) as response:  # noqa: S310 - URLs fixas dos providers configurados
            raw = response.read(2_000_000)
        return json.loads(raw.decode("utf-8", errors="ignore") or "{}")

    async def _http_session_persistente(self) -> aiohttp.ClientSession:
        loop = asyncio.get_running_loop()
        session = self._http_session
        if session is not None and not session.closed and self._http_loop is loop:
            return session
        if session is not None and not session.closed:
            await session.close()

        pool_limit = max(2, int(getattr(config, "MUSIC_SEARCH_HTTP_POOL_LIMIT", 8) or 8))
        per_host = max(1, min(pool_limit, int(getattr(config, "MUSIC_SEARCH_HTTP_POOL_LIMIT_PER_HOST", 4) or 4)))
        keepalive = max(5.0, float(getattr(config, "MUSIC_SEARCH_HTTP_KEEPALIVE_SECONDS", 30.0) or 30.0))
        dns_ttl = max(30.0, float(getattr(config, "MUSIC_SEARCH_HTTP_DNS_CACHE_SECONDS", 300.0) or 300.0))
        connector = aiohttp.TCPConnector(
            limit=pool_limit,
            limit_per_host=per_host,
            keepalive_timeout=keepalive,
            ttl_dns_cache=int(dns_ttl),
        )
        timeout = aiohttp.ClientTimeout(
            total=self.timeout,
            connect=min(2.0, self.timeout),
            sock_connect=min(2.0, self.timeout),
            sock_read=self.timeout,
        )
        session = aiohttp.ClientSession(
            connector=connector,
            timeout=timeout,
            headers={
                "User-Agent": "DiscordMusicBot/1.0",
                "Accept": "application/json",
            },
        )
        self._http_session = session
        self._http_loop = loop
        return session

    async def close(self) -> None:
        session = self._http_session
        self._http_session = None
        self._http_loop = None
        if session is not None and not session.closed:
            await session.close()

    async def _to_thread_json(self, url: str, *, method: str = "GET", data: bytes | None = None, headers: dict[str, str] | None = None) -> dict[str, Any]:
        # O nome é preservado por compatibilidade interna, mas o caminho quente
        # deixou de abrir urllib em uma thread por chamada. ClientSession mantém
        # DNS/TCP/TLS/keep-alive entre pesquisas consecutivas.
        session = await self._http_session_persistente()
        async with session.request(method, url, data=data, headers=headers or None) as response:
            raw = await response.content.read(2_000_001)
            if len(raw) > 2_000_000:
                raise ValueError("resposta JSON do provider excedeu 2 MB")
            if response.status >= 400:
                raise HTTPError(
                    url,
                    int(response.status),
                    str(response.reason or "HTTP error"),
                    response.headers,
                    None,
                )
        return json.loads(raw.decode("utf-8", errors="ignore") or "{}")

    async def search_youtube_fast(
        self,
        query: str,
        *,
        limit: int = 3,
        timeout_seconds: float | None = None,
    ) -> list[ApiTrackCandidate]:
        """Uma unica search.list para o caminho API-first simplificado.

        O circuit breaker pertence a ``busca.fontes`` para distinguir falha de
        resposta vazia e decidir quando cair no Phone Worker. Aqui nao existe
        retry nem fallback: uma tentativa, um timeout curto, e o erro propaga.
        """
        if not (self.enabled and self.youtube_api_key and str(query or "").strip()):
            return []
        timeout_provider = max(
            0.10,
            float(
                timeout_seconds
                if timeout_seconds is not None
                else getattr(config, "MUSIC_SEARCH_API_FIRST_TIMEOUT_SECONDS", 0.30) or 0.30
            ),
        )
        return await asyncio.wait_for(
            self._youtube_search_com_quota(query, limit=limit, include_details=False),
            timeout=timeout_provider,
        )


