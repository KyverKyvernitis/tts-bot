from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Mapping

from cogs.musica import configuracao as config

from ..nucleo.modelos import MusicTrack
from .protocolo import montar_comando, montar_consulta_status
from .modelos import (
    MUSIC_WORKER_ENGINE_UNAVAILABLE_MESSAGE,
    MUSIC_WORKER_UNAVAILABLE_MESSAGE,
    MusicWorkerEngineUnavailable,
    MusicWorkerUnavailable,
)
from .selecao import require_music_worker_available_async
from .roteamento import (
    DestinoWorker,
    destino_vinculado,
    resolver_destino_worker,
    vincular_guild_worker,
    desvincular_guild_worker,
)
from .transporte_http import post_json_worker
from .utilitarios import _phone_worker_base_url

logger = logging.getLogger(__name__)

# Só comandos que iniciam/estendem playback podem sobreviver alguns segundos a
# uma queda de rota. TTS e controles são temporais: reproduzi-los atrasados seria
# pior do que falhar explicitamente.
_DEFERRED_PLAY_ACTIONS = {"play", "enqueue_many"}
_DEFERRED_TASKS: dict[int, asyncio.Task] = {}
_DEFERRED_STATE: dict[int, dict[str, Any]] = {}


def _normalizar_acao(action: object) -> str:
    return str(action or "").strip().lower().replace("-", "_")


def _destino_configurado_sem_healthcheck() -> DestinoWorker | None:
    """Retorna o endpoint configurado mesmo durante uma queda de rede.

    A seleção normal valida health/registry. Para um play diferido isso criava
    um paradoxo: justamente quando a rota estava fora, não havia destino para
    guardar e reenviar o comando. Aqui usamos apenas configuração já existente;
    o retry posterior continua validando/reselecionando quando a rede voltar.
    """
    base = _phone_worker_base_url()
    token = str(getattr(config, "PHONE_WORKER_TOKEN", "") or "").strip()
    if not base or not token:
        return None
    worker_id = str(getattr(config, "PHONE_WORKER_ID", "") or "phone-worker-configured").strip()
    return DestinoWorker(
        worker_id=worker_id,
        name="Phone Worker configurado",
        base=base,
        token=token,
    )


def _erro_transporte_transitorio(exc: BaseException) -> bool:
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, (asyncio.TimeoutError, TimeoutError, ConnectionError, OSError)):
            return True
        text = f"{type(current).__name__} {current}".lower()
        if any(
            token in text
            for token in (
                "connection",
                "connect",
                "closing transport",
                "refused",
                "timed out",
                "timeout",
                "network is unreachable",
                "no route to host",
                "temporary failure in name resolution",
                "server disconnected",
            )
        ):
            return True
        current = current.__cause__ or current.__context__
    return False


def _fingerprint_comando(payload: Mapping[str, Any]) -> tuple[Any, ...]:
    track = payload.get("track") if isinstance(payload.get("track"), Mapping) else {}
    tracks = payload.get("tracks") if isinstance(payload.get("tracks"), list) else []
    first_many = tracks[0] if tracks and isinstance(tracks[0], Mapping) else {}
    principal = track or first_many
    return (
        _normalizar_acao(payload.get("action")),
        int(payload.get("guild_id") or 0),
        int(payload.get("voice_channel_id") or 0),
        str(payload.get("query") or "").strip(),
        str(principal.get("webpage_url") or principal.get("original_url") or principal.get("title") or "").strip(),
        len(tracks),
    )


def estado_comando_diferido(guild_id: int) -> dict[str, Any]:
    """Snapshot de auditoria do último play diferido desta guild."""
    try:
        guild = int(guild_id or 0)
    except Exception:
        return {}
    item = _DEFERRED_STATE.get(guild)
    if not isinstance(item, dict):
        return {}
    # Resultados finais ficam alguns minutos para o monitor poder explicar por
    # que um painel saiu de reconnecting para erro/recuperado.
    finished_at = float(item.get("finished_at") or 0.0)
    if finished_at and time.monotonic() - finished_at > 180.0:
        _DEFERRED_STATE.pop(guild, None)
        return {}
    return dict(item)


def _estado_sintetico_diferido(payload: Mapping[str, Any], *, erro: str, attempts: int = 0) -> dict[str, Any]:
    track = payload.get("track") if isinstance(payload.get("track"), Mapping) else None
    if track is None:
        tracks = payload.get("tracks") if isinstance(payload.get("tracks"), list) else []
        track = tracks[0] if tracks and isinstance(tracks[0], Mapping) else None
    return {
        "status": "starting",
        "confirmed_playing": False,
        "voice_connected": False,
        "player_present": False,
        "current": dict(track) if isinstance(track, Mapping) else None,
        "queue": [],
        "queue_size": 0,
        "last_action": _normalizar_acao(payload.get("action")),
        "last_event": "command_delivery_deferred",
        "last_error": "",
        "deferred_delivery": True,
        "deferred_command_id": str(payload.get("command_id") or ""),
        "deferred_attempts": max(0, int(attempts or 0)),
        "deferred_last_transport_error": str(erro or "")[:220],
    }


