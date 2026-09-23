"""Modelos de estado do Music Agent executado no Phone Worker."""
from __future__ import annotations

import contextlib
import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class AgentTrack:
    title: str = "Música"
    requester_id: int = 0
    requester_name: str = ""
    query: str = ""
    webpage_url: str = ""
    original_url: str = ""
    stream_url: str = ""
    duration: float | None = None
    uploader: str = ""
    thumbnail: str = ""
    source: str = "worker-agent"
    transport_hint: str = ""
    audio_format_id: str = ""
    audio_ext: str = ""
    audio_codec: str = ""
    audio_abr: int = 0
    audio_sample_rate: int = 0
    audio_channels: int = 0
    start_offset_seconds: float = 0.0
    stream_recovery_attempts: int = 0
    stream_resolved_monotonic: float = 0.0
    virtual_playlist_cursor: dict[str, Any] = field(default_factory=dict)

    @property
    def is_virtual_playlist_marker(self) -> bool:
        return bool(self.virtual_playlist_cursor) or self.transport_hint == "playlist-cursor"

    def public(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "requester_id": self.requester_id,
            "requester_name": self.requester_name,
            "query": self.query,
            "webpage_url": self.webpage_url,
            "original_url": self.original_url,
            "duration": self.duration,
            "uploader": self.uploader,
            "thumbnail": self.thumbnail,
            "source": self.source,
            "transport_hint": self.transport_hint,
            "audio_format_id": self.audio_format_id,
            "audio_ext": self.audio_ext,
            "audio_codec": self.audio_codec,
            "audio_abr": self.audio_abr,
            "audio_sample_rate": self.audio_sample_rate,
            "audio_channels": self.audio_channels,
            "resolved_audio_format_id": self.audio_format_id,
            "resolved_audio_ext": self.audio_ext,
            "resolved_audio_codec": self.audio_codec,
            "resolved_audio_abr": self.audio_abr,
            "resolved_audio_sample_rate": self.audio_sample_rate,
            "resolved_audio_channels": self.audio_channels,
            "resolved_audio_max_abr": self.audio_abr,
            "start_offset_seconds": self.start_offset_seconds,
            "virtual_playlist_cursor": dict(self.virtual_playlist_cursor) if self.virtual_playlist_cursor else {},
        }


@dataclass
class GuildMusicState:
    guild_id: int
    voice_channel_id: int = 0
    text_channel_id: int = 0
    current: AgentTrack | None = None
    queue: list[AgentTrack] = field(default_factory=list)
    history: list[AgentTrack] = field(default_factory=list)
    status: str = "idle"
    paused: bool = False
    last_error: str = ""
    last_action: str = ""
    last_event: str = ""
    transport: str = ""
    preparing_since: float = 0.0
    playing_since: float = 0.0
    started_monotonic: float = 0.0
    paused_monotonic: float = 0.0
    last_audio_end_monotonic: float = 0.0
    updated_at: float = field(default_factory=time.time)
    player: Any = None
    volume_percent: int = 55
    normal_volume_percent: int = 55
    ducked: bool = False
    playback_token: int = 0
    shuffle: bool = False
    loop_mode: str = "off"
    # Shuffle virtual: embaralha cada janela materializada da playlist sem
    # carregar a coleção inteira em memória. O seed mantém o comportamento
    # determinístico durante a sessão e é descartado quando o cursor termina.
    virtual_shuffle_active: bool = False
    virtual_shuffle_seed: int = 0

    def _first_virtual_marker(self) -> tuple[int, AgentTrack] | None:
        for index, item in enumerate(self.queue):
            if item.is_virtual_playlist_marker:
                return index, item
        return None

    def _public_queue_preview(self) -> list[dict[str, Any]]:
        preview: list[dict[str, Any]] = []
        for item in self.queue:
            if item.is_virtual_playlist_marker:
                # Itens depois do marker pertencem logicamente ao fim da coleção
                # virtual; não os faça parecer anteriores ao restante da playlist.
                break
            preview.append(item.public())
            # A janela virtual padrão tem até 25 itens. Expor até 50 mantém o
            # controlador de fila coerente com toda a janela materializada sem
            # transformar o snapshot em uma cópia da playlist inteira.
            if len(preview) >= 50:
                break
        return preview

    def _public_virtual_playlist(self) -> dict[str, Any] | None:
        found = self._first_virtual_marker()
        if found is None:
            return None
        index, marker = found
        return {
            "cursor": dict(marker.virtual_playlist_cursor),
            "materialized_before": sum(1 for item in self.queue[:index] if not item.is_virtual_playlist_marker),
            "waiting": bool(index == 0 and self.current is None),
            "requester_id": marker.requester_id,
            "requester_name": marker.requester_name,
        }

    def state_revision(self) -> str:
        return f"{self.updated_at:.6f}:{self.playback_token}"

    def public(self) -> dict[str, Any]:
        player = self.player
        voice_connected = False
        playing = False
        position_ms = 0
        if player is not None:
            with contextlib.suppress(Exception):
                connected_attr = getattr(player, "connected", None)
                voice_connected = bool(connected_attr) if connected_attr is not None else voice_connected
            with contextlib.suppress(Exception):
                checker = getattr(player, "is_connected", None)
                if callable(checker):
                    voice_connected = bool(checker())
            with contextlib.suppress(Exception):
                checker = getattr(player, "is_playing", None)
                if callable(checker):
                    playing = bool(checker())
            with contextlib.suppress(Exception):
                playing = bool(playing or getattr(player, "playing", False))
            with contextlib.suppress(Exception):
                position_ms = int(float(getattr(player, "position", 0) or 0))
        if position_ms <= 0 and self.current is not None and self.status in {"playing", "paused"} and self.started_monotonic:
            with contextlib.suppress(Exception):
                base = max(0.0, float(getattr(self.current, "start_offset_seconds", 0.0) or 0.0))
                clock = float(self.paused_monotonic or time.monotonic()) if self.paused else time.monotonic()
                elapsed = max(0.0, clock - float(self.started_monotonic))
                position_ms = int(max(0.0, base + elapsed) * 1000)
        status_age = max(0.0, time.time() - float(self.updated_at or time.time()))
        return {
            "guild_id": self.guild_id,
            "voice_channel_id": self.voice_channel_id,
            "text_channel_id": self.text_channel_id,
            "status": self.status,
            "paused": self.paused,
            "last_error": self.last_error,
            "last_action": self.last_action,
            "last_event": self.last_event,
            "transport": self.transport,
            "preparing_since": self.preparing_since,
            "playing_since": self.playing_since,
            "status_age_seconds": round(status_age, 2),
            "voice_connected": voice_connected,
            "player_present": player is not None,
            "player_playing": playing,
            "position_ms": position_ms,
            "confirmed_playing": bool(
                self.status == "playing"
                and player is not None
                and voice_connected
                and (playing or position_ms > 0)
            ),
            "state_revision": self.state_revision(),
            "playback_token": int(self.playback_token),
            "updated_at": self.updated_at,
            "current": self.current.public() if self.current else None,
            "queue_size": sum(1 for item in self.queue if not item.is_virtual_playlist_marker),
            "history_size": len(self.history),
            "previous_available": bool(self.history),
            "volume_percent": self.volume_percent,
            "normal_volume_percent": self.normal_volume_percent,
            "ducked": self.ducked,
            "shuffle": bool(self.shuffle or self.virtual_shuffle_active),
            "loop_mode": self.loop_mode,
            "repeat": self.loop_mode,
            "queue": self._public_queue_preview(),
            "virtual_playlist": self._public_virtual_playlist(),
        }
