import asyncio
import hashlib
import inspect
import os
import shutil
import tempfile
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Optional

import discord
import edge_tts
from gtts import gTTS

import config
from tts_helpers import validate_voice


GTTS_DEFAULT_LANGUAGE = getattr(config, "GTTS_DEFAULT_LANGUAGE", "pt-br")
TTS_IDLE_DISCONNECT_SECONDS = int(getattr(config, "TTS_IDLE_DISCONNECT_SECONDS", 180))
TTS_AUDIO_CACHE_SIZE = int(getattr(config, "TTS_AUDIO_CACHE_SIZE", 64))
TTS_AUDIO_CACHE_TTL_SECONDS = int(getattr(config, "TTS_AUDIO_CACHE_TTL_SECONDS", 900))
TTS_DEBUG_LOGS = bool(getattr(config, "TTS_DEBUG_LOGS", False))

_CACHE_DIR = os.path.join(tempfile.gettempdir(), "chat_revive_tts_cache")
os.makedirs(_CACHE_DIR, exist_ok=True)


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
    last_playback_finished_at: float = 0.0
    cache_order: OrderedDict[str, float] = field(default_factory=OrderedDict)


class TTSAudioMixin:
    def _log_debug(self, text: str) -> None:
        if TTS_DEBUG_LOGS:
            print(text)

    def _get_state(self, guild_id: int) -> GuildTTSState:
        state = self.guild_states.get(guild_id)
        if state is None:
            state = GuildTTSState(queue=asyncio.Queue())
            self.guild_states[guild_id] = state
        return state

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
            value = f"+{value}"
        sign, number = value[0], value[1:]
        if not number.isdigit():
            return "+0Hz"
        return f"{sign}{number}Hz"

    def _normalize_cache_text(self, text: str) -> str:
        return " ".join((text or "").strip().split())

    def _cache_key(self, item: QueueItem) -> str:
        text = self._normalize_cache_text(item.text)
        engine = (item.engine or "gtts").strip().lower()
        if engine == "edge":
            voice = validate_voice(item.voice, getattr(self, "edge_voice_names", set()))
            payload = f"edge|{voice}|{self._normalize_edge_rate(item.rate)}|{self._normalize_edge_pitch(item.pitch)}|{text}"
        else:
            language = (item.language or GTTS_DEFAULT_LANGUAGE).strip().lower()
            payload = f"gtts|{language}|{text}"
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _cache_path(self, key: str) -> str:
        return os.path.join(_CACHE_DIR, f"{key}.mp3")

    def _touch_cache_entry(self, state: GuildTTSState, key: str) -> None:
        now = time.time()
        state.cache_order[key] = now
        state.cache_order.move_to_end(key)

    def _purge_cache(self, state: GuildTTSState) -> None:
        now = time.time()

        expired = []
        for key in list(state.cache_order.keys()):
            path = self._cache_path(key)
            ts = state.cache_order.get(key, 0.0)
            if (not os.path.exists(path)) or (now - ts > TTS_AUDIO_CACHE_TTL_SECONDS):
                expired.append(key)

        for key in expired:
            state.cache_order.pop(key, None)
            path = self._cache_path(key)
            try:
                if os.path.exists(path):
                    os.remove(path)
            except Exception:
                pass

        while len(state.cache_order) > TTS_AUDIO_CACHE_SIZE:
            oldest_key, _ = state.cache_order.popitem(last=False)
            path = self._cache_path(oldest_key)
            try:
                if os.path.exists(path):
                    os.remove(path)
            except Exception:
                pass

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
            state.cache_order.pop(key, None)
            return None

        self._touch_cache_entry(state, key)
        self._purge_cache(state)
        self._log_debug(f"[tts_voice] cache hit | guild={item.guild_id} key={key[:10]}")
        return path

    def _store_in_cache(self, state: GuildTTSState, item: QueueItem, source_path: str) -> str:
        key = self._cache_key(item)
        path = self._cache_path(key)

        try:
            shutil.copyfile(source_path, path)
        except Exception:
            return source_path

        self._touch_cache_entry(state, key)
        self._purge_cache(state)
        return path

    async def _generate_gtts_file(self, text: str, language: str) -> str:
        language = (language or GTTS_DEFAULT_LANGUAGE).strip().lower()
        self._log_debug(f"[tts_voice] gTTS synth | language={language!r} text={text[:80]!r}")

        fd, path = tempfile.mkstemp(suffix=".mp3")
        os.close(fd)
        try:
            gTTS(text=text, lang=language).save(path)
            return path
        except Exception:
            try:
                os.remove(path)
            except Exception:
                pass
            raise

    async def _generate_edge_file(self, text: str, voice: str, rate: str, pitch: str) -> str:
        original_voice, original_rate, original_pitch = voice, rate, pitch
        voice = validate_voice(voice, getattr(self, "edge_voice_names", set()))
        rate = self._normalize_edge_rate(rate)
        pitch = self._normalize_edge_pitch(pitch)

        self._log_debug(
            "[tts_voice] Edge synth | "
            f"voice={voice!r} (orig={original_voice!r}) "
            f"rate={rate!r} (orig={original_rate!r}) "
            f"pitch={pitch!r} (orig={original_pitch!r}) "
            f"text={text[:80]!r}"
        )

        fd, path = tempfile.mkstemp(suffix=".mp3")
        os.close(fd)
        try:
            await edge_tts.Communicate(text=text, voice=voice, rate=rate, pitch=pitch).save(path)
            return path
        except Exception as e:
            print(f"[tts_voice] Edge synth falhou | voice={voice!r} rate={rate!r} pitch={pitch!r} erro={e}")
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
                print(f"[tts_voice] Edge falhou, usando gTTS. Guild {item.guild_id}: {e}")
                return await self._generate_gtts_file(item.text, item.language or GTTS_DEFAULT_LANGUAGE)

        return await self._generate_gtts_file(item.text, item.language or GTTS_DEFAULT_LANGUAGE)

    async def _resolve_audio_path(self, state: GuildTTSState, item: QueueItem) -> tuple[str, bool]:
        cached = self._try_get_cached_path(state, item)
        if cached:
            return cached, False

        generated = await self._generate_audio_file(item)

        should_cache = len(self._normalize_cache_text(item.text)) <= 220
        if should_cache:
            cached_path = self._store_in_cache(state, item, generated)
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

        def _after(error: Optional[Exception]) -> None:
            if error:
                if not finished.done():
                    loop.call_soon_threadsafe(finished.set_exception, error)
            else:
                if not finished.done():
                    loop.call_soon_threadsafe(finished.set_result, None)

        source = discord.FFmpegPCMAudio(
            path,
            before_options="-nostdin",
            options="-vn -loglevel error",
        )
        vc.play(source, after=_after)
        await finished

    async def _disconnect_idle(self, guild: discord.Guild) -> bool:
        vc = guild.voice_client
        if vc is None or not vc.is_connected() or vc.channel is None:
            return True

        members = list(getattr(vc.channel, "members", []))
        humans = [m for m in members if not m.bot]
        if humans:
            self._log_debug(f"[tts_voice] Idle timeout ignorado | ainda há humanos na call | guild={guild.id}")
            return False

        try:
            await vc.disconnect(force=False)
            print(f"[tts_voice] Desconectado por inatividade | sozinho ou só com bots | guild={guild.id}")
            return True
        except Exception as e:
            print(f"[tts_voice] Erro ao desconectar por inatividade na guild {guild.id}: {e}")
            return False

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
                        item = await asyncio.wait_for(state.queue.get(), timeout=TTS_IDLE_DISCONNECT_SECONDS)
                        fetched_from_queue = True
                    except asyncio.TimeoutError:
                        disconnected = await self._disconnect_idle(guild)
                        if disconnected:
                            return
                        continue
                    audio_task = None

                try:
                    target_channel = guild.get_channel(item.channel_id) or self.bot.get_channel(item.channel_id)
                    if target_channel is None:
                        self._log_debug(f"[tts_voice] Canal de voz não encontrado | guild={guild_id} channel={item.channel_id}")
                        continue

                    if hasattr(self, "_should_block_for_voice_bot"):
                        blocked = await self._maybe_await(self._should_block_for_voice_bot(guild, target_channel))
                        if blocked:
                            print(f"[tts_voice] Worker bloqueado por outro bot de voz | guild={guild_id} channel={item.channel_id}")
                            if hasattr(self, "_disconnect_if_blocked"):
                                await self._maybe_await(self._disconnect_if_blocked(guild))
                            continue

                    vc = await self._maybe_await(self._ensure_connected(guild, target_channel))
                    if vc is None:
                        print(f"[tts_voice] Worker não conseguiu conectar | guild={guild_id} channel={item.channel_id}")
                        continue

                    if audio_task is None:
                        current_path, should_cleanup = await self._resolve_audio_path(state, item)
                    else:
                        current_path, should_cleanup = await audio_task

                    if prefetched_item is None and not state.queue.empty():
                        try:
                            prefetched_item = state.queue.get_nowait()
                            prefetched_audio_task = asyncio.create_task(self._resolve_audio_path(state, prefetched_item))
                        except asyncio.QueueEmpty:
                            prefetched_item = None
                            prefetched_audio_task = None

                    try:
                        await self._play_file(vc, current_path)
                    finally:
                        if should_cleanup:
                            try:
                                os.remove(current_path)
                            except Exception:
                                pass
                        state.last_playback_finished_at = time.monotonic()

                except Exception as e:
                    print(f"[tts_voice] Erro no worker da guild {guild_id}: {e}")
                finally:
                    if fetched_from_queue:
                        state.queue.task_done()
        finally:
            state.worker_task = None
