from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import time
from typing import Any, Optional


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
    resolved_audio_max_abr: int = 0
    resolved_audio_abr: int = 0
    resolved_audio_ext: str = ""
    resolved_audio_codec: str = ""
    resolved_audio_format_id: str = ""
    resolved_audio_sample_rate: int = 0
    resolved_audio_channels: int = 0
    # Dados de runtime do Lavalink/Wavelink. Usado apenas em memória para que
    # resultados já resolvidos pelo node (seleção/texto ou link direto) sejam
    # tocados exatamente como retornaram, sem refazer busca por título genérico.
    lavalink_playable: Any = None
    lavalink_encoded: str = ""
    lavalink_query: str = ""
    lavalink_resolved: bool = False
    # Motivo curto exibido no painel quando o Lavalink cai para o player local.
    # Ex.: "Spotify", "SoundCloud", "YouTube". Vazio = playback local normal.
    fallback_reason: str = ""
    # Metadados oficiais preservados para exibição quando o áudio vem de um
    # mirror LavaSrc/SoundCloud. Ex.: Spotify track tocando via Lavalink deve
    # mostrar "Heaven Pierce Her - Castle Vein", não o título poluído do mirror.
    display_title: str = ""
    display_uploader: str = ""
    display_thumbnail: str = ""
    display_source: str = ""
    # Controle de recuperação quando o stream do Lavalink/SoundCloud termina
    # cedo demais. Mantido por faixa para evitar retry infinito em playlists.
    lavalink_recoveries: int = 0
    lavalink_last_position_ms: int = 0
    lavalink_last_played_seconds: float = 0.0

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
class PlaylistCursor:
    """Cursor leve para continuar uma coleção sem mantê-la inteira em RAM.

    ``next_offset`` aponta para a próxima posição lógica ainda não materializada.
    Providers podem reinterpretar esse deslocamento internamente, mas o restante
    do domínio não precisa conhecer paginação, HTML ou tokens específicos.
    """

    provider: str
    source_url: str
    title: str = ""
    resource_type: str = "playlist"
    resource_id: str = ""
    next_offset: int = 0
    total_tracks: Optional[int] = None
    exhausted: bool = False

    def advanced(self, count: int, *, exhausted: Optional[bool] = None) -> "PlaylistCursor":
        return PlaylistCursor(
            provider=self.provider,
            source_url=self.source_url,
            title=self.title,
            resource_type=self.resource_type,
            resource_id=self.resource_id,
            next_offset=max(0, int(self.next_offset) + max(0, int(count))),
            total_tracks=self.total_tracks,
            exhausted=self.exhausted if exhausted is None else bool(exhausted),
        )

    def public(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "source_url": self.source_url,
            "title": self.title,
            "resource_type": self.resource_type,
            "resource_id": self.resource_id,
            "next_offset": max(0, int(self.next_offset)),
            "total_tracks": self.total_tracks,
            "exhausted": bool(self.exhausted),
        }


@dataclass(slots=True)
class ExtractedBatch:
    tracks: list[MusicTrack]
    query: str
    is_playlist: bool = False
    playlist_title: str = ""
    truncated: bool = False
    playlist_cursor: Optional[PlaylistCursor] = None
