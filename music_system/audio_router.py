from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import time
import threading
from array import array
from dataclasses import dataclass, field
from typing import Optional

import discord

import config
from .extractor import MusicExtractor, MusicExtractionError
from .models import LoopMode, MusicTrack

logger = logging.getLogger(__name__)

MUSIC_DEFAULT_VOLUME = max(0.0, min(2.0, float(getattr(config, "MUSIC_DEFAULT_VOLUME", 0.55))))
MUSIC_DUCK_VOLUME = max(0.0, min(1.0, float(getattr(config, "MUSIC_DUCK_VOLUME", 0.15))))
TTS_VOLUME = max(0.0, min(2.0, float(getattr(config, "MUSIC_TTS_VOLUME", 1.0))))
MUSIC_IDLE_DISCONNECT_SECONDS = max(30.0, float(getattr(config, "MUSIC_IDLE_DISCONNECT_SECONDS", 180)))
MUSIC_QUEUE_MAXSIZE = max(1, int(getattr(config, "MUSIC_QUEUE_MAXSIZE", 50)))
MUSIC_MAX_PLAYLIST_ITEMS = max(1, int(getattr(config, "MUSIC_MAX_PLAYLIST_ITEMS", 25)))
MUSIC_SEARCH_RESULTS = max(1, min(10, int(getattr(config, "MUSIC_SEARCH_RESULTS", 5))))
MUSIC_YTDLP_TIMEOUT_SECONDS = max(5.0, float(getattr(config, "MUSIC_YTDLP_TIMEOUT_SECONDS", 20.0)))
MUSIC_RECONNECT_BEFORE_OPTIONS = str(getattr(config, "MUSIC_FFMPEG_BEFORE_OPTIONS", "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -nostdin") or "-nostdin")
MUSIC_FFMPEG_OPTIONS = str(getattr(config, "MUSIC_FFMPEG_OPTIONS", "-vn -loglevel error") or "-vn -loglevel error")
MUSIC_TTS_FFMPEG_OPTIONS = str(getattr(config, "MUSIC_TTS_FFMPEG_OPTIONS", "-vn -loglevel error") or "-vn -loglevel error")

PCM_FRAME_BYTES = 3840  # 20ms, 48kHz, stereo, signed 16-bit little endian


@dataclass(slots=True, eq=False)
class TTSOverlay:
    source: discord.AudioSource
    volume: float
    future: asyncio.Future
    started_at: float = field(default_factory=time.monotonic)
    ended: bool = False


