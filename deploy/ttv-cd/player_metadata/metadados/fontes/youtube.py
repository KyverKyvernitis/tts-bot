from __future__ import annotations

import html
import re
from urllib.parse import parse_qs, urlencode, urlparse

from ..modelos import ApiTrackCandidate
from ..normalizacao import parse_iso8601_duration


class ProvedorYouTubeMixin:
    """Metadados e pesquisa via YouTube Data API; nunca reproduz áudio."""

    youtube_api_key: str

    async def _to_thread_json(self, url: str, **kwargs):  # pragma: no cover - contrato do mixin
        raise NotImplementedError

    def _youtube_video_id_from_url(self, url: str) -> str:
        parsed = urlparse(url or "")
        host = (parsed.netloc or "").lower()
        if "youtu.be" in host:
            return (parsed.path or "").strip("/").split("/", 1)[0]
        if "youtube" in host:
            if parsed.path.startswith("/watch"):
                return str((parse_qs(parsed.query).get("v") or [""])[0]).strip()
            for prefix in ("/shorts/", "/embed/", "/live/"):
                if parsed.path.startswith(prefix):
                    return parsed.path[len(prefix):].strip("/").split("/", 1)[0]
        return ""

    async def youtube_video_metadata(self, video_id_or_url: str) -> ApiTrackCandidate | None:
        if not self.youtube_api_key:
            return None
        video_id = self._youtube_video_id_from_url(video_id_or_url) or str(video_id_or_url or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{6,}", video_id):
            return None
        params = urlencode({
            "part": "snippet,contentDetails,status",
            "id": video_id,
            "key": self.youtube_api_key,
        })
        data = await self._to_thread_json(f"https://www.googleapis.com/youtube/v3/videos?{params}")
        first = next((item for item in (data.get("items") or []) if item), None)
        if not first:
            return None
        snippet = first.get("snippet") or {}
        thumbnails = snippet.get("thumbnails") or {}
        thumb = ((thumbnails.get("maxres") or thumbnails.get("high") or thumbnails.get("medium") or thumbnails.get("default") or {}).get("url") or "")
        candidate = ApiTrackCandidate(
            title=html.unescape(str(snippet.get("title") or "").strip()),
            artist=html.unescape(str(snippet.get("channelTitle") or "").strip()),
            thumbnail=thumb,
            webpage_url=f"https://www.youtube.com/watch?v={video_id}",
            source="YouTube API",
            provider="youtube",
            query=video_id,
            score=45,
        )
        candidate.duration = parse_iso8601_duration(str((first.get("contentDetails") or {}).get("duration") or ""))
        status = first.get("status") or {}
        if str(status.get("embeddable", "true")).lower() == "false":
            candidate.score -= 10
        return candidate

    async def youtube_search(self, query: str, *, limit: int = 3, include_details: bool = True) -> list[ApiTrackCandidate]:
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
            # Reduz payload e parsing no caminho de UI.
            "fields": "items(id/videoId,snippet(title,channelTitle,thumbnails/default/url,thumbnails/medium/url,thumbnails/high/url))",
        })
        data = await self._to_thread_json(f"https://www.googleapis.com/youtube/v3/search?{params}")
        items = data.get("items") or []
        video_ids: list[str] = []
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
        if include_details and video_ids:
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
