import contextlib
import asyncio
import base64
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
from typing import Any, Optional

import discord
import aiohttp
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
TTS_GTTS_TIMEOUT_SECONDS = max(5.0, float(getattr(config, "TTS_GTTS_TIMEOUT_SECONDS", 20.0)))
TTS_GCLOUD_TIMEOUT_SECONDS = max(5.0, float(getattr(config, "TTS_GCLOUD_TIMEOUT_SECONDS", 20.0)))
TTS_PLAYBACK_TIMEOUT_BASE_SECONDS = max(5.0, float(getattr(config, "TTS_PLAYBACK_TIMEOUT_BASE_SECONDS", 12.0)))
TTS_PLAYBACK_TIMEOUT_PER_CHAR_SECONDS = max(0.0, float(getattr(config, "TTS_PLAYBACK_TIMEOUT_PER_CHAR_SECONDS", 0.08)))
TTS_PLAYBACK_TIMEOUT_MAX_SECONDS = max(TTS_PLAYBACK_TIMEOUT_BASE_SECONDS, float(getattr(config, "TTS_PLAYBACK_TIMEOUT_MAX_SECONDS", 120.0)))
TTS_VOICE_HARD_RESET_COOLDOWN_SECONDS = max(5.0, float(getattr(config, "TTS_VOICE_HARD_RESET_COOLDOWN_SECONDS", 25.0)))
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
TTS_TURBO_BENCHMARK_ENABLED = bool(getattr(config, "TTS_TURBO_BENCHMARK_ENABLED", True))
TTS_TURBO_BENCHMARK_GUILD_ID = int(getattr(config, "TTS_TURBO_BENCHMARK_GUILD_ID", 927002914449424404) or 927002914449424404)
TTS_TURBO_BENCHMARK_TRIGGER_TEXT = str(getattr(config, "TTS_TURBO_BENCHMARK_TRIGGER_TEXT", "teste") or "teste").strip().lower() or "teste"
TTS_TURBO_BENCHMARK_TIMEOUT_SECONDS = max(1.5, float(getattr(config, "TTS_TURBO_BENCHMARK_TIMEOUT_SECONDS", 12.0) or 12.0))
TTS_TURBO_BENCHMARK_MAX_AUDIO_MB = max(1, int(getattr(config, "TTS_TURBO_BENCHMARK_MAX_AUDIO_MB", 4) or 4))
TTS_PIPER_EXPERIMENT_ENABLED = bool(getattr(config, "TTS_PIPER_EXPERIMENT_ENABLED", True))
TTS_PIPER_EXPERIMENT_GUILD_ID = int(getattr(config, "TTS_PIPER_EXPERIMENT_GUILD_ID", 0) or 0)
TTS_PIPER_EXPERIMENT_PREFIX = str(getattr(config, "TTS_PIPER_EXPERIMENT_PREFIX", "%") or "%").strip() or "%"
TTS_PIPER_WORKER_TIMEOUT_SECONDS = max(1.0, float(getattr(config, "TTS_PIPER_WORKER_TIMEOUT_SECONDS", 6.0) or 6.0))
TTS_PIPER_MAX_TEXT_LENGTH = max(16, int(getattr(config, "TTS_PIPER_MAX_TEXT_LENGTH", 600) or 600))
TTS_PIPER_MAX_AUDIO_MB = max(1, int(getattr(config, "TTS_PIPER_MAX_AUDIO_MB", 8) or 8))
TTS_PIPER_MODEL_NAME = str(getattr(config, "TTS_PIPER_MODEL_NAME", "turbo-default") or "turbo-default").strip() or "turbo-default"
TTS_PIPER_VPS_CACHE_SIZE = max(32, int(getattr(config, "TTS_PIPER_VPS_CACHE_SIZE", 2048) or 2048))
TTS_PIPER_VPS_CACHE_MAX_MB = max(64, int(getattr(config, "TTS_PIPER_VPS_CACHE_MAX_MB", 2048) or 2048))
TTS_PIPER_VPS_CACHE_MAX_BYTES = TTS_PIPER_VPS_CACHE_MAX_MB * 1024 * 1024
TTS_TURBO_WORKER_CACHE_ENABLED = bool(getattr(config, "TTS_TURBO_WORKER_CACHE_ENABLED", True))
TTS_TURBO_WORKER_CACHE_LOOKUP_TIMEOUT_SECONDS = max(0.15, float(getattr(config, "TTS_TURBO_WORKER_CACHE_LOOKUP_TIMEOUT_SECONDS", 0.65) or 0.65))
TTS_TURBO_WORKER_CACHE_STORE_TIMEOUT_SECONDS = max(0.5, float(getattr(config, "TTS_TURBO_WORKER_CACHE_STORE_TIMEOUT_SECONDS", 2.5) or 2.5))
TTS_TURBO_WORKER_CACHE_MAX_AUDIO_MB = max(1, int(getattr(config, "TTS_TURBO_WORKER_CACHE_MAX_AUDIO_MB", 8) or 8))
TTS_TURBO_WORKER_CACHE_STORE_BACKGROUND = bool(getattr(config, "TTS_TURBO_WORKER_CACHE_STORE_BACKGROUND", True))
TTS_TURBO_WORKER_CACHE_MISS_COOLDOWN_SECONDS = max(1.0, float(getattr(config, "TTS_TURBO_WORKER_CACHE_MISS_COOLDOWN_SECONDS", 45.0) or 45.0))
TTS_TURBO_WORKER_CACHE_ERROR_COOLDOWN_SECONDS = max(1.0, float(getattr(config, "TTS_TURBO_WORKER_CACHE_ERROR_COOLDOWN_SECONDS", 10.0) or 10.0))
TTS_TURBO_WORKER_CACHE_INDEX_MAX_ENTRIES = max(128, int(getattr(config, "TTS_TURBO_WORKER_CACHE_INDEX_MAX_ENTRIES", 4096) or 4096))
PHONE_WORKER_ENABLED = bool(getattr(config, "PHONE_WORKER_ENABLED", False))
PHONE_WORKER_HOST = str(getattr(config, "PHONE_WORKER_HOST", "") or "").strip()
PHONE_WORKER_PORT = int(getattr(config, "PHONE_WORKER_PORT", 8766) or 8766)
PHONE_WORKER_SCHEME = str(getattr(config, "PHONE_WORKER_SCHEME", "http") or "http").strip().lower() or "http"
PHONE_WORKER_TOKEN = str(getattr(config, "PHONE_WORKER_TOKEN", "") or "").strip()

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
    piper_fallback_engine: str = field(default="gtts", repr=False, compare=False)
    piper_fallback_voice: str = field(default="", repr=False, compare=False)
    piper_fallback_language: str = field(default="", repr=False, compare=False)
    piper_fallback_rate: str = field(default="+0%", repr=False, compare=False)
    piper_fallback_pitch: str = field(default="+0Hz", repr=False, compare=False)
    piper_model: str = field(default="", repr=False, compare=False)