class MixedAudioSource(discord.AudioSource):
    """Mistura música + overlays de TTS em PCM sem numpy.

    O Discord chama read() em uma thread de áudio. Por isso, qualquer Future é
    resolvida com call_soon_threadsafe no loop principal.
    """

    def __init__(self, *, loop: asyncio.AbstractEventLoop, music_source: discord.AudioSource, music_volume: float, duck_volume: float) -> None:
        self.loop = loop
        self.music_source = music_source
        self.music_volume = float(music_volume)
        self.normal_music_volume = float(music_volume)
        self.duck_volume = float(duck_volume)
        self.duck_enabled = True
        self._overlays: list[TTSOverlay] = []
        self._overlay_lock = threading.RLock()
        self._closed = False

    def is_opus(self) -> bool:
        return False

    @property
    def has_overlays(self) -> bool:
        with self._overlay_lock:
            return bool(self._overlays)

    def set_music_volume(self, volume: float) -> None:
        self.normal_music_volume = max(0.0, min(2.0, float(volume)))

    def set_duck_volume(self, volume: float) -> None:
        self.duck_volume = max(0.0, min(1.0, float(volume)))

    def add_tts(self, source: discord.AudioSource, *, volume: float) -> asyncio.Future:
        future = self.loop.create_future()
        overlay = TTSOverlay(source=source, volume=max(0.0, min(2.0, float(volume))), future=future)
        with self._overlay_lock:
            self._overlays.append(overlay)
        return future

    def _finish_overlay(self, overlay: TTSOverlay, error: Exception | None = None) -> None:
        if overlay.ended:
            return
        overlay.ended = True
        with contextlib.suppress(Exception):
            overlay.source.cleanup()
        if not overlay.future.done():
            if error is None:
                self.loop.call_soon_threadsafe(overlay.future.set_result, None)
            else:
                self.loop.call_soon_threadsafe(overlay.future.set_exception, error)

    def _apply_volume(self, frame: bytes, volume: float) -> array:
        samples = array("h")
        samples.frombytes(frame)
        if volume != 1.0:
            for i, sample in enumerate(samples):
                mixed = int(sample * volume)
                if mixed > 32767:
                    mixed = 32767
                elif mixed < -32768:
                    mixed = -32768
                samples[i] = mixed
        return samples

    def _mix_into(self, base: array, frame: bytes, volume: float) -> None:
        if not frame:
            return
        other = self._apply_volume(frame, volume)
        if len(other) < len(base):
            other.extend([0] * (len(base) - len(other)))
        elif len(other) > len(base):
            del other[len(base):]
        for i, sample in enumerate(other):
            mixed = int(base[i]) + int(sample)
            if mixed > 32767:
                mixed = 32767
            elif mixed < -32768:
                mixed = -32768
            base[i] = mixed

    def read(self) -> bytes:
        if self._closed:
            return b""

        music_frame = self.music_source.read()
        if not music_frame:
            self.cleanup()
            return b""

        with self._overlay_lock:
            active_overlays = list(self._overlays)
        target_music_volume = self.duck_volume if (self.duck_enabled and active_overlays) else self.normal_music_volume
        base = self._apply_volume(music_frame, target_music_volume)

        if active_overlays:
            ended: list[TTSOverlay] = []
            for overlay in active_overlays:
                try:
                    frame = overlay.source.read()
                    if frame:
                        self._mix_into(base, frame, overlay.volume)
                    else:
                        ended.append(overlay)
                        self._finish_overlay(overlay)
                except Exception as exc:
                    ended.append(overlay)
                    self._finish_overlay(overlay, exc)
            if ended:
                with self._overlay_lock:
                    self._overlays = [ov for ov in self._overlays if ov not in ended]

        return base.tobytes()

    def cleanup(self) -> None:
        if self._closed:
            return
        self._closed = True
        with contextlib.suppress(Exception):
            self.music_source.cleanup()
        with self._overlay_lock:
            overlays = list(self._overlays)
            self._overlays.clear()
        for overlay in overlays:
            self._finish_overlay(overlay)


@dataclass
class MusicGuildState:
    queue: asyncio.Queue[MusicTrack] = field(default_factory=lambda: asyncio.Queue(maxsize=MUSIC_QUEUE_MAXSIZE))
    worker_task: Optional[asyncio.Task] = None
    current: Optional[MusicTrack] = None
    last_text_channel_id: Optional[int] = None
    last_voice_channel_id: Optional[int] = None
    volume: float = MUSIC_DEFAULT_VOLUME
    duck_volume: float = MUSIC_DUCK_VOLUME
    duck_enabled: bool = True
    loop_mode: LoopMode = LoopMode.OFF
    shuffle: bool = False
    stop_requested: bool = False
    paused: bool = False
    current_source: Optional[MixedAudioSource] = None
    now_message: Optional[discord.Message] = None
    voice_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def queue_size(self) -> int:
        return self.queue.qsize()


