from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

from ..modelos import ApiTrackBatch, ApiTrackCandidate


class ProvedorSoundCloudMixin:
    """Metadados do SoundCloud; não contém player nem extração de áudio."""

    soundcloud_enabled: bool
    soundcloud_token: str
    soundcloud_client_id: str
    soundcloud_base_url: str

    async def _to_thread_json(self, url: str, **kwargs):  # pragma: no cover - contrato do mixin
        raise NotImplementedError

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