@dataclass
class GuildTTSState:
    queue: asyncio.Queue
    worker_task: Optional[asyncio.Task] = None
    last_text_channel_id: Optional[int] = None
    last_channel_id: Optional[int] = None
    warmed_until: float = 0.0
    cache_order: OrderedDict[str, float] = field(default_factory=OrderedDict)
    pending_signatures: dict[str, int] = field(default_factory=dict)
    last_hard_reset_at: float = 0.0
    lavalink_ignore_logged_until: float = 0.0


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

    def _estimate_playback_timeout(self, item: QueueItem | None = None) -> float:
        text_len = len((getattr(item, "text", "") or "").strip()) if item is not None else 0
        timeout = TTS_PLAYBACK_TIMEOUT_BASE_SECONDS + (min(text_len, 1600) * TTS_PLAYBACK_TIMEOUT_PER_CHAR_SECONDS)
        return max(TTS_PLAYBACK_TIMEOUT_BASE_SECONDS, min(TTS_PLAYBACK_TIMEOUT_MAX_SECONDS, timeout))

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
                "worker_cache_lookup_hits": 0,
                "worker_cache_lookup_misses": 0,
                "worker_cache_lookup_skipped": 0,
                "worker_cache_lookup_errors": 0,
                "worker_cache_store_ok": 0,
                "worker_cache_store_failed": 0,
                "worker_cache_hit_total_ms": 0.0,
                "worker_cache_hit_samples": 0,
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

    def _record_worker_cache_lookup(self, status: str, *, total_ms: float | None = None) -> None:
        metrics = self._get_metrics_store()
        key_map = {
            "hit": "worker_cache_lookup_hits",
            "miss": "worker_cache_lookup_misses",
            "skip": "worker_cache_lookup_skipped",
            "error": "worker_cache_lookup_errors",
        }
        metric_key = key_map.get(str(status or "").strip().lower())
        if metric_key:
            metrics[metric_key] = int(metrics.get(metric_key, 0) or 0) + 1
        if status == "hit" and total_ms is not None:
            self._record_average_metric("worker_cache_hit_total_ms", "worker_cache_hit_samples", float(total_ms))

    def _record_worker_cache_store(self, ok: bool) -> None:
        metrics = self._get_metrics_store()
        key = "worker_cache_store_ok" if ok else "worker_cache_store_failed"
        metrics[key] = int(metrics.get(key, 0) or 0) + 1

    def _get_worker_cache_index(self) -> OrderedDict[str, dict[str, Any]]:
        index = getattr(self, "_tts_worker_cache_index", None)
        if index is None:
            index = OrderedDict()
            setattr(self, "_tts_worker_cache_index", index)
        return index

    def _prune_worker_cache_index(self) -> None:
        index = self._get_worker_cache_index()
        now = time.monotonic()
        for key in list(index.keys()):
            entry = index.get(key) or {}
            expires_at = float(entry.get("expires_at", 0.0) or 0.0)
            if expires_at and expires_at <= now:
                index.pop(key, None)
        while len(index) > TTS_TURBO_WORKER_CACHE_INDEX_MAX_ENTRIES:
            index.popitem(last=False)

    def _mark_worker_cache_index(self, key: str, status: str, *, ttl: float | None = None, meta: dict[str, Any] | None = None) -> None:
        clean_key = str(key or "").strip()
        if not clean_key:
            return
        status = str(status or "").strip().lower() or "unknown"
        if ttl is None:
            if status == "miss":
                ttl = TTS_TURBO_WORKER_CACHE_MISS_COOLDOWN_SECONDS
            elif status == "error":
                ttl = TTS_TURBO_WORKER_CACHE_ERROR_COOLDOWN_SECONDS
            else:
                ttl = max(float(TTS_AUDIO_CACHE_TTL_SECONDS), 3600.0)
        index = self._get_worker_cache_index()
        index[clean_key] = {
            "status": status,
            "updated_at": time.monotonic(),
            "expires_at": time.monotonic() + max(1.0, float(ttl)),
            **(meta or {}),
        }
        index.move_to_end(clean_key)
        self._prune_worker_cache_index()

    def _worker_cache_recent_negative_status(self, key: str) -> str:
        clean_key = str(key or "").strip()
        if not clean_key:
            return ""
        self._prune_worker_cache_index()
        entry = self._get_worker_cache_index().get(clean_key) or {}
        status = str(entry.get("status") or "").strip().lower()
        if status in {"miss", "error"}:
            return status
        return ""

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

    async def _record_persistent_synt_success(self, guild_id: int | None, engine: str) -> None:
        try:
            gid = int(guild_id or 0)
        except Exception:
            gid = 0
        if gid <= 0:
            return

        db = getattr(getattr(self, "bot", None), "settings_db", None)
        increment = getattr(db, "increment_tts_synt_count", None)
        if not callable(increment):
            return

        try:
            result = increment(gid, engine, 1)
            if inspect.isawaitable(result):
                await result
        except Exception:
            logger.exception("[tts_voice] Falha ao persistir synt TTS | guild=%s engine=%s", gid, engine)

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
        self._get_worker_cache_index()
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
            "worker_cache_lookup_hits": int(metrics.get("worker_cache_lookup_hits", 0) or 0),
            "worker_cache_lookup_misses": int(metrics.get("worker_cache_lookup_misses", 0) or 0),
            "worker_cache_lookup_skipped": int(metrics.get("worker_cache_lookup_skipped", 0) or 0),
            "worker_cache_lookup_errors": int(metrics.get("worker_cache_lookup_errors", 0) or 0),
            "worker_cache_store_ok": int(metrics.get("worker_cache_store_ok", 0) or 0),
            "worker_cache_store_failed": int(metrics.get("worker_cache_store_failed", 0) or 0),
            "avg_worker_cache_hit_ms": round((float(metrics.get("worker_cache_hit_total_ms", 0.0) or 0.0) / int(metrics.get("worker_cache_hit_samples", 0) or 1)), 2) if int(metrics.get("worker_cache_hit_samples", 0) or 0) else 0.0,
            "worker_cache_index_entries": int(len(self._get_worker_cache_index())),
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

        effective_max_files = TTS_TEMP_MAX_FILES + TTS_PIPER_VPS_CACHE_SIZE
        effective_max_bytes = TTS_TEMP_MAX_BYTES + TTS_PIPER_VPS_CACHE_MAX_BYTES

        if total_files <= effective_max_files and total_bytes <= effective_max_bytes:
            return

        cache_order = self._get_global_cache_order()

        for _, _, size, path in sorted(files, key=lambda item: (item[0], item[1])):
            abs_path = os.path.abspath(path)
            if abs_path in protected:
                continue
            if total_files <= effective_max_files and total_bytes <= effective_max_bytes:
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

    def _is_piper_cache_key(self, key: str) -> bool:
        return str(key or "").startswith("piper_")

    def _cache_quota_overflow(self, cache_order: OrderedDict[str, float]) -> tuple[bool, bool, bool]:
        piper_count = sum(1 for key in cache_order if self._is_piper_cache_key(key))
        normal_count = max(0, len(cache_order) - piper_count)
        piper_over = piper_count > TTS_PIPER_VPS_CACHE_SIZE
        normal_over = normal_count > TTS_AUDIO_CACHE_SIZE
        total_over = len(cache_order) > (TTS_AUDIO_CACHE_SIZE + TTS_PIPER_VPS_CACHE_SIZE)
        return normal_over, piper_over, total_over

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
        while True:
            normal_over, piper_over, total_over = self._cache_quota_overflow(cache_order)
            if not (normal_over or piper_over or total_over):
                break

            candidate_key = None
            candidate_score = None

            for key, last_used_ts in cache_order.items():
                is_piper = self._is_piper_cache_key(key)
                if piper_over and not is_piper:
                    continue
                if normal_over and not piper_over and is_piper:
                    continue
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
        elif engine == "piper":
            model = str(getattr(item, "piper_model", "") or TTS_PIPER_MODEL_NAME).strip() or TTS_PIPER_MODEL_NAME
            payload = f"piper|worker|{model}|{text}"
        else:
            language = (item.language or GTTS_DEFAULT_LANGUAGE).strip().lower().replace('_', '-')
            if language == 'pt-br':
                language = 'pt'
            payload = f"gtts|{language}|{text}"
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        cached_key = f"piper_{digest}" if engine == "piper" else digest
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
                await asyncio.wait_for(asyncio.to_thread(_write_gtts_file, path), timeout=TTS_GTTS_TIMEOUT_SECONDS)
            return path
        except asyncio.TimeoutError as exc:
            logger.warning("[tts_voice] gTTS travou e foi cancelado | language=%s timeout=%.1fs", language, TTS_GTTS_TIMEOUT_SECONDS)
            try:
                os.remove(path)
            except Exception:
                pass
            raise RuntimeError(f"gTTS timeout após {TTS_GTTS_TIMEOUT_SECONDS:.1f}s") from exc
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

    def _parse_google_credentials_json(self, raw_json: str) -> dict:
        text = (raw_json or "").strip()
        candidates = [text]
        if len(text) >= 2 and text[0] == text[-1] and text[0] in {"\"", "'"}:
            candidates.append(text[1:-1].strip())

        last_error: Exception | None = None
        for candidate in candidates:
            if not candidate:
                continue
            try:
                parsed = json.loads(candidate)
                if isinstance(parsed, str):
                    parsed = json.loads(parsed)
                if not isinstance(parsed, dict):
                    raise RuntimeError("GOOGLE_CREDENTIALS_JSON não contém um objeto JSON válido.")
                return parsed
            except Exception as exc:
                last_error = exc

        preview = text[:180].replace("\n", "\\n")
        if isinstance(last_error, json.JSONDecodeError):
            detail = f"linha {last_error.lineno}, coluna {last_error.colno}: {last_error.msg}"
        elif last_error is not None:
            detail = str(last_error)
        else:
            detail = "conteúdo ausente"
        raise RuntimeError(
            "GOOGLE_CREDENTIALS_JSON está inválido. "
            f"Detalhe: {detail}. Prévia: {preview or 'vazia'}"
        ) from last_error

    def _ensure_google_credentials_file(self) -> None:
        if os.getenv("GOOGLE_APPLICATION_CREDENTIALS"):
            setattr(self, "_google_credentials_error", None)
            return

        raw_json = (os.getenv("GOOGLE_CREDENTIALS_JSON", "") or "").strip()
        if not raw_json:
            setattr(self, "_google_credentials_error", None)
            return

        cached_error = getattr(self, "_google_credentials_error", None)
        if cached_error:
            raise RuntimeError(cached_error)

        try:
            parsed = self._parse_google_credentials_json(raw_json)
        except Exception as exc:
            message = str(exc).strip() or "GOOGLE_CREDENTIALS_JSON está inválido."
            setattr(self, "_google_credentials_error", message)
            logger.error("[tts_voice] Credenciais Google inválidas: %s", message)
            raise RuntimeError(message) from exc

        path = os.path.join(_CREDENTIALS_DIR, "chat_revive_google_credentials.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(parsed, handle, ensure_ascii=False)
        setattr(self, "_google_credentials_error", None)
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
            client = await asyncio.wait_for(asyncio.to_thread(self._get_google_tts_client), timeout=TTS_GCLOUD_TIMEOUT_SECONDS)
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
                response = await asyncio.wait_for(asyncio.to_thread(client.synthesize_speech, request=request), timeout=TTS_GCLOUD_TIMEOUT_SECONDS)
                def _write_audio_file(target_path: str, data: bytes) -> None:
                    with open(target_path, 'wb') as handle:
                        handle.write(data)
                await asyncio.wait_for(asyncio.to_thread(_write_audio_file, path, response.audio_content), timeout=max(5.0, TTS_GCLOUD_TIMEOUT_SECONDS))
            return path
        except asyncio.TimeoutError as exc:
            logger.warning("[tts_voice] Google Cloud TTS travou e foi cancelado | language=%s timeout=%.1fs", language, TTS_GCLOUD_TIMEOUT_SECONDS)
            try:
                os.remove(path)
            except Exception:
                pass
            raise RuntimeError(f"Google Cloud TTS timeout após {TTS_GCLOUD_TIMEOUT_SECONDS:.1f}s") from exc
        except Exception:
            try:
                os.remove(path)
            except Exception:
                pass
            raise

    def _phone_worker_tts_benchmark_base_url(self) -> str:
        if not PHONE_WORKER_ENABLED or not PHONE_WORKER_HOST or not PHONE_WORKER_TOKEN:
            return ""
        scheme = PHONE_WORKER_SCHEME if PHONE_WORKER_SCHEME in {"http", "https"} else "http"
        return f"{scheme}://{PHONE_WORKER_HOST}:{PHONE_WORKER_PORT}"

    def _phone_worker_tts_base_url(self) -> str:
        return self._phone_worker_tts_benchmark_base_url()

    def _normalize_worker_audio_format(self, value: Any) -> str:
        fmt = str(value or "mp3").strip().lower().replace(".", "")
        if fmt in {"wav", "wave"}:
            return "wav"
        if fmt in {"ogg", "opus"}:
            return "ogg"
        return "mp3"

    async def _request_phone_worker_json(self, *, task: str, payload: dict[str, Any], timeout_seconds: float, max_audio_mb: int, raise_on_worker_error: bool = True) -> dict[str, Any]:
        base = self._phone_worker_tts_base_url()
        if not base:
            raise RuntimeError("PHONE_WORKER_ENABLED/HOST/TOKEN não configurado")
        headers = {
            "Authorization": f"Bearer {PHONE_WORKER_TOKEN}",
            "Content-Type": "application/json",
        }
        request_payload = dict(payload)
        request_payload["task"] = task
        max_audio_bytes = max(1, int(max_audio_mb)) * 1024 * 1024
        request_payload.setdefault("max_audio_bytes", max_audio_bytes)
        started = time.monotonic()
        timeout = aiohttp.ClientTimeout(total=max(1.0, float(timeout_seconds)))
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(f"{base}/task", headers=headers, json=request_payload) as response:
                response_text = await response.text()
                if response.status < 200 or response.status >= 300:
                    raise RuntimeError(f"HTTP {response.status}: {response_text[:260]}")
                data = json.loads(response_text or "{}")
        data["total_ms"] = round((time.monotonic() - started) * 1000.0, 2)
        data["audio_format"] = self._normalize_worker_audio_format(data.get("audio_format"))
        if raise_on_worker_error and data.get("ok") is False:
            raise RuntimeError(str(data.get("error") or "worker retornou ok=false"))
        return data

    def _decode_worker_audio_payload(self, data: dict[str, Any], *, max_audio_mb: int) -> dict[str, Any]:
        max_audio_bytes = max(1, int(max_audio_mb)) * 1024 * 1024
        out_b64 = str(data.get("data_b64") or "")
        if not out_b64:
            raise RuntimeError("worker não retornou data_b64")
        raw = base64.b64decode(out_b64.encode("ascii"), validate=True)
        if not raw:
            raise RuntimeError("worker retornou áudio vazio")
        if len(raw) > max_audio_bytes:
            raise RuntimeError(f"worker retornou áudio grande demais: {len(raw)} bytes")
        expected_hash = str(data.get("sha256") or "")
        actual_hash = hashlib.sha256(raw).hexdigest()
        if expected_hash and expected_hash != actual_hash:
            raise RuntimeError("sha256 do áudio retornado não confere")
        data["raw_audio"] = raw
        data["sha256"] = actual_hash
        data["audio_format"] = self._normalize_worker_audio_format(data.get("audio_format"))
        return data

    async def _request_phone_worker_tts_audio(self, *, task: str, payload: dict[str, Any], timeout_seconds: float, max_audio_mb: int) -> dict[str, Any]:
        data = await self._request_phone_worker_json(
            task=task,
            payload=payload,
            timeout_seconds=timeout_seconds,
            max_audio_mb=max_audio_mb,
            raise_on_worker_error=True,
        )
        return self._decode_worker_audio_payload(data, max_audio_mb=max_audio_mb)

    def _worker_tts_cache_payload_base(self, item: QueueItem, key: str) -> dict[str, Any]:
        engine = str(item.engine or "gtts").strip().lower() or "gtts"
        payload: dict[str, Any] = {
            "cache_key": key,
            "engine": engine,
            "text_length": len(str(item.text or "")),
            "max_audio_bytes": TTS_TURBO_WORKER_CACHE_MAX_AUDIO_MB * 1024 * 1024,
        }
        if engine == "piper":
            payload["model_name"] = str(getattr(item, "piper_model", "") or TTS_PIPER_MODEL_NAME)
        return payload

    def _path_audio_format(self, path: str) -> str:
        suffix = os.path.splitext(str(path or ""))[1].lower().replace(".", "")
        if suffix in {"wav", "wave"}:
            return "wav"
        if suffix in {"ogg", "opus"}:
            return "ogg"
        return "mp3"

    async def _try_get_worker_turbo_cache_path(self, item: QueueItem) -> str | None:
        if not TTS_TURBO_WORKER_CACHE_ENABLED:
            return None
        if not PHONE_WORKER_ENABLED or not PHONE_WORKER_HOST or not PHONE_WORKER_TOKEN:
            return None
        key = self._cache_key(item)
        recent_negative = self._worker_cache_recent_negative_status(key)
        if recent_negative:
            self._record_worker_cache_lookup("skip")
            self._log_debug(
                f"[tts_worker_cache] consulta pulada por índice negativo | guild={item.guild_id} engine={item.engine} key={key[:10]} status={recent_negative}"
            )
            return None
        payload = self._worker_tts_cache_payload_base(item, key)
        try:
            data = await self._request_phone_worker_json(
                task="tts_cache_lookup",
                payload=payload,
                timeout_seconds=TTS_TURBO_WORKER_CACHE_LOOKUP_TIMEOUT_SECONDS,
                max_audio_mb=TTS_TURBO_WORKER_CACHE_MAX_AUDIO_MB,
                raise_on_worker_error=False,
            )
            if not bool(data.get("cache_hit")):
                self._mark_worker_cache_index(key, "miss", meta={"engine": item.engine})
                self._record_worker_cache_lookup("miss")
                self._log_debug(
                    f"[tts_worker_cache] miss | guild={item.guild_id} engine={item.engine} key={key[:10]} total={data.get('total_ms')}ms erro={data.get('error')}"
                )
                return None
            data = self._decode_worker_audio_payload(data, max_audio_mb=TTS_TURBO_WORKER_CACHE_MAX_AUDIO_MB)
            suffix = ".wav" if data.get("audio_format") == "wav" else (".ogg" if data.get("audio_format") == "ogg" else ".mp3")
            path = self._make_runtime_temp_file(suffix=suffix)
            try:
                with open(path, "wb") as handle:
                    handle.write(data["raw_audio"])
                if os.path.getsize(path) <= 0:
                    raise RuntimeError("worker cache retornou áudio vazio")
                self._record_cache_hit(item.engine)
                self._record_worker_cache_lookup("hit", total_ms=float(data.get("total_ms", 0.0) or 0.0))
                self._mark_worker_cache_index(key, "hit", meta={
                    "engine": item.engine,
                    "audio_format": data.get("audio_format"),
                    "size": len(data.get("raw_audio") or b""),
                })
                self._log_debug(
                    f"[tts_worker_cache] hit | guild={item.guild_id} engine={item.engine} key={key[:10]} total={data.get('total_ms')}ms read={data.get('cache_read_ms')}ms"
                )
                return path
            except Exception:
                with contextlib.suppress(Exception):
                    os.remove(path)
                raise
        except Exception as exc:
            self._mark_worker_cache_index(key, "error", ttl=TTS_TURBO_WORKER_CACHE_ERROR_COOLDOWN_SECONDS, meta={"engine": item.engine, "error": str(exc)[:160]})
            self._record_worker_cache_lookup("error")
            self._log_debug(f"[tts_worker_cache] miss/indisponível | guild={item.guild_id} engine={item.engine} erro={exc}")
            return None

    async def _store_worker_turbo_cache(self, item: QueueItem, path: str) -> None:
        if not TTS_TURBO_WORKER_CACHE_ENABLED:
            return
        if not PHONE_WORKER_ENABLED or not PHONE_WORKER_HOST or not PHONE_WORKER_TOKEN:
            return
        if not path or not os.path.exists(path):
            return
        try:
            size = os.path.getsize(path)
        except Exception:
            return
        max_bytes = TTS_TURBO_WORKER_CACHE_MAX_AUDIO_MB * 1024 * 1024
        if size <= 0 or size > max_bytes:
            return
        key = self._cache_key(item)
        try:
            def _read_file(target: str) -> bytes:
                with open(target, "rb") as handle:
                    return handle.read()
            raw = await asyncio.to_thread(_read_file, path)
            if not raw or len(raw) > max_bytes:
                return
            digest = hashlib.sha256(raw).hexdigest()
            payload = self._worker_tts_cache_payload_base(item, key)
            payload.update({
                "audio_format": self._path_audio_format(path),
                "sha256": digest,
                "data_b64": base64.b64encode(raw).decode("ascii"),
            })
            base = self._phone_worker_tts_base_url()
            if not base:
                return
            headers = {
                "Authorization": f"Bearer {PHONE_WORKER_TOKEN}",
                "Content-Type": "application/json",
            }
            request_payload = dict(payload)
            request_payload["task"] = "tts_cache_store"
            timeout = aiohttp.ClientTimeout(total=TTS_TURBO_WORKER_CACHE_STORE_TIMEOUT_SECONDS)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(f"{base}/task", headers=headers, json=request_payload) as response:
                    text = await response.text()
                    if response.status < 200 or response.status >= 300:
                        raise RuntimeError(f"HTTP {response.status}: {text[:160]}")
            self._mark_worker_cache_index(key, "hit", meta={"engine": item.engine, "size": size, "source": "store"})
            self._record_worker_cache_store(True)
            self._log_debug(f"[tts_worker_cache] store ok | guild={item.guild_id} engine={item.engine} key={key[:10]} size={size}")
        except Exception as exc:
            self._record_worker_cache_store(False)
            self._log_debug(f"[tts_worker_cache] store falhou | guild={item.guild_id} engine={item.engine} erro={exc}")

    def _schedule_worker_turbo_cache_store(self, item: QueueItem, path: str) -> None:
        if not TTS_TURBO_WORKER_CACHE_STORE_BACKGROUND:
            return
        if not TTS_TURBO_WORKER_CACHE_ENABLED:
            return
        if not PHONE_WORKER_ENABLED or not PHONE_WORKER_HOST or not PHONE_WORKER_TOKEN:
            return
        task = asyncio.create_task(self._store_worker_turbo_cache(item, path))
        task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)

    async def _generate_piper_worker_file(self, item: QueueItem) -> str:
        text = str(item.text or "").strip()
        if not text:
            raise RuntimeError("texto vazio para Piper")
        if len(text) > TTS_PIPER_MAX_TEXT_LENGTH:
            raise RuntimeError(f"texto grande demais para Piper experimental ({len(text)}/{TTS_PIPER_MAX_TEXT_LENGTH})")
        payload = {
            "text": text,
            "model_name": str(getattr(item, "piper_model", "") or TTS_PIPER_MODEL_NAME),
            "timeout_seconds": max(1.0, TTS_PIPER_WORKER_TIMEOUT_SECONDS - 0.5),
        }
        data = await self._request_phone_worker_tts_audio(
            task="tts_synthesize_piper",
            payload=payload,
            timeout_seconds=TTS_PIPER_WORKER_TIMEOUT_SECONDS,
            max_audio_mb=TTS_PIPER_MAX_AUDIO_MB,
        )
        suffix = ".wav" if data.get("audio_format") == "wav" else ".mp3"
        path = self._make_runtime_temp_file(suffix=suffix)
        try:
            with open(path, "wb") as handle:
                handle.write(data["raw_audio"])
            if os.path.getsize(path) <= 0:
                raise RuntimeError("Piper retornou áudio vazio")
            logs = data.get("logs") if isinstance(data.get("logs"), list) else []
            if logs:
                self._log_debug("[tts_piper] " + " | ".join(self._short_tts_benchmark_text(x, limit=120) for x in logs[:3]))
            return path
        except Exception:
            with contextlib.suppress(Exception):
                os.remove(path)
            raise

    async def _generate_piper_fallback_file(self, item: QueueItem) -> str:
        fallback_engine = str(getattr(item, "piper_fallback_engine", "gtts") or "gtts").strip().lower()
        if fallback_engine == "edge":
            voice = str(getattr(item, "piper_fallback_voice", "") or item.voice or "pt-BR-FranciscaNeural")
            rate = str(getattr(item, "piper_fallback_rate", "") or item.rate or "+0%")
            pitch = str(getattr(item, "piper_fallback_pitch", "") or item.pitch or "+0Hz")
            return await self._run_timed_generation("edge", lambda: self._generate_edge_file(item.text, voice, rate, pitch), guild_id=item.guild_id)
        if fallback_engine == "gcloud":
            language = str(getattr(item, "piper_fallback_language", "") or GOOGLE_CLOUD_TTS_LANGUAGE_CODE)
            voice = str(getattr(item, "piper_fallback_voice", "") or GOOGLE_CLOUD_TTS_VOICE_NAME)
            rate = str(getattr(item, "piper_fallback_rate", "") or GOOGLE_CLOUD_TTS_SPEAKING_RATE)
            pitch = str(getattr(item, "piper_fallback_pitch", "") or GOOGLE_CLOUD_TTS_PITCH)
            try:
                return await self._run_timed_generation("gcloud", lambda: self._generate_google_cloud_file(item.text, language, voice, rate, pitch), guild_id=item.guild_id)
            except Exception as exc:
                logger.warning("[tts_piper] fallback gcloud falhou, usando gTTS | guild=%s erro=%s", item.guild_id, exc)
        language = str(getattr(item, "piper_fallback_language", "") or item.language or GTTS_DEFAULT_LANGUAGE)
        return await self._run_timed_generation("gtts", lambda: self._generate_gtts_file(item.text, language), guild_id=item.guild_id)

    def _short_tts_benchmark_text(self, value: Any, *, limit: int = 180) -> str:
        text = str(value or "").replace("`", "'").replace("\r", " ").replace("\n", " ").strip()
        text = " ".join(text.split())
        if len(text) > limit:
            return text[: max(0, limit - 1)] + "…"
        return text

    def _format_tts_benchmark_ms(self, value: Any) -> str:
        try:
            numeric = float(value)
        except Exception:
            return "—"
        return f"{numeric:.0f} ms" if numeric >= 10 else f"{numeric:.1f} ms"

    def _format_tts_benchmark_delta(self, local_ms: Any, worker_ms: Any) -> str:
        try:
            local = float(local_ms)
            worker = float(worker_ms)
        except Exception:
            return "sem cálculo"
        delta = local - worker
        pct = (delta / local * 100.0) if local > 0 else 0.0
        if delta > 0:
            return f"worker ganhou por {delta:.0f} ms ({pct:.1f}%)"
        if delta < 0:
            return f"VPS ganhou por {abs(delta):.0f} ms ({abs(pct):.1f}%)"
        return "empate técnico"

    def _should_run_tts_turbo_benchmark(self, message: discord.Message, active_prefix: str) -> bool:
        if not TTS_TURBO_BENCHMARK_ENABLED:
            return False
        guild = getattr(message, "guild", None)
        if guild is None or int(getattr(guild, "id", 0) or 0) != TTS_TURBO_BENCHMARK_GUILD_ID:
            return False
        content = str(getattr(message, "content", "") or "")
        prefix = str(active_prefix or "")
        if not prefix or not content.startswith(prefix):
            return False
        spoken = content[len(prefix):].strip().lower()
        return spoken == TTS_TURBO_BENCHMARK_TRIGGER_TEXT

    def _build_tts_benchmark_item(self, base_item: QueueItem, engine: str, resolved: dict[str, Any] | None, *, text: str) -> QueueItem:
        resolved = dict(resolved or {})
        engine = str(engine or "gtts").strip().lower()
        if engine == "edge":
            voice = str(resolved.get("voice") or base_item.voice or "pt-BR-FranciscaNeural")
            language = str(resolved.get("language") or base_item.language or GTTS_DEFAULT_LANGUAGE)
            rate = str(resolved.get("rate") or base_item.rate or "+0%")
            pitch = str(resolved.get("pitch") or base_item.pitch or "+0Hz")
        elif engine == "gcloud":
            voice = str(resolved.get("gcloud_voice") or GOOGLE_CLOUD_TTS_VOICE_NAME)
            language = str(resolved.get("gcloud_language") or GOOGLE_CLOUD_TTS_LANGUAGE_CODE)
            rate = str(resolved.get("gcloud_rate") or GOOGLE_CLOUD_TTS_SPEAKING_RATE)
            pitch = str(resolved.get("gcloud_pitch") or GOOGLE_CLOUD_TTS_PITCH)
        elif engine == "piper":
            voice = ""
            language = str(resolved.get("language") or base_item.language or GTTS_DEFAULT_LANGUAGE)
            rate = "+0%"
            pitch = "+0Hz"
        else:
            engine = "gtts"
            voice = ""
            language = str(resolved.get("language") or base_item.language or GTTS_DEFAULT_LANGUAGE)
            rate = "+0%"
            pitch = "+0Hz"
        return QueueItem(
            guild_id=base_item.guild_id,
            channel_id=base_item.channel_id,
            author_id=base_item.author_id,
            text=text,
            engine=engine,
            voice=voice,
            language=language,
            rate=rate,
            pitch=pitch,
            piper_model=str(resolved.get("piper_model") or getattr(base_item, "piper_model", "") or TTS_PIPER_MODEL_NAME),
        )

    async def _tts_benchmark_local_engine(self, item: QueueItem) -> dict[str, Any]:
        engine = str(item.engine or "gtts").strip().lower()
        started = time.monotonic()
        path = ""
        try:
            if engine == "piper":
                raise RuntimeError("Piper experimental roda apenas no phone-worker turbo")
            if engine == "edge":
                path = await self._generate_edge_file(item.text, item.voice, item.rate, item.pitch)
            elif engine == "gcloud":
                path = await self._generate_google_cloud_file(item.text, item.language, item.voice, item.rate, item.pitch)
            else:
                engine = "gtts"
                path = await self._generate_gtts_file(item.text, item.language)
            elapsed_ms = (time.monotonic() - started) * 1000.0
            size = os.path.getsize(path) if path and os.path.exists(path) else 0
            if size <= 0:
                raise RuntimeError("engine gerou áudio vazio (0 B)")
            sha256 = ""
            if path and os.path.exists(path):
                def _hash_file(target: str) -> str:
                    digest = hashlib.sha256()
                    with open(target, "rb") as handle:
                        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                            digest.update(chunk)
                    return digest.hexdigest()
                sha256 = await asyncio.to_thread(_hash_file, path)
            return {
                "ok": True,
                "engine": engine,
                "elapsed_ms": round(elapsed_ms, 2),
                "size": int(size),
                "sha256": sha256,
                "logs": [f"VPS gerou {size} bytes em {elapsed_ms:.1f} ms"],
            }
        except Exception as exc:
            elapsed_ms = (time.monotonic() - started) * 1000.0
            return {
                "ok": False,
                "engine": engine,
                "elapsed_ms": round(elapsed_ms, 2),
                "error": self._short_tts_benchmark_text(f"{type(exc).__name__}: {exc}", limit=220),
                "logs": [f"VPS falhou após {elapsed_ms:.1f} ms"],
            }
        finally:
            if path:
                with contextlib.suppress(Exception):
                    os.remove(path)

    async def _tts_benchmark_worker_engine_once(self, item: QueueItem, *, cache_mode: str | None = None) -> dict[str, Any]:
        engine = str(item.engine or "gtts").strip().lower()
        base = self._phone_worker_tts_benchmark_base_url()
        if not base:
            return {
                "ok": False,
                "engine": engine,
                "error": "PHONE_WORKER_ENABLED/HOST/TOKEN não configurado",
                "logs": ["worker indisponível na config da VPS"],
            }
        payload = {
            "task": "tts_synthesize_benchmark",
            "engine": engine,
            "text": item.text,
            "voice": item.voice,
            "language": item.language,
            "rate": item.rate,
            "pitch": item.pitch,
            "model_name": str(getattr(item, "piper_model", "") or TTS_PIPER_MODEL_NAME),
            "timeout_seconds": int(max(2.0, TTS_TURBO_BENCHMARK_TIMEOUT_SECONDS - 1.0)),
            "max_audio_bytes": TTS_TURBO_BENCHMARK_MAX_AUDIO_MB * 1024 * 1024,
        }
        if cache_mode:
            payload["cache_mode"] = cache_mode
        headers = {
            "Authorization": f"Bearer {PHONE_WORKER_TOKEN}",
            "Content-Type": "application/json",
        }
        started = time.monotonic()
        try:
            timeout = aiohttp.ClientTimeout(total=TTS_TURBO_BENCHMARK_TIMEOUT_SECONDS)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(f"{base}/task", headers=headers, json=payload) as response:
                    response_text = await response.text()
                    if response.status < 200 or response.status >= 300:
                        raise RuntimeError(f"HTTP {response.status}: {response_text[:260]}")
                    data = json.loads(response_text or "{}")
            if isinstance(data, dict) and data.get("ok") is False:
                total_ms = (time.monotonic() - started) * 1000.0
                logs = data.get("logs") if isinstance(data.get("logs"), list) else []
                clean_logs = [self._short_tts_benchmark_text(line, limit=160) for line in logs[:5]]
                clean_logs.append(f"VPS recebeu resposta sem áudio; total real {total_ms:.1f} ms")
                return {
                    "ok": False,
                    "engine": engine,
                    "total_ms": round(total_ms, 2),
                    "worker_total_ms": data.get("worker_total_ms"),
                    "worker_synth_ms": data.get("worker_synth_ms"),
                    "size": int(data.get("size") or 0),
                    "error": self._short_tts_benchmark_text(str(data.get("error") or "worker retornou ok=false"), limit=260),
                    "worker_profile": data.get("worker_profile"),
                    "worker_version": data.get("worker_version"),
                    "audio_format": data.get("audio_format"),
                    "cache_hit": bool(data.get("cache_hit")),
                    "cache_exists_before": bool(data.get("cache_exists_before")),
                    "cache_mode": data.get("cache_mode"),
                    "cache_key": data.get("cache_key"),
                    "cache_file": data.get("cache_file"),
                    "cache_read_ms": data.get("cache_read_ms"),
                    "cache_stored": bool(data.get("cache_stored")),
                    "logs": clean_logs,
                }
            out_b64 = str(data.get("data_b64") or "")
            if not out_b64:
                raise RuntimeError("worker não retornou data_b64")
            raw = base64.b64decode(out_b64.encode("ascii"), validate=True)
            if not raw:
                raise RuntimeError("worker retornou áudio vazio")
            max_audio_bytes = TTS_TURBO_BENCHMARK_MAX_AUDIO_MB * 1024 * 1024
            if len(raw) > max_audio_bytes:
                raise RuntimeError(f"worker retornou áudio grande demais: {len(raw)} bytes")
            expected_hash = str(data.get("sha256") or "")
            actual_hash = hashlib.sha256(raw).hexdigest()
            if expected_hash and expected_hash != actual_hash:
                raise RuntimeError("sha256 do áudio retornado não confere")

            def _write_and_stat(content: bytes) -> int:
                suffix = ".wav" if data.get("audio_format") == "wav" else ".mp3"
                path = self._make_runtime_temp_file(suffix=suffix)
                try:
                    with open(path, "wb") as handle:
                        handle.write(content)
                    return os.path.getsize(path)
                finally:
                    with contextlib.suppress(Exception):
                        os.remove(path)

            saved_size = await asyncio.to_thread(_write_and_stat, raw)
            if saved_size <= 0:
                raise RuntimeError("worker retornou áudio vazio após salvar temp")
            total_ms = (time.monotonic() - started) * 1000.0
            logs = data.get("logs") if isinstance(data.get("logs"), list) else []
            clean_logs = [self._short_tts_benchmark_text(line, limit=160) for line in logs[:4]]
            clean_logs.append(f"VPS validou/salvou temp {saved_size} bytes; total real {total_ms:.1f} ms")
            return {
                "ok": True,
                "engine": engine,
                "total_ms": round(total_ms, 2),
                "worker_synth_ms": data.get("worker_synth_ms"),
                "size": int(saved_size),
                "sha256": actual_hash,
                "worker_profile": data.get("worker_profile"),
                "worker_version": data.get("worker_version"),
                "audio_format": data.get("audio_format"),
                "cache_hit": bool(data.get("cache_hit")),
                "cache_exists_before": bool(data.get("cache_exists_before")),
                "cache_mode": data.get("cache_mode"),
                "cache_key": data.get("cache_key"),
                "cache_file": data.get("cache_file"),
                "cache_read_ms": data.get("cache_read_ms"),
                "cache_stored": bool(data.get("cache_stored")),
                "logs": clean_logs,
            }
        except Exception as exc:
            total_ms = (time.monotonic() - started) * 1000.0
            return {
                "ok": False,
                "engine": engine,
                "total_ms": round(total_ms, 2),
                "error": self._short_tts_benchmark_text(f"{type(exc).__name__}: {exc}", limit=260),
                "logs": [f"worker falhou após {total_ms:.1f} ms"],
            }

    async def _tts_benchmark_worker_engine(self, item: QueueItem) -> dict[str, Any]:
        engine = str(item.engine or "gtts").strip().lower()
        if engine != "piper":
            return await self._tts_benchmark_worker_engine_once(item)

        miss = await self._tts_benchmark_worker_engine_once(item, cache_mode="refresh")
        hit = await self._tts_benchmark_worker_engine_once(item, cache_mode="cache_only")
        if not miss.get("ok"):
            return miss
        hit_is_real = bool(hit.get("ok")) and bool(hit.get("cache_hit"))
        if not hit_is_real:
            combined = dict(miss)
            combined["piper_cache_miss"] = miss
            combined["piper_cache_hit"] = hit
            combined["piper_cache_hit_real"] = False
            reason = hit.get("error") or "segunda chamada não retornou cache_hit=true"
            combined["logs"] = list(miss.get("logs") or [])[:4] + ["cache hit inválido: " + str(reason)] + list(hit.get("logs") or [])[:3]
            return combined
        combined = dict(hit)
        combined["piper_cache_miss"] = miss
        combined["piper_cache_hit"] = hit
        combined["piper_cache_hit_real"] = True
        combined["worker_synth_ms"] = miss.get("worker_synth_ms")
        combined["total_ms"] = hit.get("total_ms")
        logs = []
        logs.extend(list(miss.get("logs") or [])[:3])
        logs.extend(list(hit.get("logs") or [])[:4])
        combined["logs"] = logs[:7]
        return combined

    def _format_tts_benchmark_engine_block(self, engine: str, local: dict[str, Any], worker: dict[str, Any]) -> list[str]:
        local_ok = bool(local.get("ok"))
        worker_ok = bool(worker.get("ok"))
        local_ms = local.get("elapsed_ms")
        worker_total_ms = worker.get("total_ms")
        worker_synth_ms = worker.get("worker_synth_ms")
        if engine == "piper":
            miss = worker.get("piper_cache_miss") if isinstance(worker.get("piper_cache_miss"), dict) else None
            hit = worker.get("piper_cache_hit") if isinstance(worker.get("piper_cache_hit"), dict) else None
            hit_real = bool(worker.get("piper_cache_hit_real")) or (bool(hit and hit.get("ok")) and bool(hit and hit.get("cache_hit")))
            if hit_real:
                title = "Piper funcional · cache hit real"
            elif worker_ok and hit is not None:
                title = "Piper funcional, mas cache hit falhou"
            elif worker_ok:
                title = "Piper funcional, mas cache hit não foi medido"
            else:
                title = "Piper falhou no worker"
            lines = [f"**piper** — {title}"]
            lines.append("VPS: indisponível · Piper experimental roda apenas no phone-worker turbo")
            if worker_ok:
                miss_total = miss.get("total_ms") if miss else worker.get("total_ms")
                miss_synth = miss.get("worker_synth_ms") if miss else worker.get("worker_synth_ms")
                miss_size = int((miss or worker).get("size") or 0)
                lines.append(
                    f"Worker geração/miss: ok · total {self._format_tts_benchmark_ms(miss_total)}"
                    + (f" · synth {self._format_tts_benchmark_ms(miss_synth)}" if miss_synth is not None else "")
                    + f" · {miss_size} B"
                )
                if hit is not None:
                    hit_total = hit.get("total_ms")
                    hit_read = hit.get("cache_read_ms")
                    hit_size = int(hit.get("size") or 0)
                    if hit_real:
                        lines.append(
                            f"Worker cache hit: ok · total {self._format_tts_benchmark_ms(hit_total)}"
                            + (f" · read {self._format_tts_benchmark_ms(hit_read)}" if hit_read is not None else "")
                            + f" · {hit_size} B"
                            + (f" · key `{hit.get('cache_key')}`" if hit.get("cache_key") else "")
                        )
                    else:
                        lines.append(
                            f"Worker cache hit: falhou/ inválido · total {self._format_tts_benchmark_ms(hit_total)}"
                            + f" · {hit.get('error') or 'cache_hit não confirmado'}"
                            + (f" · key `{hit.get('cache_key')}`" if hit.get("cache_key") else "")
                        )
            else:
                lines.append(f"Worker: falhou · total {self._format_tts_benchmark_ms(worker_total_ms)} · {worker.get('error') or 'erro sem detalhe'}")
            logs: list[str] = []
            for source, data in (("Worker", worker),):
                raw_logs = data.get("logs") if isinstance(data.get("logs"), list) else []
                for entry in raw_logs[:6]:
                    logs.append(f"{source}: {self._short_tts_benchmark_text(entry, limit=120)}")
            if logs:
                lines.append("Logs curtas: " + " | ".join(logs[:6]))
            return lines

        if local_ok and worker_ok:
            winner = self._format_tts_benchmark_delta(local_ms, worker_total_ms)
        elif local_ok:
            winner = "só VPS funcionou"
        elif worker_ok:
            winner = "só worker funcionou"
        else:
            winner = "ambos falharam"
        lines = [f"**{engine}** — {winner}"]
        lines.append(
            f"VPS: {'ok' if local_ok else 'falhou'} · {self._format_tts_benchmark_ms(local_ms)}"
            + (f" · {int(local.get('size') or 0)} B" if local_ok else f" · {local.get('error') or 'erro sem detalhe'}")
        )
        lines.append(
            f"Worker: {'ok' if worker_ok else 'falhou'} · total {self._format_tts_benchmark_ms(worker_total_ms)}"
            + (f" · synth {self._format_tts_benchmark_ms(worker_synth_ms)} · {int(worker.get('size') or 0)} B" if worker_ok else f" · {worker.get('error') or 'erro sem detalhe'}")
        )
        logs: list[str] = []
        for source, data in (("VPS", local), ("Worker", worker)):
            raw_logs = data.get("logs") if isinstance(data.get("logs"), list) else []
            for entry in raw_logs[:2]:
                logs.append(f"{source}: {self._short_tts_benchmark_text(entry, limit=120)}")
        if logs:
            lines.append("Logs curtas: " + " | ".join(logs[:4]))
        return lines

    async def _send_tts_turbo_benchmark_report(self, channel: Any, base_item: QueueItem, resolved: dict[str, Any] | None) -> None:
        benchmark_text = TTS_TURBO_BENCHMARK_TRIGGER_TEXT
        engines = ("edge", "gtts", "gcloud", "piper")
        started = time.monotonic()
        results: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
        worker_meta: dict[str, Any] = {}
        for engine in engines:
            item = self._build_tts_benchmark_item(base_item, engine, resolved, text=benchmark_text)
            local_task = asyncio.create_task(self._tts_benchmark_local_engine(item))
            worker_task = asyncio.create_task(self._tts_benchmark_worker_engine(item))
            local, worker = await asyncio.gather(local_task, worker_task)
            if worker.get("worker_profile") or worker.get("worker_version"):
                worker_meta = worker
            results.append((engine, local, worker))

        total_ms = (time.monotonic() - started) * 1000.0
        good_comparisons = 0
        worker_wins = 0
        local_wins = 0
        best_saving_ms = 0.0
        for _, local, worker in results:
            if local.get("ok") and worker.get("ok"):
                good_comparisons += 1
                try:
                    delta = float(local.get("elapsed_ms") or 0.0) - float(worker.get("total_ms") or 0.0)
                except Exception:
                    delta = 0.0
                if delta > 0:
                    worker_wins += 1
                    best_saving_ms = max(best_saving_ms, delta)
                elif delta < 0:
                    local_wins += 1
        piper_hit_ms = None
        piper_miss_ms = None
        piper_hit_real = False
        piper_cache_error = ""
        for engine, _, worker in results:
            if engine == "piper" and (worker.get("ok") or worker.get("piper_cache_miss") or worker.get("piper_cache_hit")):
                hit = worker.get("piper_cache_hit") if isinstance(worker.get("piper_cache_hit"), dict) else None
                miss = worker.get("piper_cache_miss") if isinstance(worker.get("piper_cache_miss"), dict) else None
                piper_hit_real = bool(worker.get("piper_cache_hit_real")) or (bool(hit and hit.get("ok")) and bool(hit and hit.get("cache_hit")))
                piper_hit_ms = hit.get("total_ms") if hit else None
                piper_miss_ms = miss.get("total_ms") if miss else worker.get("worker_synth_ms")
                if hit and not piper_hit_real:
                    piper_cache_error = str(hit.get("error") or "cache_hit não confirmado")
                break

        if good_comparisons <= 0:
            verdict = "não deu para comparar com segurança: nenhuma engine teve os dois lados ok."
        elif worker_wins >= 2:
            verdict = f"worker turbo parece promissor ({worker_wins}/{good_comparisons} vitórias; melhor ganho {best_saving_ms:.0f} ms)."
        elif worker_wins == 1:
            verdict = "worker turbo ganhou só em uma engine; ainda não dá para usar em TTS real."
        else:
            verdict = "VPS foi igual ou melhor em edge/gtts; worker deve continuar opcional."
        if piper_miss_ms is not None:
            if piper_hit_real and piper_hit_ms is not None:
                verdict += f" Piper: miss {self._format_tts_benchmark_ms(piper_miss_ms)}; cache hit real {self._format_tts_benchmark_ms(piper_hit_ms)} — recomendado quando cacheado."
            else:
                detail = f" ({self._short_tts_benchmark_text(piper_cache_error, limit=90)})" if piper_cache_error else ""
                verdict += f" Piper: miss {self._format_tts_benchmark_ms(piper_miss_ms)}; cache hit ainda não validado{detail}."

        header = [
            "🧪 **Benchmark TTS Worker Turbo**",
            f"Servidor autorizado: `{TTS_TURBO_BENCHMARK_GUILD_ID}` · texto: `{benchmark_text}` · total: {self._format_tts_benchmark_ms(total_ms)}",
        ]
        if worker_meta:
            header.append(
                f"Worker: perfil `{worker_meta.get('worker_profile') or '?'}` · versão `{worker_meta.get('worker_version') or '?'}`"
            )
        blocks: list[str] = []
        for engine, local, worker in results:
            blocks.append("\n".join(self._format_tts_benchmark_engine_block(engine, local, worker)))
        footer = f"**Resumo:** {verdict}"
        content = "\n".join(header + ["", *blocks, "", footer])
        if len(content) > 1900:
            content = content[:1880] + "\n… relatório cortado para caber na mensagem."
        try:
            await channel.send(content, allowed_mentions=discord.AllowedMentions.none())
        except Exception:
            logger.exception("[tts_benchmark] falha ao enviar relatório no canal")

    def _schedule_tts_turbo_benchmark_if_needed(self, message: discord.Message, active_prefix: str, item: QueueItem, resolved: dict[str, Any] | None) -> bool:
        if not self._should_run_tts_turbo_benchmark(message, active_prefix):
            return False
        channel = getattr(message, "channel", None)
        if channel is None:
            return False
        task = asyncio.create_task(self._send_tts_turbo_benchmark_report(channel, item, resolved))
        task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
        return True

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

                worker_cached = await self._try_get_worker_turbo_cache_path(item)
                if worker_cached:
                    if store_in_cache:
                        cached_path = await self._store_in_cache(state, item, worker_cached)
                        self._schedule_worker_turbo_cache_store(item, cached_path)
                        return cached_path, False
                    return worker_cached, False

            generated = await self._generate_audio_file(item)
            if store_in_cache:
                cached_path = await self._store_in_cache(state, item, generated)
                self._schedule_worker_turbo_cache_store(item, cached_path)
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

    async def _run_timed_generation(self, engine: str, factory, *, guild_id: int | None = None) -> str:
        started_at = time.monotonic()
        try:
            result = await factory()
        except Exception as exc:
            duration_ms = (time.monotonic() - started_at) * 1000.0
            self._record_engine_failure(engine, exc, duration_ms=duration_ms)
            raise
        duration_ms = (time.monotonic() - started_at) * 1000.0
        self._record_engine_success(engine, duration_ms)
        await self._record_persistent_synt_success(guild_id, engine)
        return result

    async def _generate_audio_file(self, item: QueueItem) -> str:
        if item.engine == "piper":
            try:
                return await self._run_timed_generation(
                    "piper",
                    lambda: self._generate_piper_worker_file(item),
                    guild_id=item.guild_id,
                )
            except Exception as e:
                logger.warning("[tts_piper] Piper experimental falhou, usando fallback local | guild=%s erro=%s", item.guild_id, e)
                return await self._generate_piper_fallback_file(item)

        if item.engine == "edge":
            try:
                return await self._run_timed_generation(
                    "edge",
                    lambda: self._generate_edge_file(item.text, item.voice, item.rate, item.pitch),
                    guild_id=item.guild_id,
                )
            except Exception as e:
                logger.warning("[tts_voice] Edge falhou, usando gTTS | guild=%s erro=%s", item.guild_id, e)
                return await self._run_timed_generation(
                    "gtts",
                    lambda: self._generate_gtts_file(item.text, item.language),
                    guild_id=item.guild_id,
                )

        if item.engine == "gcloud":
            try:
                return await self._run_timed_generation(
                    "gcloud",
                    lambda: self._generate_google_cloud_file(item.text, item.language, item.voice, item.rate, item.pitch),
                    guild_id=item.guild_id,
                )
            except Exception as e:
                logger.warning("[tts_voice] Google Cloud TTS falhou, usando gTTS | guild=%s erro=%s", item.guild_id, e)
                return await self._run_timed_generation(
                    "gtts",
                    lambda: self._generate_gtts_file(item.text, item.language),
                    guild_id=item.guild_id,
                )

        return await self._run_timed_generation(
            "gtts",
            lambda: self._generate_gtts_file(item.text, item.language),
            guild_id=item.guild_id,
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

    async def _play_file(self, vc: discord.VoiceClient, path: str, *, item: QueueItem | None = None) -> dict[str, float]:
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

        try:
            router = getattr(getattr(self, "bot", None), "audio_router", None)
            play_tts = getattr(router, "play_tts", None)
            if callable(play_tts) and guild is not None:
                router_result = await play_tts(
                    guild=guild,
                    vc=vc,
                    path=path,
                    before_options=TTS_FFMPEG_BEFORE_OPTIONS,
                    options=TTS_FFMPEG_OPTIONS,
                    timeout=self._estimate_playback_timeout(item),
                    item=item,
                )
                if not (isinstance(router_result, dict) and router_result.get("tts_lavalink_failed")):
                    return router_result

                fallback = getattr(router, "prepare_tts_local_fallback_after_lavalink_failure", None)
                if callable(fallback):
                    reason = str(router_result.get("tts_lavalink_error") or router_result.get("error") or "tts_lavalink_failed")
                    fallback_vc = await fallback(guild, vc, reason=reason)
                    if fallback_vc is not None and not getattr(router, "_is_lavalink_voice_client", lambda _vc: False)(fallback_vc):
                        vc = fallback_vc
                        logger.warning(
                            "[tts_voice] TTS via Lavalink falhou; usando playback local direto | guild=%s reason=%s",
                            getattr(guild, "id", None),
                            reason,
                        )
                    else:
                        return router_result
                else:
                    return router_result

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
            playback_timeout = self._estimate_playback_timeout(item)
            try:
                await asyncio.wait_for(finished, timeout=playback_timeout)
            except asyncio.TimeoutError as exc:
                with contextlib.suppress(Exception):
                    if self._voice_client_is_playing_or_paused(vc):
                        vc.stop()
                raise RuntimeError(f"Playback timeout após {playback_timeout:.1f}s") from exc
            playback_duration_ms = max(0.0, (time.monotonic() - playback_started_at) * 1000.0)
            return {
                "source_setup_ms": source_setup_ms,
                "play_call_ms": play_call_ms,
                "playback_ms": playback_duration_ms,
                "playback_started_at": playback_started_at,
            }
        finally:
            pass

    def _is_music_active_for_guild(self, guild_id: int) -> bool:
        router = getattr(getattr(self, "bot", None), "audio_router", None)
        is_music_active = getattr(router, "is_music_active", None)
        if not callable(is_music_active):
            return False
        with contextlib.suppress(Exception):
            return bool(is_music_active(int(guild_id)))
        return False

    async def _reset_voice_client(self, guild: discord.Guild, *, reason: str = "unknown") -> None:
        lock_getter = getattr(self, "_get_voice_connect_lock", None)
        lock = lock_getter(guild.id) if callable(lock_getter) else None

        async def _do_reset() -> None:
            vc = self._get_voice_client_for_guild(guild)
            if vc is None:
                return
            if getattr(self, "_is_lavalink_voice_client", lambda _vc: False)(vc):
                logger.info("[tts_voice] reset de voice client ignorado | player Lavalink ativo | guild=%s reason=%s", guild.id, reason)
                return
            try:
                if self._voice_client_is_playing_or_paused(vc):
                    vc.stop()
            except Exception:
                pass
            try:
                await vc.disconnect(force=True)
            except Exception as exc:
                logger.warning("[tts_voice] Falha ao resetar voice client | guild=%s reason=%s erro=%s", guild.id, reason, exc)
            state = self.guild_states.get(guild.id)
            if state is not None:
                state.last_channel_id = None
                state.last_hard_reset_at = time.monotonic()

        if lock is None:
            await _do_reset()
            return

        async with lock:
            await _do_reset()

    async def _play_file_with_recovery(self, guild: discord.Guild, item: QueueItem, vc: discord.VoiceClient, path: str) -> dict[str, float]:
        current_vc = vc
        last_error: Exception | None = None
        state = self.guild_states.get(guild.id)
        for attempt in range(2):
            try:
                return await self._play_file(current_vc, path, item=item)
            except Exception as exc:
                last_error = exc
                music_active = self._is_music_active_for_guild(guild.id)
                if music_active:
                    logger.warning(
                        "[tts_voice] Falha no playback do TTS com música ativa; descartando só este TTS sem resetar a call | guild=%s channel=%s erro=%s",
                        guild.id,
                        item.channel_id,
                        exc,
                    )
                    now = time.monotonic()
                    return {
                        "source_setup_ms": 0.0,
                        "play_call_ms": 0.0,
                        "playback_ms": 0.0,
                        "playback_started_at": now,
                        "tts_discarded": True,
                    }

                logger.warning(
                    "[tts_voice] Falha no playback, tentando recuperar | guild=%s channel=%s tentativa=%s erro=%s",
                    guild.id,
                    item.channel_id,
                    attempt + 1,
                    exc,
                )
                if attempt >= 1:
                    break

                last_hard_reset_at = float(getattr(state, "last_hard_reset_at", 0.0) or 0.0) if state is not None else 0.0
                time_since_reset = time.monotonic() - last_hard_reset_at if last_hard_reset_at > 0.0 else TTS_VOICE_HARD_RESET_COOLDOWN_SECONDS
                should_suppress_hard_reset = time_since_reset < TTS_VOICE_HARD_RESET_COOLDOWN_SECONDS

                if should_suppress_hard_reset:
                    logger.warning(
                        "[tts_voice] Hard reset suprimido para evitar reconexão em loop | guild=%s channel=%s cooldown_restante=%.2fs",
                        guild.id,
                        item.channel_id,
                        max(0.0, TTS_VOICE_HARD_RESET_COOLDOWN_SECONDS - time_since_reset),
                    )
                else:
                    await self._reset_voice_client(guild, reason=f"playback_failure:{type(exc).__name__}")
                    await asyncio.sleep(0.25)

                current_vc = await self._ensure_connected_fast(guild, item)
                if current_vc is None:
                    break
        if last_error is None:
            raise RuntimeError("Falha desconhecida no playback do TTS")
        raise last_error

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
        if vc is None or not self._voice_client_is_connected(vc) or self._voice_client_channel(vc) is None:
            return True

        router = getattr(getattr(self, "bot", None), "audio_router", None)
        is_music_active = getattr(router, "is_music_active", None)
        if callable(is_music_active):
            with contextlib.suppress(Exception):
                if is_music_active(guild.id):
                    self._log_debug(f"[tts_voice] Idle timeout ignorado | player de música ativo | guild={guild.id}")
                    return False

        should_defer = getattr(router, "should_defer_tts_auto_leave", None)
        if callable(should_defer):
            with contextlib.suppress(Exception):
                if should_defer(guild.id):
                    schedule_idle = getattr(router, "schedule_music_idle_disconnect", None)
                    if callable(schedule_idle):
                        await schedule_idle(guild.id)
                    self._log_debug(f"[tts_voice] Idle timeout adiado | sessão de música aguardando timeout | guild={guild.id}")
                    return False

        members = list(getattr(self._voice_client_channel(vc), "members", []))
        humans = [m for m in members if not m.bot]
        if humans:
            self._log_debug(f"[tts_voice] Idle timeout ignorado | ainda há humanos na call | guild={guild.id}")
            return False

        try:
            await vc.disconnect(force=False)
            if hasattr(self, "_clear_remembered_voice_channel"):
                with contextlib.suppress(Exception):
                    await self._maybe_await(self._clear_remembered_voice_channel(guild.id))
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
        if getattr(self, "_is_lavalink_voice_client", lambda _vc: False)(vc):
            if not getattr(self, "_lavalink_music_should_own_voice", lambda _guild: False)(guild):
                # Wavelink ficou como voice_client fantasma depois de stop/falha,
                # mas o roteador não está usando Lavalink agora. Limpe para o TTS
                # local conseguir conectar/tocar normalmente.
                with contextlib.suppress(Exception):
                    await vc.disconnect(force=True)
                vc = None
            else:
                lavalink_channel = self._voice_client_channel(vc) or getattr(getattr(guild, "me", None), "voice", None) and getattr(getattr(guild, "me", None).voice, "channel", None)
                lavalink_channel_id = getattr(lavalink_channel, "id", None)
                if lavalink_channel_id is not None and lavalink_channel_id != item.channel_id:
                    now = time.monotonic()
                    if now >= float(getattr(state, "lavalink_ignore_logged_until", 0.0) or 0.0):
                        logger.info(
                            "[tts_voice] TTS ignorado porque o Lavalink está em outro canal | guild=%s lavalink_channel=%s tts_channel=%s",
                            guild.id,
                            lavalink_channel_id,
                            item.channel_id,
                        )
                        state.lavalink_ignore_logged_until = now + 20.0
                    return None
                state.last_channel_id = int(lavalink_channel_id or item.channel_id)
                logger.debug("[tts_voice] TTS encaminhado para reprodução via Lavalink | guild=%s channel=%s", guild.id, state.last_channel_id)
                return vc

        lavalink_voice_guard = getattr(self, "_lavalink_music_should_own_voice", None)
        if callable(lavalink_voice_guard):
            try:
                if lavalink_voice_guard(guild):
                    now = time.monotonic()
                    if now >= float(getattr(state, "lavalink_ignore_logged_until", 0.0) or 0.0):
                        logger.info(
                            "[tts_voice] TTS local ignorado porque o player de música via Wavelink está assumindo a voz | guild=%s tts_channel=%s",
                            guild.id,
                            item.channel_id,
                        )
                        state.lavalink_ignore_logged_until = now + 20.0
                    return None
            except Exception:
                logger.debug("[tts_voice] falha ao consultar guarda Lavalink antes do TTS local", exc_info=True)

        is_receive_client = bool(vc is not None and hasattr(vc, "listen") and hasattr(vc, "is_listening"))
        if vc is not None and self._voice_client_is_connected(vc):
            if is_receive_client:
                with contextlib.suppress(Exception):
                    await vc.disconnect(force=True)
                vc = None
            elif self._voice_client_channel(vc) is not None and self._voice_client_channel(vc).id == item.channel_id:
                await self._ensure_self_deaf_fast(guild, target_channel)
                state.last_channel_id = item.channel_id
                return vc
            else:
                try:
                    await vc.move_to(target_channel)
                    await self._ensure_self_deaf_fast(guild, target_channel)
                    state.last_channel_id = item.channel_id
                    return vc
                except Exception:
                    pass

        vc = await self._maybe_await(self._ensure_connected(
            guild,
            target_channel,
            notify_owner_on_failure=True,
            failure_context=f"entrada automática do TTS para reproduzir mensagem de {item.author_id}",
        ))
        if vc is None:
            current = self._get_voice_client_for_guild(guild)
            if current is not None and self._voice_client_is_connected(current):
                if self._voice_client_channel(current) is not None and self._voice_client_channel(current).id == item.channel_id:
                    state.last_channel_id = item.channel_id
                    return current
            return None

        if self._voice_client_is_connected(vc):
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
                        if time.monotonic() >= float(getattr(state, "lavalink_ignore_logged_until", 0.0) or 0.0):
                            logger.warning("[tts_voice] Worker não conseguiu conectar | guild=%s channel=%s", guild_id, item.channel_id)
                        continue

                    current_path, should_cleanup = await active_audio_task
                    if not current_path or not os.path.isfile(current_path) or os.path.getsize(current_path) <= 0:
                        logger.warning(
                            "[tts_voice] áudio temporário sumiu antes do playback; descartando item sem resetar voice | guild=%s channel=%s path=%s",
                            guild_id,
                            item.channel_id,
                            current_path,
                        )
                        if should_cleanup and current_path:
                            with contextlib.suppress(Exception):
                                os.remove(current_path)
                        continue

                    dequeue_started_at = float(getattr(item, "_dequeued_at_monotonic", time.monotonic()))
                    queue_wait_ms = max(0.0, (dequeue_started_at - float(getattr(item, "enqueued_at_monotonic", dequeue_started_at))) * 1000.0)

                    try:
                        playback_result = await self._play_file_with_recovery(guild, item, vc, current_path)
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
