"""Modelos de estado do Music Agent executado no Phone Worker."""
from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from .efeitos import velocidade


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
    is_live: bool = False
    start_offset_seconds: float = 0.0
    stream_recovery_attempts: int = 0
    voice_recovery_attempts: int = 0
    stream_resolved_monotonic: float = 0.0
    virtual_playlist_cursor: dict[str, Any] = field(default_factory=dict)
    queue_item_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    attachment_ref: dict[str, int] = field(default_factory=dict)
    audio_stream_index: int = -1

    def __post_init__(self) -> None:
        if not str(self.queue_item_id or "").strip():
            self.queue_item_id = uuid.uuid4().hex

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
            "is_live": self.is_live,
            "resolved_audio_format_id": self.audio_format_id,
            "resolved_audio_ext": self.audio_ext,
            "resolved_audio_codec": self.audio_codec,
            "resolved_audio_abr": self.audio_abr,
            "resolved_audio_sample_rate": self.audio_sample_rate,
            "resolved_audio_channels": self.audio_channels,
            "resolved_audio_max_abr": self.audio_abr,
            "start_offset_seconds": self.start_offset_seconds,
            "virtual_playlist_cursor": dict(self.virtual_playlist_cursor) if self.virtual_playlist_cursor else {},
            "queue_item_id": self.queue_item_id,
            "attachment_ref": dict(self.attachment_ref),
            "audio_stream_index": self.audio_stream_index,
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
    # Só stop/limpar fila invalidam inserções cujo áudio ainda está em prova.
    queue_reset_generation: int = 0
    shuffle: bool = False
    loop_mode: str = "off"
    bassboost: bool = False
    nightcore: bool = False
    slowed_reverb: bool = False
    bassboost_level: int = 0
    nightcore_level: int = 0
    slowed_reverb_level: int = 0
    effects_revision: int = 0
    effects_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    # Shuffle virtual: embaralha cada janela materializada da playlist sem
    # carregar a coleção inteira em memória. O seed mantém o comportamento
    # determinístico durante a sessão e é descartado quando o cursor termina.
    virtual_shuffle_active: bool = False
    virtual_shuffle_seed: int = 0
    # Auditoria de startup. Estes campos são pequenos, persistem somente em
    # memória e ajudam a distinguir mídia inválida de falha do transporte de voz.
    play_attempt_sequence: int = 0
    consecutive_start_failures: int = 0
    last_error_category: str = ""
    last_error_phase: str = ""
    voice_runtime_recovery_pending: bool = False
    voice_runtime_recovery_attempts: int = 0
    voice_runtime_recovery_last_error: str = ""
    queue_invariant_repairs: int = 0
    # Por que o VoiceClient continua conectado. O worker é o dono físico da
    # call, então essa informação precisa viver junto do player real.
    #
    # - music_active: música/fila ainda mantém a sessão;
    # - music_idle_grace: a fila terminou e vale a janela musical de 120 s;
    # - tts_active: TTS direto está usando a sessão;
    # - voice_idle: a sessão já foi reutilizada pelo TTS e volta à política
    #   curta de presença humana (~2 s sem humanos);
    # - disconnected: não existe mais sessão de voz controlada pelo agente.
    voice_session_mode: str = "disconnected"
    voice_human_count: int = -1
    voice_presence_reason: str = ""
    # Motivo autoritativo da última saída de voz. Diferente de last_event:
    # este campo só muda quando a sessão de voz realmente é encerrada/perdida.
    last_disconnect_reason: str = ""
    last_disconnect_event: str = ""
    last_disconnect_at: float = 0.0
    last_disconnect_human_count: int = -1
    auto_leave_enabled: bool = True

    def _repair_current_queue_alias(self) -> int:
        """Remove somente a MESMA entrada de fila que também virou current.

        Repetições legítimas da mesma música possuem ``queue_item_id`` distintos
        e permanecem intactas. Este guard é uma última defesa contra races de
        promoção/espelhamento que poderiam tocar o mesmo item duas vezes.
        """
        current_id = str(getattr(self.current, "queue_item_id", "") or "")
        if not current_id or not self.queue:
            return 0
        before = len(self.queue)
        self.queue[:] = [
            item for item in self.queue
            if item.is_virtual_playlist_marker or str(getattr(item, "queue_item_id", "") or "") != current_id
        ]
        removed = before - len(self.queue)
        if removed > 0:
            self.queue_invariant_repairs += removed
            self.updated_at = time.time()
        return removed

    @staticmethod
    def _virtual_marker_remaining(marker: AgentTrack) -> int | None:
        cursor = marker.virtual_playlist_cursor if isinstance(marker.virtual_playlist_cursor, dict) else {}
        try:
            start = max(0, int(cursor.get("next_offset") or 0))
        except Exception:
            start = 0
        raw_end = cursor.get("block_end_offset")
        if raw_end in (None, ""):
            raw_end = cursor.get("total_tracks")
        if raw_end in (None, ""):
            return None
        try:
            return max(0, int(raw_end) - start)
        except Exception:
            return None

    def _virtual_markers(self) -> list[tuple[int, AgentTrack]]:
        return [(index, item) for index, item in enumerate(self.queue) if item.is_virtual_playlist_marker]

    def _first_virtual_marker(self) -> tuple[int, AgentTrack] | None:
        markers = self._virtual_markers()
        return markers[0] if markers else None

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

    def _public_virtual_playlists(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        playable_before = 0
        for physical_index, item in enumerate(self.queue):
            if not item.is_virtual_playlist_marker:
                playable_before += 1
                continue
            cursor = dict(item.virtual_playlist_cursor)
            result.append({
                "cursor": cursor,
                "materialized_before": playable_before,
                "physical_index": physical_index,
                "waiting": bool(playable_before == 0 and self.current is None),
                "requester_id": item.requester_id,
                "requester_name": item.requester_name,
                "remaining": self._virtual_marker_remaining(item),
            })
        return result

    def _public_virtual_playlist(self) -> dict[str, Any] | None:
        values = self._public_virtual_playlists()
        return values[0] if values else None

    def _public_queue_layout(self) -> list[dict[str, Any]]:
        layout: list[dict[str, Any]] = []
        for item in self.queue:
            if item.is_virtual_playlist_marker:
                layout.append({
                    "kind": "virtual",
                    "cursor": dict(item.virtual_playlist_cursor),
                    "requester_id": item.requester_id,
                    "requester_name": item.requester_name,
                    "remaining": self._virtual_marker_remaining(item),
                })
            else:
                layout.append({"kind": "track", "track": item.public()})
        return layout

    def _logical_queue_size(self) -> int:
        total = sum(1 for item in self.queue if not item.is_virtual_playlist_marker)
        for _index, marker in self._virtual_markers():
            remaining = self._virtual_marker_remaining(marker)
            if remaining is not None:
                total += remaining
        return max(0, total)

    def state_revision(self) -> str:
        return f"{self.updated_at:.6f}:{self.playback_token}"

    def effect_level(self, effect: str) -> int:
        flag = bool(getattr(self, effect, False))
        raw = getattr(self, f"{effect}_level", 0)
        try:
            level = int(raw or 0)
        except (TypeError, ValueError):
            level = 0
        if level <= 0 and flag:
            level = 1
        max_level = 6 if effect == "bassboost" else 3
        return max(0, min(max_level, level))

    def effect_signature(self) -> tuple[int, int, int]:
        return (self.effect_level("bassboost"), self.effect_level("nightcore"),
                self.effect_level("slowed_reverb"))

    def apply_effect_signature(self, levels: tuple[int, int, int]) -> None:
        bass = max(0, min(6, int(levels[0] or 0)))
        night = max(0, min(3, int(levels[1] or 0)))
        slow = max(0, min(3, int(levels[2] or 0)))
        self.bassboost_level, self.nightcore_level, self.slowed_reverb_level = bass, night, slow
        self.bassboost, self.nightcore, self.slowed_reverb = bass > 0, night > 0, slow > 0

    @property
    def playback_speed(self) -> float:
        return velocidade(
            nightcore=self.nightcore, slowed_reverb=self.slowed_reverb,
            nightcore_level=self.effect_level("nightcore"),
            slowed_reverb_level=self.effect_level("slowed_reverb"),
            is_live=bool(self.current and self.current.is_live),
        )

    def source_position_seconds(self, *, now: float | None = None) -> float:
        if self.current is None:
            return 0.0
        base = max(0.0, float(self.current.start_offset_seconds or 0.0))
        if self.status not in {"playing", "paused"} or not self.started_monotonic:
            return base
        clock = float(now if now is not None else time.monotonic())
        if self.paused and self.paused_monotonic:
            clock = self.paused_monotonic
        position = base + max(0.0, clock - self.started_monotonic) * self.playback_speed
        if self.current.duration is not None:
            position = min(position, max(0.0, float(self.current.duration)))
        return position

    def public(self) -> dict[str, Any]:
        self._repair_current_queue_alias()
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
        if self.current is not None and self.transport == "direct":
            position_ms = int(self.source_position_seconds() * 1000)
        elif position_ms <= 0 and self.current is not None and self.status in {"playing", "paused"}:
            position_ms = int(self.source_position_seconds() * 1000)
        status_age = max(0.0, time.time() - float(self.updated_at or time.time()))
        return {
            "guild_id": self.guild_id,
            "voice_channel_id": self.voice_channel_id,
            "text_channel_id": self.text_channel_id,
            "status": self.status,
            "paused": self.paused,
            "last_error": self.last_error,
            "last_error_category": self.last_error_category,
            "last_error_phase": self.last_error_phase,
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
            "play_attempt_sequence": int(self.play_attempt_sequence),
            "consecutive_start_failures": int(self.consecutive_start_failures),
            "voice_runtime_recovery_pending": bool(self.voice_runtime_recovery_pending),
            "voice_runtime_recovery_attempts": int(self.voice_runtime_recovery_attempts),
            "voice_runtime_recovery_last_error": str(self.voice_runtime_recovery_last_error or ""),
            "queue_invariant_repairs": int(self.queue_invariant_repairs),
            "voice_session_mode": str(self.voice_session_mode or "disconnected"),
            "voice_human_count": int(self.voice_human_count),
            "voice_presence_reason": str(self.voice_presence_reason or ""),
            "last_disconnect_reason": str(self.last_disconnect_reason or ""),
            "last_disconnect_event": str(self.last_disconnect_event or ""),
            "last_disconnect_at": float(self.last_disconnect_at or 0.0),
            "last_disconnect_human_count": int(self.last_disconnect_human_count),
            "auto_leave_enabled": bool(self.auto_leave_enabled),
            "updated_at": self.updated_at,
            "current": self.current.public() if self.current else None,
            "queue_size": sum(1 for item in self.queue if not item.is_virtual_playlist_marker),
            "logical_queue_size": self._logical_queue_size(),
            "history_size": len(self.history),
            "previous_available": bool(self.history),
            "volume_percent": self.volume_percent,
            "bassboost": self.effect_level("bassboost") > 0,
            "nightcore": self.effect_level("nightcore") > 0,
            "slowed_reverb": self.effect_level("slowed_reverb") > 0,
            "bassboost_level": self.effect_level("bassboost"),
            "nightcore_level": self.effect_level("nightcore"),
            "slowed_reverb_level": self.effect_level("slowed_reverb"),
            "speed_multiplier": self.playback_speed,
            "effects_revision": self.effects_revision,
            "normal_volume_percent": self.normal_volume_percent,
            "ducked": self.ducked,
            "shuffle": bool(self.shuffle or self.virtual_shuffle_active),
            "loop_mode": self.loop_mode,
            "repeat": self.loop_mode,
            "queue": self._public_queue_preview(),
            "queue_layout": self._public_queue_layout(),
            "virtual_playlist": self._public_virtual_playlist(),
            "virtual_playlists": self._public_virtual_playlists(),
        }
