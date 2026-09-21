from __future__ import annotations

import asyncio
import base64
import html
import json
import logging
import re
import time
from typing import Any, Iterable
from urllib.error import HTTPError
from urllib.parse import quote, urlencode, urlparse
from urllib.request import Request, urlopen

from ..modelos import ApiTrackBatch, ApiTrackCandidate
from ..normalizacao import compact_key, normalize_text, parse_iso8601_duration

logger = logging.getLogger(__name__)

from .spotify_autenticacao import _SPOTIFY_TRANSIENT_HTTP_CODES

class SpotifyApiMixin:
    def _spotify_track_id(self, url: str) -> str:
        kind, item_id = self._spotify_resource(url)
        return item_id if kind == "track" else ""

    def _spotify_resource(self, url: str) -> tuple[str, str]:
        parsed = urlparse(url)
        parts = [part.strip() for part in parsed.path.split("/") if part.strip()]
        for index, part in enumerate(parts):
            kind = part.lower()
            if kind not in {"track", "album", "playlist"} or index + 1 >= len(parts):
                continue
            item_id = re.sub(r"[^A-Za-z0-9]", "", parts[index + 1])
            if item_id:
                return kind, item_id
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
                return await self._spotify_to_thread_json(url, headers=headers)
            except HTTPError as exc:
                last_error = exc
                # Tenta variantes sem market/additional_types antes de desistir.
                # 502/503/504/429 podem oscilar; _spotify_to_thread_json já fez
                # retry curto, então continua para a próxima variante/token.
                if exc.code in {400, 403, 404} or exc.code in _SPOTIFY_TRANSIENT_HTTP_CODES:
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
            "limit": max(1, min(100, int(limit))),
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
                        data = await self._spotify_to_thread_json(
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
                        data = await self._spotify_to_thread_json(
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
                    # de metadata recusa fields/market. Em erro transitório, tente
                    # os itens/próximo token/fallback público antes de desistir.
                    if exc.code not in {400, 403, 404} and exc.code not in _SPOTIFY_TRANSIENT_HTTP_CODES:
                        raise
                except Exception as exc:
                    last_error = exc
                    continue

                tracks: list[ApiTrackCandidate] = []
                offset = 0
                try:
                    while len(tracks) < limit:
                        page_limit = min(100, limit - len(tracks))
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
                    if exc.code not in {400, 403, 404} and exc.code not in _SPOTIFY_TRANSIENT_HTTP_CODES:
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
        data = await self._spotify_to_thread_json(f"https://api.spotify.com/v1/tracks/{quote(track_id)}?market={quote(self.spotify_market)}", headers=self._spotify_headers(token))
        return self._spotify_candidate(data, url=url)

    async def spotify_search(self, query: str, *, limit: int = 5) -> list[ApiTrackCandidate]:
        token = await self.spotify_token()
        if not token:
            return []
        params = urlencode({"q": query, "type": "track", "limit": max(1, min(10, int(limit))), "market": self.spotify_market})
        data = await self._spotify_to_thread_json(f"https://api.spotify.com/v1/search?{params}", headers=self._spotify_headers(token))
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
