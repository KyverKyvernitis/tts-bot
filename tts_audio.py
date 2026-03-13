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
from tts_helpers import validate_voice


GTTS_DEFAULT_LANGUAGE = getattr(config, "GTTS_DEFAULT_LANGUAGE", "pt")
TTS_IDLE_DISCONNECT_SECONDS = int(getattr(config, "TTS_IDLE_DISCONNECT_SECONDS", 240))
TTS_AUDIO_CACHE_SIZE = int(getattr(config, "TTS_AUDIO_CACHE_SIZE", 128))
TTS_AUDIO_CACHE_TTL_SECONDS = int(getattr(config, "TTS_AUDIO_CACHE_TTL_SECONDS", 900))
TTS_DEBUG_LOGS = bool(getattr(config, "TTS_DEBUG_LOGS", False))
TTS_WARM_HOLD_SECONDS = float(getattr(config, "TTS_WARM_HOLD_SECONDS", 30))
TTS_QUEUE_MAXSIZE = max(1, int(getattr(config, "TTS_QUEUE_MAXSIZE", 20)))
TTS_SYNTH_CONCURRENCY = max(1, int(getattr(config, "TTS_SYNTH_CONCURRENCY", 2)))
TTS_EDGE_TIMEOUT_SECONDS = max(1.0, float(getattr(config, "TTS_EDGE_TIMEOUT_SECONDS", 10)))
TTS_GTTS_MAX_RETRIES = max(0, int(getattr(config, "TTS_GTTS_MAX_RETRIES", 2)))
TTS_GTTS_RETRY_BASE_DELAY_SECONDS = max(0.25, float(getattr(config, "TTS_GTTS_RETRY_BASE_DELAY_SECONDS", 1.25)))
TTS_GTTS_TLDS = tuple(
    str(getattr(config, "TTS_GTTS_TLDS", "com,com.br,com.hk,ie,co.uk") or "com,com.br,com.hk,ie,co.uk").split(",")
)
TTS_GTTS_MIN_INTERVAL_SECONDS = max(0.0, float(getattr(config, "TTS_GTTS_MIN_INTERVAL_SECONDS", 1.35)))
TTS_GTTS_RATE_LIMIT_COOLDOWN_SECONDS = max(0.0, float(getattr(config, "TTS_GTTS_RATE_LIMIT_COOLDOWN_SECONDS", 8.0)))
TTS_GTTS_CONCURRENCY = max(1, int(getattr(config, "TTS_GTTS_CONCURRENCY", 1)))
GOOGLE_CLOUD_TTS_LANGUAGE_CODE = str(getattr(config, "GOOGLE_CLOUD_TTS_LANGUAGE_CODE", "pt-BR") or "pt-BR").strip() or "pt-BR"
GOOGLE_CLOUD_TTS_VOICE_NAME = str(getattr(config, "GOOGLE_CLOUD_TTS_VOICE_NAME", "pt-BR-Standard-A") or "pt-BR-Standard-A").strip() or "pt-BR-Standard-A"
GOOGLE_CLOUD_TTS_SPEAKING_RATE = float(getattr(config, "GOOGLE_CLOUD_TTS_SPEAKING_RATE", 1.0))
GOOGLE_CLOUD_TTS_PITCH = float(getattr(config, "GOOGLE_CLOUD_TTS_PITCH", 0.0))
TTS_FFMPEG_BEFORE_OPTIONS = getattr(config, "TTS_FFMPEG_BEFORE_OPTIONS", "-nostdin")
TTS_FFMPEG_OPTIONS = getattr(config, "TTS_FFMPEG_OPTIONS", "-vn -loglevel error")

_CACHE_DIR = os.path.join(tempfile.gettempdir(), "chat_revive_tts_cache")
os.makedirs(_CACHE_DIR, exist_ok=True)

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


