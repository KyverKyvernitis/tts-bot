from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import time
from typing import Optional


class LoopMode(str, Enum):
    OFF = "off"
    ONE = "one"
    ALL = "all"

    @property
    def label(self) -> str:
        if self is LoopMode.ONE:
            return "música atual"
        if self is LoopMode.ALL:
            return "queue"
        return "desligado"


@dataclass(slots=True)
class MusicTrack:
    title: str
    webpage_url: str
    requester_id: int
    requester_name: str = ""
    stream_url: str = ""
    duration: Optional[float] = None
    uploader: str = ""
    thumbnail: str = ""
    source: str = ""
    original_url: str = ""
    extractor: str = ""
    is_live: bool = False
    added_at_monotonic: float = field(default_factory=time.monotonic)
    resolved_at_monotonic: float = 0.0

    @property
    def display_url(self) -> str:
        return self.webpage_url or self.original_url or self.stream_url

    @property
    def duration_label(self) -> str:
        if self.is_live:
            return "ao vivo"
        if self.duration is None:
            return "desconhecida"
        total = max(0, int(self.duration))
        hours, rem = divmod(total, 3600)
        minutes, seconds = divmod(rem, 60)
        if hours:
            return f"{hours}:{minutes:02d}:{seconds:02d}"
        return f"{minutes}:{seconds:02d}"

    @property
    def short_title(self) -> str:
        clean = (self.title or "Música sem título").strip()
        return clean if len(clean) <= 90 else clean[:87].rstrip() + "..."


@dataclass(slots=True)
class ExtractedBatch:
    tracks: list[MusicTrack]
    query: str
    is_playlist: bool = False
    playlist_title: str = ""
    truncated: bool = False
