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
_CHANNEL_PREPARE_TIMEOUT = 2.0
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


def channel_bitrate_hints(prepared: dict) -> dict:
    """Only a confirmed edit can ask the agent to observe its channel cache."""
    if (prepared.get("state") == "ready" and prepared.get("bitrate_bps", 0) > 0
            and prepared.get("channel_id", 0) > 0):
        return {"channel_bitrate_hint_bps": int(prepared["bitrate_bps"]),
                "channel_bitrate_hint_channel_id": int(prepared["channel_id"])}
    return {}


def _music_channel_policy_lock(router, guild_id: int):
    locks = getattr(router, "_music_channel_policy_locks", None)
    if not isinstance(locks, dict):
        locks = {}
        router._music_channel_policy_locks = locks
    return locks.setdefault(int(guild_id), asyncio.Lock())


async def prepare_music_channel(router, *, guild_id: int, voice_channel_id: int,
                                generation: int | None = None, operation_id: str = "") -> dict:
    """Coalesce the existing temporary bitrate policy before music preparation.

    This never changes source/volume and never trusts a desired bitrate as an
    accomplished edit. The returned hint is advisory: the agent must observe
    its own Discord channel cache before using the new capacity.
    """
    guild_id, voice_channel_id = int(guild_id), int(voice_channel_id)
    empty = {"state": "skipped", "channel_id": voice_channel_id, "bitrate_bps": 0, "changed": False}
    boost = getattr(router, "_boost_auto_bitrate_for_music", None)
    bot = getattr(router, "bot", None)
    if (not callable(boost) or not callable(getattr(bot, "get_guild", None))
            or not guild_id or not voice_channel_id):
        return empty
    current_generation = getattr(router, "current_music_operation_generation", None)
    if not callable(current_generation):
        return empty
    if generation is None:
        generation = int(current_generation(guild_id))
    if int(current_generation(guild_id)) != generation:
        return {**empty, "state": "cancelled"}
    guild = bot.get_guild(guild_id)
    if guild is None:
        return empty
    channel = guild.get_channel(voice_channel_id) or bot.get_channel(voice_channel_id)
    if (channel is None or int(getattr(channel, "id", 0) or 0) != voice_channel_id
            or int(getattr(getattr(channel, "guild", None), "id", guild_id) or 0) != guild_id):
        return empty
    records = getattr(router, "_music_channel_preparations", None)
    if not isinstance(records, dict):
        records = {}
        router._music_channel_preparations = records
    now = time.monotonic()
    for old_key, old in list(records.items()):
        if (old.get("task") is not None and old["task"].done() and not old.get("owners")
                and now - old.get("started", now) > 30.0):
            records.pop(old_key, None)
    key = (guild_id, voice_channel_id)
    record = records.get(key)
    reusable_done = True
    if record is not None and record["task"].done():
        old_result = {}
        with contextlib.suppress(asyncio.CancelledError, Exception):
            old_result = record["task"].result()
        state = router.get_state(guild_id)
        reusable_done = bool(
            old_result.get("state") == "ready"
            and int(getattr(state, "auto_bitrate_channel_id", 0) or 0) == voice_channel_id
            and int(getattr(state, "auto_bitrate_boosted", 0) or 0) == old_result.get("bitrate_bps")
        )
    if (record is None or (record.get("generation") != generation and record["task"].done())
            or not reusable_done
            or (record["task"].done() and not record.get("owners")
                and now - record.get("started", now) > 5.0)):
        record = {"generation": generation, "owners": set(), "committed": False,
                  "started": now, "cleanup_needed": False, "task": None}
        records[key] = record

        async def apply_policy() -> dict:
            # One persisted record belongs to the guild, not to an individual
            # channel. Serialize edits/restores even across channel changes.
            async with _music_channel_policy_lock(router, guild_id):
                if int(current_generation(guild_id)) != generation:
                    return {**empty, "state": "cancelled"}
                state = router.get_state(guild_id)
                loaded_record = False
                if not int(getattr(state, "auto_bitrate_channel_id", 0) or 0):
                    load_record = getattr(router, "_load_auto_bitrate_record_into_state", None)
                    if callable(load_record):
                        loaded_record = bool(load_record(guild_id, state))
                previous_channel = int(getattr(state, "auto_bitrate_channel_id", 0) or 0)
                policy_channel = channel
                if previous_channel and (previous_channel != voice_channel_id or loaded_record):
                    fetch = getattr(guild, "fetch_channel", None) or getattr(bot, "fetch_channel", None)
                    restore = getattr(router, "_restore_auto_bitrate_for_state", None)
                    if not callable(fetch) or not callable(restore):
                        return empty
                    try:
                        fresh = await fetch(previous_channel)
                    except Exception:
                        return empty  # leave persisted intent; play can use PCM
                    if (int(current_generation(guild_id)) != generation
                            or int(getattr(fresh, "id", 0) or 0) != previous_channel
                            or int(getattr(getattr(fresh, "guild", None), "id", guild_id) or 0) != guild_id):
                        return {**empty, "state": "cancelled"}
                    if (previous_channel == voice_channel_id
                            and int(getattr(fresh, "bitrate", 0) or 0) == int(getattr(state, "auto_bitrate_boosted", 0) or 0)):
                        policy_channel = fresh  # recovered, freshly confirmed edit
                    else:
                        await restore(guild, state, reason="preplay_channel_change", channel_hint=fresh)
                        if int(getattr(state, "auto_bitrate_channel_id", 0) or 0) == previous_channel:
                            return empty  # restoration failed; never overwrite it
                if int(current_generation(guild_id)) != generation:
                    return {**empty, "state": "cancelled"}
                before = tuple(getattr(state, field, None) for field in (
                    "auto_bitrate_channel_id", "auto_bitrate_original", "auto_bitrate_boosted"))
                await boost(guild, policy_channel, state)
                after = tuple(getattr(state, field, None) for field in (
                    "auto_bitrate_channel_id", "auto_bitrate_original", "auto_bitrate_boosted"))
                confirmed = int(after[2] or 0) if int(after[0] or 0) == voice_channel_id else 0
                record["cleanup_needed"] = bool(confirmed and int(after[1] or 0) < confirmed)
                changed = before != after and record["cleanup_needed"]
                # Matching state proves an older successful edit only while
                # the channel cache agrees; never hint a merely desired target.
                if not changed and int(getattr(policy_channel, "bitrate", 0) or 0) != confirmed:
                    confirmed = 0
                return {"state": "ready" if confirmed else "skipped", "channel_id": voice_channel_id,
                        "bitrate_bps": confirmed, "changed": bool(changed)}

        async def run() -> dict:
            try:
                # The deadline includes waiting for the guild lock, fetching
                # an old channel, restoring it and editing the new channel.
                return await asyncio.wait_for(apply_policy(), timeout=_CHANNEL_PREPARE_TIMEOUT)
            except asyncio.CancelledError:
                record["cleanup_needed"] = True
                raise
            except asyncio.TimeoutError:
                # The persisted intent may outlive a lost HTTP response.
                record["cleanup_needed"] = True
                return {**empty, "state": "timeout"}
            except Exception:
                logger.debug("[music/start] ajuste antecipado indisponível | guild=%s channel=%s", guild_id, voice_channel_id, exc_info=True)
                return {**empty, "state": "failed"}

        record["task"] = asyncio.create_task(run(), name=f"music-channel-prepare-{guild_id}-{voice_channel_id}")
        record["task"].add_done_callback(lambda task: None if task.cancelled() else task.exception())
    owner = str(operation_id or f"task:{id(asyncio.current_task())}")
    record["owners"].add(owner)
    try:
        result = await asyncio.wait_for(asyncio.shield(record["task"]), timeout=_CHANNEL_PREPARE_TIMEOUT + 0.1)
    except asyncio.TimeoutError:
        return {**empty, "state": "timeout"}
    if int(current_generation(guild_id)) != generation:
        return {**empty, "state": "cancelled"}
    if result.get("state") == "cancelled" and record["task"].done():
        # A newer generation may share an edit already in flight. If the old
        # task had not begun its edit, retry once under the still-current owner.
        if records.get(key) is record:
            records.pop(key, None)
        return await prepare_music_channel(router, guild_id=guild_id, voice_channel_id=voice_channel_id,
                                           generation=generation, operation_id=operation_id)
    return dict(result)


