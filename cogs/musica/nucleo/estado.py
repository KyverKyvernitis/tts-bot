from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Optional

from cogs.musica import configuracao as config

from .modelos import LoopMode, MusicTrack

MUSIC_DEFAULT_VOLUME = max(0.0, min(2.0, float(getattr(config, "MUSIC_DEFAULT_VOLUME", 0.55))))
MUSIC_QUEUE_MAXSIZE = min(100, max(1, int(getattr(config, "MUSIC_QUEUE_MAXSIZE", 100))))
MUSIC_HISTORY_MAXSIZE = max(5, int(getattr(config, "MUSIC_HISTORY_MAXSIZE", 25)))
MUSIC_CONTROL_VOTE_SECONDS = max(10.0, float(getattr(config, "MUSIC_CONTROL_VOTE_SECONDS", 45)))
MUSIC_HIGH_QUALITY_MAX_ABR = max(96, int(getattr(config, "MUSIC_HIGH_QUALITY_MAX_ABR", 256)))


@dataclass(slots=True)
class ControlVote:
    action: str
    voters: set[int] = field(default_factory=set)
    started_at: float = field(default_factory=time.monotonic)

    def expired(self) -> bool:
        return (time.monotonic() - self.started_at) > MUSIC_CONTROL_VOTE_SECONDS


