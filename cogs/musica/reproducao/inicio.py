"""Trabalho limitado e medições locais compartilhadas pelo início do play."""
from __future__ import annotations

import asyncio
import contextlib
from datetime import datetime, timezone
import json
import logging
import math
import time
import uuid

from ..metadados.provedores import describe_url
from ..nucleo.modelos import ExtractedBatch, MusicTrack

logger = logging.getLogger(__name__)
_CONTEXT_VALUES = {
    "entrypoint": {"command", "selection", "modal", "attachment", "unknown"},
    "input_kind": {"url", "search", "playlist", "album", "attachment", "unknown"},
    "source_mode": {"archive", "online", "unknown"},
    "voice_state": {"disconnected", "connected", "unknown"},
    "resolve_cache": {"hit", "miss", "unknown"},
    "helper_state": {"cold", "warming", "warm", "closed", "unknown"},
    "queue_state": {"immediate", "queued", "unknown"},
}


def retain_music_task(router, coroutine, *, name: str, limit: int = 32):
    tasks = getattr(router, "_music_fast_start_tasks", None)
    if tasks is None:
        tasks = set()
        router._music_fast_start_tasks = tasks
    if len(tasks) >= limit:
        coroutine.close()
        return None
    task = asyncio.create_task(coroutine, name=name)
    tasks.add(task)

    def completed(finished):
        tasks.discard(finished)
        if not finished.cancelled() and finished.exception() is not None:
            logger.warning("[music/start] trabalho adiado falhou | task=%s erro=%s", name, finished.exception())

    task.add_done_callback(completed)
    return task


def schedule_deferred_learning(router):
    from ..busca.memoria import processar_lotes_link_pendentes
    return retain_music_task(router, asyncio.to_thread(processar_lotes_link_pendentes, limit=2),
                             name="music-learn-after-start")


async def archived_url_batch(query: str, *, guild_id: int, requester_id: int,
                             requester_name: str) -> ExtractedBatch | None:
    profile = describe_url(query)
    if not profile.is_url or profile.resource_type in {"playlist", "album"}:
        return None
    from ..busca.arquivo import archived_track_for_url
    track = await asyncio.to_thread(archived_track_for_url, profile.canonical or query,
                                   guild_id=guild_id, requester_id=requester_id,
                                   requester_name=requester_name)
    return ExtractedBatch(tracks=[track], query=query, is_playlist=False) if track is not None else None


def direct_url_batch(query: str, *, requester_id: int, requester_name: str) -> ExtractedBatch:
    profile = describe_url(query)
    track = MusicTrack(title="YouTube" if profile.is_youtube else "Link",
                       webpage_url=profile.canonical or query, original_url=query,
                       requester_id=requester_id, requester_name=requester_name or "",
                       source="YouTube" if profile.is_youtube else (profile.platform or "link"),
                       extractor="worker-ytdlp")
    return ExtractedBatch(tracks=[track], query=query, is_playlist=False)


async def prepare_voice(router, command, *, guild_id: int, voice_channel_id: int,
                        generation: int, prepare_id: str, trace_id: str,
                        worker_validation=None) -> None:
    if router.current_music_operation_generation(guild_id) != generation:
        return
    try:
        await command("prepare_voice", guild_id=guild_id, voice_channel_id=voice_channel_id,
                      prepare_id=prepare_id, trace_id=trace_id, lease_seconds=45,
                      timeout_seconds=3.0, worker_validation=worker_validation)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.debug("[music/start] voz antecipada indisponível | guild=%s erro=%s", guild_id, exc)


async def cancel_voice_preparation(command, guild_id: int, prepare_id: str, task) -> None:
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=5.0)
    except asyncio.CancelledError:
        if not task.cancelled():
            raise
    except Exception:
        pass
    with contextlib.suppress(Exception):
        await command("cancel_prepare_voice", guild_id=guild_id,
                      prepare_id=prepare_id, timeout_seconds=2.0)