async def finish_music_channel_preparation(router, *, guild_id: int, voice_channel_id: int,
                                           operation_id: str, committed: bool) -> None:
    """Release a play's ownership; a failed owner cannot undo another play."""
    records = getattr(router, "_music_channel_preparations", None)
    if not isinstance(records, dict):
        return
    key = (int(guild_id), int(voice_channel_id))
    record = records.get(key)
    if record is None:
        return
    record["owners"].discard(str(operation_id))
    record["committed"] = bool(record["committed"] or committed)
    if record["owners"] or record["committed"] or record.get("cleanup_running"):
        return
    record["cleanup_running"] = True
    try:
        with contextlib.suppress(Exception):
            await asyncio.wait_for(asyncio.shield(record["task"]), timeout=_CHANNEL_PREPARE_TIMEOUT + 0.1)

        async def cleanup() -> None:
            async with _music_channel_policy_lock(router, int(guild_id)):
                state = router.get_state(int(guild_id))
                if not int(getattr(state, "auto_bitrate_channel_id", 0) or 0):
                    load_record = getattr(router, "_load_auto_bitrate_record_into_state", None)
                    if callable(load_record):
                        load_record(int(guild_id), state)
                another_owner = any(other_key[0] == int(guild_id) and other is not record and other.get("owners")
                                    for other_key, other in records.items())
                if (records.get(key) is not record or record["owners"] or record["committed"] or another_owner
                        or getattr(state, "current", None) is not None or not record["cleanup_needed"]
                        or int(getattr(state, "auto_bitrate_channel_id", 0) or 0) not in {0, int(voice_channel_id)}):
                    return
                bot = getattr(router, "bot", None)
                guild = bot.get_guild(int(guild_id)) if bot is not None else None
                restore = getattr(router, "_restore_auto_bitrate_for_state", None)
                fetch = getattr(guild, "fetch_channel", None) or getattr(bot, "fetch_channel", None)
                if guild is None or not callable(restore) or not callable(fetch):
                    return
                generation = router.current_music_operation_generation(int(guild_id))
                try:
                    # Never restore from the pre-edit cached channel object.
                    fresh = await fetch(int(voice_channel_id))
                except Exception:
                    return  # persisted policy remains for startup reconcile
                another_owner = any(other_key[0] == int(guild_id) and other is not record and other.get("owners")
                                    for other_key, other in records.items())
                if (records.get(key) is not record or record["owners"] or record["committed"] or another_owner
                        or getattr(state, "current", None) is not None
                        or router.current_music_operation_generation(int(guild_id)) != generation
                        or int(getattr(fresh, "id", 0) or 0) != int(voice_channel_id)
                        or int(getattr(getattr(fresh, "guild", None), "id", guild_id) or 0) != int(guild_id)):
                    return
                await restore(guild, state, reason="preplay_cancelled", channel_hint=fresh)

        await asyncio.wait_for(cleanup(), timeout=_CHANNEL_PREPARE_TIMEOUT)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.debug("[music/start] restauração antecipada adiada | guild=%s channel=%s", guild_id, voice_channel_id, exc_info=True)
    finally:
        record["cleanup_running"] = False
        if records.get(key) is record and not record["owners"] and not record["committed"]:
            records.pop(key, None)