@dataclass
class GuildTTSState:
    queue: asyncio.Queue
    worker_task: Optional[asyncio.Task] = None
    last_text_channel_id: Optional[int] = None
    last_channel_id: Optional[int] = None
    warmed_until: float = 0.0
    cache_order: OrderedDict[str, float] = field(default_factory=OrderedDict)


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

    async def _enqueue_tts_item(self, guild_id: int, item: QueueItem) -> tuple[bool, int]:
        state = self._get_state(guild_id)
        dropped = 0

        while state.queue.full():
            try:
                state.queue.get_nowait()
                state.queue.task_done()
                dropped += 1
            except asyncio.QueueEmpty:
                break

        await state.queue.put(item)
        return dropped == 0, dropped

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

    async def _wait_for_gtts_slot(self) -> None:
        lock = self._get_gtts_rate_lock()
        async with lock:
            now = time.monotonic()
            next_request_at = float(getattr(self, "_tts_gtts_next_request_at", 0.0) or 0.0)
            rate_limited_until = float(getattr(self, "_tts_gtts_rate_limited_until", 0.0) or 0.0)
            ready_at = max(now, next_request_at, rate_limited_until)
            delay = ready_at - now
            if delay > 0:
                await asyncio.sleep(delay)
            setattr(self, "_tts_gtts_next_request_at", time.monotonic() + TTS_GTTS_MIN_INTERVAL_SECONDS)

    async def _note_gtts_rate_limit(self) -> float:
        lock = self._get_gtts_rate_lock()
        async with lock:
            now = time.monotonic()
            cooldown_until = max(now, float(getattr(self, "_tts_gtts_rate_limited_until", 0.0) or 0.0)) + TTS_GTTS_RATE_LIMIT_COOLDOWN_SECONDS
            setattr(self, "_tts_gtts_rate_limited_until", cooldown_until)
            return max(0.0, cooldown_until - now)

    def _get_global_cache_order(self) -> OrderedDict[str, float]:
        cache_order = getattr(self, "_tts_cache_order", None)
        if cache_order is None:
            cache_order = OrderedDict()
            setattr(self, "_tts_cache_order", cache_order)
        return cache_order

    def _touch_cache_entry(self, state: GuildTTSState, key: str) -> None:
        cache_order = self._get_global_cache_order()
        now = time.time()
        cache_order[key] = now
        cache_order.move_to_end(key)

    def _purge_cache(self, state: GuildTTSState) -> None:
        now = time.time()
        cache_order = self._get_global_cache_order()

        expired = []
        for key in list(cache_order.keys()):
            path = self._cache_path(key)
            ts = cache_order.get(key, 0.0)
            if (not os.path.exists(path)) or (now - ts > TTS_AUDIO_CACHE_TTL_SECONDS):
                expired.append(key)

        for key in expired:
            cache_order.pop(key, None)
            path = self._cache_path(key)
            try:
                if os.path.exists(path):
                    os.remove(path)
            except Exception:
                pass

        while len(cache_order) > TTS_AUDIO_CACHE_SIZE:
            oldest_key, _ = cache_order.popitem(last=False)
            path = self._cache_path(oldest_key)
            try:
                if os.path.exists(path):
                    os.remove(path)
            except Exception:
                pass

    async def _store_in_cache(self, state: GuildTTSState, item: QueueItem, source_path: str) -> str:
        key = self._cache_key(item)
        path = self._cache_path(key)

        if os.path.exists(path):
            self._touch_cache_entry(state, key)
            self._purge_cache(state)
            return path

        try:
            await asyncio.to_thread(shutil.copyfile, source_path, path)
        except Exception:
            return source_path

        self._touch_cache_entry(state, key)
        self._purge_cache(state)
        return path

    def _cache_key(self, item: QueueItem) -> str:
        text = self._normalize_cache_text(item.text)
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
            language = (item.language or GTTS_DEFAULT_LANGUAGE).strip().lower()
            payload = f"gtts|{language}|{text}"
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _cache_path(self, key: str) -> str:
        return os.path.join(_CACHE_DIR, f"{key}.mp3")


    def _try_get_cached_path(self, state: GuildTTSState, item: QueueItem) -> Optional[str]:
        key = self._cache_key(item)
        path = self._cache_path(key)

        if not os.path.exists(path):
            return None

        age = time.time() - os.path.getmtime(path)
        if age > TTS_AUDIO_CACHE_TTL_SECONDS:
            try:
                os.remove(path)
            except Exception:
                pass
            self._get_global_cache_order().pop(key, None)
            return None

        self._touch_cache_entry(state, key)
        self._purge_cache(state)
        self._log_debug(f"[tts_voice] cache hit | guild={item.guild_id} key={key[:10]}")
        return path


    def _get_gtts_tlds(self) -> tuple[str, ...]:
        values = [str(tld or "").strip() for tld in TTS_GTTS_TLDS]
        values = [value for value in values if value]
        return tuple(dict.fromkeys(values)) or ("com",)

    def _is_gtts_rate_limit_error(self, error: Exception) -> bool:
        if isinstance(error, gTTSError):
            message = str(error).lower()
            if "429" in message or "too many requests" in message:
                return True

        current = error
        visited: set[int] = set()
        while current is not None and id(current) not in visited:
            visited.add(id(current))
            message = str(current).lower()
            if "429" in message or "too many requests" in message:
                return True
            current = getattr(current, "__cause__", None) or getattr(current, "__context__", None)

        return False

    async def _generate_gtts_file(self, text: str, language: str, *, tld: str = "com") -> str:
        language = (language or GTTS_DEFAULT_LANGUAGE).strip().lower()
        tld = str(tld or "com").strip() or "com"
        self._log_debug(f"[tts_voice] gTTS synth | language={language!r} tld={tld!r} text={text[:80]!r}")

        fd, path = tempfile.mkstemp(suffix=".mp3")
        os.close(fd)
        try:
            tts = gTTS(text=text, lang=language, tld=tld)
            async with self._get_gtts_semaphore():
                await self._wait_for_gtts_slot()
                await asyncio.to_thread(tts.save, path)
            return path
        except Exception:
            try:
                os.remove(path)
            except Exception:
                pass
            raise

    async def _generate_gtts_with_retries(self, item: QueueItem) -> str:
        language = (item.language or GTTS_DEFAULT_LANGUAGE).strip().lower()
        tlds = self._get_gtts_tlds()
        attempts = max(1, TTS_GTTS_MAX_RETRIES + 1, len(tlds))
        last_error: Optional[Exception] = None

        for attempt in range(attempts):
            tld = tlds[attempt % len(tlds)]
            try:
                return await self._generate_gtts_file(item.text, language, tld=tld)
            except Exception as error:
                last_error = error
                if not self._is_gtts_rate_limit_error(error) or attempt >= attempts - 1:
                    break

                cooldown_delay = await self._note_gtts_rate_limit()
                retry_delay = TTS_GTTS_RETRY_BASE_DELAY_SECONDS * (2 ** attempt)
                delay = max(cooldown_delay, retry_delay, TTS_GTTS_MIN_INTERVAL_SECONDS)
                logger.warning(
                    "[tts_voice] gTTS limitou a requisição, tentando novamente | guild=%s attempt=%s/%s tld=%s wait=%.2fs",
                    item.guild_id,
                    attempt + 1,
                    attempts,
                    tld,
                    delay,
                )
                await asyncio.sleep(delay)

        if last_error is not None:
            raise last_error
        raise RuntimeError("Falha ao sintetizar áudio com gTTS.")

    async def _generate_edge_file(self, text: str, voice: str, rate: str, pitch: str) -> str:
        voice = validate_voice(voice, getattr(self, "edge_voice_names", set()))
        rate = self._normalize_edge_rate(rate)
        pitch = self._normalize_edge_pitch(pitch)

        self._log_debug(
            "[tts_voice] Edge synth | "
            f"voice={voice!r} rate={rate!r} pitch={pitch!r} text={text[:80]!r}"
        )

        fd, path = tempfile.mkstemp(suffix=".mp3")
        os.close(fd)
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

        path = os.path.join(tempfile.gettempdir(), "chat_revive_google_credentials.json")
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

        fd, path = tempfile.mkstemp(suffix=".mp3")
        os.close(fd)

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

    async def _generate_audio_file(self, item: QueueItem) -> str:
        if item.engine == "edge":
            try:
                return await self._generate_edge_file(item.text, item.voice, item.rate, item.pitch)
            except Exception as e:
                logger.warning("[tts_voice] Edge falhou, usando gTTS | guild=%s erro=%s", item.guild_id, e)
                return await self._generate_gtts_with_retries(item)

        if item.engine == "gcloud":
            return await self._generate_google_cloud_file(item.text, item.language, item.voice, item.rate, item.pitch)

        return await self._generate_gtts_with_retries(item)

    async def _resolve_audio_path(self, state: GuildTTSState, item: QueueItem) -> tuple[str, bool]:
        cached = self._try_get_cached_path(state, item)
        if cached:
            return cached, False

        generated = await self._generate_audio_file(item)

        should_cache = len(self._normalize_cache_text(item.text)) <= 220
        if should_cache:
            cached_path = await self._store_in_cache(state, item, generated)
            if cached_path != generated:
                try:
                    os.remove(generated)
                except Exception:
                    pass
                return cached_path, False

        return generated, True

    async def _play_file(self, vc: discord.VoiceClient, path: str) -> None:
        loop = asyncio.get_running_loop()
        finished = loop.create_future()

        def _after_playback(error: Optional[Exception]) -> None:
            if error:
                if not finished.done():
                    loop.call_soon_threadsafe(finished.set_exception, error)
            else:
                if not finished.done():
                    loop.call_soon_threadsafe(finished.set_result, None)

        source = discord.FFmpegPCMAudio(
            path,
            before_options=TTS_FFMPEG_BEFORE_OPTIONS,
            options=TTS_FFMPEG_OPTIONS,
        )
        vc.play(source, after=_after_playback)
        await finished

    async def _disconnect_idle(self, guild: discord.Guild) -> bool:
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
                state.last_channel_id = item.channel_id
                return vc
            try:
                await vc.move_to(target_channel)
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
            state.last_channel_id = item.channel_id
        return vc

    async def _maybe_prefetch_next(self, state: GuildTTSState):
        prefetched_item: Optional[QueueItem] = None
        prefetched_audio_task: Optional[asyncio.Task] = None

        if state.queue.empty():
            return prefetched_item, prefetched_audio_task

        try:
            prefetched_item = state.queue.get_nowait()
        except asyncio.QueueEmpty:
            return None, None

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

                    vc = await connect_task
                    if vc is None:
                        if own_audio_task is not None and not own_audio_task.done():
                            own_audio_task.cancel()
                            with contextlib.suppress(BaseException):
                                await own_audio_task
                        logger.warning("[tts_voice] Worker não conseguiu conectar | guild=%s channel=%s", guild_id, item.channel_id)
                        continue

                    current_path, should_cleanup = await active_audio_task

                    if prefetched_item is None:
                        prefetched_item, prefetched_audio_task = await self._maybe_prefetch_next(state)

                    try:
                        await self._play_file(vc, current_path)
                    finally:
                        if should_cleanup:
                            try:
                                os.remove(current_path)
                            except Exception:
                                pass
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
