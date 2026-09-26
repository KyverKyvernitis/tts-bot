from __future__ import annotations

import contextlib
import logging
from collections import deque
from typing import Any

from ..agente_telefone.conversao import faixa_do_payload
from ..busca import registrar_link_busca
from ..agente_telefone.roteamento import desvincular_guild_worker
from ..nucleo.modelos import MusicTrack

logger = logging.getLogger(__name__)


def _normalizar_playlist_virtual(virtual: Any) -> dict[str, Any]:
    if not isinstance(virtual, dict):
        return {}
    cursor = virtual.get("cursor") if isinstance(virtual.get("cursor"), dict) else None
    if not cursor or bool(cursor.get("exhausted")):
        return {}

    def inteiro(value: Any, default: int = 0) -> int:
        try:
            return max(0, int(value if value not in (None, "") else default))
        except Exception:
            return max(0, int(default))

    total_raw = cursor.get("total_tracks")
    try:
        total_tracks = None if total_raw in (None, "") else max(0, int(total_raw))
    except Exception:
        total_tracks = None
    block_raw = cursor.get("block_end_offset")
    try:
        block_end = None if block_raw in (None, "") else max(0, int(block_raw))
    except Exception:
        block_end = None

    return {
        "active": True,
        "provider": str(cursor.get("provider") or "").strip(),
        "source_url": str(cursor.get("source_url") or "").strip(),
        "title": str(cursor.get("title") or "").strip(),
        "resource_type": str(cursor.get("resource_type") or "playlist").strip() or "playlist",
        "resource_id": str(cursor.get("resource_id") or "").strip(),
        "instance_id": str(cursor.get("instance_id") or "").strip(),
        "next_offset": inteiro(cursor.get("next_offset")),
        "total_tracks": total_tracks,
        "block_end_offset": block_end,
        "shuffle_seed": inteiro(cursor.get("shuffle_seed")),
        "materialized_before": inteiro(virtual.get("materialized_before")),
        "physical_index": inteiro(virtual.get("physical_index")),
        "waiting": bool(virtual.get("waiting")),
        "requester_id": inteiro(virtual.get("requester_id")),
        "requester_name": str(virtual.get("requester_name") or ""),
        "remaining": (None if virtual.get("remaining") in (None, "") else inteiro(virtual.get("remaining"))),
    }


def _playlists_virtuais_publicas(remote: dict[str, Any]) -> list[dict[str, Any]]:
    raw = remote.get("virtual_playlists")
    values: list[dict[str, Any]] = []
    if isinstance(raw, list):
        for item in raw:
            normalized = _normalizar_playlist_virtual(item)
            if normalized:
                values.append(normalized)
    if not values:
        single = _normalizar_playlist_virtual(remote.get("virtual_playlist"))
        if single:
            values.append(single)
    return values


def _playlist_virtual_legada(info: dict[str, Any]) -> dict[str, Any]:
    """Shape singular preservado para clientes/testes anteriores à Wave 15."""
    if not isinstance(info, dict) or not info:
        return {}
    keys = (
        "active", "provider", "source_url", "title", "resource_type",
        "resource_id", "next_offset", "total_tracks", "materialized_before", "waiting",
    )
    return {key: info.get(key) for key in keys}


def _playlist_virtual_publica(remote: dict[str, Any]) -> dict[str, Any]:
    values = _playlists_virtuais_publicas(remote)
    return _playlist_virtual_legada(values[0]) if values else {}


def _virtual_playlist_browse_key(info: dict[str, Any]) -> str:
    if not isinstance(info, dict) or not info:
        return ""
    try:
        consumed = max(0, int(info.get("next_offset") or 0) - int(info.get("materialized_before") or 0))
    except Exception:
        consumed = 0
    return "|".join(
        (
            str(info.get("provider") or "").strip(),
            str(info.get("source_url") or "").strip(),
            str(info.get("instance_id") or ""),
            str(info.get("block_end_offset") if info.get("block_end_offset") not in (None, "") else "?"),
            str(info.get("shuffle_seed") or 0),
            # Preserve consumed|total at the tail for compatibility with the
            # existing browse-key contract/tests.
            str(consumed),
            str(info.get("total_tracks") if info.get("total_tracks") not in (None, "") else "?"),
        )
    )