async def prepare_voice(router, command, *, guild_id: int, voice_channel_id: int,
                        generation: int, prepare_id: str, trace_id: str,
                        worker_validation=None) -> None:
    if router.current_music_operation_generation(guild_id) != generation:
        return
    try:
        await asyncio.gather(
            prepare_music_channel(router, guild_id=guild_id, voice_channel_id=voice_channel_id,
                                  generation=generation, operation_id=trace_id or prepare_id),
            command("prepare_voice", guild_id=guild_id, voice_channel_id=voice_channel_id,
                    prepare_id=prepare_id, trace_id=trace_id, lease_seconds=45,
                    timeout_seconds=3.0, worker_validation=worker_validation),
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.debug("[music/start] voz antecipada indisponível | guild=%s erro=%s", guild_id, exc)


async def cancel_voice_preparation(command, guild_id: int, prepare_id: str, task, *, router=None) -> None:
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
    if router is not None:
        records = getattr(router, "_music_channel_preparations", None)
        if isinstance(records, dict):
            for (owner_guild, channel_id), record in list(records.items()):
                if int(owner_guild) == int(guild_id) and str(prepare_id) in record.get("owners", ()):
                    await finish_music_channel_preparation(
                        router, guild_id=int(guild_id), voice_channel_id=int(channel_id),
                        operation_id=str(prepare_id), committed=False)


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