class MusicStartTrace:
    """Não soma etapas paralelas nem compara relógios de máquinas distintas."""

    def __init__(self, guild_id: int, *, entrypoint: str, input_kind: str,
                 started: float | None = None, trace_id: str = "", timing_ms=None):
        self.guild_id = int(guild_id)
        self.trace_id = trace_id or uuid.uuid4().hex
        self.started = time.monotonic() if started is None else started
        self.timings = dict(timing_ms or {})
        self.context = {"entrypoint": entrypoint, "input_kind": input_kind}
        self._attempted = False
        self._finished = False
        self._handed_off = False

    def _emit(self, event: str, **extra) -> None:
        timings = {str(key)[:64]: float(value) for key, value in list(self.timings.items())[:20]
                   if isinstance(value, (int, float)) and not isinstance(value, bool)
                   and 0 <= value <= 86400000 and math.isfinite(float(value))}
        context = {key: value for key, value in self.context.items()
                   if key in _CONTEXT_VALUES and isinstance(value, str) and value in _CONTEXT_VALUES[key]}
        record = {"event": event, "origin": "controller", "guild_id": self.guild_id,
                  "trace_id": self.trace_id, "attempt_id": self.trace_id,
                  "at": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                  "controller_timing_ms": timings, "start_context": context, **extra}
        logger.info("[music-start] %s", json.dumps(record, ensure_ascii=False, separators=(",", ":")))

    def attempt(self) -> None:
        if not self._attempted:
            self._attempted = True
            self._emit("music_start_attempt")

    def dispatch(self) -> None:
        phase = "click" if self.context["entrypoint"] == "selection" else "command"
        self.timings[phase + "_to_dispatch_ms"] = round((time.monotonic() - self.started) * 1000, 2)
        self.attempt()

    def result(self, result: dict, *, confirmed: bool) -> None:
        if result.get("coalesced"):
            self.finish("coalesced", related_trace_id=str(result.get("related_trace_id") or "")[:128])
        elif result.get("cancelled"):
            self.finish("cancelled")
        elif result.get("ok") is False:
            self.finish("failed")
        elif result.get("queued"):
            self.context["queue_state"] = "queued"
            self.finish("queued")
        elif confirmed:
            phase = "click" if self.context["entrypoint"] == "selection" else "command"
            self.timings[phase + "_to_confirmation_ms"] = round((time.monotonic() - self.started) * 1000, 2)
            self.finish("success")
        else:
            # Um ACK de preparação/retry não confirma som. O agente registra
            # o pacote real ou a falha final, mesmo se a resposta HTTP se perder.
            self._handed_off = True

    def search_results(self) -> None:
        self.timings["search_to_results_ms"] = round((time.monotonic() - self.started) * 1000, 2)
        self._handed_off = True
        self._emit("music_search_results")

    def finish(self, outcome: str = "failed", **extra) -> None:
        if self._finished or (self._handed_off and outcome == "failed"):
            return
        self.attempt()
        self._finished = True
        self._emit("music_start_outcome", outcome=outcome, **extra)


def record_deferred_outcome(payload: dict, outcome: str, *, wait_ms: float) -> None:
    """Observe a entrega tardia sem inventar o instante do primeiro pacote."""
    trace_id = str(payload.get("trace_id") or "")[:128]
    if not trace_id:
        return
    context = payload.get("start_context")
    context = context if isinstance(context, dict) else {}
    trace = MusicStartTrace(payload.get("guild_id") or 0,
                            entrypoint=context.get("entrypoint", "unknown"),
                            input_kind=context.get("input_kind", "unknown"),
                            trace_id=trace_id,
                            timing_ms=payload.get("controller_timing_ms"))
    trace.context.update(context)
    trace.timings["deferred_wait_ms"] = max(0.0, wait_ms)
    # A tentativa original já foi registrada pelo fluxo que entregou o play.
    trace._emit("music_start_outcome", outcome=outcome)
