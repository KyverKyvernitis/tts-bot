from __future__ import annotations

from typing import Any
from urllib.parse import quote, urlencode, urlparse

from ..modelos import ApiTrackBatch, ApiTrackCandidate


class ProvedorDeezerMixin:
    """Metadados oficiais do Deezer, sem qualquer caminho de reprodução."""

    deezer_enabled: bool

    async def _to_thread_json(self, url: str, **kwargs):  # pragma: no cover - contrato do mixin
        raise NotImplementedError

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
