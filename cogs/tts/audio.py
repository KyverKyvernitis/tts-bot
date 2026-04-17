import contextlib
import asyncio
import hashlib
import json
import inspect
import os
import shutil
import tempfile
import time
import logging
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Optional

import discord
import edge_tts
from gtts import gTTS
from gtts.tts import gTTSError

try:
    from google.cloud import texttospeech_v1 as google_texttospeech
except Exception:  # pragma: no cover - dependência opcional em tempo de import
    google_texttospeech = None

import config
from .helpers import validate_voice


GTTS_DEFAULT_LANGUAGE = getattr(config, "GTTS_DEFAULT_LANGUAGE", "pt")
TTS_IDLE_DISCONNECT_SECONDS = int(getattr(config, "TTS_IDLE_DISCONNECT_SECONDS", 240))
TTS_AUDIO_CACHE_SIZE = max(1, int(getattr(config, "TTS_AUDIO_CACHE_SIZE", 128)))
TTS_AUDIO_CACHE_TTL_SECONDS = int(getattr(config, "TTS_AUDIO_CACHE_TTL_SECONDS", 900))
TTS_DEBUG_LOGS = bool(getattr(config, "TTS_DEBUG_LOGS", False))
TTS_WARM_HOLD_SECONDS = float(getattr(config, "TTS_WARM_HOLD_SECONDS", 30))
TTS_QUEUE_MAXSIZE = max(1, int(getattr(config, "TTS_QUEUE_MAXSIZE", 20)))
TTS_SYNTH_CONCURRENCY = max(1, int(getattr(config, "TTS_SYNTH_CONCURRENCY", 3)))
TTS_EDGE_TIMEOUT_SECONDS = max(1.0, float(getattr(config, "TTS_EDGE_TIMEOUT_SECONDS", 10)))
TTS_GTTS_CONCURRENCY = max(1, int(getattr(config, "TTS_GTTS_CONCURRENCY", 1)))
TTS_CACHEABLE_TEXT_MAX_LENGTH = max(64, int(getattr(config, "TTS_CACHEABLE_TEXT_MAX_LENGTH", 320)))
TTS_CACHEABLE_TEXT_HARD_MAX_LENGTH = max(TTS_CACHEABLE_TEXT_MAX_LENGTH, int(getattr(config, "TTS_CACHEABLE_TEXT_HARD_MAX_LENGTH", 1200)))
TTS_LONG_TEXT_CACHE_MIN_REPEATS = max(1, int(getattr(config, "TTS_LONG_TEXT_CACHE_MIN_REPEATS", 2)))
TTS_TEMP_PRUNE_INTERVAL_SECONDS = max(5.0, float(getattr(config, "TTS_TEMP_PRUNE_INTERVAL_SECONDS", 20)))
TTS_BOOT_WARMUP_ENABLED = bool(getattr(config, "TTS_BOOT_WARMUP_ENABLED", True))
TTS_ENGINE_ALERT_COOLDOWN_SECONDS = max(60.0, float(getattr(config, "TTS_ENGINE_ALERT_COOLDOWN_SECONDS", 900)))
TTS_ENGINE_FAILURE_ALERT_THRESHOLD = max(1, int(getattr(config, "TTS_ENGINE_FAILURE_ALERT_THRESHOLD", 3)))
TTS_ENGINE_SLOW_WARN_SECONDS = max(1.0, float(getattr(config, "TTS_ENGINE_SLOW_WARN_SECONDS", 8.0)))
GOOGLE_CLOUD_TTS_LANGUAGE_CODE = str(getattr(config, "GOOGLE_CLOUD_TTS_LANGUAGE_CODE", "pt-BR") or "pt-BR").strip() or "pt-BR"
GOOGLE_CLOUD_TTS_VOICE_NAME = str(getattr(config, "GOOGLE_CLOUD_TTS_VOICE_NAME", "pt-BR-Standard-A") or "pt-BR-Standard-A").strip() or "pt-BR-Standard-A"
GOOGLE_CLOUD_TTS_SPEAKING_RATE = float(getattr(config, "GOOGLE_CLOUD_TTS_SPEAKING_RATE", 1.0))
GOOGLE_CLOUD_TTS_PITCH = float(getattr(config, "GOOGLE_CLOUD_TTS_PITCH", 0.0))
TTS_FFMPEG_BEFORE_OPTIONS = getattr(config, "TTS_FFMPEG_BEFORE_OPTIONS", "-nostdin")
TTS_FFMPEG_OPTIONS = getattr(config, "TTS_FFMPEG_OPTIONS", "-vn -loglevel error")
TTS_TEMP_DIR = os.path.abspath(str(getattr(config, "TTS_TEMP_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "tmp_audio")) or os.path.join(os.path.dirname(os.path.abspath(__file__)), "tmp_audio")).strip() or os.path.join(os.path.dirname(os.path.abspath(__file__)), "tmp_audio"))
TTS_TEMP_MAX_MB = max(64, int(getattr(config, "TTS_TEMP_MAX_MB", 256)))
TTS_TEMP_MAX_FILES = max(32, int(getattr(config, "TTS_TEMP_MAX_FILES", 256)))
TTS_TEMP_MAX_BYTES = TTS_TEMP_MAX_MB * 1024 * 1024

_RUNTIME_DIR = os.path.join(TTS_TEMP_DIR, "runtime")
_CACHE_DIR = os.path.join(TTS_TEMP_DIR, "cache")
_CREDENTIALS_DIR = os.path.join(TTS_TEMP_DIR, "credentials")

for _dir in (TTS_TEMP_DIR, _RUNTIME_DIR, _CACHE_DIR, _CREDENTIALS_DIR):
    os.makedirs(_dir, exist_ok=True)

logger = logging.getLogger(__name__)


@dataclass
class QueueItem:
    guild_id: int
    channel_id: int
    author_id: int
    text: str
    engine: str
    voice: str
    language: str
    rate: str
    pitch: str
    enqueued_at_monotonic: float = field(default_factory=time.monotonic, repr=False, compare=False)
    _normalized_cache_text: Optional[str] = field(default=None, repr=False, compare=False)
    _cache_key_value: Optional[str] = field(default=None, repr=False, compare=False)
    _dedup_signature: Optional[str] = field(default=None, repr=False, compare=False)


@dataclass
class GuildTTSState:
    queue: asyncio.Queue
    worker_task: Optional[asyncio.Task] = None
    last_text_channel_id: Optional[int] = None
    last_channel_id: Optional[int] = None
    warmed_until: float = 0.0
    cache_order: OrderedDict[str, float] = field(default_factory=OrderedDict)
    pending_signatures: dict[str, int] = field(default_factory=dict)


class TTSAudioMixin:
    def _log_debug(self, text: str) -> None:
        if TTS_DEBUG_LOGS:
            logger.debug(text)

    def _get_state(self, guild_id: int) -> GuildTTSState:
        state = self.guild_states.get(guild_id)
        if state is None:
            state = GuildTTSState(queue=asyncio.Queue(maxsize=TTS_QUEUE_MAXSIZE))
            self.guild_states[guild_id] = state
        return state


    def _cleanup_guild_state_if_idle(self, guild_id: int) -> bool:
        state = self.guild_states.get(guild_id)
        if state is None:
            return True

        task = getattr(state, "worker_task", None)
        if task is not None and not task.done():
            return False

        if not state.queue.empty():
            return False

        self.guild_states.pop(guild_id, None)

        cleanup = getattr(self, "_cleanup_guild_runtime_state", None)
        if cleanup is not None:
            try:
                cleanup(guild_id)
            except Exception:
                logger.exception("[tts_voice] Falha ao limpar estado runtime da guild=%s", guild_id)

        return True

    async def _enqueue_tts_item(self, guild_id: int, item: QueueItem) -> tuple[bool, int, bool]:
        state = self._get_state(guild_id)
        dropped = 0
        deduplicated = False
        signature = self._queue_signature(item)

        while state.queue.full():
            try:
                dropped_item = state.queue.get_nowait()
                self._decrement_pending_signature(state, dropped_item)
                state.queue.task_done()
                dropped += 1
            except asyncio.QueueEmpty:
                break

        if int(state.pending_signatures.get(signature, 0) or 0) > 0:
            self._record_queue_enqueue(deduplicated=True)
            return False, dropped, True

        await state.queue.put(item)
        self._increment_pending_signature(state, item)
        self._record_queue_enqueue(dropped=dropped, deduplicated=False, queue_depth=state.queue.qsize())
        return True, dropped, deduplicated

    def _ensure_worker(self, guild_id: int) -> None:
        state = self._get_state(guild_id)
        if state.worker_task is None or state.worker_task.done():
            state.worker_task = asyncio.create_task(self._worker_loop(guild_id))

    async def _maybe_await(self, value):
        if inspect.isawaitable(value):
            return await value
        return value

    def _normalize_edge_rate(self, raw: str) -> str:
        value = str(raw or "").strip().replace("％", "%").replace("−", "-").replace("–", "-").replace("—", "-").replace(" ", "")
        if value.endswith("%"):
            value = value[:-1]
        if not value:
            return "+0%"
        if value[0] not in "+-":
            value = f"+{value}"
        sign, number = value[0], value[1:]
        if not number.isdigit():
            return "+0%"
        return f"{sign}{number}%"

    def _normalize_edge_pitch(self, raw: str) -> str:
        value = str(raw or "").strip().replace("−", "-").replace("–", "-").replace("—", "-").replace(" ", "")
        if value.lower().endswith("hz"):
            value = value[:-2]
        if not value:
            return "+0Hz"
        if value[0] not in "+-":
            value = f"{value}" if value.startswith(("+", "-")) else f"+{value}"
        sign, number = value[0], value[1:]
        if not number.isdigit():
            return "+0Hz"
        return f"{sign}{number}Hz"

    def _normalize_gcloud_language(self, raw: str) -> str:
        value = str(raw or "").strip() or GOOGLE_CLOUD_TTS_LANGUAGE_CODE
        value = value.replace("_", "-")
        return value or "pt-BR"

    def _normalize_gcloud_voice(self, raw: str) -> str:
        value = str(raw or "").strip() or GOOGLE_CLOUD_TTS_VOICE_NAME
        return value or "pt-BR-Standard-A"

    def _normalize_gcloud_rate(self, raw: str | float) -> str:
        try:
            numeric = float(str(raw).strip().replace(",", "."))
        except Exception:
            numeric = float(GOOGLE_CLOUD_TTS_SPEAKING_RATE or 1.0)
        numeric = max(0.25, min(2.0, numeric))
        return f"{numeric:.2f}".rstrip("0").rstrip(".")

    def _normalize_gcloud_pitch(self, raw: str | float) -> str:
        try:
            numeric = float(str(raw).strip().replace(",", "."))
        except Exception:
            numeric = float(GOOGLE_CLOUD_TTS_PITCH or 0.0)
        numeric = max(-20.0, min(20.0, numeric))
        if abs(numeric - round(numeric)) < 1e-9:
            return str(int(round(numeric)))
        return f"{numeric:.2f}".rstrip("0").rstrip(".")

    def _normalize_cache_text(self, text: str) -> str:
        text = " ".join((text or "").strip().split())
        text = text.lower()
        text = text.replace("!!", "!").replace("??", "?").replace("..", ".")
        return text

    def _get_item_normalized_cache_text(self, item: QueueItem) -> str:
        cached = getattr(item, "_normalized_cache_text", None)
        if cached is None:
            cached = self._normalize_cache_text(item.text)
            item._normalized_cache_text = cached
        return cached

    def _get_synth_semaphore(self) -> asyncio.Semaphore:
        semaphore = getattr(self, "_tts_synth_semaphore", None)
        if semaphore is None:
            semaphore = asyncio.Semaphore(TTS_SYNTH_CONCURRENCY)
            setattr(self, "_tts_synth_semaphore", semaphore)
        return semaphore

    def _get_gtts_semaphore(self) -> asyncio.Semaphore:
        semaphore = getattr(self, "_tts_gtts_semaphore", None)
        if semaphore is None:
            semaphore = asyncio.Semaphore(TTS_GTTS_CONCURRENCY)
            setattr(self, "_tts_gtts_semaphore", semaphore)
        return semaphore

    def _get_gtts_rate_lock(self) -> asyncio.Lock:
        lock = getattr(self, "_tts_gtts_rate_lock", None)
        if lock is None:
            lock = asyncio.Lock()
            setattr(self, "_tts_gtts_rate_lock", lock)
        return lock

    def _get_global_cache_order(self) -> OrderedDict[str, float]:
        cache_order = getattr(self, "_tts_cache_order", None)
        if cache_order is None:
            cache_order = OrderedDict()
            setattr(self, "_tts_cache_order", cache_order)
        return cache_order

    def _get_long_text_repeat_counts(self) -> dict[str, int]:
        counts = getattr(self, "_tts_long_text_repeat_counts", None)
        if counts is None:
            counts = {}
            setattr(self, "_tts_long_text_repeat_counts", counts)
        return counts

    def _remember_long_text_repeat(self, key: str) -> int:
        counts = self._get_long_text_repeat_counts()
        seen_count = int(counts.get(key, 0) or 0) + 1
        counts[key] = seen_count

        max_entries = max(TTS_AUDIO_CACHE_SIZE * 8, 256)
        if len(counts) > max_entries:
            overflow = len(counts) - max_entries
            for stale_key in list(counts.keys())[:overflow]:
                counts.pop(stale_key, None)

        return seen_count

    def _get_inflight_cache_tasks(self) -> dict[str, asyncio.Task]:
        tasks = getattr(self, "_tts_inflight_cache_tasks", None)
        if tasks is None:
            tasks = {}
            setattr(self, "_tts_inflight_cache_tasks", tasks)
        return tasks
    def _get_cache_frequency_map(self) -> dict[str, int]:
        frequencies = getattr(self, "_tts_cache_frequency", None)
        if frequencies is None:
            frequencies = {}
            setattr(self, "_tts_cache_frequency", frequencies)
        return frequencies

    def _get_metrics_store(self) -> dict[str, object]:
        metrics = getattr(self, "_tts_metrics", None)
        if metrics is None:
            metrics = {
                "queue_enqueued": 0,
                "queue_deduplicated": 0,
                "queue_dropped": 0,
                "cache_hits": 0,
                "cache_misses": 0,
                "cache_stores": 0,
                "queue_wait_total_ms": 0.0,
                "queue_wait_samples": 0,
                "dispatch_total_ms": 0.0,
                "dispatch_samples": 0,
                "source_setup_total_ms": 0.0,
                "source_setup_samples": 0,
                "play_call_total_ms": 0.0,
                "play_call_samples": 0,
                "playback_total_ms": 0.0,
                "playback_samples": 0,
                "total_to_playback_total_ms": 0.0,
                "total_to_playback_samples": 0,
                "queue_depth_total": 0,
                "queue_depth_samples": 0,
                "queue_depth_max": 0,
                "prefetch_started": 0,
                "boot_warmups": 0,
                "last_warmup_started_at": None,
                "last_warmup_completed_at": None,
                "last_warmup_duration_ms": None,
                "engines": {},
            }
            setattr(self, "_tts_metrics", metrics)
        return metrics

    def _get_engine_metrics(self, engine: str) -> dict[str, object]:
        engine = (engine or "gtts").strip().lower()
        metrics = self._get_metrics_store()
        engines = metrics.setdefault("engines", {})
        if engine not in engines:
            engines[engine] = {
                "synth_count": 0,
                "synth_failures": 0,
                "slow_alerts": 0,
                "cache_hits": 0,
                "cache_misses": 0,
                "synth_total_ms": 0.0,
                "last_synth_ms": None,
                "last_error": None,
                "last_error_at": None,
                "consecutive_failures": 0,
            }
        return engines[engine]

    def _record_average_metric(self, total_key: str, samples_key: str, value_ms: float) -> None:
        metrics = self._get_metrics_store()
        metrics[total_key] = float(metrics.get(total_key, 0.0) or 0.0) + float(value_ms)
        metrics[samples_key] = int(metrics.get(samples_key, 0) or 0) + 1

    def _queue_signature(self, item: QueueItem) -> str:
        cached = getattr(item, "_dedup_signature", None)
        if cached is not None:
            return cached
        cached = f"{int(item.channel_id)}|{self._cache_key(item)}"
        item._dedup_signature = cached
        return cached

    def _increment_pending_signature(self, state: GuildTTSState, item: QueueItem) -> None:
        signature = self._queue_signature(item)
        state.pending_signatures[signature] = int(state.pending_signatures.get(signature, 0) or 0) + 1

    def _decrement_pending_signature(self, state: GuildTTSState, item: QueueItem) -> None:
        signature = self._queue_signature(item)
        count = int(state.pending_signatures.get(signature, 0) or 0)
        if count <= 1:
            state.pending_signatures.pop(signature, None)
        else:
            state.pending_signatures[signature] = count - 1

    def _record_queue_enqueue(self, *, dropped: int = 0, deduplicated: bool = False, queue_depth: int | None = None) -> None:
        metrics = self._get_metrics_store()
        if deduplicated:
            metrics["queue_deduplicated"] = int(metrics.get("queue_deduplicated", 0) or 0) + 1
            return
        metrics["queue_enqueued"] = int(metrics.get("queue_enqueued", 0) or 0) + 1
        if dropped:
            metrics["queue_dropped"] = int(metrics.get("queue_dropped", 0) or 0) + int(dropped)
        if queue_depth is not None:
            queue_depth = max(0, int(queue_depth))
            metrics["queue_depth_total"] = int(metrics.get("queue_depth_total", 0) or 0) + queue_depth
            metrics["queue_depth_samples"] = int(metrics.get("queue_depth_samples", 0) or 0) + 1
            metrics["queue_depth_max"] = max(int(metrics.get("queue_depth_max", 0) or 0), queue_depth)

    def _record_prefetch_started(self) -> None:
        metrics = self._get_metrics_store()
        metrics["prefetch_started"] = int(metrics.get("prefetch_started", 0) or 0) + 1

    def _record_cache_hit(self, engine: str) -> None:
        metrics = self._get_metrics_store()
        metrics["cache_hits"] = int(metrics.get("cache_hits", 0) or 0) + 1
        engine_metrics = self._get_engine_metrics(engine)
        engine_metrics["cache_hits"] = int(engine_metrics.get("cache_hits", 0) or 0) + 1

    def _record_cache_miss(self, engine: str) -> None:
        metrics = self._get_metrics_store()
        metrics["cache_misses"] = int(metrics.get("cache_misses", 0) or 0) + 1
        engine_metrics = self._get_engine_metrics(engine)
        engine_metrics["cache_misses"] = int(engine_metrics.get("cache_misses", 0) or 0) + 1

    def _record_cache_store(self) -> None:
        metrics = self._get_metrics_store()
        metrics["cache_stores"] = int(metrics.get("cache_stores", 0) or 0) + 1

    def _get_engine_alert_state(self) -> dict[str, float]:
        state = getattr(self, "_tts_engine_alert_last_sent", None)
        if state is None:
            state = {}
            setattr(self, "_tts_engine_alert_last_sent", state)
        return state

    def _schedule_alert_script(self, alert_type: str, title: str, body: str) -> None:
        script_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "alert.sh")
        if not os.path.exists(script_path):
            return

        async def _runner() -> None:
            try:
                process = await asyncio.create_subprocess_exec(
                    "bash",
                    script_path,
                    alert_type,
                    title,
                    body,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await process.communicate()
            except Exception:
                logger.exception("[tts_voice] Falha ao enviar alerta de engine via webhook")

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(_runner())

    def _maybe_send_engine_alert(self, alert_key: str, alert_type: str, title: str, body: str) -> None:
        state = self._get_engine_alert_state()
        now = time.monotonic()
        last_sent = float(state.get(alert_key, 0.0) or 0.0)
        if (now - last_sent) < TTS_ENGINE_ALERT_COOLDOWN_SECONDS:
            return
        state[alert_key] = now
        self._schedule_alert_script(alert_type, title, body)

    def _record_engine_success(self, engine: str, duration_ms: float) -> None:
        engine_metrics = self._get_engine_metrics(engine)
        engine_metrics["synth_count"] = int(engine_metrics.get("synth_count", 0) or 0) + 1
        engine_metrics["synth_total_ms"] = float(engine_metrics.get("synth_total_ms", 0.0) or 0.0) + float(duration_ms)
        engine_metrics["last_synth_ms"] = round(float(duration_ms), 2)
        engine_metrics["consecutive_failures"] = 0

        if duration_ms >= TTS_ENGINE_SLOW_WARN_SECONDS * 1000.0:
            engine_metrics["slow_alerts"] = int(engine_metrics.get("slow_alerts", 0) or 0) + 1
            title = f"Engine TTS lenta: {engine}"
            body = (
                f"Engine: {engine}\n"
                f"Duração da síntese: {round(duration_ms, 2)} ms\n"
                f"Limite de alerta: {round(TTS_ENGINE_SLOW_WARN_SECONDS * 1000.0, 2)} ms"
            )
            self._maybe_send_engine_alert(f"slow:{engine}", "warn", title, body)

    def _record_engine_failure(self, engine: str, error: Exception, duration_ms: float | None = None) -> None:
        engine_metrics = self._get_engine_metrics(engine)
        engine_metrics["synth_failures"] = int(engine_metrics.get("synth_failures", 0) or 0) + 1
        engine_metrics["consecutive_failures"] = int(engine_metrics.get("consecutive_failures", 0) or 0) + 1
        engine_metrics["last_error"] = str(error)
        engine_metrics["last_error_at"] = time.time()
        if duration_ms is not None:
            engine_metrics["last_synth_ms"] = round(float(duration_ms), 2)

        if int(engine_metrics.get("consecutive_failures", 0) or 0) >= TTS_ENGINE_FAILURE_ALERT_THRESHOLD:
            title = f"Falhas repetidas na engine TTS: {engine}"
            body = (
                f"Engine: {engine}\n"
                f"Falhas consecutivas: {engine_metrics['consecutive_failures']}\n"
                f"Último erro: {error}"
            )
            if duration_ms is not None:
                body += f"\nDuração até falhar: {round(float(duration_ms), 2)} ms"
            self._maybe_send_engine_alert(f"fail:{engine}", "error", title, body)

    def _record_queue_timing(
        self,
        *,
        queue_wait_ms: float | None = None,
        dispatch_ms: float | None = None,
        source_setup_ms: float | None = None,
        play_call_ms: float | None = None,
        playback_ms: float | None = None,
        total_to_playback_ms: float | None = None,
    ) -> None:
        if queue_wait_ms is not None:
            self._record_average_metric("queue_wait_total_ms", "queue_wait_samples", queue_wait_ms)
        if dispatch_ms is not None:
            self._record_average_metric("dispatch_total_ms", "dispatch_samples", dispatch_ms)
        if source_setup_ms is not None:
            self._record_average_metric("source_setup_total_ms", "source_setup_samples", source_setup_ms)
        if play_call_ms is not None:
            self._record_average_metric("play_call_total_ms", "play_call_samples", play_call_ms)
        if playback_ms is not None:
            self._record_average_metric("playback_total_ms", "playback_samples", playback_ms)
        if total_to_playback_ms is not None:
            self._record_average_metric("total_to_playback_total_ms", "total_to_playback_samples", total_to_playback_ms)

    def _hydrate_cache_index(self) -> None:
        cache_order = self._get_global_cache_order()
        cache_frequency = self._get_cache_frequency_map()
        cache_order.clear()
        if not os.path.isdir(_CACHE_DIR):
            return
        cache_files = []
        try:
            with os.scandir(_CACHE_DIR) as entries:
                for entry in entries:
                    if not entry.is_file(follow_symlinks=False):
                        continue
                    if not entry.name.lower().endswith(".mp3"):
                        continue
                    try:
                        stat = entry.stat()
                    except FileNotFoundError:
                        continue
                    cache_files.append((stat.st_mtime, entry.name[:-4]))
        except FileNotFoundError:
            return

        for modified_ts, key in sorted(cache_files, key=lambda item: item[0]):
            cache_order[key] = modified_ts
            cache_frequency.setdefault(key, 1)

    def _prime_tts_runtime(self) -> None:
        self._get_synth_semaphore()
        self._get_gtts_semaphore()
        self._get_gtts_rate_lock()
        self._get_global_cache_order()
        self._get_cache_frequency_map()
        self._get_inflight_cache_tasks()
        self._get_metrics_store()
        self._hydrate_cache_index()

    async def _boot_warmup(self) -> None:
        metrics = self._get_metrics_store()
        started_at = time.monotonic()
        metrics["boot_warmups"] = int(metrics.get("boot_warmups", 0) or 0) + 1
        metrics["last_warmup_started_at"] = time.time()

        try:
            await asyncio.to_thread(self._prime_tts_runtime)
            await asyncio.to_thread(self._prune_tmp_audio_dir, force=True)

            if google_texttospeech is not None and (((os.getenv("GOOGLE_APPLICATION_CREDENTIALS") or "").strip()) or ((os.getenv("GOOGLE_CREDENTIALS_JSON") or "").strip())):
                with contextlib.suppress(Exception):
                    await asyncio.to_thread(self._get_google_tts_client)
        finally:
            duration_ms = (time.monotonic() - started_at) * 1000.0
            metrics["last_warmup_completed_at"] = time.time()
            metrics["last_warmup_duration_ms"] = round(duration_ms, 2)

    def get_tts_metrics_snapshot(self) -> dict[str, object]:
        metrics = self._get_metrics_store()
        snapshot = {
            "queue_enqueued": int(metrics.get("queue_enqueued", 0) or 0),
            "queue_deduplicated": int(metrics.get("queue_deduplicated", 0) or 0),
            "queue_dropped": int(metrics.get("queue_dropped", 0) or 0),
            "cache_hits": int(metrics.get("cache_hits", 0) or 0),
            "cache_misses": int(metrics.get("cache_misses", 0) or 0),
            "cache_stores": int(metrics.get("cache_stores", 0) or 0),
            "avg_queue_wait_ms": round((float(metrics.get("queue_wait_total_ms", 0.0) or 0.0) / int(metrics.get("queue_wait_samples", 0) or 1)), 2) if int(metrics.get("queue_wait_samples", 0) or 0) else 0.0,
            "avg_dispatch_ms": round((float(metrics.get("dispatch_total_ms", 0.0) or 0.0) / int(metrics.get("dispatch_samples", 0) or 1)), 2) if int(metrics.get("dispatch_samples", 0) or 0) else 0.0,
            "avg_source_setup_ms": round((float(metrics.get("source_setup_total_ms", 0.0) or 0.0) / int(metrics.get("source_setup_samples", 0) or 1)), 2) if int(metrics.get("source_setup_samples", 0) or 0) else 0.0,
            "avg_play_call_ms": round((float(metrics.get("play_call_total_ms", 0.0) or 0.0) / int(metrics.get("play_call_samples", 0) or 1)), 2) if int(metrics.get("play_call_samples", 0) or 0) else 0.0,
            "avg_playback_ms": round((float(metrics.get("playback_total_ms", 0.0) or 0.0) / int(metrics.get("playback_samples", 0) or 1)), 2) if int(metrics.get("playback_samples", 0) or 0) else 0.0,
            "avg_total_to_playback_ms": round((float(metrics.get("total_to_playback_total_ms", 0.0) or 0.0) / int(metrics.get("total_to_playback_samples", 0) or 1)), 2) if int(metrics.get("total_to_playback_samples", 0) or 0) else 0.0,
            "avg_queue_depth_at_enqueue": round((float(metrics.get("queue_depth_total", 0.0) or 0.0) / int(metrics.get("queue_depth_samples", 0) or 1)), 2) if int(metrics.get("queue_depth_samples", 0) or 0) else 0.0,
            "max_queue_depth_seen": int(metrics.get("queue_depth_max", 0) or 0),
            "prefetch_started": int(metrics.get("prefetch_started", 0) or 0),
            "boot_warmups": int(metrics.get("boot_warmups", 0) or 0),
            "last_warmup_duration_ms": metrics.get("last_warmup_duration_ms"),
            "queued_items_current": int(sum(state.queue.qsize() for state in self.guild_states.values())),
            "guild_states_current": int(len(self.guild_states)),
            "engines": {},
        }
        for engine, engine_metrics in dict(metrics.get("engines", {})).items():
            synth_count = int(engine_metrics.get("synth_count", 0) or 0)
            total_ms = float(engine_metrics.get("synth_total_ms", 0.0) or 0.0)
            snapshot["engines"][engine] = {
                "synth_count": synth_count,
                "synth_failures": int(engine_metrics.get("synth_failures", 0) or 0),
                "slow_alerts": int(engine_metrics.get("slow_alerts", 0) or 0),
                "cache_hits": int(engine_metrics.get("cache_hits", 0) or 0),
                "cache_misses": int(engine_metrics.get("cache_misses", 0) or 0),
                "avg_synth_ms": round(total_ms / synth_count, 2) if synth_count else 0.0,
                "last_synth_ms": engine_metrics.get("last_synth_ms"),
                "last_error": engine_metrics.get("last_error"),
                "consecutive_failures": int(engine_metrics.get("consecutive_failures", 0) or 0),
            }
        return snapshot

    def _should_prune_tmp_audio_dir(self, *, force: bool = False) -> bool:
        if force:
            setattr(self, "_tts_last_prune_ts", time.monotonic())
            return True

        now = time.monotonic()
        last_prune = float(getattr(self, "_tts_last_prune_ts", 0.0) or 0.0)
        if (now - last_prune) < TTS_TEMP_PRUNE_INTERVAL_SECONDS:
            return False

        setattr(self, "_tts_last_prune_ts", now)
        return True

    def _make_runtime_temp_file(self, suffix: str = ".mp3") -> str:
        fd, path = tempfile.mkstemp(prefix="tts_", suffix=suffix, dir=_RUNTIME_DIR)
        os.close(fd)
        return path

    def _list_tmp_audio_files(self) -> list[tuple[int, float, int, str]]:
        result: list[tuple[int, float, int, str]] = []
        for directory, priority in ((_RUNTIME_DIR, 0), (_CACHE_DIR, 1)):
            try:
                with os.scandir(directory) as entries:
                    for entry in entries:
                        if not entry.is_file(follow_symlinks=False):
                            continue
                        if not entry.name.lower().endswith((".mp3", ".wav", ".ogg", ".tmp")):
                            continue
                        try:
                            stat = entry.stat()
                        except FileNotFoundError:
                            continue
                        result.append((priority, stat.st_mtime, stat.st_size, entry.path))
            except FileNotFoundError:
                continue
        return result

    def _prune_tmp_audio_dir(self, *, protected_paths: Optional[set[str]] = None, force: bool = False) -> None:
        if not self._should_prune_tmp_audio_dir(force=force):
            return

        protected = {os.path.abspath(p) for p in (protected_paths or set()) if p}
        files = self._list_tmp_audio_files()
        total_files = len(files)
        total_bytes = sum(size for _, _, size, _ in files)

        if total_files <= TTS_TEMP_MAX_FILES and total_bytes <= TTS_TEMP_MAX_BYTES:
            return

        cache_order = self._get_global_cache_order()

        for _, _, size, path in sorted(files, key=lambda item: (item[0], item[1])):
            abs_path = os.path.abspath(path)
            if abs_path in protected:
                continue
            if total_files <= TTS_TEMP_MAX_FILES and total_bytes <= TTS_TEMP_MAX_BYTES:
                break
            try:
                os.remove(abs_path)
            except FileNotFoundError:
                pass
            except Exception:
                continue
            total_files = max(0, total_files - 1)
            total_bytes = max(0, total_bytes - size)
            if abs_path.startswith(os.path.abspath(_CACHE_DIR) + os.sep):
                cache_key = os.path.splitext(os.path.basename(abs_path))[0]
                cache_order.pop(cache_key, None)
                self._get_cache_frequency_map().pop(cache_key, None)


    def _touch_cache_entry(self, state: GuildTTSState, key: str) -> None:
        cache_order = self._get_global_cache_order()
        cache_frequency = self._get_cache_frequency_map()
        now = time.time()
        cache_order[key] = now
        cache_order.move_to_end(key)
        cache_frequency[key] = int(cache_frequency.get(key, 0) or 0) + 1

    def _purge_cache(self, state: GuildTTSState, *, protected_paths: Optional[set[str]] = None, force_tmp_prune: bool = False) -> None:
        cache_order = self._get_global_cache_order()
        cache_frequency = self._get_cache_frequency_map()

        missing = []
        for key in list(cache_order.keys()):
            path = self._cache_path(key)
            if not os.path.exists(path):
                missing.append(key)

        for key in missing:
            cache_order.pop(key, None)
            cache_frequency.pop(key, None)

        protected = {os.path.abspath(p) for p in (protected_paths or set()) if p}
        while len(cache_order) > TTS_AUDIO_CACHE_SIZE:
            candidate_key = None
            candidate_score = None

            for key, last_used_ts in cache_order.items():
                path = self._cache_path(key)
                abs_path = os.path.abspath(path)
                if abs_path in protected:
                    continue
                score = (int(cache_frequency.get(key, 0) or 0), float(last_used_ts))
                if candidate_score is None or score < candidate_score:
                    candidate_key = key
                    candidate_score = score

            if candidate_key is None:
                break

            path = self._cache_path(candidate_key)
            cache_order.pop(candidate_key, None)
            cache_frequency.pop(candidate_key, None)
            try:
                if os.path.exists(path):
                    os.remove(path)
            except Exception:
                pass

        self._prune_tmp_audio_dir(protected_paths=protected_paths, force=force_tmp_prune)

    async def _store_in_cache(self, state: GuildTTSState, item: QueueItem, source_path: str) -> str:
        key = self._cache_key(item)
        path = self._cache_path(key)

        if os.path.exists(path):
            self._touch_cache_entry(state, key)
            self._purge_cache(state, protected_paths={path}, force_tmp_prune=True)
            return path

        try:
            await asyncio.to_thread(shutil.move, source_path, path)
        except Exception:
            try:
                await asyncio.to_thread(shutil.copyfile, source_path, path)
            except Exception:
                return source_path

        self._touch_cache_entry(state, key)
        self._record_cache_store()
        self._purge_cache(state, protected_paths={path}, force_tmp_prune=True)
        return path

    def _cache_key(self, item: QueueItem) -> str:
        cached_key = getattr(item, "_cache_key_value", None)
        if cached_key is not None:
            return cached_key

        text = self._get_item_normalized_cache_text(item)
        engine = (item.engine or "gtts").strip().lower()
        if engine == "edge":
            voice = validate_voice(item.voice, getattr(self, "edge_voice_names", set()))
            payload = f"edge|{voice}|{self._normalize_edge_rate(item.rate)}|{self._normalize_edge_pitch(item.pitch)}|{text}"
        elif engine == "gcloud":
            language = self._normalize_gcloud_language(item.language)
            voice = self._normalize_gcloud_voice(item.voice)
            rate = self._normalize_gcloud_rate(item.rate)
            pitch = self._normalize_gcloud_pitch(item.pitch)
            payload = f"gcloud|{language}|{voice}|{rate}|{pitch}|{text}"
        else:
            language = (item.language or GTTS_DEFAULT_LANGUAGE).strip().lower().replace('_', '-')
            if language == 'pt-br':
                language = 'pt'
            payload = f"gtts|{language}|{text}"
        cached_key = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        item._cache_key_value = cached_key
        return cached_key

    def _cache_path(self, key: str) -> str:
        return os.path.join(_CACHE_DIR, f"{key}.mp3")


    def _try_get_cached_path(self, state: GuildTTSState, item: QueueItem) -> Optional[str]:
        key = self._cache_key(item)
        path = self._cache_path(key)

        if not os.path.exists(path):
            return None

        self._touch_cache_entry(state, key)
        self._record_cache_hit(item.engine)
        self._log_debug(f"[tts_voice] cache hit | guild={item.guild_id} key={key[:10]}")
        return path


    async def _generate_gtts_file(self, text: str, language: str, *, tld: str = "com") -> str:
        language = (language or GTTS_DEFAULT_LANGUAGE).strip().lower().replace('_', '-')
        if language == 'pt-br':
            language = 'pt'
        tld = str(tld or "com").strip() or "com"
        self._log_debug(f"[tts_voice] gTTS synth | language={language!r} tld={tld!r} text={text[:80]!r}")

        path = self._make_runtime_temp_file(suffix=".mp3")
        try:
            tts = gTTS(text=text, lang=language, tld=tld)

            def _write_gtts_file(target_path: str):
                with open(target_path, "wb") as handle:
                    tts.write_to_fp(handle)

            async with self._get_gtts_semaphore():
                await asyncio.to_thread(_write_gtts_file, path)
            return path
        except Exception:
            try:
                os.remove(path)
            except Exception:
                pass
            raise

    async def _generate_edge_file(self, text: str, voice: str, rate: str, pitch: str) -> str:
        voice = validate_voice(voice, getattr(self, "edge_voice_names", set()))
        rate = self._normalize_edge_rate(rate)
        pitch = self._normalize_edge_pitch(pitch)

        self._log_debug(
            "[tts_voice] Edge synth | "
            f"voice={voice!r} rate={rate!r} pitch={pitch!r} text={text[:80]!r}"
        )

        path = self._make_runtime_temp_file(suffix=".mp3")
        try:
            communicate = edge_tts.Communicate(text=text, voice=voice, rate=rate, pitch=pitch)
            async with self._get_synth_semaphore():
                await asyncio.wait_for(communicate.save(path), timeout=TTS_EDGE_TIMEOUT_SECONDS)
            return path
        except Exception:
            try:
                os.remove(path)
            except Exception:
                pass
            raise

    def _ensure_google_credentials_file(self) -> None:
        if os.getenv("GOOGLE_APPLICATION_CREDENTIALS"):
            return

        raw_json = (os.getenv("GOOGLE_CREDENTIALS_JSON", "") or "").strip()
        if not raw_json:
            return

        try:
            parsed = json.loads(raw_json)
        except Exception as exc:
            raise RuntimeError("GOOGLE_CREDENTIALS_JSON está inválido e não pôde ser lido como JSON.") from exc

        path = os.path.join(_CREDENTIALS_DIR, "chat_revive_google_credentials.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(parsed, handle, ensure_ascii=False)
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = path

    def _get_google_tts_client(self):
        if google_texttospeech is None:
            raise RuntimeError("A dependência google-cloud-texttospeech não está instalada.")
        self._ensure_google_credentials_file()
        client = getattr(self, "_google_tts_client", None)
        if client is None:
            client = google_texttospeech.TextToSpeechClient()
            setattr(self, "_google_tts_client", client)
        return client

    async def _generate_google_cloud_file(self, text: str, language: str, voice_name: str, rate: str, pitch: str) -> str:
        language = self._normalize_gcloud_language(language)
        voice_name = self._normalize_gcloud_voice(voice_name)
        normalized_rate = self._normalize_gcloud_rate(rate)
        normalized_pitch = self._normalize_gcloud_pitch(pitch)
        if voice_name and not str(voice_name).lower().startswith(str(language).lower() + '-'):
            voice_name = ''
        self._log_debug(
            "[tts_voice] Google Cloud TTS synth | "
            f"voice={voice_name!r} language={language!r} rate={normalized_rate!r} pitch={normalized_pitch!r} text={text[:80]!r}"
        )

        path = self._make_runtime_temp_file(suffix=".mp3")

        try:
            client = await asyncio.to_thread(self._get_google_tts_client)
            synthesis_input = google_texttospeech.SynthesisInput(text=text)
            voice_kwargs = {"language_code": language}
            if voice_name:
                voice_kwargs["name"] = voice_name
            voice = google_texttospeech.VoiceSelectionParams(**voice_kwargs)
            audio_config = google_texttospeech.AudioConfig(
                audio_encoding=google_texttospeech.AudioEncoding.MP3,
                speaking_rate=float(normalized_rate),
                pitch=float(normalized_pitch),
            )
            request = google_texttospeech.SynthesizeSpeechRequest(
                input=synthesis_input,
                voice=voice,
                audio_config=audio_config,
            )

            async with self._get_synth_semaphore():
                response = await asyncio.to_thread(client.synthesize_speech, request=request)
                def _write_audio_file(target_path: str, data: bytes) -> None:
                    with open(target_path, 'wb') as handle:
                        handle.write(data)
                await asyncio.to_thread(_write_audio_file, path, response.audio_content)
            return path
        except Exception:
            try:
                os.remove(path)
            except Exception:
                pass
            raise

    async def _resolve_or_generate_singleflight_audio(self, state: GuildTTSState, item: QueueItem, *, read_cache: bool, store_in_cache: bool) -> tuple[str, bool]:
        key = self._cache_key(item)
        inflight = self._get_inflight_cache_tasks()

        existing = inflight.get(key)
        if existing is not None:
            return await existing

        async def _runner() -> tuple[str, bool]:
            if read_cache:
                cached = self._try_get_cached_path(state, item)
                if cached:
                    return cached, False

            generated = await self._generate_audio_file(item)
            if store_in_cache:
                cached_path = await self._store_in_cache(state, item, generated)
                if cached_path != generated:
                    return cached_path, False
            return generated, False

        task = asyncio.create_task(_runner())
        inflight[key] = task
        try:
            return await task
        finally:
            if inflight.get(key) is task:
                inflight.pop(key, None)

    async def _run_timed_generation(self, engine: str, factory) -> str:
        started_at = time.monotonic()
        try:
            result = await factory()
        except Exception as exc:
            duration_ms = (time.monotonic() - started_at) * 1000.0
            self._record_engine_failure(engine, exc, duration_ms=duration_ms)
            raise
        duration_ms = (time.monotonic() - started_at) * 1000.0
        self._record_engine_success(engine, duration_ms)
        return result

    async def _generate_audio_file(self, item: QueueItem) -> str:
        if item.engine == "edge":
            try:
                return await self._run_timed_generation(
                    "edge",
                    lambda: self._generate_edge_file(item.text, item.voice, item.rate, item.pitch),
                )
            except Exception as e:
                logger.warning("[tts_voice] Edge falhou, usando gTTS | guild=%s erro=%s", item.guild_id, e)
                return await self._run_timed_generation(
                    "gtts",
                    lambda: self._generate_gtts_file(item.text, item.language),
                )

        if item.engine == "gcloud":
            return await self._run_timed_generation(
                "gcloud",
                lambda: self._generate_google_cloud_file(item.text, item.language, item.voice, item.rate, item.pitch),
            )

        return await self._run_timed_generation(
            "gtts",
            lambda: self._generate_gtts_file(item.text, item.language),
        )

    async def _resolve_audio_path(self, state: GuildTTSState, item: QueueItem) -> tuple[str, bool]:
        normalized_text = self._get_item_normalized_cache_text(item)
        text_length = len(normalized_text)
        if text_length <= TTS_CACHEABLE_TEXT_MAX_LENGTH:
            return await self._resolve_or_generate_singleflight_audio(
                state,
                item,
                read_cache=True,
                store_in_cache=True,
            )

        cached = self._try_get_cached_path(state, item)
        if cached:
            return cached, False

        key = self._cache_key(item)
        seen_count = self._remember_long_text_repeat(key)

        should_cache_long_text = (
            text_length <= TTS_CACHEABLE_TEXT_HARD_MAX_LENGTH
            and seen_count >= TTS_LONG_TEXT_CACHE_MIN_REPEATS
        )

        self._record_cache_miss(item.engine)
        return await self._resolve_or_generate_singleflight_audio(
            state,
            item,
            read_cache=False,
            store_in_cache=should_cache_long_text,
        )

    async def _play_file(self, vc: discord.VoiceClient, path: str) -> dict[str, float]:
        loop = asyncio.get_running_loop()
        finished = loop.create_future()
        guild = getattr(vc, "guild", None)

        def _after_playback(error: Optional[Exception]) -> None:
            if error:
                if not finished.done():
                    loop.call_soon_threadsafe(finished.set_exception, error)
            else:
                if not finished.done():
                    loop.call_soon_threadsafe(finished.set_result, None)

        if guild is not None and hasattr(self, "_notify_voice_moderation_playback_start"):
            with contextlib.suppress(Exception):
                await self._maybe_await(self._notify_voice_moderation_playback_start(guild, vc))

        try:
            source_setup_started_at = time.monotonic()
            source = discord.FFmpegPCMAudio(
                path,
                before_options=TTS_FFMPEG_BEFORE_OPTIONS,
                options=TTS_FFMPEG_OPTIONS,
            )
            source_setup_ms = max(0.0, (time.monotonic() - source_setup_started_at) * 1000.0)

            play_call_started_at = time.monotonic()
            vc.play(source, after=_after_playback)
            play_call_ms = max(0.0, (time.monotonic() - play_call_started_at) * 1000.0)

            playback_started_at = time.monotonic()
            await finished
            playback_duration_ms = max(0.0, (time.monotonic() - playback_started_at) * 1000.0)
            return {
                "source_setup_ms": source_setup_ms,
                "play_call_ms": play_call_ms,
                "playback_ms": playback_duration_ms,
                "playback_started_at": playback_started_at,
            }
        finally:
            if guild is not None and hasattr(self, "_notify_voice_moderation_playback_end"):
                with contextlib.suppress(Exception):
                    await self._maybe_await(self._notify_voice_moderation_playback_end(guild, vc))

    async def _ensure_self_deaf_fast(self, guild: discord.Guild, target_channel=None) -> bool:
        should_self_deaf = True
        try:
            if hasattr(self, "_voice_should_self_deaf"):
                should_self_deaf = bool(await self._maybe_await(self._voice_should_self_deaf(guild.id)))
        except Exception:
            should_self_deaf = True

        last_error = None
        for _ in range(3):
            try:
                me = getattr(guild, "me", None)
                me_voice = getattr(me, "voice", None)
                target = getattr(me_voice, "channel", None) or target_channel
                current_self_deaf = bool(getattr(me_voice, "self_deaf", False)) if me_voice else None
                if me_voice and current_self_deaf == should_self_deaf:
                    return True
                if target is None:
                    return False
                await guild.change_voice_state(channel=target, self_deaf=should_self_deaf)
                await asyncio.sleep(0.35)
                me = getattr(guild, "me", None)
                me_voice = getattr(me, "voice", None)
                current_self_deaf = bool(getattr(me_voice, "self_deaf", False)) if me_voice else None
                if me_voice and current_self_deaf == should_self_deaf:
                    return True
            except Exception as e:
                last_error = e
                await asyncio.sleep(0.35)
        if last_error is not None:
            logger.warning(
                "[tts_voice] Falha ao reaplicar estado de voz | guild=%s channel=%s self_deaf=%s erro=%s",
                guild.id,
                getattr(target_channel, "id", None),
                should_self_deaf,
                last_error,
            )
        return False

    async def _disconnect_idle(self, guild: discord.Guild) -> bool:
        if hasattr(self, "_get_guild_toggle_value"):
            try:
                auto_leave_enabled = await self._maybe_await(
                    self._get_guild_toggle_value(
                        guild.id,
                        public_key="auto_leave",
                        raw_key="auto_leave_enabled",
                        default=True,
                    )
                )
            except Exception as e:
                logger.warning("[tts_voice] Falha ao consultar auto_leave no idle timeout | guild=%s erro=%s", guild.id, e)
                auto_leave_enabled = True
            if not auto_leave_enabled:
                self._log_debug(f"[tts_voice] Idle timeout ignorado | auto_leave desativado | guild={guild.id}")
                return False

        vc = self._get_voice_client_for_guild(guild)
        if vc is None or not vc.is_connected() or vc.channel is None:
            return True

        members = list(getattr(vc.channel, "members", []))
        humans = [m for m in members if not m.bot]
        if humans:
            self._log_debug(f"[tts_voice] Idle timeout ignorado | ainda há humanos na call | guild={guild.id}")
            return False

        try:
            await vc.disconnect(force=False)
            logger.info("[tts_voice] Desconectado por inatividade | guild=%s", guild.id)
            return True
        except Exception as e:
            logger.warning("[tts_voice] Erro ao desconectar por inatividade | guild=%s erro=%s", guild.id, e)
            return False

    async def _ensure_connected_fast(self, guild: discord.Guild, item: QueueItem):
        state = self._get_state(guild.id)
        target_channel = guild.get_channel(item.channel_id) or self.bot.get_channel(item.channel_id)
        if target_channel is None:
            return None

        vc = self._get_voice_client_for_guild(guild)
        if vc is not None and vc.is_connected():
            if vc.channel is not None and vc.channel.id == item.channel_id:
                await self._ensure_self_deaf_fast(guild, target_channel)
                state.last_channel_id = item.channel_id
                return vc
            try:
                await vc.move_to(target_channel)
                await self._ensure_self_deaf_fast(guild, target_channel)
                state.last_channel_id = item.channel_id
                return vc
            except Exception:
                pass

        vc = await self._maybe_await(self._ensure_connected(guild, target_channel))
        if vc is None:
            current = self._get_voice_client_for_guild(guild)
            if current is not None and current.is_connected():
                if current.channel is not None and current.channel.id == item.channel_id:
                    state.last_channel_id = item.channel_id
                    return current
            return None

        if vc.is_connected():
            await self._ensure_self_deaf_fast(guild, target_channel)
            state.last_channel_id = item.channel_id
        return vc

    async def _maybe_prefetch_next(self, state: GuildTTSState):
        prefetched_item: Optional[QueueItem] = None
        prefetched_audio_task: Optional[asyncio.Task] = None

        if state.queue.empty():
            return prefetched_item, prefetched_audio_task

        try:
            prefetched_item = state.queue.get_nowait()
            self._decrement_pending_signature(state, prefetched_item)
        except asyncio.QueueEmpty:
            return None, None

        setattr(prefetched_item, "_dequeued_at_monotonic", time.monotonic())
        self._record_prefetch_started()
        prefetched_audio_task = asyncio.create_task(self._resolve_audio_path(state, prefetched_item))
        return prefetched_item, prefetched_audio_task

    async def _worker_loop(self, guild_id: int) -> None:
        state = self._get_state(guild_id)
        prefetched_item: Optional[QueueItem] = None
        prefetched_audio_task: Optional[asyncio.Task] = None

        try:
            while True:
                guild = self.bot.get_guild(guild_id)
                if guild is None:
                    self._log_debug(f"[tts_voice] Guild não encontrada no worker | guild={guild_id}")
                    return

                fetched_from_queue = False

                if prefetched_item is not None:
                    item = prefetched_item
                    fetched_from_queue = True
                    prefetched_item = None
                    audio_task = prefetched_audio_task
                    prefetched_audio_task = None
                else:
                    try:
                        timeout = TTS_IDLE_DISCONNECT_SECONDS
                        if state.warmed_until > time.monotonic():
                            timeout = min(timeout, max(1.0, state.warmed_until - time.monotonic()))
                        item = await asyncio.wait_for(state.queue.get(), timeout=timeout)
                        self._decrement_pending_signature(state, item)
                        setattr(item, "_dequeued_at_monotonic", time.monotonic())
                        fetched_from_queue = True
                    except asyncio.TimeoutError:
                        if state.warmed_until > time.monotonic():
                            continue
                        disconnected = await self._disconnect_idle(guild)
                        if disconnected:
                            return
                        continue
                    audio_task = None

                try:
                    if hasattr(self, "_should_block_for_voice_bot"):
                        target_channel = guild.get_channel(item.channel_id) or self.bot.get_channel(item.channel_id)
                        if target_channel is not None:
                            blocked = await self._maybe_await(self._should_block_for_voice_bot(guild, target_channel))
                            if blocked:
                                logger.info("[tts_voice] Worker bloqueado por outro bot de voz | guild=%s channel=%s", guild_id, item.channel_id)
                                if hasattr(self, "_disconnect_if_blocked"):
                                    await self._maybe_await(self._disconnect_if_blocked(guild))
                                continue

                    connect_task = asyncio.create_task(self._ensure_connected_fast(guild, item))
                    own_audio_task = None
                    if audio_task is None:
                        own_audio_task = asyncio.create_task(self._resolve_audio_path(state, item))
                        active_audio_task = own_audio_task
                    else:
                        active_audio_task = audio_task

                    if prefetched_item is None and not state.queue.empty():
                        prefetched_item, prefetched_audio_task = await self._maybe_prefetch_next(state)

                    vc = await connect_task
                    if vc is None:
                        if own_audio_task is not None and not own_audio_task.done():
                            own_audio_task.cancel()
                            with contextlib.suppress(BaseException):
                                await own_audio_task
                        logger.warning("[tts_voice] Worker não conseguiu conectar | guild=%s channel=%s", guild_id, item.channel_id)
                        continue

                    current_path, should_cleanup = await active_audio_task

                    dequeue_started_at = float(getattr(item, "_dequeued_at_monotonic", time.monotonic()))
                    queue_wait_ms = max(0.0, (dequeue_started_at - float(getattr(item, "enqueued_at_monotonic", dequeue_started_at))) * 1000.0)

                    try:
                        playback_result = await self._play_file(vc, current_path)
                        playback_started_at = float(playback_result.get("playback_started_at", time.monotonic()) or time.monotonic())
                        source_setup_ms = max(0.0, float(playback_result.get("source_setup_ms", 0.0) or 0.0))
                        play_call_ms = max(0.0, float(playback_result.get("play_call_ms", 0.0) or 0.0))
                        playback_duration_ms = max(0.0, float(playback_result.get("playback_ms", 0.0) or 0.0))
                        dispatch_ms = max(0.0, (playback_started_at - dequeue_started_at) * 1000.0)
                        total_to_playback_ms = max(0.0, (playback_started_at - float(getattr(item, "enqueued_at_monotonic", playback_started_at))) * 1000.0)
                        self._record_queue_timing(
                            queue_wait_ms=queue_wait_ms,
                            dispatch_ms=dispatch_ms,
                            source_setup_ms=source_setup_ms,
                            play_call_ms=play_call_ms,
                            playback_ms=playback_duration_ms,
                            total_to_playback_ms=total_to_playback_ms,
                        )
                        logger.debug(
                            "[tts_perf] pronto para playback | guild=%s engine=%s queue_wait_ms=%.2f dispatch_ms=%.2f source_setup_ms=%.2f play_call_ms=%.2f total_to_playback_ms=%.2f text_len=%s",
                            guild_id,
                            item.engine,
                            queue_wait_ms,
                            dispatch_ms,
                            source_setup_ms,
                            play_call_ms,
                            total_to_playback_ms,
                            len(item.text or ""),
                        )
                    finally:
                        protected_paths: set[str] = set()
                        if should_cleanup:
                            protected_paths.add(current_path)
                        if prefetched_audio_task is not None and prefetched_audio_task.done() and not prefetched_audio_task.cancelled():
                            with contextlib.suppress(Exception):
                                prefetched_path, _ = prefetched_audio_task.result()
                                if prefetched_path:
                                    protected_paths.add(prefetched_path)
                        self._purge_cache(state, protected_paths=protected_paths)
                        state.warmed_until = time.monotonic() + TTS_WARM_HOLD_SECONDS

                    if prefetched_item is None and not state.queue.empty():
                        prefetched_item, prefetched_audio_task = await self._maybe_prefetch_next(state)

                except Exception as e:
                    logger.exception("[tts_voice] Erro no worker da guild %s: %s", guild_id, e)
                finally:
                    if fetched_from_queue:
                        state.queue.task_done()
        finally:
            state.worker_task = None
            self._cleanup_guild_state_if_idle(guild_id)
