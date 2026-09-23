from __future__ import annotations

import asyncio
import logging
from typing import Any

from cogs.musica import configuracao as config

from .comandos import estado_comando_diferido, music_agent_status
from .conversao import estado_da_guild_no_payload
from .roteamento import desvincular_guild_worker
from ..reproducao.playlist_virtual import schedule_playlist_refill_if_needed

logger = logging.getLogger(__name__)


def _assinatura_faixa_painel(item: Any) -> tuple[Any, ...]:
    if not isinstance(item, dict):
        return ()
    return tuple(
        item.get(key)
        for key in (
            "title",
            "webpage_url",
            "duration",
            "uploader",
            "thumbnail",
            "source",
            "resolved_audio_format_id",
            "resolved_audio_ext",
            "resolved_audio_codec",
            "resolved_audio_abr",
            "resolved_audio_sample_rate",
            "resolved_audio_channels",
        )
    )


def _assinatura_playlist_virtual(remote: dict[str, Any]) -> tuple[Any, ...]:
    virtual = remote.get("virtual_playlist") if isinstance(remote.get("virtual_playlist"), dict) else None
    if not virtual:
        return ()
    cursor = virtual.get("cursor") if isinstance(virtual.get("cursor"), dict) else {}
    return (
        str(cursor.get("provider") or ""),
        str(cursor.get("source_url") or ""),
        str(cursor.get("title") or ""),
        cursor.get("next_offset"),
        cursor.get("total_tracks"),
        bool(cursor.get("exhausted")),
        virtual.get("materialized_before"),
        bool(virtual.get("waiting")),
    )


def _assinatura_painel_remoto(remote: dict[str, Any]) -> tuple[Any, ...]:
    """Retorna apenas campos remotos que podem mudar o painel/controles.

    Telemetria volátil como position_ms/status_age_seconds não participa. Assim
    o poll de estado pode continuar em alta frequência sem gerar um PATCH/edit
    idêntico no Discord a cada rodada.
    """
    current = remote.get("current") if isinstance(remote.get("current"), dict) else {}
    queue = remote.get("queue") if isinstance(remote.get("queue"), list) else []
    try:
        queue_size = max(0, int(remote.get("queue_size") or 0))
    except Exception:
        queue_size = len(queue)
    try:
        history_size = max(0, int(remote.get("history_size") or 0))
    except Exception:
        history_size = 0
    queue_signature = tuple(
        _assinatura_faixa_painel(item)
        for item in queue[:10]
        if isinstance(item, dict)
    )
    return (
        str(remote.get("status") or "").lower(),
        bool(remote.get("paused")),
        bool(remote.get("confirmed_playing")),
        bool(remote.get("voice_connected")),
        bool(remote.get("player_present")),
        str(remote.get("last_error") or ""),
        str(remote.get("last_action") or ""),
        str(remote.get("last_event") or ""),
        bool(remote.get("voice_runtime_recovery_pending")),
        int(remote.get("voice_runtime_recovery_attempts") or 0),
        str(remote.get("voice_runtime_recovery_last_error") or ""),
        str(remote.get("loop_mode") or remote.get("repeat") or "").lower(),
        queue_size,
        history_size,
        bool(remote.get("previous_available")),
        _assinatura_faixa_painel(current),
        queue_signature,
        _assinatura_playlist_virtual(remote),
    )


def _deve_atualizar_painel(
    *,
    assinatura: tuple[Any, ...],
    assinatura_anterior: tuple[Any, ...] | None,
    agora: float,
    ultimo_refresh: float,
    painel_existe: bool,
    refresh_seconds: float,
) -> bool:
    if not painel_existe:
        return True
    if assinatura_anterior is None or assinatura != assinatura_anterior:
        return True
    return agora - float(ultimo_refresh or 0.0) >= max(10.0, float(refresh_seconds or 30.0))


def _revisao_estado_remoto(remote: dict[str, Any]) -> str:
    """Revisão estável enviada pelo Worker; vazia mantém compatibilidade antiga."""
    value = remote.get("state_revision") if isinstance(remote, dict) else None
    return str(value or "").strip()


