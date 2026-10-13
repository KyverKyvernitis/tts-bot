from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from ..nucleo.modelos import PlaylistCursor


class PlayInputKind(str, Enum):
    """Intenção do comando de reprodução antes de resolver metadata/áudio.

    URLs sempre são direct play. A distinção track/playlist só decide se o
    pipeline deve materializar uma faixa ou uma coleção; jamais abre o seletor
    de resultados usado por pesquisa textual.
    """

    SEARCH = "search"
    DIRECT_TRACK = "direct_track"
    DIRECT_PLAYLIST = "direct_playlist"


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
    playlist_cursor: PlaylistCursor | None = None
