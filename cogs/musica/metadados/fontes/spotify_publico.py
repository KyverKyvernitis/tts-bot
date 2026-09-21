from __future__ import annotations

import html
import json
import logging
import re
from typing import Any
from urllib.parse import quote

from ..modelos import ApiTrackBatch, ApiTrackCandidate
from ..normalizacao import compact_key, normalize_text, parse_iso8601_duration

logger = logging.getLogger(__name__)

class SpotifyPublicoMixin:
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
        return self.rank_and_dedupe(enriched, query=" ".join(c.title for c in enriched[:5]), limit=limit) if enriched else []

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
                tracks = await self._spotify_enrich_candidates(
                    self.rank_and_dedupe(tracks, query=last_title or original_url, limit=limit),
                    limit=limit,
                )
                if tracks:
                    return ApiTrackBatch(
                        tracks=tracks,
                        title=last_title or (tracks[0].album if kind == "album" else "Spotify"),
                        is_playlist=kind in {"album", "playlist"} or len(tracks) > 1,
                        truncated=kind in {"album", "playlist"} and len(tracks) >= limit,
                        source="Spotify público",
                    )
        return None

    async def spotify_public_batch_from_url(self, url: str, *, limit: int = 25) -> ApiTrackBatch | None:
        """Resolve metadata Spotify somente pelas páginas públicas/embed.

        Este método é deliberadamente independente de ``api.spotify.com`` e
        dos fluxos OAuth. O Spotify continua sendo apenas fonte de identidade
        da música; áudio e busca tocável permanecem no pipeline do worker.
        """
        kind, item_id = self._spotify_resource(url)
        if not item_id or not self.spotify_public_fallback_enabled:
            return None
        limit = max(1, min(self.spotify_public_fallback_max_tracks, int(limit)))
        return await self._spotify_public_page_batch(kind, item_id, limit=limit, original_url=url)