def _intervalo_poll_music_agent(status: str, *, falhas: int = 0) -> float:
    """Escolhe polling rápido só nas transições sensíveis à latência."""
    status = str(status or "").strip().lower()
    startup = max(0.4, min(1.5, float(getattr(config, "MUSIC_AGENT_STATUS_POLL_SECONDS", 0.75) or 0.75)))
    playing = max(1.0, min(4.0, float(getattr(config, "MUSIC_AGENT_PANEL_POLL_SECONDS", 2.0) or 2.0)))
    paused = max(playing, min(6.0, float(getattr(config, "MUSIC_AGENT_PAUSED_POLL_SECONDS", 3.0) or 3.0)))
    if falhas >= 30:
        # Outage longo (Tailscale desligado/rede móvel trocada): reduza o
        # polling para não martelar Worker/VPS, mas mantenha o watcher vivo
        # tempo suficiente para autorrecuperar quando a rota voltar.
        return max(6.0, min(10.0, playing * 3.0))
    if falhas >= 12:
        return max(4.0, min(8.0, playing * 2.0))
    if falhas:
        return min(4.0, max(startup, playing))
    if status in {"preparing", "resolving", "starting", "queued", ""}:
        return startup
    if status == "paused":
        return paused
    if status == "playing":
        return playing
    return min(playing, 1.0)


def monitor_music_agent_ativo(router: Any, guild_id: int) -> bool:
    try:
        task = getattr(router.get_state(int(guild_id)), "agent_monitor_task", None)
        return bool(task is not None and not task.done())
    except Exception:
        return False


def estado_local_music_agent(router: Any, guild_id: int) -> dict[str, Any]:
    """Snapshot mínimo do espelho local para watchers de mensagens.

    O monitor é o único polling contínuo. Watchers consultam este espelho em
    memória, evitando uma segunda sequência de requests ao Phone Worker.
    """
    state = router.get_state(int(guild_id))
    local_status = str(getattr(state, "current_status", "") or "").strip().lower()
    remote_status = {
        "resolving": "preparing",
        "error": "failed",
    }.get(local_status, local_status or "idle")
    current = getattr(state, "current", None)
    current_payload = None
    if current is not None:
        current_payload = {
            "title": str(getattr(current, "title", "") or ""),
            "webpage_url": str(getattr(current, "webpage_url", "") or ""),
            "duration": getattr(current, "duration", None),
            "uploader": str(getattr(current, "uploader", "") or ""),
            "thumbnail": str(getattr(current, "thumbnail", "") or ""),
            "source": str(getattr(current, "source", "") or ""),
        }
    return {
        "status": remote_status,
        "paused": bool(getattr(state, "paused", False)),
        "confirmed_playing": bool(remote_status == "playing" and current is not None),
        "current": current_payload,
        "last_error": str(getattr(state, "current_status_detail", "") or "") if remote_status in {"failed", "error"} else "",
    }




async def _marcar_monitor_reconectando(router: Any, guild_id: int, *, falhas: int, erro: str = "") -> None:
    """Espelha indisponibilidade transitória sem fingir que a faixa terminou."""
    state = router.get_state(int(guild_id))
    state.agent_monitor_failures = max(0, int(falhas or 0))
    state.agent_monitor_last_error = str(erro or "")[:260]
    deferred = estado_comando_diferido(guild_id)
    state.agent_deferred_command_status = str(deferred.get("status") or "")
    state.agent_deferred_command_attempts = max(0, int(deferred.get("attempts") or 0))
    state.agent_deferred_command_error = str(deferred.get("last_error") or "")[:260]
    if not float(getattr(state, "agent_monitor_reconnecting_since", 0.0) or 0.0):
        try:
            state.agent_monitor_reconnecting_since = asyncio.get_running_loop().time()
        except RuntimeError:
            state.agent_monitor_reconnecting_since = 0.0
    active = bool(
        getattr(state, "current", None) is not None
        or getattr(state, "music_session_active", False)
        or str(getattr(state, "current_status", "") or "") in {"resolving", "starting", "playing", "paused", "queued", "reconnecting"}
    )
    if active:
        setter = getattr(router, "_set_current_status", None)
        if callable(setter):
            setter(state, "reconnecting")
        else:
            state.current_status = "reconnecting"
        state.current_status_detail = "Phone Worker temporariamente inacessível; reconectando"
        updater = getattr(router, "update_panel", None)
        if callable(updater) and getattr(state, "now_message", None) is not None:
            try:
                await updater(int(guild_id), create=False)
            except Exception:
                logger.debug("[music/agent] painel de reconexão não pôde ser atualizado | guild=%s", guild_id, exc_info=True)