def _cancelar_comando_diferido(guild_id: int, *, reason: str = "superseded") -> None:
    try:
        guild = int(guild_id or 0)
    except Exception:
        return
    task = _DEFERRED_TASKS.pop(guild, None)
    if isinstance(task, asyncio.Task) and not task.done():
        task.cancel()
    state = _DEFERRED_STATE.get(guild)
    if isinstance(state, dict) and state.get("status") == "pending":
        state["status"] = "cancelled"
        state["reason"] = reason
        state["finished_at"] = time.monotonic()


async def cancelar_comandos_diferidos() -> None:
    tasks = [task for task in _DEFERRED_TASKS.values() if isinstance(task, asyncio.Task) and not task.done()]
    _DEFERRED_TASKS.clear()
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def _resolver_destino_retry(original: DestinoWorker, guild_id: int) -> DestinoWorker:
    """Atualiza endpoint apenas se continuar sendo o mesmo worker lógico."""
    try:
        selection = await require_music_worker_available_async()
        candidate = resolver_destino_worker(selection, guild_id=guild_id, preferir_vinculo=False)
    except Exception:
        candidate = None
    if candidate is None:
        return original
    original_id = str(original.worker_id or "").strip()
    candidate_id = str(candidate.worker_id or "").strip()
    if original_id and candidate_id and candidate_id != original_id:
        return original
    return candidate


