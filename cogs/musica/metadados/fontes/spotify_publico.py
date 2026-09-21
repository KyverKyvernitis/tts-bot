from __future__ import annotations

import html
import json
import logging
import re
from typing import Any
from urllib.parse import quote

from .spotify_publico_parser import (
    html_meta,
    html_title,
    json_script_blobs,
    parse_embed_document,
)

from ..modelos import ApiTrackBatch, ApiTrackCandidate
from ...nucleo.modelos import PlaylistCursor
from ..normalizacao import compact_key, normalize_text, parse_iso8601_duration

logger = logging.getLogger(__name__)

class SpotifyPublicoMixin:
    def _spotify_public_urls(self, kind: str, item_id: str) -> list[str]:
        item_id = quote(item_id)
        # O embed é menor e server-renderiza a lista de faixas; priorizá-lo reduz
        # bytes/latência e evita depender do bundle completo do Web Player.
        if kind == "track":
            return [f"https://open.spotify.com/embed/track/{item_id}", f"https://open.spotify.com/track/{item_id}"]
        if kind == "album":
            return [f"https://open.spotify.com/embed/album/{item_id}", f"https://open.spotify.com/album/{item_id}"]
        if kind == "playlist":
            return [f"https://open.spotify.com/embed/playlist/{item_id}", f"https://open.spotify.com/playlist/{item_id}"]
        return []

    async def _spotify_public_oembed(self, url: str) -> dict[str, str]:
        """Metadata básica oficial e pública, sem OAuth/Web API.

        oEmbed é usado só como fonte leve de título/capa. Artista, duração e
        faixas de playlists continuam vindo do HTML público do embed.
        """
        try:
            data = await self._to_thread_json(
                "https://open.spotify.com/oembed?url=" + quote(url, safe=""),
                headers={"Accept": "application/json"},
            )
        except Exception:
            logger.debug("[music-api] Spotify oEmbed público falhou | url=%s", url, exc_info=True)
            return {}
        if not isinstance(data, dict):
            return {}
        return {
            "title": html.unescape(str(data.get("title") or "")).strip(),
            "thumbnail": str(data.get("thumbnail_url") or "").strip(),
        }

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
            for key in ("images", "sources", "coverArt", "albumOfTrack", "album"):
                found = self._spotify_public_images(value.get(key))
                if found:
                    return found
        if isinstance(value, list):
            for item in value:
                found = self._spotify_public_images(item)
                if found:
                    return found
        return ""

    def _spotify_public_text(self, value: Any, *, preferred_keys: tuple[str, ...] = ("name", "title", "text"), depth: int = 0) -> str:
        """Extrai texto útil de payloads públicos/GraphQL do Spotify.

        O web player muda bastante o formato. Em alguns payloads `name` vem
        como string simples; em outros vem dentro de `profile.name`,
        `data.name`, `transformedLabel`, etc. Mantemos a extração conservadora
        para não confundir ids/URLs com nomes de música/artista.
        """
        if value is None or depth > 7:
            return ""
        if isinstance(value, str):
            clean = html.unescape(value).strip()
            if not clean:
                return ""
            low = clean.lower()
            if low in {"spotify", "track", "playlist", "album", "music"}:
                return ""
            if low.startswith(("spotify:", "http://", "https://")):
                return ""
            return clean
        if isinstance(value, (int, float, bool)):
            return ""
        if isinstance(value, list):
            for item in value:
                found = self._spotify_public_text(item, preferred_keys=preferred_keys, depth=depth + 1)
                if found:
                    return found
            return ""
        if isinstance(value, dict):
            for key in preferred_keys:
                if key in value:
                    found = self._spotify_public_text(value.get(key), preferred_keys=preferred_keys, depth=depth + 1)
                    if found:
                        return found
            for key in ("profile", "data", "entity", "item", "content", "label", "transformedLabel"):
                if key in value:
                    found = self._spotify_public_text(value.get(key), preferred_keys=preferred_keys, depth=depth + 1)
                    if found:
                        return found
        return ""

    def _spotify_public_duration_any(self, data: dict[str, Any]) -> float | None:
        for key in (
            "duration_ms",
            "durationMs",
            "duration",
            "trackDuration",
            "totalMilliseconds",
            "milliseconds",
            "length",
        ):
            if key in data:
                parsed = self._spotify_public_duration(data.get(key))
                if parsed:
                    return parsed
        for key in ("duration", "trackDuration", "audio", "track"):
            nested = data.get(key)
            if isinstance(nested, dict):
                parsed = self._spotify_public_duration_any(nested)
                if parsed:
                    return parsed
        return None

    def _spotify_public_external_url(self, data: dict[str, Any], *, fallback_url: str = "") -> str:
        for key in ("external_urls", "externalUrls", "sharingInfo", "shareUrl", "uri"):
            value = data.get(key)
            if isinstance(value, str):
                if value.startswith("http"):
                    return value
                if value.startswith("spotify:track:"):
                    return f"https://open.spotify.com/track/{value.split(':')[-1]}"
            elif isinstance(value, dict):
                for sub_key in ("spotify", "url", "shareUrl"):
                    raw = str(value.get(sub_key) or "").strip()
                    if raw.startswith("http"):
                        return raw
        item_id = str(data.get("id") or data.get("trackId") or data.get("gid") or "").strip()
        uri = str(data.get("uri") or data.get("playableUri") or "")
        if uri.startswith("spotify:track:"):
            item_id = uri.split(":")[-1]
        if item_id and re.fullmatch(r"[A-Za-z0-9]{16,32}", item_id):
            return f"https://open.spotify.com/track/{item_id}"
        return fallback_url

    def _spotify_track_id_from_candidate(self, candidate: ApiTrackCandidate) -> str:
        for raw in (candidate.webpage_url, candidate.extra.get("uri", "") if isinstance(candidate.extra, dict) else ""):
            text = str(raw or "")
            if "open.spotify.com/track/" in text:
                kind, item_id = self._spotify_resource(text)
                if kind == "track" and item_id:
                    return item_id
            if text.startswith("spotify:track:"):
                return text.split(":")[-1]
        return ""

    def _spotify_candidate_metadata_ok(self, candidate: ApiTrackCandidate) -> bool:
        return bool((candidate.title or "").strip() and (candidate.artist or "").strip())

    async def _spotify_enrich_candidate(self, candidate: ApiTrackCandidate) -> ApiTrackCandidate:
        """Mantém o candidato público sem consultar a Spotify Web API.

        Antes este passo tentava ``/v1/tracks`` e ``/v1/search`` para completar
        metadata incompleta. O fluxo de reprodução não pode depender de OAuth,
        Client Credentials, refresh token ou plano de API. A segurança da
        resolução continua garantida por ``_spotify_enrich_candidates``, que só
        aceita itens públicos com título e artista.
        """
        return candidate

    async def _spotify_enrich_candidates(self, candidates: list[ApiTrackCandidate], *, limit: int) -> list[ApiTrackCandidate]:
        if not candidates:
            return []
        enriched: list[ApiTrackCandidate] = []
        # Serial para não abrir rajada de requests quando playlist pública vier grande.
        for candidate in candidates[:limit]:
            item = await self._spotify_enrich_candidate(candidate)
            if self._spotify_candidate_metadata_ok(item):
                enriched.append(item)
            else:
                logger.debug(
                    "[music-api] faixa Spotify pública ignorada por metadata fraca | title=%r artist=%r url=%r",
                    item.title,
                    item.artist,
                    item.webpage_url,
                )
        # Não ranqueia uma coleção: score é heurística de busca e poderia
        # reordenar uma playlist. O parser público já preserva a ordem da fonte.
        return enriched[:limit]

    def _spotify_public_artist_names(self, data: dict[str, Any]) -> str:
        def names_from(value: Any, depth: int = 0) -> list[str]:
            if value is None or depth > 8:
                return []
            names: list[str] = []
            if isinstance(value, str):
                clean = html.unescape(value).strip()
                if clean and clean.lower() not in {"spotify", "various artists"} and not clean.startswith(("spotify:", "http")):
                    names.append(clean)
            elif isinstance(value, dict):
                # Formatos modernos do web player: {items:[{profile:{name}}]},
                # {profile:{name}}, {name}, {displayName}, etc.
                for key in ("name", "displayName", "title", "text"):
                    clean = self._spotify_public_text(value.get(key))
                    if clean:
                        names.append(clean)
                        break
                for key in ("profile", "data", "artist", "artists", "items", "nodes", "edges"):
                    nested = value.get(key)
                    if nested is not None:
                        names.extend(names_from(nested, depth + 1))
            elif isinstance(value, list):
                for item in value:
                    names.extend(names_from(item, depth + 1))
            return names

        for key in ("artists", "artist", "byArtist", "firstArtist", "creator", "authors", "owner"):
            result = names_from(data.get(key))
            if result:
                # Remove duplicatas mantendo a ordem.
                seen: set[str] = set()
                unique: list[str] = []
                for name in result:
                    marker = normalize_text(name)
                    if marker and marker not in seen and marker not in {"spotify", "track", "playlist"}:
                        seen.add(marker)
                        unique.append(name)
                if unique:
                    return ", ".join(unique[:4])
        return ""

    def _spotify_public_candidate_from_obj(self, data: dict[str, Any], *, url: str = "") -> ApiTrackCandidate | None:
        if not isinstance(data, dict):
            return None
        if isinstance(data.get("track"), dict):
            nested = self._spotify_public_candidate_from_obj(data["track"], url=url)
            if nested:
                return nested

        uri = str(data.get("uri") or data.get("playableUri") or data.get("playUri") or "")
        item_type = str(data.get("type") or data.get("__typename") or data.get("contentType") or data.get("typename") or "").lower()
        has_artistish = bool(
            data.get("artists")
            or data.get("artist")
            or data.get("byArtist")
            or data.get("firstArtist")
            or (isinstance(data.get("albumOfTrack"), dict) and data.get("albumOfTrack"))
        )
        duration = self._spotify_public_duration_any(data)
        is_trackish = (
            uri.startswith("spotify:track:")
            or item_type in {"track", "trackresponsewrapper", "playlisttrack", "trackentity", "trackv2"}
            or (bool(duration) and has_artistish)
        )
        if not is_trackish:
            return None

        title = self._spotify_public_text(data.get("name") or data.get("title") or data.get("trackName"))
        if not title:
            title = self._spotify_public_text(data, preferred_keys=("name", "title", "trackName", "text"))
        if not title:
            return None

        artist = self._spotify_public_artist_names(data)
        album_source = data.get("album") if isinstance(data.get("album"), dict) else data.get("albumOfTrack") if isinstance(data.get("albumOfTrack"), dict) else {}
        album = self._spotify_public_text(album_source, preferred_keys=("name", "title")) if isinstance(album_source, dict) else ""
        images = data.get("images") or data.get("image") or data.get("coverArt") or data.get("albumOfTrack") or album_source
        thumbnail = self._spotify_public_images(images)
        webpage_url = self._spotify_public_external_url(data, fallback_url=url)
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
            extra={"uri": uri} if uri else {},
        )

    def _spotify_public_candidates_from_json(
        self,
        data: Any,
        *,
        url: str,
        limit: int,
        offset: int = 0,
    ) -> list[ApiTrackCandidate]:
        results: list[ApiTrackCandidate] = []
        seen: set[str] = set()
        skipped = 0
        offset = max(0, int(offset))

        def add(candidate: ApiTrackCandidate | None) -> None:
            nonlocal skipped
            if not candidate:
                return
            key = compact_key(f"{candidate.artist} {candidate.title}") or candidate.webpage_url.lower()
            if not key or key in seen:
                return
            seen.add(key)
            if skipped < offset:
                skipped += 1
                return
            if len(results) < limit:
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

    async def _spotify_public_page_batch(
        self,
        kind: str,
        item_id: str,
        *,
        limit: int,
        original_url: str,
        offset: int = 0,
    ) -> ApiTrackBatch | None:
        if not self.spotify_public_fallback_enabled:
            return None
        limit = max(1, min(self.spotify_public_fallback_max_tracks, int(limit)))
        offset = max(0, int(offset))
        # Um item extra permite distinguir "janela cheia" de "fim conhecido"
        # sem manter o restante da playlist em memória.
        probe_limit = min(self.spotify_public_fallback_max_tracks, limit + 1)
        oembed = await self._spotify_public_oembed(original_url)
        last_title = str(oembed.get("title") or "").strip()
        fallback_thumbnail = str(oembed.get("thumbnail") or "").strip()

        for url in self._spotify_public_urls(kind, item_id):
            try:
                content = await self._to_thread_text(url, max_bytes=6_000_000)
            except Exception:
                logger.debug("[music-api] fallback público Spotify HTML falhou | url=%s", url, exc_info=True)
                continue

            # Cabeçalho/metatags são fallbacks baratos e independentes do
            # framework JS que o Spotify estiver usando no Web Player.
            page_title = html_title(content)
            if page_title:
                last_title = last_title or page_title
            page_thumbnail = html_meta(content, "og:image", "twitter:image")
            if page_thumbnail:
                fallback_thumbnail = fallback_thumbnail or page_thumbnail

            tracks: list[ApiTrackCandidate] = []
            json_blobs = json_script_blobs(content)

            # Fallback genérico legado para objetos JSON pequenos embutidos em
            # payloads RSC. Não executa JS; só aceita JSON válido.
            for raw_match in re.finditer(r'\{[^{}]{0,2500}spotify:track:[^{}]{0,2500}\}', content):
                raw = html.unescape(raw_match.group(0))
                try:
                    json_blobs.append(json.loads(raw))
                except Exception:
                    pass

            if json_blobs:
                tracks.extend(
                    self._spotify_public_candidates_from_json(
                        json_blobs,
                        url="",
                        limit=probe_limit,
                        offset=offset,
                    )
                )

            # O embed também entrega HTML server-rendered. Isso é crucial para
            # playlists quando o payload interno muda ou deixa de expor JSON.
            document = parse_embed_document(content, limit=probe_limit, offset=offset)
            if document.title:
                last_title = last_title or document.title
            if document.thumbnail:
                fallback_thumbnail = fallback_thumbnail or document.thumbnail

            if not tracks and document.rows:
                tracks = [
                    ApiTrackCandidate(
                        title=row.title,
                        artist=row.artist,
                        duration=row.duration,
                        thumbnail=fallback_thumbnail,
                        webpage_url="",
                        source="Spotify público",
                        provider="spotify",
                        query=" ".join(part for part in (row.artist, row.title, "official audio") if part),
                        score=30,
                    )
                    for row in document.rows[:probe_limit]
                ]

            # Track individual no embed usa heading principal/subtítulo em vez
            # de linhas h3/h4. oEmbed completa título/capa quando disponível.
            if kind == "track" and not tracks:
                track_title = (document.title or last_title).strip()
                artist = (document.subtitle or "").strip()
                if track_title and artist:
                    tracks = [
                        ApiTrackCandidate(
                            title=track_title,
                            artist=artist,
                            duration=None,
                            thumbnail=fallback_thumbnail,
                            webpage_url=original_url,
                            source="Spotify público",
                            provider="spotify",
                            query=f"{artist} {track_title} official audio".strip(),
                            score=30,
                        )
                    ]

            if tracks:
                # Completa capa do lote sem fazer request por faixa.
                if fallback_thumbnail:
                    for candidate in tracks:
                        if not candidate.thumbnail:
                            candidate.thumbnail = fallback_thumbnail
                tracks = await self._spotify_enrich_candidates(tracks, limit=probe_limit)
                if tracks:
                    has_more_in_document = kind in {"album", "playlist"} and len(tracks) > limit
                    window = tracks[:limit]
                    playlist_title = last_title or (window[0].album if kind == "album" else window[0].title if kind == "track" else "Spotify")
                    cursor = None
                    if kind in {"album", "playlist"}:
                        # O embed público costuma expor uma janela server-rendered.
                        # Mesmo quando não vemos o item +1, 25 linhas podem ser só
                        # o primeiro recorte; nesse caso mantemos continuação aberta
                        # e a etapa lazy confirma o fim ao pedir o próximo offset.
                        maybe_embed_window = len(window) >= 25
                        cursor = PlaylistCursor(
                            provider="spotify_public",
                            source_url=original_url,
                            title=playlist_title,
                            resource_type=kind,
                            resource_id=item_id,
                            next_offset=offset + len(window),
                            total_tracks=None,
                            exhausted=not (has_more_in_document or maybe_embed_window),
                        )
                    return ApiTrackBatch(
                        tracks=window,
                        title=playlist_title,
                        is_playlist=kind in {"album", "playlist"} or len(window) > 1,
                        truncated=bool(cursor and not cursor.exhausted),
                        source="Spotify público",
                        playlist_cursor=cursor,
                    )
        return None

    async def spotify_public_batch_from_url(self, url: str, *, limit: int = 25, offset: int = 0) -> ApiTrackBatch | None:
        """Resolve metadata Spotify somente pelas páginas públicas/embed.

        Este método é deliberadamente independente de ``api.spotify.com`` e
        dos fluxos OAuth. O Spotify continua sendo apenas fonte de identidade
        da música; áudio e busca tocável permanecem no pipeline do worker.
        """
        kind, item_id = self._spotify_resource(url)
        if (
            kind not in {"track", "album", "playlist"}
            or not item_id
            or not re.fullmatch(r"[A-Za-z0-9]{16,32}", item_id)
            or not self.spotify_public_fallback_enabled
        ):
            return None
        limit = max(1, min(self.spotify_public_fallback_max_tracks, int(limit)))
        offset = max(0, int(offset))
        if offset:
            return await self._spotify_public_page_batch(
                kind,
                item_id,
                limit=limit,
                original_url=url,
                offset=offset,
            )
        # Mantém compatibilidade com wrappers/testes/overrides antigos do
        # resolver público que ainda expõem a assinatura sem ``offset``.
        return await self._spotify_public_page_batch(
            kind,
            item_id,
            limit=limit,
            original_url=url,
        )