def _limpar_auditoria_monitor(router: Any, guild_id: int, *, recovered: bool) -> None:
    state = router.get_state(int(guild_id))
    previous = int(getattr(state, "agent_monitor_failures", 0) or 0)
    state.agent_monitor_failures = 0
    state.agent_monitor_last_error = ""
    state.agent_monitor_reconnecting_since = 0.0
    deferred = estado_comando_diferido(guild_id)
    state.agent_deferred_command_status = str(deferred.get("status") or "")
    state.agent_deferred_command_attempts = max(0, int(deferred.get("attempts") or 0))
    state.agent_deferred_command_error = str(deferred.get("last_error") or "")[:260]
    if recovered and previous:
        state.agent_monitor_recoveries = int(getattr(state, "agent_monitor_recoveries", 0) or 0) + 1

def iniciar_monitor_music_agent(
    router: Any,
    guild_id: int,
    *,
    voice_channel_id: int | None = None,
    text_channel_id: int | None = None,
    callback_conclusao: Any | None = None,
) -> None:
    """Mantém o espelho da VPS atualizado enquanto o Phone Worker toca."""
    guild_id = int(guild_id)
    state = router.get_state(guild_id)
    task = getattr(state, "agent_monitor_task", None)
    if task is not None and not task.done():
        return

    async def _runner() -> None:
        idle_seen = 0
        failure_seen = 0
        ultimo_erro_monitor = ""
        assinatura_painel: tuple[Any, ...] | None = None
        revisao_remota = ""
        ultimo_estado_remoto: dict[str, Any] | None = None
        status_hint = str(getattr(router.get_state(guild_id), "current_status", "") or "")
        ultimo_refresh_painel = 0.0
        refresh_painel = max(
            10.0,
            min(300.0, float(getattr(config, "MUSIC_AGENT_PANEL_REFRESH_SECONDS", 30.0) or 30.0)),
        )
        try:
            while True:
                await asyncio.sleep(_intervalo_poll_music_agent(status_hint, falhas=failure_seen))
                try:
                    agora_pre_poll = asyncio.get_running_loop().time()
                    estado_pre_poll = router.get_state(guild_id)
                    painel_existe = getattr(estado_pre_poll, "now_message", None) is not None
                    refresh_vencido = agora_pre_poll - float(ultimo_refresh_painel or 0.0) >= refresh_painel
                    # Fora do refresh periódico, envie a revisão conhecida para o
                    # Worker poder responder apenas `unchanged`, sem serializar
                    # faixa/fila/posição novamente.
                    known_revision = "" if (not painel_existe or refresh_vencido) else revisao_remota
                    payload = await music_agent_status(
                        timeout_seconds=getattr(config, "MUSIC_AGENT_STATUS_TIMEOUT_SECONDS", 5.0),
                        guild_id=guild_id,
                        known_revision=known_revision,
                    )
                except Exception as exc:
                    failure_seen += 1
                    ultimo_erro_monitor = f"{type(exc).__name__}: {exc}"[:260]
                    logger.debug(
                        "[music/agent] monitor não conseguiu consultar status | guild=%s falhas=%s",
                        guild_id,
                        failure_seen,
                        exc_info=True,
                    )
                    if failure_seen >= int(getattr(config, "MUSIC_AGENT_MONITOR_UI_FAILURES", 2) or 2):
                        await _marcar_monitor_reconectando(router, guild_id, falhas=failure_seen, erro=ultimo_erro_monitor)
                    deferred = estado_comando_diferido(guild_id)
                    if (
                        ultimo_estado_remoto is None
                        and str(deferred.get("status") or "") in {"failed", "expired"}
                    ):
                        state = router.get_state(guild_id)
                        detail = str(deferred.get("last_error") or ultimo_erro_monitor or "Phone Worker indisponível")[:300]
                        setter = getattr(router, "_set_current_status", None)
                        if callable(setter):
                            setter(state, "error")
                        else:
                            state.current_status = "error"
                        state.current_status_detail = detail
                        state.agent_deferred_command_status = str(deferred.get("status") or "")
                        state.agent_deferred_command_attempts = max(0, int(deferred.get("attempts") or 0))
                        state.agent_deferred_command_error = detail
                        updater = getattr(router, "update_panel", None)
                        if callable(updater) and getattr(state, "now_message", None) is not None:
                            try:
                                await updater(guild_id, create=False)
                            except Exception:
                                logger.debug("[music/agent] painel de falha diferida não pôde ser atualizado | guild=%s", guild_id, exc_info=True)
                        desvincular_guild_worker(guild_id)
                        return
                    rebind_every = max(1, int(getattr(config, "MUSIC_AGENT_MONITOR_REBIND_FAILURES", 4) or 4))
                    if failure_seen % rebind_every == 0:
                        # Solte periodicamente a afinidade. Assim uma rota/Worker
                        # que ficou stale após troca de rede nunca prende a guild
                        # ao mesmo endpoint até o monitor expirar.
                        desvincular_guild_worker(guild_id)
                        revisao_remota = ""
                    if failure_seen >= int(getattr(config, "MUSIC_AGENT_MONITOR_MAX_FAILURES", 30) or 30):
                        desvincular_guild_worker(guild_id)
                        return
                    continue
                if not bool(payload.get("ok", True)) or (payload.get("available") is False and payload.get("error")):
                    failure_seen += 1
                    ultimo_erro_monitor = str(payload.get("error") or "worker indisponível")[:260]
                    if failure_seen >= int(getattr(config, "MUSIC_AGENT_MONITOR_UI_FAILURES", 2) or 2):
                        await _marcar_monitor_reconectando(router, guild_id, falhas=failure_seen, erro=ultimo_erro_monitor)
                    deferred = estado_comando_diferido(guild_id)
                    if (
                        ultimo_estado_remoto is None
                        and str(deferred.get("status") or "") in {"failed", "expired"}
                    ):
                        state = router.get_state(guild_id)
                        detail = str(deferred.get("last_error") or ultimo_erro_monitor or "Phone Worker indisponível")[:300]
                        setter = getattr(router, "_set_current_status", None)
                        if callable(setter):
                            setter(state, "error")
                        else:
                            state.current_status = "error"
                        state.current_status_detail = detail
                        state.agent_deferred_command_status = str(deferred.get("status") or "")
                        state.agent_deferred_command_attempts = max(0, int(deferred.get("attempts") or 0))
                        state.agent_deferred_command_error = detail
                        updater = getattr(router, "update_panel", None)
                        if callable(updater) and getattr(state, "now_message", None) is not None:
                            try:
                                await updater(guild_id, create=False)
                            except Exception:
                                logger.debug("[music/agent] painel de falha diferida não pôde ser atualizado | guild=%s", guild_id, exc_info=True)
                        desvincular_guild_worker(guild_id)
                        return
                    rebind_every = max(1, int(getattr(config, "MUSIC_AGENT_MONITOR_REBIND_FAILURES", 4) or 4))
                    if failure_seen % rebind_every == 0:
                        desvincular_guild_worker(guild_id)
                        revisao_remota = ""
                    if failure_seen >= int(getattr(config, "MUSIC_AGENT_MONITOR_MAX_FAILURES", 30) or 30):
                        desvincular_guild_worker(guild_id)
                        return
                    continue
                recovered_after_failures = failure_seen > 0
                failure_seen = 0
                if recovered_after_failures:
                    logger.info(
                        "[music/agent] monitor recuperado | guild=%s erro_anterior=%s",
                        guild_id,
                        ultimo_erro_monitor or "-",
                    )
                _limpar_auditoria_monitor(router, guild_id, recovered=recovered_after_failures)
                if bool(payload.get("unchanged")) and revisao_remota:
                    # Se a rede caiu, o espelho local foi marcado como
                    # `reconnecting`. Mesmo que o Worker responda `unchanged`,
                    # reaplique o último snapshot autoritativo para não deixar o
                    # painel congelado em reconexão.
                    if ultimo_estado_remoto:
                        schedule_playlist_refill_if_needed(router, guild_id, ultimo_estado_remoto)
                        local_status = str(getattr(router.get_state(guild_id), "current_status", "") or "").lower()
                        if recovered_after_failures or local_status == "reconnecting":
                            await router.sync_music_agent_state(
                                guild_id,
                                None,
                                ultimo_estado_remoto,
                                voice_channel_id=voice_channel_id,
                                text_channel_id=text_channel_id,
                                queued=False,
                                create_panel=True,
                            )
                    idle_seen = 0 if status_hint in {"preparing", "starting", "playing", "paused", "queued", "resolving"} else idle_seen
                    continue
                remote = estado_da_guild_no_payload(payload, guild_id)
                if not remote:
                    idle_seen += 1
                    status_hint = "idle"
                    if idle_seen >= 4:
                        desvincular_guild_worker(guild_id)
                        return
                    continue

                ultimo_estado_remoto = remote
                schedule_playlist_refill_if_needed(router, guild_id, remote)
                status = str(remote.get("status") or "").lower()
                status_hint = status
                nova_assinatura = _assinatura_painel_remoto(remote)
                nova_revisao = _revisao_estado_remoto(remote)
                agora = asyncio.get_running_loop().time()
                estado_local = router.get_state(guild_id)
                atualizar_painel = _deve_atualizar_painel(
                    assinatura=nova_assinatura,
                    assinatura_anterior=assinatura_painel,
                    agora=agora,
                    ultimo_refresh=ultimo_refresh_painel,
                    painel_existe=getattr(estado_local, "now_message", None) is not None,
                    refresh_seconds=refresh_painel,
                )
                revisao_inalterada = bool(nova_revisao and revisao_remota and nova_revisao == revisao_remota)
                assinatura_inalterada = assinatura_painel is not None and nova_assinatura == assinatura_painel
                precisa_sincronizar = bool(
                    atualizar_painel
                    or not revisao_inalterada
                    or not assinatura_inalterada
                )
                if precisa_sincronizar:
                    await router.sync_music_agent_state(
                        guild_id,
                        None,
                        remote,
                        voice_channel_id=voice_channel_id,
                        text_channel_id=text_channel_id,
                        queued=False,
                        create_panel=atualizar_painel,
                    )
                assinatura_painel = nova_assinatura
                if nova_revisao:
                    revisao_remota = nova_revisao
                if atualizar_painel and getattr(router.get_state(guild_id), "now_message", None) is not None:
                    ultimo_refresh_painel = agora

                has_current = isinstance(remote.get("current"), dict) and bool(remote.get("current"))
                idle_seen = idle_seen + 1 if status in {"idle", "stopped", "failed", "error"} and not has_current else 0
                if idle_seen >= 3:
                    desvincular_guild_worker(guild_id)
                    return
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("[music/agent] monitor encerrado por falha", exc_info=True)
        finally:
            st = router.get_state(guild_id)
            if getattr(st, "agent_monitor_task", None) is asyncio.current_task():
                st.agent_monitor_task = None

    try:
        task = asyncio.create_task(_runner())
        if callable(callback_conclusao):
            task.add_done_callback(callback_conclusao)
        else:
            task.add_done_callback(lambda done: done.exception() if not done.cancelled() else None)
        state.agent_monitor_task = task
    except RuntimeError:
        state.agent_monitor_task = None