class AudioRouter:
    """Ponto único de áudio do bot.

    Música usa MixedAudioSource. TTS usa overlay quando existe música ativa; caso contrário,
    cai no playback direto para manter latência baixa.
    """

    def __init__(self, bot) -> None:
        self.bot = bot
        self.extractor = MusicExtractor(
            max_playlist_items=MUSIC_MAX_PLAYLIST_ITEMS,
            search_results=MUSIC_SEARCH_RESULTS,
            timeout_seconds=MUSIC_YTDLP_TIMEOUT_SECONDS,
        )
        self._states: dict[int, MusicGuildState] = {}

    def get_state(self, guild_id: int) -> MusicGuildState:
        state = self._states.get(int(guild_id))
        if state is None:
            state = MusicGuildState()
            self._states[int(guild_id)] = state
        return state

    def is_music_active(self, guild_id: int) -> bool:
        state = self._states.get(int(guild_id))
        return bool(state and (state.current or not state.queue.empty() or (state.worker_task and not state.worker_task.done())))

    async def close(self) -> None:
        for guild_id in list(self._states):
            with contextlib.suppress(Exception):
                await self.stop(guild_id, disconnect=False)

    async def enqueue(self, guild: discord.Guild, voice_channel: discord.abc.Connectable, text_channel: discord.abc.Messageable, tracks: list[MusicTrack]) -> tuple[int, int]:
        if not tracks:
            return 0, 0
        state = self.get_state(guild.id)
        state.last_text_channel_id = getattr(text_channel, "id", None)
        state.last_voice_channel_id = getattr(voice_channel, "id", None)

        added = 0
        dropped = 0
        for track in tracks:
            if state.queue.full():
                dropped += 1
                continue
            await state.queue.put(track)
            added += 1
        self.ensure_music_worker(guild.id)
        return added, dropped

    def ensure_music_worker(self, guild_id: int) -> None:
        state = self.get_state(guild_id)
        if state.worker_task is None or state.worker_task.done():
            state.stop_requested = False
            state.worker_task = asyncio.create_task(self._music_worker_loop(int(guild_id)))

    async def _music_worker_loop(self, guild_id: int) -> None:
        state = self.get_state(guild_id)
        try:
            while not state.stop_requested:
                guild = self.bot.get_guild(guild_id)
                if guild is None:
                    return

                try:
                    track = await asyncio.wait_for(state.queue.get(), timeout=MUSIC_IDLE_DISCONNECT_SECONDS)
                except asyncio.TimeoutError:
                    await self._maybe_disconnect_idle(guild, state)
                    return

                state.current = track
                try:
                    await self._play_track(guild, state, track)
                except Exception as exc:
                    logger.warning("[music] Falha ao tocar | guild=%s track=%r erro=%s", guild_id, track.title, exc)
                    await self._send_text(guild, state, f"⚠️ Não consegui tocar **{track.short_title}**. Pulando para a próxima.")
                finally:
                    state.current_source = None
                    if state.loop_mode is LoopMode.ONE and not state.stop_requested:
                        with contextlib.suppress(Exception):
                            # Repetir a atual antes de qualquer item já presente na fila.
                            state.queue._queue.appendleft(track)
                    elif state.loop_mode is LoopMode.ALL and not state.stop_requested:
                        with contextlib.suppress(Exception):
                            await state.queue.put(track)
                    state.current = None
                    state.paused = False
                    with contextlib.suppress(Exception):
                        state.queue.task_done()
        finally:
            state.worker_task = None
            if state.stop_requested or state.queue.empty():
                state.current = None

    async def _play_track(self, guild: discord.Guild, state: MusicGuildState, track: MusicTrack) -> None:
        if not state.last_voice_channel_id:
            raise RuntimeError("Canal de voz não definido.")
        channel = guild.get_channel(state.last_voice_channel_id) or self.bot.get_channel(state.last_voice_channel_id)
        if channel is None or not hasattr(channel, "connect"):
            raise RuntimeError("Canal de voz não encontrado.")

        vc = await self._ensure_voice(guild, channel)
        if vc is None:
            raise RuntimeError("Não consegui conectar ao canal de voz.")

        await self.extractor.resolve_stream(track, force=False)
        if not track.stream_url:
            raise MusicExtractionError("A música não retornou URL de stream.")

        # Se um TTS direto ainda estiver tocando, espera acabar antes da música entrar.
        for _ in range(60):
            if not (vc.is_playing() or vc.is_paused()):
                break
            await asyncio.sleep(0.1)

        loop = asyncio.get_running_loop()
        finished = loop.create_future()
        ffmpeg_source = discord.FFmpegPCMAudio(
            track.stream_url,
            before_options=MUSIC_RECONNECT_BEFORE_OPTIONS,
            options=MUSIC_FFMPEG_OPTIONS,
        )
        mixed_source = MixedAudioSource(
            loop=loop,
            music_source=ffmpeg_source,
            music_volume=state.volume,
            duck_volume=state.duck_volume if state.duck_enabled else state.volume,
        )
        mixed_source.duck_enabled = state.duck_enabled
        state.current_source = mixed_source

        def _after(error: Exception | None) -> None:
            if error:
                logger.warning("[music] after playback error | guild=%s erro=%s", guild.id, error)
            if not finished.done():
                loop.call_soon_threadsafe(finished.set_result, None)

        async with state.voice_lock:
            if vc.is_playing() or vc.is_paused():
                with contextlib.suppress(Exception):
                    vc.stop()
            vc.play(mixed_source, after=_after)

        await self._announce_now_playing(guild, state, track)
        await finished

    async def _ensure_voice(self, guild: discord.Guild, channel: discord.abc.Connectable) -> Optional[discord.VoiceClient]:
        vc = guild.voice_client
        if vc and vc.is_connected():
            if getattr(getattr(vc, "channel", None), "id", None) != getattr(channel, "id", None):
                try:
                    await vc.move_to(channel)
                except Exception:
                    await vc.disconnect(force=True)
                    vc = None
            if vc and vc.is_connected():
                with contextlib.suppress(Exception):
                    await guild.change_voice_state(channel=channel, self_deaf=True)
                return vc
        try:
            return await channel.connect(self_deaf=True)
        except Exception as exc:
            logger.warning("[music] falha ao conectar | guild=%s channel=%s erro=%s", guild.id, getattr(channel, "id", None), exc)
            return guild.voice_client if guild.voice_client and guild.voice_client.is_connected() else None

    async def _maybe_disconnect_idle(self, guild: discord.Guild, state: MusicGuildState) -> None:
        vc = guild.voice_client
        if not vc or not vc.is_connected():
            return
        try:
            members = list(getattr(vc.channel, "members", []))
            humans = [m for m in members if not getattr(m, "bot", False)]
            if humans:
                return
            await vc.disconnect(force=False)
        except Exception:
            logger.debug("[music] idle disconnect falhou", exc_info=True)

    async def _send_text(self, guild: discord.Guild, state: MusicGuildState, content: str) -> None:
        if not state.last_text_channel_id:
            return
        channel = guild.get_channel(state.last_text_channel_id) or self.bot.get_channel(state.last_text_channel_id)
        if channel is None:
            return
        with contextlib.suppress(Exception):
            await channel.send(content)

    async def _announce_now_playing(self, guild: discord.Guild, state: MusicGuildState, track: MusicTrack) -> None:
        if not state.last_text_channel_id:
            return
        channel = guild.get_channel(state.last_text_channel_id) or self.bot.get_channel(state.last_text_channel_id)
        if channel is None:
            return
        try:
            from .ui import build_now_playing_embed, MusicPlayerView

            embed = build_now_playing_embed(state, track)
            view = MusicPlayerView(self, guild.id)
            if state.now_message:
                with contextlib.suppress(Exception):
                    await state.now_message.edit(embed=embed, view=view)
                    return
            state.now_message = await channel.send(embed=embed, view=view)
        except Exception:
            logger.debug("[music] falha ao anunciar now playing", exc_info=True)

    async def play_tts(
        self,
        *,
        guild: discord.Guild | None,
        vc: discord.VoiceClient,
        path: str,
        before_options: str = "-nostdin",
        options: str = MUSIC_TTS_FFMPEG_OPTIONS,
        timeout: float = 120.0,
        item=None,
    ) -> dict[str, float]:
        """Toca TTS. Se música estiver ativa, mistura por cima com ducking."""
        loop = asyncio.get_running_loop()
        source_setup_started_at = time.monotonic()
        source = discord.FFmpegPCMAudio(path, before_options=before_options, options=options)
        source_setup_ms = max(0.0, (time.monotonic() - source_setup_started_at) * 1000.0)
        play_call_ms = 0.0
        playback_started_at = time.monotonic()

        guild_id = getattr(guild, "id", None) or getattr(getattr(vc, "guild", None), "id", None)
        state = self._states.get(int(guild_id)) if guild_id is not None else None
        active_source = state.current_source if state is not None else None

        if active_source is not None and not getattr(active_source, "_closed", True) and (vc.is_playing() or vc.is_paused()):
            future = active_source.add_tts(source, volume=TTS_VOLUME)
            await asyncio.wait_for(future, timeout=max(1.0, float(timeout)))
            playback_ms = max(0.0, (time.monotonic() - playback_started_at) * 1000.0)
            return {
                "source_setup_ms": source_setup_ms,
                "play_call_ms": play_call_ms,
                "playback_ms": playback_ms,
                "playback_started_at": playback_started_at,
            }

        finished = loop.create_future()

        def _after(error: Exception | None) -> None:
            with contextlib.suppress(Exception):
                source.cleanup()
            if not finished.done():
                if error is None:
                    loop.call_soon_threadsafe(finished.set_result, None)
                else:
                    loop.call_soon_threadsafe(finished.set_exception, error)

        play_call_started_at = time.monotonic()
        vc.play(source, after=_after)
        play_call_ms = max(0.0, (time.monotonic() - play_call_started_at) * 1000.0)
        playback_started_at = time.monotonic()
        await asyncio.wait_for(finished, timeout=max(1.0, float(timeout)))
        playback_ms = max(0.0, (time.monotonic() - playback_started_at) * 1000.0)
        return {
            "source_setup_ms": source_setup_ms,
            "play_call_ms": play_call_ms,
            "playback_ms": playback_ms,
            "playback_started_at": playback_started_at,
        }

    async def pause(self, guild_id: int) -> bool:
        guild = self.bot.get_guild(int(guild_id))
        vc = guild.voice_client if guild else None
        if not vc or not vc.is_connected() or not vc.is_playing():
            return False
        vc.pause()
        self.get_state(guild_id).paused = True
        return True

    async def resume(self, guild_id: int) -> bool:
        guild = self.bot.get_guild(int(guild_id))
        vc = guild.voice_client if guild else None
        if not vc or not vc.is_connected() or not vc.is_paused():
            return False
        vc.resume()
        self.get_state(guild_id).paused = False
        return True

    async def skip(self, guild_id: int) -> bool:
        guild = self.bot.get_guild(int(guild_id))
        vc = guild.voice_client if guild else None
        if not vc or not (vc.is_playing() or vc.is_paused()):
            return False
        vc.stop()
        return True

    async def stop(self, guild_id: int, *, disconnect: bool = True) -> bool:
        state = self.get_state(guild_id)
        state.stop_requested = True
        while not state.queue.empty():
            with contextlib.suppress(Exception):
                state.queue.get_nowait()
                state.queue.task_done()
        guild = self.bot.get_guild(int(guild_id))
        vc = guild.voice_client if guild else None
        if vc:
            with contextlib.suppress(Exception):
                if vc.is_playing() or vc.is_paused():
                    vc.stop()
            if disconnect:
                with contextlib.suppress(Exception):
                    await vc.disconnect(force=False)
        state.current = None
        state.current_source = None
        state.paused = False
        return True

    async def set_volume(self, guild_id: int, volume_percent: int) -> float:
        state = self.get_state(guild_id)
        volume = max(0, min(150, int(volume_percent))) / 100.0
        state.volume = volume
        if state.current_source is not None:
            state.current_source.set_music_volume(volume)
        return volume

    async def set_duck_volume(self, guild_id: int, volume_percent: int) -> float:
        state = self.get_state(guild_id)
        volume = max(0, min(100, int(volume_percent))) / 100.0
        state.duck_volume = volume
        if state.current_source is not None:
            state.current_source.set_duck_volume(volume)
        return volume

    async def toggle_duck(self, guild_id: int) -> bool:
        state = self.get_state(guild_id)
        state.duck_enabled = not state.duck_enabled
        if state.current_source is not None:
            state.current_source.duck_enabled = state.duck_enabled
        return state.duck_enabled

    async def toggle_shuffle(self, guild_id: int) -> bool:
        state = self.get_state(guild_id)
        items = self.snapshot_queue(guild_id)
        state.shuffle = not state.shuffle
        if state.shuffle and len(items) > 1:
            random.shuffle(items)
            await self.replace_queue(guild_id, items)
        return state.shuffle

    async def cycle_loop(self, guild_id: int) -> LoopMode:
        state = self.get_state(guild_id)
        if state.loop_mode is LoopMode.OFF:
            state.loop_mode = LoopMode.ONE
        elif state.loop_mode is LoopMode.ONE:
            state.loop_mode = LoopMode.ALL
        else:
            state.loop_mode = LoopMode.OFF
        return state.loop_mode

    def snapshot_queue(self, guild_id: int) -> list[MusicTrack]:
        state = self.get_state(guild_id)
        return list(getattr(state.queue, "_queue", []))

    async def replace_queue(self, guild_id: int, tracks: list[MusicTrack]) -> None:
        state = self.get_state(guild_id)
        while not state.queue.empty():
            with contextlib.suppress(Exception):
                state.queue.get_nowait()
                state.queue.task_done()
        for track in tracks[:MUSIC_QUEUE_MAXSIZE]:
            await state.queue.put(track)

    async def remove_at(self, guild_id: int, index_1based: int) -> Optional[MusicTrack]:
        items = self.snapshot_queue(guild_id)
        idx = int(index_1based) - 1
        if idx < 0 or idx >= len(items):
            return None
        removed = items.pop(idx)
        await self.replace_queue(guild_id, items)
        return removed

    async def move(self, guild_id: int, from_pos: int, to_pos: int) -> bool:
        items = self.snapshot_queue(guild_id)
        src = int(from_pos) - 1
        dst = int(to_pos) - 1
        if src < 0 or src >= len(items) or dst < 0 or dst >= len(items):
            return False
        track = items.pop(src)
        items.insert(dst, track)
        await self.replace_queue(guild_id, items)
        return True
