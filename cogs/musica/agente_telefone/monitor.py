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
            "queue_item_id",
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
        str(remote.get("voice_session_mode") or ""),
        str(remote.get("voice_presence_reason") or ""),
        str(remote.get("last_disconnect_reason") or ""),
        str(remote.get("last_disconnect_event") or ""),
        int(remote.get("last_disconnect_human_count") if remote.get("last_disconnect_human_count") is not None else -1),
        str(remote.get("loop_mode") or remote.get("repeat") or "").lower(),
        queue_size,
        history_size,
        bool(remote.get("previous_available")),
        int(remote.get("playback_token") or 0),
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
        state.current_status_detail = "Conexão do player temporariamente indisponível; tentando restabelecer"
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

def _sessao_local_exige_monitor(router: Any, guild_id: int) -> bool:
    """Retorna se ainda existe evidência local de uma sessão remota ativa.

    O monitor não usa apenas ``current`` porque uma queda de rota pode acontecer
    durante ``preparing``/``reconnecting``. Enquanto a sessão ainda for do agente,
    perder o watcher transforma uma falha transitória em painel/status congelados.
    """
    try:
        state = router.get_state(int(guild_id))
    except Exception:
        return False
    if str(getattr(state, "current_backend", "") or "").lower() != "agent":
        return False
    status = str(getattr(state, "current_status", "") or "").lower()
    return bool(
        getattr(state, "current", None) is not None
        or getattr(state, "music_session_active", False)
        or status in {"resolving", "starting", "playing", "paused", "queued", "reconnecting"}
    )


def _identidade_reproducao_remota(remote: dict[str, Any]) -> tuple[int, str]:
    current = remote.get("current") if isinstance(remote.get("current"), dict) else {}
    try:
        token = int(remote.get("playback_token") or -1)
    except Exception:
        token = -1
    return token, str(current.get("queue_item_id") or "")


def _identidade_reproducao_local(state: Any) -> tuple[int, str]:
    try:
        token = int(getattr(state, "agent_playback_token", -1) or -1)
    except Exception:
        token = -1
    current = getattr(state, "current", None)
    return token, str(getattr(current, "queue_item_id", "") or "") if current is not None else ""