def _virtual_layout_browse_key(playlists: list[dict[str, Any]], layout: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for info in playlists:
        parts.append(_virtual_playlist_browse_key(info))
    for entry in layout:
        kind = str(entry.get("kind") or "") if isinstance(entry, dict) else ""
        if kind == "track":
            track = entry.get("track") if isinstance(entry, dict) else None
            parts.append(f"t:{getattr(track, 'queue_item_id', '')}")
        elif kind == "virtual":
            info = entry.get("virtual") if isinstance(entry, dict) else None
            if isinstance(info, dict):
                parts.append(f"v:{_virtual_playlist_browse_key(info)}")
    return "||".join(parts)


def sincronizar_fila_remota(
    state: Any,
    remote: dict[str, Any],
    *,
    limite_fila: int,
    limite_historico: int,
) -> None:
    """Espelha a fila remota somente para painel e controles da VPS.

    A fila real continua pertencendo ao Phone Worker.
    """
    if not isinstance(remote, dict):
        return

    try:
        remote_repair_total = max(0, int(remote.get("queue_invariant_repairs") or 0))
    except Exception:
        remote_repair_total = 0
    previous_remote_repairs = max(0, int(getattr(state, "agent_queue_invariant_repairs", 0) or 0))
    if remote_repair_total > previous_remote_repairs:
        logger.warning(
            "[music/queue] Worker reparou alias current/queue | repairs_total=%s novos=%s",
            remote_repair_total,
            remote_repair_total - previous_remote_repairs,
        )
    state.agent_queue_invariant_repairs = remote_repair_total

    # Atualize o cursor mesmo quando o Worker omitir o preview da fila. Isso
    # evita a UI dizer "fila vazia" enquanto está parada exatamente no marker.
    virtual_playlists = _playlists_virtuais_publicas(remote)
    virtual_info = virtual_playlists[0] if virtual_playlists else {}
    state.agent_virtual_playlist = _playlist_virtual_legada(virtual_info)
    state.agent_virtual_playlists = list(virtual_playlists)

    layout: list[dict[str, Any]] = []
    raw_layout = remote.get("queue_layout")
    if isinstance(raw_layout, list):
        for entry in raw_layout:
            if not isinstance(entry, dict):
                continue
            kind = str(entry.get("kind") or "").strip().lower()
            if kind == "track" and isinstance(entry.get("track"), dict):
                track_value = faixa_do_payload(entry.get("track"))
                if track_value is not None:
                    layout.append({"kind": "track", "track": track_value})
            elif kind == "virtual":
                normalized = _normalizar_playlist_virtual({
                    "cursor": entry.get("cursor") if isinstance(entry.get("cursor"), dict) else {},
                    "requester_id": entry.get("requester_id"),
                    "requester_name": entry.get("requester_name"),
                    "remaining": entry.get("remaining"),
                })
                if normalized:
                    layout.append({"kind": "virtual", "virtual": normalized})
    state.agent_queue_layout = layout

    def commit_virtual_browse_state() -> None:
        browse_key = _virtual_layout_browse_key(virtual_playlists, layout)
        previous_browse_key = str(getattr(state, "agent_virtual_playlist_browse_key", "") or "")
        if browse_key != previous_browse_key:
            # As posições lógicas mudaram (nova playlist ou a faixa atual avançou).
            # Invalide só o cache visual; a fila autoritativa continua no Worker.
            state.agent_virtual_playlist_pages.clear()
            state.agent_virtual_playlist_browse_error = ""
            state.agent_virtual_playlist_browse_key = browse_key

    remote_queue = remote.get("queue")
    has_logical_queue_size = remote.get("logical_queue_size") not in (None, "")
    try:
        state.agent_remote_materialized_queue_size = max(0, int(remote.get("queue_size") or 0))
    except Exception:
        state.agent_remote_materialized_queue_size = 0
    try:
        state.agent_remote_queue_size = max(
            state.agent_remote_materialized_queue_size,
            int(remote.get("logical_queue_size") or 0),
        )
    except Exception:
        state.agent_remote_queue_size = state.agent_remote_materialized_queue_size

    if remote_queue is None and remote.get("queue_size") in (0, "0"):
        remote_queue = []
    if not isinstance(remote_queue, list):
        commit_virtual_browse_state()
        return

    if not state.agent_remote_queue_size:
        state.agent_remote_queue_size = len(remote_queue)
    else:
        state.agent_remote_queue_size = max(state.agent_remote_queue_size, len(remote_queue))

    mirrored: deque[MusicTrack] = deque(maxlen=limite_fila)
    current_payload = remote.get("current") if isinstance(remote.get("current"), dict) else {}
    current_queue_item_id = str(current_payload.get("queue_item_id") or "").strip()
    mirrored_duplicate_repairs = 0
    for item in remote_queue[:limite_fila]:
        if isinstance(item, dict):
            if current_queue_item_id and str(item.get("queue_item_id") or "").strip() == current_queue_item_id:
                # Defesa para snapshots produzidos durante uma corrida antiga:
                # a MESMA entrada não pode estar em current e queue. Repetições
                # legítimas possuem ids diferentes e não são removidas.
                mirrored_duplicate_repairs += 1
                continue
            track = faixa_do_payload(item)
            if track is not None:
                mirrored.append(track)
    if mirrored_duplicate_repairs:
        state.agent_remote_materialized_queue_size = max(0, state.agent_remote_materialized_queue_size - mirrored_duplicate_repairs)
        if not has_logical_queue_size:
            state.agent_remote_queue_size = max(0, state.agent_remote_queue_size - mirrored_duplicate_repairs)
        state.agent_queue_snapshot_repairs = max(0, int(getattr(state, "agent_queue_snapshot_repairs", 0) or 0)) + mirrored_duplicate_repairs
        repair_signature = f"{current_queue_item_id}:{mirrored_duplicate_repairs}"
        if repair_signature != str(getattr(state, "agent_queue_last_repair_signature", "") or ""):
            logger.warning(
                "[music/queue] snapshot remoto continha current também na fila; reparando espelho | id=%s removidos=%s",
                current_queue_item_id[:24],
                mirrored_duplicate_repairs,
            )
        state.agent_queue_last_repair_signature = repair_signature
        if virtual_info:
            # Snapshots de versões antigas podiam contar a faixa atual também
            # em ``materialized_before``. Corrija a mesma unidade no cursor,
            # senão a UI ainda exibiria +1 no total lógico apesar de filtrar a
            # duplicata visual da queue.
            virtual_info["materialized_before"] = max(
                0,
                int(virtual_info.get("materialized_before") or 0) - mirrored_duplicate_repairs,
            )
            state.agent_virtual_playlist = _playlist_virtual_legada(virtual_info)

    if not mirrored_duplicate_repairs:
        state.agent_queue_last_repair_signature = ""

    commit_virtual_browse_state()

    state.forward_queue.clear()
    state.forward_queue.extend(mirrored)

    # A fila local é apenas compatibilidade visual; nunca deve assumir playback.
    with contextlib.suppress(Exception):
        while not state.queue.empty():
            state.queue.get_nowait()
            state.queue.task_done()


async def sincronizar_estado_agente(
    router: Any,
    guild_id: int,
    track: MusicTrack | None = None,
    agent_state: dict | None = None,
    *,
    voice_channel_id: int | None = None,
    text_channel_id: int | None = None,
    queued: bool = False,
    create_panel: bool = True,
):
    """Espelha o estado autoritativo do Phone Worker no estado/UI da VPS.

    A função não reproduz áudio, não conecta voz e não resolve mídia localmente.
    O Phone Worker continua sendo o único dono da reprodução.
    """
    import time

    from cogs.musica import configuracao as config

    from ..nucleo.estado import MUSIC_HISTORY_MAXSIZE, MUSIC_QUEUE_MAXSIZE
    from ..nucleo.fila import registrar_historico, tem_pendentes
    from ..nucleo.modelos import LoopMode

    guild_id = int(guild_id)
    state = router.get_state(guild_id)
    remote = agent_state if isinstance(agent_state, dict) else {}
    previous_status = str(getattr(state, "current_status", "") or "")
    previous_speed = float(getattr(state, "playback_speed", 1.0) or 1.0)
    previous_current = getattr(state, "current", None)
    previous_current_key = router._panel_key_for_track(previous_current) if previous_current is not None else ""

    try:
        if text_channel_id:
            state.last_text_channel_id = int(text_channel_id)
        elif remote.get("text_channel_id"):
            state.last_text_channel_id = int(remote.get("text_channel_id"))
    except Exception:
        pass
    try:
        if voice_channel_id:
            state.last_voice_channel_id = int(voice_channel_id)
        elif remote.get("voice_channel_id"):
            state.last_voice_channel_id = int(remote.get("voice_channel_id"))
    except Exception:
        pass

    remote_status_original = str(remote.get("status") or "").strip().lower()
    raw_status = remote_status_original
    previous_playback_token = int(getattr(state, "agent_playback_token", -1) or -1)
    remote_playback_token = None
    with contextlib.suppress(Exception):
        if remote.get("playback_token") is not None:
            remote_playback_token = int(remote.get("playback_token"))
    current_payload = remote.get("current") if isinstance(remote.get("current"), dict) else {}
    if current_payload:
        track = faixa_do_payload(current_payload, track)
        incoming_key = router._panel_key_for_track(track) if track is not None else ""
        if (
            previous_current is not None
            and incoming_key
            and previous_current_key
            and incoming_key != previous_current_key
            and previous_status not in {"stopped"}
        ):
            registrar_historico(state, previous_current)
    last_error = str(remote.get("last_error") or "").strip()
    sincronizar_fila_remota(state, remote, limite_fila=MUSIC_QUEUE_MAXSIZE, limite_historico=MUSIC_HISTORY_MAXSIZE)
    from .duracao_fila import schedule_virtual_duration_scan

    schedule_virtual_duration_scan(router, guild_id, state)
    with contextlib.suppress(Exception):
        state.agent_remote_history_size = max(0, int(remote.get("history_size") or 0))
    had_active_agent_session = bool(
        str(getattr(state, "current_backend", "") or "").lower() == "agent"
        and (state.current is not None or previous_status in {"resolving", "starting", "playing", "paused", "queued"})
    )
    confirmed_playing = bool(remote.get("confirmed_playing"))
    if raw_status == "playing" and not confirmed_playing:
        if "voice_connected" in remote or "player_present" in remote:
            confirmed_playing = bool(remote.get("voice_connected")) and bool(remote.get("player_present"))
    if raw_status == "playing" and not confirmed_playing:
        raw_status = "starting"
    if queued:
        if state.current is None and track is not None:
            state.current = track
            router._set_current_status(state, "queued")
    else:
        if last_error and raw_status in {"", "idle", "stopped"}:
            raw_status = "failed"
        if raw_status in {"failed", "error"}:
            if track is not None:
                state.current = track
            state.idle_reason = "track_failed"
            state.current_status_detail = last_error[:300]
            router._set_current_status(state, "error")
        elif raw_status in {"idle", "stopped"} and not current_payload and not last_error:
            if had_active_agent_session:
                last_action = str(remote.get("last_action") or "").strip().lower()
                last_event = str(remote.get("last_event") or "").strip().lower()
                if state.current is not None:
                    registrar_historico(state, state.current)
                state.current = None
                state.paused = False
                state.music_session_active = False
                state.agent_started_track_key = ""
                state.agent_started_playback_token = -1
                state.panel_last_repost_key = ""
                if remote_playback_token is not None:
                    state.agent_playback_token = remote_playback_token
                state.agent_last_idle_event = last_event or raw_status
                disconnect_reason = str(remote.get("last_disconnect_reason") or remote.get("voice_presence_reason") or "").strip().lower()
                if last_action == "stop" or last_event == "stop" or disconnect_reason == "manual_stop":
                    router._set_idle_reason(state, "manual_stop")
                    router._invalidate_panel_controls_now(guild_id)
                elif disconnect_reason == "music_alone" or last_event == "voice_alone_timeout_disconnect":
                    router._set_idle_reason(state, "music_alone_timeout")
                    router._invalidate_panel_controls_now(guild_id)
                elif disconnect_reason == "music_idle_timeout" or last_event == "idle_timeout_disconnect":
                    router._set_idle_reason(state, "music_idle_timeout")
                    router._invalidate_panel_controls_now(guild_id)
                elif disconnect_reason in {"voice_idle_empty", "voice_empty"} or last_event == "voice_empty_timeout_disconnect":
                    router._set_idle_reason(state, "voice_idle_empty")
                    router._invalidate_panel_controls_now(guild_id)
                elif disconnect_reason == "voice_transport_lost" or last_event == "voice_transport_disconnected":
                    router._set_idle_reason(state, "voice_connection_lost")
                    router._invalidate_panel_controls_now(guild_id)
                elif last_event in {"external_disconnect", "voice_disconnected", "kicked"}:
                    # Eventos legados não possuem causalidade suficiente para
                    # acusar ação humana. O Audit Log do gateway decide isso.
                    router._set_idle_reason(state, "unknown_disconnect")
                    router._invalidate_panel_controls_now(guild_id)
                else:
                    router._set_idle_reason(state, "queue_finished")
                    if not state.history:
                        router._set_panel_controls_invalidation(guild_id, delay=60.0)
                router._set_current_status(state, "idle")
                router._mark_internal_voice_disconnect(guild_id, seconds=8.0)
                router._schedule_agent_session_finished_effects(guild_id, "agent_idle")
                desvincular_guild_worker(guild_id)
            else:
                if track is not None:
                    state.current = track
                    router._set_current_status(state, "starting")
                else:
                    state.current = None
                    router._set_current_status(state, "idle")
        else:
            if track is not None:
                state.current = track
            mapped = {
                "preparing": "resolving",
                "starting": "starting",
                "playing": "playing",
                "paused": "paused",
                "queued": "queued",
            }.get(raw_status or "starting", "starting")
            router._set_current_status(state, mapped)

    state.current_backend = "agent"
    state.agent_voice_recovery_pending = bool(remote.get("voice_runtime_recovery_pending"))
    with contextlib.suppress(Exception):
        state.agent_voice_recovery_attempts = max(0, int(remote.get("voice_runtime_recovery_attempts") or 0))
    state.agent_voice_recovery_last_error = str(remote.get("voice_runtime_recovery_last_error") or "")[:260]
    state.agent_voice_session_mode = str(remote.get("voice_session_mode") or "")[:64]
    state.agent_voice_presence_reason = str(remote.get("voice_presence_reason") or "")[:120]
    state.agent_last_disconnect_reason = str(remote.get("last_disconnect_reason") or "")[:96]
    state.agent_last_disconnect_event = str(remote.get("last_disconnect_event") or "")[:96]
    with contextlib.suppress(Exception):
        state.agent_last_disconnect_at = float(remote.get("last_disconnect_at") or 0.0)
    with contextlib.suppress(Exception):
        state.agent_last_disconnect_human_count = int(remote.get("last_disconnect_human_count") if remote.get("last_disconnect_human_count") is not None else -1)
    state.agent_monitor_failures = 0
    state.agent_monitor_last_error = ""
    state.agent_monitor_reconnecting_since = 0.0
    remote_loop_mode = str(remote.get("loop_mode") or remote.get("repeat") or "").strip().lower()
    if remote_loop_mode in {"off", "one", "all"}:
        with contextlib.suppress(Exception):
            state.loop_mode = LoopMode(remote_loop_mode)
    with contextlib.suppress(Exception):
        if remote.get("volume_percent") is not None:
            state.volume = max(0.0, min(1.5, float(remote.get("volume_percent") or 0.0) / 100.0))
    for effect in ("bassboost", "nightcore", "slowed_reverb"):
        level_key = f"{effect}_level"
        if level_key in remote:
            try:
                max_level = 6 if effect == "bassboost" else 3
                level = max(0, min(max_level, int(remote.get(level_key) or 0)))
            except (TypeError, ValueError):
                level = 1 if bool(remote.get(effect)) else 0
            setattr(state, level_key, level)
            setattr(state, effect, level > 0)
        elif effect in remote:
            enabled = bool(remote[effect])
            setattr(state, effect, enabled)
            setattr(state, level_key, 1 if enabled else 0)
    with contextlib.suppress(Exception):
        if remote.get("speed_multiplier") is not None:
            state.playback_speed = max(0.4, min(1.75, float(remote["speed_multiplier"])))
    with contextlib.suppress(Exception):
        if remote.get("effects_revision") is not None:
            state.effects_revision = max(0, int(remote["effects_revision"]))
    state.shuffle = False
    if state.current is not None:
        # O estado nasce com defaults usados pelo player legado. No backend do
        # agente esses defaults não são evidência da mídia real; se a nova faixa
        # ainda não foi resolvida, zerar evita exibir "Spotify · 256 kbps" antes
        # de o yt-dlp informar codec/bitrate de verdade (e evita herdar a faixa
        # anterior durante uma troca).
        state.current_quality_kbps = 0
        state.current_quality_label = ""
        with contextlib.suppress(Exception):
            kbps = int(float(getattr(state.current, "resolved_audio_abr", 0) or getattr(state.current, "resolved_audio_max_abr", 0) or 0))
            if kbps > 0:
                state.current_quality_kbps = kbps
        ext = str(getattr(state.current, "resolved_audio_ext", "") or "").strip()
        codec = str(getattr(state.current, "resolved_audio_codec", "") or "").strip()
        if ext or codec or state.current_quality_kbps > 0:
            state.current_quality_label = "Worker"
    state.current_lavalink_player = None
    state.current_source = None
    remote_position_seconds = None
    with contextlib.suppress(Exception):
        if remote.get("position_ms") is not None:
            remote_position_seconds = max(0.0, float(remote.get("position_ms") or 0.0) / 1000.0)
    if raw_status == "paused":
        state.paused = True
        if remote_position_seconds is not None:
            state.voice_status_pause_position_seconds = remote_position_seconds
    else:
        if (previous_status == "paused" or state.playback_speed != previous_speed) and raw_status == "playing" and remote_position_seconds is not None:
            # Rebaseia o relógio local para que {elapsed}/{position} não conte o
            # tempo em que o Music Agent permaneceu pausado.
            state.current_start_offset_seconds = remote_position_seconds
            state.current_started_at_monotonic = time.monotonic()
        state.paused = False
        state.voice_status_pause_position_seconds = -1.0
    state.music_session_active = bool(state.current or raw_status in {"preparing", "starting", "playing", "paused", "queued"})
    if raw_status and raw_status not in {"failed", "error"}:
        if state.agent_voice_recovery_pending:
            attempt = max(0, int(getattr(state, "agent_voice_recovery_attempts", 0) or 0))
            state.current_status_detail = f"voice_recovery:{attempt}" if attempt else "voice_recovery"
        else:
            state.current_status_detail = raw_status
    active_statuses = {"resolving", "starting", "reconnecting", "playing", "paused", "queued"}
    new_panel_key = router._panel_key_for_track(state.current)
    # Payloads atuais do agente possuem confirmação explícita de voz/player.
    # Não trate apenas `status=playing` como início confirmado nesses payloads:
    # durante uma reconexão pode existir um snapshot transitório ainda marcado
    # como playing sem áudio realmente ativo. O fallback simples permanece só
    # para agentes legados que não publicavam os campos de confirmação.
    has_playback_confirmation = any(
        key in remote for key in ("confirmed_playing", "voice_connected", "player_present")
    )
    active_started_signal = bool(
        remote_status_original == "playing"
        and state.current is not None
        and not has_playback_confirmation
    )
    active_confirmed = bool(raw_status == "playing" and confirmed_playing and state.current is not None)
    if state.current is not None or tem_pendentes(state) or state.current_status in active_statuses:
        router._reactivate_panel_controls_now(guild_id)
    previous_started_key = str(getattr(state, "agent_started_track_key", "") or "")
    previous_started_token = int(getattr(state, "agent_started_playback_token", -1) or -1)
    playback_generation_changed = bool(
        remote_playback_token is not None
        and remote_playback_token != previous_playback_token
    )
    started_generation_changed = bool(
        remote_playback_token is not None
        and remote_playback_token != previous_started_token
    )
    legacy_track_changed = bool(new_panel_key and previous_started_key != new_panel_key)
    just_started_agent_track = bool(
        (active_confirmed or active_started_signal)
        and new_panel_key
        and (started_generation_changed or legacy_track_changed)
    )
    if str(remote.get("last_event") or "") == "audio_effect" and new_panel_key == previous_started_key:
        # Troca de filtro reinicia o PCM, mas não é início de outra música.
        just_started_agent_track = False
        state.agent_started_track_key = new_panel_key
        if remote_playback_token is not None:
            state.agent_started_playback_token = remote_playback_token
    if remote_playback_token is not None:
        state.agent_playback_token = remote_playback_token
    if just_started_agent_track:
        same_track_restart = bool(previous_started_key and previous_started_key == new_panel_key)
        state.agent_started_track_key = new_panel_key
        if remote_playback_token is not None:
            state.agent_started_playback_token = remote_playback_token
        state.current_started_at_monotonic = time.monotonic()
        # O Worker é autoritativo também no primeiro snapshot que a VPS recebe.
        # Isso cobre recovery/seek da mesma faixa e também restart/rebind da VPS
        # quando ela reencontra uma música que já estava, por exemplo, em 0:42.
        # Para uma faixa realmente nova `position_ms` naturalmente estará perto
        # de zero, então usar a posição remota é seguro e evita saltos visuais.
        remote_resume_offset = remote_position_seconds
        if remote_resume_offset is None:
            with contextlib.suppress(Exception):
                if current_payload.get("start_offset_seconds") is not None:
                    remote_resume_offset = max(0.0, float(current_payload.get("start_offset_seconds") or 0.0))
        state.current_start_offset_seconds = max(0.0, float(remote_resume_offset or 0.0))
        state.voice_status_pause_position_seconds = -1.0
        # Se esta faixa nasceu de um link direto, o título real só fica
        # disponível depois que o Music Agent resolve o stream. Aprenda aqui,
        # no primeiro `playing`, para que buscas futuras virem direct-hit.
        if state.current is not None:
            with contextlib.suppress(Exception):
                registrar_link_busca(state.current)
        router._schedule_agent_playback_started_effects(guild_id, new_panel_key)

    status_transition = previous_status != state.current_status
    if (
        status_transition
        and state.current is not None
        and previous_status in {"playing", "paused"}
        and state.current_status in {"playing", "paused"}
        and not just_started_agent_track
    ):
        router._mark_voice_status_track_change(state)
        router._schedule_voice_status_track_sync(
            guild_id,
            repeat_after=0.0,
            reason="agent_pause" if state.current_status == "paused" else "agent_resume",
        )

    started_track_changed_for_panel = bool(new_panel_key and previous_started_key != new_panel_key)
    repost_key = f"{guild_id}:{new_panel_key}" if new_panel_key else ""
    already_reposted = bool(repost_key and repost_key == str(getattr(state, "panel_last_repost_key", "") or ""))
    should_repost_panel = bool(
        create_panel
        and state.now_message is not None
        and new_panel_key
        and just_started_agent_track
        and (started_track_changed_for_panel or playback_generation_changed or started_generation_changed)
        and not already_reposted
        and bool(getattr(config, "MUSIC_PANEL_REPOST_ON_TRACK_CHANGE", True))
    )
    if create_panel:
        await router.update_panel(guild_id, create=True, repost=should_repost_panel)
    if state.current_backend == "agent" and state.current_status in active_statuses:
        router.start_music_agent_monitor(guild_id, voice_channel_id=voice_channel_id, text_channel_id=text_channel_id)
    return state
