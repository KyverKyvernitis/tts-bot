from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class UrlProfile:
    raw: str
    canonical: str
    host: str
    is_url: bool
    is_youtube: bool = False
    is_metadata_only: bool = False
    is_direct_audio: bool = False
    youtube_video_id: str = ""
    platform: str = ""
    resource_type: str = ""
    resource_id: str = ""


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
        from .normalizacao import compact_key

        return compact_key(f"{self.artist} {self.title}")


@dataclass(slots=True)
class ApiTrackBatch:
    tracks: list[ApiTrackCandidate]
    title: str = ""
    is_playlist: bool = False
    truncated: bool = False
    source: str = ""
