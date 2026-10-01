from __future__ import annotations

import asyncio
import base64
from collections import OrderedDict
import html
import json
import logging
import re
import time
import sys
from typing import Any, Iterable
from cogs.musica import configuracao as config
from urllib.error import HTTPError
from urllib.parse import quote, urlencode, urlparse
from urllib.request import Request, urlopen

from ..modelos import ApiTrackBatch, ApiTrackCandidate
from ..normalizacao import compact_key, normalize_text, parse_iso8601_duration

logger = logging.getLogger(__name__)

_SPOTIFY_TRANSIENT_HTTP_CODES = {429, 500, 502, 503, 504}

class SpotifyAutenticacaoMixin:
    @property
    def spotify_has_user_auth(self) -> bool:
        return bool(self.spotify_client_id and self.spotify_client_secret and self.spotify_refresh_token)

    async def _spotify_to_thread_json(
        self,
        url: str,
        *,
        method: str = "GET",
        data: bytes | None = None,
        headers: dict[str, str] | None = None,
        attempts: int = 3,
    ) -> dict[str, Any]:
        attempts = max(1, int(attempts or 1))
        last_error: Exception | None = None
        for attempt in range(attempts):
            try:
                return await self._to_thread_json(url, method=method, data=data, headers=headers)
            except HTTPError as exc:
                last_error = exc
                if exc.code not in _SPOTIFY_TRANSIENT_HTTP_CODES or attempt >= attempts - 1:
                    raise
                await asyncio.sleep(min(1.5, 0.35 * (attempt + 1)))
            except Exception as exc:
                last_error = exc
                if attempt >= attempts - 1:
                    raise
                await asyncio.sleep(min(1.5, 0.35 * (attempt + 1)))
        if last_error:
            raise last_error
        return {}

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
        session = await self._http_session_persistente()
        # Só documentos públicos de coleções; nenhum token ou áudio no cache.
        cacheable = bool(re.fullmatch(r"https://open\.spotify\.com/(?:embed/)?(?:playlist|album)/[A-Za-z0-9]+", url) and not headers)
        ttl = max(0.0, min(60.0, float(getattr(config, "MUSIC_SPOTIFY_PUBLIC_HTML_CACHE_TTL_SECONDS", 20.0))))
        cache = getattr(self, "_spotify_public_html_cache", None)
        if cache is None:
            cache = OrderedDict()
            self._spotify_public_html_cache = cache
        key = (url, max_bytes)
        previous = cache.get(key) if cacheable and ttl else None
        if previous is not None and previous[0] > time.monotonic():
            cache.move_to_end(key)
            return previous[1]
        request_headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/json;q=0.8,*/*;q=0.7",
            "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
            **(headers or {}),
        }
        if previous is not None:
            if previous[2]:
                request_headers["If-None-Match"] = previous[2]
            if previous[3]:
                request_headers["If-Modified-Since"] = previous[3]
        async with session.get(url, headers=request_headers) as response:
            if response.status == 304 and previous is not None:
                cache[key] = (time.monotonic() + ttl, *previous[1:])
                cache.move_to_end(key)
                return previous[1]
            if response.status >= 400:
                raise HTTPError(url, int(response.status), str(response.reason or "HTTP error"), response.headers, None)
            chunks, size = [], 0
            while True:
                chunk = await response.content.read(min(65_536, max_bytes + 1 - size))
                if not chunk:
                    break
                size += len(chunk)
                if size > max_bytes:
                    raise ValueError("resposta HTML do provider excedeu o orçamento")
                chunks.append(chunk)
            content = b"".join(chunks).decode("utf-8", errors="ignore")
            if cacheable and ttl:
                cache[key] = (time.monotonic() + ttl, content, response.headers.get("ETag", ""),
                              response.headers.get("Last-Modified", ""), sys.getsizeof(content))
                cache.move_to_end(key)
                while len(cache) > 4 or sum(item[4] for item in cache.values()) > 8 * 1024 * 1024:
                    cache.popitem(last=False)
        return content

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