def _agendar_comando_diferido(
    *,
    destino: DestinoWorker,
    payload: dict[str, Any],
    total_timeout: float,
    erro_inicial: BaseException,
) -> dict[str, Any]:
    guild_id = int(payload.get("guild_id") or 0)
    fingerprint = _fingerprint_comando(payload)
    existing = _DEFERRED_STATE.get(guild_id)
    existing_task = _DEFERRED_TASKS.get(guild_id)
    if (
        guild_id > 0
        and isinstance(existing, dict)
        and existing.get("status") == "pending"
        and existing.get("fingerprint") == fingerprint
        and isinstance(existing_task, asyncio.Task)
        and not existing_task.done()
    ):
        return {
            "ok": True,
            "queued": False,
            "deferred": True,
            "coalesced": True,
            "state": _estado_sintetico_diferido(
                payload,
                erro=str(existing.get("last_error") or erro_inicial),
                attempts=int(existing.get("attempts") or 0),
            ),
        }

    if guild_id > 0:
        _cancelar_comando_diferido(guild_id, reason="superseded_by_new_play")

    window = max(10.0, float(getattr(config, "MUSIC_AGENT_DEFERRED_PLAY_SECONDS", 120.0) or 120.0))
    retry_max = max(0.25, float(getattr(config, "MUSIC_AGENT_DEFERRED_PLAY_RETRY_MAX_SECONDS", 5.0) or 5.0))
    command_id = str(payload.get("command_id") or "")
    state = {
        "status": "pending",
        "action": _normalizar_acao(payload.get("action")),
        "command_id": command_id,
        "fingerprint": fingerprint,
        "attempts": 0,
        "started_at": time.monotonic(),
        "deadline": time.monotonic() + window,
        "last_error": f"{type(erro_inicial).__name__}: {erro_inicial}"[:260],
        "worker_id": destino.worker_id,
        "base": destino.base,
    }
    if guild_id > 0:
        _DEFERRED_STATE[guild_id] = state
        desvincular_guild_worker(guild_id)

    async def _runner() -> None:
        current_destino = destino
        attempt = 0
        delay = 0.35
        try:
            while time.monotonic() < float(state["deadline"]):
                await asyncio.sleep(delay)
                attempt += 1
                state["attempts"] = attempt
                current_destino = await _resolver_destino_retry(current_destino, guild_id)
                state["base"] = current_destino.base
                state["worker_id"] = current_destino.worker_id
                try:
                    data = await post_json_worker(
                        url=f"{current_destino.base}/task",
                        token=current_destino.token,
                        payload=payload,
                        timeout_seconds=min(max(3.0, total_timeout), 24.0),
                        max_erro=400,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    state["last_error"] = f"{type(exc).__name__}: {exc}"[:260]
                    logger.info(
                        "[music/agent] retry de comando diferido falhou | guild=%s action=%s tentativa=%s worker=%s erro=%s",
                        guild_id,
                        state["action"],
                        attempt,
                        current_destino.worker_id or current_destino.name,
                        state["last_error"],
                    )
                    if not _erro_transporte_transitorio(exc):
                        state["status"] = "failed"
                        state["finished_at"] = time.monotonic()
                        return
                    delay = min(retry_max, max(0.35, delay * 1.7))
                    continue

                if not isinstance(data, dict) or data.get("ok") is False:
                    state["last_error"] = str((data or {}).get("error") if isinstance(data, dict) else "resposta inválida")[:260]
                    state["status"] = "failed"
                    state["finished_at"] = time.monotonic()
                    logger.warning(
                        "[music/agent] comando diferido rejeitado | guild=%s action=%s tentativa=%s erro=%s",
                        guild_id,
                        state["action"],
                        attempt,
                        state["last_error"],
                    )
                    return

                if guild_id > 0:
                    vincular_guild_worker(guild_id, current_destino)
                state["status"] = "delivered"
                state["result"] = dict(data)
                state["finished_at"] = time.monotonic()
                logger.info(
                    "[music/agent] comando diferido entregue | guild=%s action=%s tentativa=%s worker=%s command_id=%s",
                    guild_id,
                    state["action"],
                    attempt,
                    current_destino.worker_id or current_destino.name,
                    command_id[:12],
                )
                return

            state["status"] = "expired"
            state["finished_at"] = time.monotonic()
            logger.warning(
                "[music/agent] comando diferido expirou | guild=%s action=%s tentativas=%s command_id=%s erro=%s",
                guild_id,
                state["action"],
                attempt,
                command_id[:12],
                state.get("last_error") or "-",
            )
        finally:
            current = asyncio.current_task()
            if _DEFERRED_TASKS.get(guild_id) is current:
                _DEFERRED_TASKS.pop(guild_id, None)

    if guild_id > 0:
        task = asyncio.create_task(_runner(), name=f"music-deferred-{guild_id}-{command_id[:8]}")
        _DEFERRED_TASKS[guild_id] = task
    logger.warning(
        "[music/agent] comando de play ficou pendente para autorrecuperação | guild=%s action=%s worker=%s janela=%.1fs command_id=%s erro=%s",
        guild_id,
        state["action"],
        destino.worker_id or destino.name,
        window,
        command_id[:12],
        state["last_error"],
    )
    return {
        "ok": True,
        "queued": False,
        "deferred": True,
        "state": _estado_sintetico_diferido(payload, erro=state["last_error"], attempts=0),
    }


async def music_agent_command(
    action: str,
    *,
    guild_id: int = 0,
    voice_channel_id: int = 0,
    text_channel_id: int = 0,
    query: str = "",
    track: MusicTrack | Mapping[str, Any] | None = None,
    requester_id: int = 0,
    requester_name: str = "",
    timeout_seconds: float | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """Send a command to the phone-worker Music Agent through /task.

    Playback-start commands are idempotent and can be deferred briefly across a
    network/Tailscale outage. This closes the old gap where the UI kept polling
    "reconnecting" forever although the original play POST never reached the
    worker.
    """
    payload = montar_comando(
        action,
        guild_id=guild_id,
        voice_channel_id=voice_channel_id,
        text_channel_id=text_channel_id,
        query=query,
        track=track,
        requester_id=requester_id,
        requester_name=requester_name,
        timeout_seconds=timeout_seconds,
        **extra,
    )
    action_normalized = _normalizar_acao(action)

    # Repetir exatamente o mesmo `_p` enquanto a rota está fora não deve criar
    # vários plays que disparem juntos quando o telefone voltar.
    pending = estado_comando_diferido(guild_id) if guild_id and action_normalized in _DEFERRED_PLAY_ACTIONS else {}
    if pending.get("status") == "pending" and pending.get("fingerprint") == _fingerprint_comando(payload):
        return {
            "ok": True,
            "queued": False,
            "deferred": True,
            "coalesced": True,
            "state": _estado_sintetico_diferido(
                payload,
                erro=str(pending.get("last_error") or "Phone Worker reconectando"),
                attempts=int(pending.get("attempts") or 0),
            ),
        }

    if action_normalized in {"stop", "disconnect"} and guild_id:
        _cancelar_comando_diferido(guild_id, reason="manual_stop")

    timeout_headroom = 2.0
    if action_normalized == "tts":
        timeout_headroom = max(3.0, float(getattr(config, "MUSIC_AGENT_TTS_HTTP_HEADROOM_SECONDS", 12.0) or 12.0))
    total_timeout = max(2.0, float(payload["timeout_seconds"]) + timeout_headroom)

    destino = destino_vinculado(guild_id)
    selection = None
    if destino is None:
        try:
            selection = await require_music_worker_available_async()
            destino = resolver_destino_worker(selection, guild_id=guild_id, preferir_vinculo=False)
        except Exception as exc:
            destino = _destino_configurado_sem_healthcheck()
            if action_normalized in _DEFERRED_PLAY_ACTIONS and int(guild_id or 0) > 0 and destino is not None:
                return _agendar_comando_diferido(
                    destino=destino,
                    payload=payload,
                    total_timeout=total_timeout,
                    erro_inicial=exc,
                )
            raise
    if destino is None:
        destino = _destino_configurado_sem_healthcheck()
    if destino is None:
        raise MusicWorkerUnavailable(MUSIC_WORKER_UNAVAILABLE_MESSAGE)
    base = destino.base
    token = destino.token
    try:
        data = await post_json_worker(
            url=f"{base}/task",
            token=token,
            payload=payload,
            timeout_seconds=total_timeout,
            max_erro=400,
        )
    except Exception as exc:
        message = str(exc or "").strip() or MUSIC_WORKER_ENGINE_UNAVAILABLE_MESSAGE
        logger.warning("[music/agent] comando remoto falhou | worker=%s action=%s erro=%s", destino.worker_id or destino.name, action, message)
        if action_normalized in _DEFERRED_PLAY_ACTIONS and int(guild_id or 0) > 0 and _erro_transporte_transitorio(exc):
            return _agendar_comando_diferido(
                destino=destino,
                payload=payload,
                total_timeout=total_timeout,
                erro_inicial=exc,
            )
        # Alguns erros de aiohttp não incluem a palavra "connection" na
        # mensagem (ex.: ClientConnectionResetError: Cannot write to closing
        # transport). Inclua o tipo para não vazar erro técnico ao usuário e
        # para o fluxo tratá-lo como indisponibilidade transitória.
        lower = f"{type(exc).__name__} {message}".lower()
        if (
            "music agent" in lower
            or "configure music_agent" in lower
            or "connection" in lower
            or "connect" in lower
            or "closing transport" in lower
            or "refused" in lower
            or "timeout" in lower
            or "pynacl" in lower
            or "davey" in lower
            or "dependency" in lower
            or "unauthorized" in lower
        ):
            message = "Sistema de música indisponível no momento: O worker está reconectando; tente novamente em alguns segundos"
        raise MusicWorkerEngineUnavailable(message[:260]) from exc
    if data.get("ok") is False:
        message = str(data.get("error") or data.get("message") or MUSIC_WORKER_ENGINE_UNAVAILABLE_MESSAGE).strip()
        lower = message.lower()
        if "music agent" in lower or "configure music_agent" in lower or "sem token" in lower:
            message = str(getattr(config, "MUSIC_AGENT_MISSING_TOKEN_MESSAGE", MUSIC_WORKER_ENGINE_UNAVAILABLE_MESSAGE) or MUSIC_WORKER_ENGINE_UNAVAILABLE_MESSAGE)
        raise MusicWorkerEngineUnavailable(message[:260])
    if int(guild_id or 0) > 0 and action_normalized in {"play", "enqueue_many", "playlist_refill"}:
        if action_normalized in _DEFERRED_PLAY_ACTIONS:
            _cancelar_comando_diferido(int(guild_id), reason="immediate_delivery_succeeded")
        vincular_guild_worker(int(guild_id), destino)
    logger.info("[music/agent] comando remoto enviado | worker=%s action=%s guild=%s", destino.worker_id or destino.name, action, guild_id)
    return data


async def music_agent_status(*, timeout_seconds: float | None = None, guild_id: int = 0, known_revision: str = "") -> dict[str, Any]:
    destino = destino_vinculado(guild_id)
    selection = None
    if destino is None:
        selection = await require_music_worker_available_async()
        destino = resolver_destino_worker(selection, guild_id=guild_id, preferir_vinculo=False)
    if destino is None:
        raise MusicWorkerUnavailable(MUSIC_WORKER_UNAVAILABLE_MESSAGE)
    base = destino.base
    token = destino.token
    payload = montar_consulta_status(
        timeout_seconds=timeout_seconds,
        guild_id=guild_id,
        compact=bool(guild_id),
        known_revision=known_revision,
    )
    total_timeout = max(1.0, float(payload["timeout_seconds"]) + 1.0)
    try:
        data = await post_json_worker(
            url=f"{base}/task",
            token=token,
            payload=payload,
            timeout_seconds=total_timeout,
            max_erro=220,
        )
    except Exception as exc:
        logger.info("[music/agent] status remoto indisponível | worker=%s guild=%s erro=%s", destino.worker_id or destino.name, guild_id, exc)
        return {"ok": False, "available": False, "error": str(exc)}
    data.setdefault("ok", True)
    data.setdefault("available", bool(data.get("discord_ready")))
    return data