@dataclass
class MusicGuildState:
    """Estado lógico da música para uma guild.

    O estado pertence ao domínio da música, não ao player legado. Campos de
    compatibilidade com o player local/Lavalink permanecem temporariamente como
    ``Any`` enquanto a migração Worker-only elimina esses caminhos.
    """

    queue: asyncio.Queue[MusicTrack] = field(default_factory=lambda: asyncio.Queue(maxsize=MUSIC_QUEUE_MAXSIZE))
    worker_task: Optional[asyncio.Task] = None
    current: Optional[MusicTrack] = None
    last_text_channel_id: Optional[int] = None
    last_voice_channel_id: Optional[int] = None
    volume: float = MUSIC_DEFAULT_VOLUME
    loop_mode: LoopMode = LoopMode.OFF
    shuffle: bool = False
    stop_requested: bool = False
    paused: bool = False
    current_source: Any = None
    current_backend: str = "local"
    current_lavalink_player: Any = None
    current_lavalink_playable: Any = None
    current_lavalink_node_label: str = ""
    current_lavalink_node_name: str = ""
    current_resolve_task: Optional[asyncio.Task] = None
    next_resolve_task: Optional[asyncio.Task] = None
    next_resolve_key: str = ""
    next_resolve_active_key: str = ""
    current_status: str = "idle"
    current_status_detail: str = ""
    current_status_changed_at: float = field(default_factory=time.monotonic)
    skip_requested: bool = False
    skip_transition_active: bool = False
    skip_history_suppressed_once: bool = False
    now_message: Any = None
    panel_track_key: Optional[str] = None
    history: deque[MusicTrack] = field(default_factory=lambda: deque(maxlen=MUSIC_HISTORY_MAXSIZE))
    forward_queue: deque[MusicTrack] = field(default_factory=lambda: deque(maxlen=MUSIC_QUEUE_MAXSIZE))
    voice_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    panel_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    music_owns_voice: bool = False
    tts_voice_touched: bool = False
    last_tts_activity_at: float = 0.0
    lavalink_tts_until: float = 0.0
    lavalink_resume_grace_until: float = 0.0
    tts_session_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    tts_session_active_until: float = 0.0
    tts_session_last_error: str = ""
    tts_session_last_cleanup_at: float = 0.0
    tts_lavalink_failures: int = 0
    tts_lavalink_local_fallback_until: float = 0.0
    music_session_active: bool = False
    music_idle_disconnect_task: Optional[asyncio.Task] = None
    music_afk_expired: bool = False
    music_operation_generation: int = 0
    control_votes: dict[str, ControlVote] = field(default_factory=dict)
    control_vote_cleanup_tasks: dict[str, asyncio.Task] = field(default_factory=dict)
    volume_loaded: bool = False
    idle_reason: str = "idle"
    idle_actor_id: Optional[int] = None
    idle_actor_name: str = ""
    idle_channel_name: str = ""
    internal_voice_disconnect_until: float = 0.0
    lavalink_transition_until: float = 0.0
    last_lavalink_error: str = ""
    panel_update_task: Optional[asyncio.Task] = None
    panel_update_create: bool = True
    panel_update_requested_at: float = 0.0
    panel_controls_invalid_at: float = 0.0
    panel_controls_invalidation_task: Optional[asyncio.Task] = None
    current_started_at_monotonic: float = 0.0
    current_start_offset_seconds: float = 0.0
    next_local_start_offset_seconds: float = 0.0
    auto_bitrate_channel_id: Optional[int] = None
    auto_bitrate_original: Optional[int] = None
    auto_bitrate_boosted: Optional[int] = None
    current_quality_label: str = "Alta"
    current_quality_kbps: int = MUSIC_HIGH_QUALITY_MAX_ABR
    voice_status_channel_id: Optional[int] = None
    voice_status_had_original: bool = False
    voice_status_original_known: bool = False
    voice_status_original: str = ""
    voice_status_owned: bool = False
    voice_status_last_bot: str = ""
    voice_status_update_task: Optional[asyncio.Task] = None
    voice_status_last_update_at: float = 0.0
    voice_status_last_track_key: str = ""
    agent_started_track_key: str = ""
    agent_started_playback_token: int = -1
    panel_last_repost_key: str = ""
    panel_last_repost_at: float = 0.0
    agent_last_idle_event: str = ""
    agent_remote_queue_size: int = 0
    agent_remote_history_size: int = 0
    voice_status_last_applied_key: str = ""
    voice_status_last_sync_request_key: str = ""
    voice_status_last_sync_request_at: float = 0.0
    voice_status_last_restore_key: str = ""
    voice_status_last_restore_at: float = 0.0
    voice_status_force_task: Optional[asyncio.Task] = None
    voice_status_generation: int = 0
    voice_status_external_override: bool = False
    voice_status_external_status: str = ""
    voice_status_gateway_status_known: bool = False
    voice_status_gateway_status: str = ""
    voice_status_gateway_event_at: float = 0.0
    voice_status_expected_status: str = ""
    voice_status_expected_until: float = 0.0
    voice_status_last_write_at: float = 0.0
    voice_status_retry_count: int = 0
    voice_status_pause_position_seconds: float = -1.0
    voice_status_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    agent_playback_token: int = -1
    agent_voice_recovery_pending: bool = False
    agent_voice_recovery_attempts: int = 0
    agent_voice_recovery_last_error: str = ""
    agent_voice_session_mode: str = ""
    agent_voice_presence_reason: str = ""
    agent_last_disconnect_reason: str = ""
    agent_last_disconnect_event: str = ""
    agent_last_disconnect_at: float = 0.0
    agent_last_disconnect_human_count: int = -1
    agent_monitor_task: Optional[asyncio.Task] = None
    agent_monitor_failures: int = 0
    agent_monitor_last_error: str = ""
    agent_monitor_reconnecting_since: float = 0.0
    agent_monitor_recoveries: int = 0
    agent_deferred_command_status: str = ""
    agent_deferred_command_attempts: int = 0
    agent_deferred_command_error: str = ""
    agent_side_effect_task: Optional[asyncio.Task] = None
    virtual_playlist_refill_task: Optional[asyncio.Task] = None
    virtual_playlist_refill_cursor_key: str = ""
    virtual_playlist_refill_failures: int = 0
    virtual_playlist_refill_retry_not_before: float = 0.0
    # Espelho leve da coleção virtual autoritativa do Phone Worker. Não guarda
    # a playlist inteira: apenas cursor/contagem suficientes para a UI explicar
    # que ainda existem faixas sendo carregadas sob demanda.
    agent_virtual_playlist: dict[str, Any] = field(default_factory=dict)
    # Cache exclusivamente visual de páginas da coleção virtual. O player
    # continua materializando somente a janela curta no Phone Worker; páginas
    # distantes são metadata sob demanda para o controlador de fila.
    agent_virtual_playlist_pages: dict[int, list[MusicTrack]] = field(default_factory=dict)
    agent_virtual_playlist_browse_key: str = ""
    agent_virtual_playlist_browse_error: str = ""
    # Auditoria das invariantes current/queue. O primeiro contador vem do
    # Worker; o segundo mede snapshots legados reparados defensivamente na VPS.
    agent_queue_invariant_repairs: int = 0
    agent_queue_snapshot_repairs: int = 0
    agent_queue_last_repair_signature: str = ""

    def queue_size(self) -> int:
        local_count = self.queue.qsize() + len(self.forward_queue)
        try:
            remote_count = int(self.agent_remote_queue_size or 0)
        except Exception:
            remote_count = 0
        if str(self.current_backend or "").lower() == "agent":
            return max(local_count, remote_count)
        return local_count