def iniciar_monitor_music_agent(
    router: Any,
    guild_id: int,
    *,
    voice_channel_id: int | None = None,
    text_channel_id: int | None = None,
    callback_conclusao: Any | None = None,
) -> None:
    """Mantém o espelho da VPS atualizado enquanto o agente toca.

    Invariantes importantes:
    - falha de rede não encerra o monitor enquanto a sessão ainda parece ativa;
    - depois de qualquer outage, a primeira consulta é um snapshot completo;
    - falha local de sync/UI não mata o watcher nem avança a revisão conhecida;
    - exceção inesperada relança o watcher se a sessão ainda existir.
    """
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
        restart_after_failure = False
        refresh_painel = max(
            10.0,
            min(300.0, float(getattr(config, "MUSIC_AGENT_PANEL_REFRESH_SECONDS", 30.0) or 30.0)),
        )
        try:
            while True:
                await asyncio.sleep(_intervalo_poll_music_agent(status_hint, falhas=failure_seen))
                loop = asyncio.get_running_loop()
                state_cycle = router.get_state(guild_id)
                state_cycle.agent_monitor_last_cycle_at = loop.time()

                try:
                    agora_pre_poll = loop.time()
                    painel_existe = getattr(state_cycle, "now_message", None) is not None
                    refresh_vencido = agora_pre_poll - float(ultimo_refresh_painel or 0.0) >= refresh_painel
                    known_revision = "" if (not painel_existe or refresh_vencido or failure_seen) else revisao_remota
                    payload = await music_agent_status(
                        timeout_seconds=getattr(config, "MUSIC_AGENT_STATUS_TIMEOUT_SECONDS", 5.0),
                        guild_id=guild_id,
                        known_revision=known_revision,
                    )
                except Exception as exc:
                    failure_seen = min(1_000_000, failure_seen + 1)
                    ultimo_erro_monitor = f"{type(exc).__name__}: {exc}"[:260]
                    # Nunca confie em revisão pré-outage. O primeiro poll que
                    # conseguir voltar deve pedir estado completo, não `unchanged`.
                    revisao_remota = ""
                    logger.debug(
                        "[music/agent] monitor não conseguiu consultar status | guild=%s falhas=%s",
                        guild_id,
                        failure_seen,
                        exc_info=True,
                    )
                    if failure_seen >= int(getattr(config, "MUSIC_AGENT_MONITOR_UI_FAILURES", 2) or 2):
                        await _marcar_monitor_reconectando(router, guild_id, falhas=failure_seen, erro=ultimo_erro_monitor)
                    deferred = estado_comando_diferido(guild_id)
                    if ultimo_estado_remoto is None and str(deferred.get("status") or "") in {"failed", "expired"}:
                        if not _sessao_local_exige_monitor(router, guild_id):
                            state_now = router.get_state(guild_id)
                            detail = str(deferred.get("last_error") or ultimo_erro_monitor or "Conexão do player indisponível")[:300]
                            setter = getattr(router, "_set_current_status", None)
                            if callable(setter):
                                setter(state_now, "error")
                            else:
                                state_now.current_status = "error"
                            state_now.current_status_detail = detail
                            state_now.agent_deferred_command_status = str(deferred.get("status") or "")
                            state_now.agent_deferred_command_attempts = max(0, int(deferred.get("attempts") or 0))
                            state_now.agent_deferred_command_error = detail
                            updater = getattr(router, "update_panel", None)
                            if callable(updater) and getattr(state_now, "now_message", None) is not None:
                                try:
                                    await updater(guild_id, create=False)
                                except Exception:
                                    logger.debug("[music/agent] painel de falha diferida não pôde ser atualizado | guild=%s", guild_id, exc_info=True)
                            desvincular_guild_worker(guild_id)
                            return
                    rebind_every = max(1, int(getattr(config, "MUSIC_AGENT_MONITOR_REBIND_FAILURES", 4) or 4))
                    if failure_seen % rebind_every == 0:
                        desvincular_guild_worker(guild_id)
                    max_failures = max(1, int(getattr(config, "MUSIC_AGENT_MONITOR_MAX_FAILURES", 30) or 30))
                    if failure_seen >= max_failures:
                        if not _sessao_local_exige_monitor(router, guild_id):
                            # Sem qualquer evidência de sessão, não deixe um watcher
                            # órfão vivo para sempre. Sessões ativas, porém, nunca são
                            # abandonadas apenas porque a rota ficou indisponível.
                            desvincular_guild_worker(guild_id)
                            return
                        if failure_seen == max_failures:
                            logger.warning(
                                "[music/agent] monitor em modo degradado; continuará tentando | guild=%s falhas=%s",
                                guild_id,
                                failure_seen,
                            )
                            desvincular_guild_worker(guild_id)
                    continue

                if not bool(payload.get("ok", True)) or (payload.get("available") is False and payload.get("error")):
                    failure_seen = min(1_000_000, failure_seen + 1)
                    ultimo_erro_monitor = str(payload.get("error") or "agente indisponível")[:260]
                    revisao_remota = ""
                    if failure_seen >= int(getattr(config, "MUSIC_AGENT_MONITOR_UI_FAILURES", 2) or 2):
                        await _marcar_monitor_reconectando(router, guild_id, falhas=failure_seen, erro=ultimo_erro_monitor)
                    deferred = estado_comando_diferido(guild_id)
                    if ultimo_estado_remoto is None and str(deferred.get("status") or "") in {"failed", "expired"}:
                        if not _sessao_local_exige_monitor(router, guild_id):
                            state_now = router.get_state(guild_id)
                            detail = str(deferred.get("last_error") or ultimo_erro_monitor or "Conexão do player indisponível")[:300]
                            setter = getattr(router, "_set_current_status", None)
                            if callable(setter):
                                setter(state_now, "error")
                            else:
                                state_now.current_status = "error"
                            state_now.current_status_detail = detail
                            state_now.agent_deferred_command_status = str(deferred.get("status") or "")
                            state_now.agent_deferred_command_attempts = max(0, int(deferred.get("attempts") or 0))
                            state_now.agent_deferred_command_error = detail
                            updater = getattr(router, "update_panel", None)
                            if callable(updater) and getattr(state_now, "now_message", None) is not None:
                                try:
                                    await updater(guild_id, create=False)
                                except Exception:
                                    logger.debug("[music/agent] painel de falha diferida não pôde ser atualizado | guild=%s", guild_id, exc_info=True)
                            desvincular_guild_worker(guild_id)
                            return
                    rebind_every = max(1, int(getattr(config, "MUSIC_AGENT_MONITOR_REBIND_FAILURES", 4) or 4))
                    if failure_seen % rebind_every == 0:
                        desvincular_guild_worker(guild_id)
                    max_failures = max(1, int(getattr(config, "MUSIC_AGENT_MONITOR_MAX_FAILURES", 30) or 30))
                    if failure_seen >= max_failures:
                        if not _sessao_local_exige_monitor(router, guild_id):
                            desvincular_guild_worker(guild_id)
                            return
                        if failure_seen == max_failures:
                            logger.warning(
                                "[music/agent] monitor em modo degradado; continuará tentando | guild=%s falhas=%s erro=%s",
                                guild_id,
                                failure_seen,
                                ultimo_erro_monitor or "-",
                            )
                            desvincular_guild_worker(guild_id)
                    continue

                recovered_after_failures = failure_seen > 0
                state_success = router.get_state(guild_id)
                state_success.agent_monitor_last_success_at = loop.time()
                failure_seen = 0
                if recovered_after_failures:
                    logger.info(
                        "[music/agent] monitor recuperado; forçando reconciliação completa | guild=%s erro_anterior=%s",
                        guild_id,
                        ultimo_erro_monitor or "-",
                    )
                _limpar_auditoria_monitor(router, guild_id, recovered=recovered_after_failures)

                if bool(payload.get("unchanged")) and revisao_remota:
                    if ultimo_estado_remoto:
                        try:
                            schedule_playlist_refill_if_needed(router, guild_id, ultimo_estado_remoto)
                        except Exception:
                            logger.warning("[music/agent] refill falhou sem encerrar monitor | guild=%s", guild_id, exc_info=True)
                        local_status = str(getattr(router.get_state(guild_id), "current_status", "") or "").lower()
                        if recovered_after_failures or local_status == "reconnecting":
                            try:
                                await router.sync_music_agent_state(
                                    guild_id,
                                    None,
                                    ultimo_estado_remoto,
                                    voice_channel_id=voice_channel_id,
                                    text_channel_id=text_channel_id,
                                    queued=False,
                                    create_panel=True,
                                )
                                router.get_state(guild_id).agent_monitor_last_full_sync_at = loop.time()
                            except Exception:
                                revisao_remota = ""
                                logger.warning("[music/agent] resync pós-recovery falhou; monitor continuará | guild=%s", guild_id, exc_info=True)
                    idle_seen = 0 if status_hint in {"preparing", "starting", "playing", "paused", "queued", "resolving"} else idle_seen
                    continue

                remote = estado_da_guild_no_payload(payload, guild_id)
                if not remote:
                    # Ausência transitória da guild no payload não prova término da
                    # sessão. Só encerre quando também não houver evidência local.
                    if _sessao_local_exige_monitor(router, guild_id):
                        failure_seen = min(1_000_000, failure_seen + 1)
                        revisao_remota = ""
                        if failure_seen >= int(getattr(config, "MUSIC_AGENT_MONITOR_UI_FAILURES", 2) or 2):
                            await _marcar_monitor_reconectando(
                                router, guild_id, falhas=failure_seen, erro="estado da guild ausente no snapshot remoto"
                            )
                        continue
                    idle_seen += 1
                    status_hint = "idle"
                    if idle_seen >= 4:
                        desvincular_guild_worker(guild_id)
                        return
                    continue

                ultimo_estado_remoto = remote
                try:
                    schedule_playlist_refill_if_needed(router, guild_id, remote)
                except Exception:
                    logger.warning("[music/agent] refill falhou sem encerrar monitor | guild=%s", guild_id, exc_info=True)

                status = str(remote.get("status") or "").lower()
                status_hint = status
                nova_assinatura = _assinatura_painel_remoto(remote)
                nova_revisao = _revisao_estado_remoto(remote)
                agora = loop.time()
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
                remote_identity = _identidade_reproducao_remota(remote)
                local_identity = _identidade_reproducao_local(estado_local)
                identidade_divergente = bool(
                    remote_identity != local_identity
                    and (remote_identity[0] >= 0 or remote_identity[1] or local_identity[0] >= 0 or local_identity[1])
                )
                if identidade_divergente:
                    logger.info(
                        "[music/agent] espelho stale detectado; forçando sync | guild=%s local=%s remoto=%s",
                        guild_id,
                        local_identity,
                        remote_identity,
                    )
                precisa_sincronizar = bool(
                    recovered_after_failures
                    or identidade_divergente
                    or atualizar_painel
                    or not revisao_inalterada
                    or not assinatura_inalterada
                )
                sync_ok = True
                if precisa_sincronizar:
                    try:
                        await router.sync_music_agent_state(
                            guild_id,
                            None,
                            remote,
                            voice_channel_id=voice_channel_id,
                            text_channel_id=text_channel_id,
                            queued=False,
                            create_panel=atualizar_painel or identidade_divergente or recovered_after_failures,
                        )
                        synced_state = router.get_state(guild_id)
                        synced_state.agent_monitor_last_full_sync_at = loop.time()
                        if getattr(synced_state, "current", None) is not None and (
                            recovered_after_failures or identidade_divergente
                        ):
                            marker = getattr(router, "_mark_voice_status_track_change", None)
                            scheduler = getattr(router, "_schedule_voice_status_track_sync", None)
                            if callable(marker):
                                marker(synced_state)
                            if callable(scheduler):
                                scheduler(
                                    guild_id,
                                    repeat_after=0.0,
                                    reason="agent_monitor_reconcile",
                                )
                    except Exception as exc:
                        sync_ok = False
                        revisao_remota = ""
                        logger.warning(
                            "[music/agent] sync local falhou; snapshot será tentado novamente | guild=%s erro=%s",
                            guild_id,
                            f"{type(exc).__name__}: {exc}"[:220],
                            exc_info=True,
                        )
                if sync_ok:
                    assinatura_painel = nova_assinatura
                    if nova_revisao:
                        revisao_remota = nova_revisao
                    if (atualizar_painel or identidade_divergente or recovered_after_failures) and getattr(router.get_state(guild_id), "now_message", None) is not None:
                        ultimo_refresh_painel = agora

                has_current = isinstance(remote.get("current"), dict) and bool(remote.get("current"))
                idle_seen = idle_seen + 1 if status in {"idle", "stopped", "failed", "error"} and not has_current else 0
                if idle_seen >= 3:
                    desvincular_guild_worker(guild_id)
                    return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            restart_after_failure = True
            logger.exception(
                "[music/agent] monitor sofreu falha inesperada; será relançado se a sessão continuar | guild=%s erro=%s",
                guild_id,
                f"{type(exc).__name__}: {exc}"[:220],
            )
        finally:
            st = router.get_state(guild_id)
            if getattr(st, "agent_monitor_task", None) is asyncio.current_task():
                st.agent_monitor_task = None
            if restart_after_failure and _sessao_local_exige_monitor(router, guild_id):
                st.agent_monitor_restart_count = int(getattr(st, "agent_monitor_restart_count", 0) or 0) + 1
                try:
                    loop = asyncio.get_running_loop()
                    loop.call_later(
                        1.0,
                        lambda: iniciar_monitor_music_agent(
                            router,
                            guild_id,
                            voice_channel_id=voice_channel_id,
                            text_channel_id=text_channel_id,
                            callback_conclusao=callback_conclusao,
                        ),
                    )
                except RuntimeError:
                    pass

    try:
        task = asyncio.create_task(_runner())
        if callable(callback_conclusao):
            task.add_done_callback(callback_conclusao)
        else:
            task.add_done_callback(lambda done: done.exception() if not done.cancelled() else None)
        state.agent_monitor_task = task
    except RuntimeError:
        state.agent_monitor_task = None
