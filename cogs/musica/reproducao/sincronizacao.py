from __future__ import annotations

import contextlib
from collections import deque
from typing import Any

from ..agente_telefone.conversao import faixa_do_payload
from ..nucleo.modelos import MusicTrack


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

    remote_queue = remote.get("queue")
    try:
        state.agent_remote_queue_size = max(0, int(remote.get("queue_size") or 0))
    except Exception:
        state.agent_remote_queue_size = 0

    if remote_queue is None and remote.get("queue_size") in (0, "0"):
        remote_queue = []
    if not isinstance(remote_queue, list):
        return

    if not state.agent_remote_queue_size:
        state.agent_remote_queue_size = len(remote_queue)
    else:
        state.agent_remote_queue_size = max(state.agent_remote_queue_size, len(remote_queue))

    mirrored: deque[MusicTrack] = deque(maxlen=limite_historico)
    for item in remote_queue[:limite_fila]:
        if isinstance(item, dict):
            track = faixa_do_payload(item)
            if track is not None:
                mirrored.append(track)

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
    previous_panel_key = getattr(state, "panel_track_key", None)
    previous_status = str(getattr(state, "current_status", "") or "")
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
                state.agent_last_idle_event = last_event or raw_status
                if last_action == "stop" or last_event == "stop":
                    router._set_idle_reason(state, "manual_stop")
                    router._invalidate_panel_controls_now(guild_id)
                elif last_event in {"external_disconnect", "voice_disconnected", "kicked"}:
                    router._set_idle_reason(state, "external_disconnect")
                    router._invalidate_panel_controls_now(guild_id)
                else:
                    router._set_idle_reason(state, "queue_finished")
                    if not state.history:
                        router._set_panel_controls_invalidation(guild_id, delay=60.0)
                router._set_current_status(state, "idle")
                router._mark_internal_voice_disconnect(guild_id, seconds=8.0)
                router._schedule_agent_session_finished_effects(guild_id, "agent_idle")
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
    remote_loop_mode = str(remote.get("loop_mode") or remote.get("repeat") or "").strip().lower()
    if remote_loop_mode in {"off", "one", "all"}:
        with contextlib.suppress(Exception):
            state.loop_mode = LoopMode(remote_loop_mode)
    state.shuffle = False
    if state.current is not None:
        with contextlib.suppress(Exception):
            kbps = int(float(getattr(state.current, "resolved_audio_abr", 0) or getattr(state.current, "resolved_audio_max_abr", 0) or 0))
            if kbps > 0:
                state.current_quality_kbps = kbps
        ext = str(getattr(state.current, "resolved_audio_ext", "") or "").strip()
        codec = str(getattr(state.current, "resolved_audio_codec", "") or "").strip()
        if ext or codec:
            state.current_quality_label = "Worker"
    state.current_lavalink_player = None
    state.current_source = None
    state.paused = raw_status == "paused"
    state.music_session_active = bool(state.current or raw_status in {"preparing", "starting", "playing", "paused", "queued"})
    if raw_status and raw_status not in {"failed", "error"}:
        state.current_status_detail = raw_status
    active_statuses = {"resolving", "starting", "playing", "paused", "queued"}
    new_panel_key = router._panel_key_for_track(state.current)
    active_started_signal = bool(remote_status_original == "playing" and state.current is not None)
    active_confirmed = bool(raw_status == "playing" and confirmed_playing and state.current is not None)
    if state.current is not None or tem_pendentes(state) or state.current_status in active_statuses:
        router._reactivate_panel_controls_now(guild_id)
    previous_started_key = str(getattr(state, "agent_started_track_key", "") or "")
    just_started_agent_track = bool((active_confirmed or active_started_signal) and new_panel_key and previous_started_key != new_panel_key)
    if just_started_agent_track:
        state.agent_started_track_key = new_panel_key
        state.current_started_at_monotonic = time.monotonic()
        state.current_start_offset_seconds = 0.0
        router._schedule_agent_playback_started_effects(guild_id, new_panel_key)

    track_changed_for_panel = bool(new_panel_key and previous_panel_key != new_panel_key)
    repost_key = f"{guild_id}:{new_panel_key}" if new_panel_key else ""
    already_reposted = bool(repost_key and repost_key == str(getattr(state, "panel_last_repost_key", "") or ""))
    should_repost_panel = bool(
        create_panel
        and state.now_message is not None
        and new_panel_key
        and just_started_agent_track
        and track_changed_for_panel
        and not already_reposted
        and bool(getattr(config, "MUSIC_PANEL_REPOST_ON_TRACK_CHANGE", True))
    )
    if create_panel:
        await router.update_panel(guild_id, create=True, repost=should_repost_panel)
    if state.current_backend == "agent" and state.current_status in active_statuses:
        router.start_music_agent_monitor(guild_id, voice_channel_id=voice_channel_id, text_channel_id=text_channel_id)
    return state
