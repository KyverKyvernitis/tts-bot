from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import random
import time
import threading
from datetime import datetime, timezone
from array import array
from collections import deque
from dataclasses import dataclass, field
from typing import Optional, Any
from urllib.parse import quote

import discord

import config
from .api_providers import compact_key
from .extractor import MusicExtractor
from .errors import MusicExtractionError, MusicPlaybackError
from .models import LoopMode, MusicTrack
from .providers import describe_url
from .backends import MusicBackendManager

logger = logging.getLogger(__name__)

MUSIC_DEFAULT_VOLUME = max(0.0, min(2.0, float(getattr(config, "MUSIC_DEFAULT_VOLUME", 0.55))))
TTS_VOLUME = max(0.0, min(2.0, float(getattr(config, "MUSIC_TTS_VOLUME", 1.0))))
MUSIC_TTS_LOCAL_DUCK_FACTOR = max(0.0, min(1.0, float(getattr(config, "MUSIC_TTS_LOCAL_DUCK_PERCENT", 5.0)) / 100.0))
MUSIC_LAVALINK_TTS_PAUSE_ENABLED = bool(getattr(config, "MUSIC_LAVALINK_TTS_PAUSE_ENABLED", True))
MUSIC_LAVALINK_TTS_PAUSE_GRACE_SECONDS = max(0.2, float(getattr(config, "MUSIC_LAVALINK_TTS_PAUSE_GRACE_SECONDS", 0.35)))
MUSIC_TTS_OVERLAY_TIMEOUT_IS_NON_FATAL = bool(getattr(config, "MUSIC_TTS_OVERLAY_TIMEOUT_IS_NON_FATAL", True))
MUSIC_IDLE_DISCONNECT_SECONDS = max(15.0, float(getattr(config, "MUSIC_IDLE_DISCONNECT_SECONDS", 120)))
MUSIC_QUEUE_MAXSIZE = min(100, max(1, int(getattr(config, "MUSIC_QUEUE_MAXSIZE", 100))))
MUSIC_MAX_PLAYLIST_ITEMS = min(100, max(1, int(getattr(config, "MUSIC_MAX_PLAYLIST_ITEMS", 100))))
MUSIC_SEARCH_RESULTS = max(1, min(10, int(getattr(config, "MUSIC_SEARCH_RESULTS", 5))))
MUSIC_YTDLP_TIMEOUT_SECONDS = max(5.0, float(getattr(config, "MUSIC_YTDLP_TIMEOUT_SECONDS", 20.0)))
MUSIC_RECONNECT_BEFORE_OPTIONS = str(getattr(config, "MUSIC_FFMPEG_BEFORE_OPTIONS", "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -nostdin") or "-nostdin")
MUSIC_FFMPEG_OPTIONS = str(getattr(config, "MUSIC_FFMPEG_OPTIONS", "-vn -loglevel error") or "-vn -loglevel error")
MUSIC_TTS_FFMPEG_OPTIONS = str(getattr(config, "MUSIC_TTS_FFMPEG_OPTIONS", "-vn -loglevel error") or "-vn -loglevel error")
MUSIC_PLAYBACK_START_TIMEOUT_SECONDS = max(2.0, float(getattr(config, "MUSIC_PLAYBACK_START_TIMEOUT_SECONDS", 8.0)))
MUSIC_HISTORY_MAXSIZE = max(5, int(getattr(config, "MUSIC_HISTORY_MAXSIZE", 25)))
MUSIC_CONTROL_VOTE_SECONDS = max(10.0, float(getattr(config, "MUSIC_CONTROL_VOTE_SECONDS", 45)))
MUSIC_PREFETCH_NEXT = bool(getattr(config, "MUSIC_PREFETCH_NEXT", True))
MUSIC_LIMITER_ENABLED = bool(getattr(config, "MUSIC_LIMITER_ENABLED", True))
MUSIC_MAX_GLOBAL_PREFETCH = max(0, int(getattr(config, "MUSIC_MAX_GLOBAL_PREFETCH", 1)))
MUSIC_DISABLE_PREFETCH_ABOVE_PLAYERS = max(0, int(getattr(config, "MUSIC_DISABLE_PREFETCH_ABOVE_PLAYERS", 2)))
MUSIC_PREFETCH_MIN_DELAY_SECONDS = max(0.0, float(getattr(config, "MUSIC_PREFETCH_MIN_DELAY_SECONDS", 18.0)))
MUSIC_PREFETCH_BEFORE_END_SECONDS = max(5.0, float(getattr(config, "MUSIC_PREFETCH_BEFORE_END_SECONDS", 45.0)))
MUSIC_PANEL_UPDATE_THROTTLE_SECONDS = max(0.05, float(getattr(config, "MUSIC_PANEL_UPDATE_THROTTLE_SECONDS", 2.0)))
MUSIC_AUDIO_MODE = str(getattr(config, "MUSIC_AUDIO_MODE", "auto") or "auto").strip().lower()
MUSIC_HIGH_QUALITY_MAX_ACTIVE_GUILDS = max(1, int(getattr(config, "MUSIC_HIGH_QUALITY_MAX_ACTIVE_GUILDS", 1)))
MUSIC_HIGH_QUALITY_MAX_ABR = max(96, int(getattr(config, "MUSIC_HIGH_QUALITY_MAX_ABR", 256)))  # compat/env antigo
MUSIC_MAX_AUDIO_BITRATE_STABLE = max(64, int(getattr(config, "MUSIC_MAX_AUDIO_BITRATE_STABLE", 160)))
MUSIC_HEAVY_LOAD_MAX_ABR = max(64, int(getattr(config, "MUSIC_HEAVY_LOAD_MAX_ABR", 128)))
MUSIC_AUTO_BITRATE_ENABLED = bool(getattr(config, "MUSIC_AUTO_BITRATE_ENABLED", True))
MUSIC_AUTO_BITRATE_MAX = max(8000, int(getattr(config, "MUSIC_AUTO_BITRATE_MAX", 384000)))
MUSIC_AUTO_BITRATE_MIN_GAIN = max(0, int(getattr(config, "MUSIC_AUTO_BITRATE_MIN_GAIN", 16000)))
MUSIC_STREAM_START_RETRIES = max(0, int(getattr(config, "MUSIC_STREAM_START_RETRIES", 1)))
MUSIC_VOICE_STATUS_ENABLED = bool(getattr(config, "MUSIC_VOICE_STATUS_ENABLED", True))
MUSIC_VOICE_STATUS_TEMPLATE = str(getattr(config, "MUSIC_VOICE_STATUS_TEMPLATE", "{source_emoji} <a:2574_Rainbow_Heart:1381731924162384023> {title}, {author} ({requester})") or "{source_emoji} <a:2574_Rainbow_Heart:1381731924162384023> {title}, {author} ({requester})").strip()
MUSIC_VOICE_STATUS_IDLE = str(getattr(config, "MUSIC_VOICE_STATUS_IDLE", "") or "").strip()
MUSIC_VOICE_STATUS_UPDATE_INTERVAL_SECONDS = max(15.0, float(getattr(config, "MUSIC_VOICE_STATUS_UPDATE_INTERVAL_SECONDS", 60.0)))
MUSIC_SOURCE_EMOJIS = {
    "youtube": "<:YouTube:1502490543891021827>",
    "spotify": "<:Spotify:1502490573205016676>",
    "deezer": "<:Deezer:1502490958997094420>",
    "soundcloud": "<:SoundCloud:1502491211485675631>",
}
MUSIC_SOURCE_EMOJI_FALLBACK = "🎵"

PCM_FRAME_BYTES = 3840  # 20ms, 48kHz, stereo, signed 16-bit little endian
PCM_FRAME_MS = 20.0
PCM_LIMITER_THRESHOLD = 30000


def _ffmpeg_options_with_base_volume(options: str, volume: float) -> tuple[str, float]:
    """Aplica volume base no FFmpeg para reduzir trabalho Python por frame.

    Sem TTS por cima, o áudio pode sair quase em passthrough. Quando há TTS
    o mixer Python aplica apenas o fator restante em cima do volume base já
    feito em C pelo FFmpeg.
    """
    raw_options = str(options or "").strip()
    try:
        base_volume = max(0.0, min(2.0, float(volume)))
    except Exception:
        base_volume = 1.0
    # Não injeta filtro se já há filtro customizado, se volume zero impediria
    # aumentar a música atual depois, ou se o volume acima de 100% precisaria
    # do limiter Python para evitar distorção.
    normalized = f" {raw_options} "
    has_filter = " -af " in normalized or " -filter:a " in normalized or " -filter_complex " in normalized
    if has_filter or base_volume <= 0.001 or base_volume > 1.0 or abs(base_volume - 1.0) < 0.001:
        return raw_options, 1.0
    return f"{raw_options} -filter:a volume={base_volume:.4f}".strip(), base_volume


def _consume_expected_music_exception(done: asyncio.Future) -> None:
    """Consome cancelamentos esperados para não gerar "exception was never retrieved"."""
    try:
        done.result()
    except asyncio.CancelledError:
        return
    except MusicPlaybackError as exc:
        message = str(exc)
        if message in {"Música pulada antes de iniciar o áudio.", "Playback cancelado."}:
            return
        logger.warning("[music] task/future terminou com erro de playback: %s", exc)
    except MusicExtractionError:
        # Tasks de resolução também são aguardadas pelo worker; deixar o worker
        # enviar a mensagem amigável evita traceback duplicado e ruído no log.
        return
    except Exception as exc:
        logger.warning(
            "[music] task/future terminou com exceção inesperada",
            exc_info=(type(exc), exc, exc.__traceback__),
        )


@dataclass(slots=True, eq=False)
class TTSOverlay:
    source: discord.AudioSource
    volume: float
    future: asyncio.Future
    started_at: float = field(default_factory=time.monotonic)
    ended: bool = False


@dataclass(slots=True)
class ControlVote:
    action: str
    voters: set[int] = field(default_factory=set)
    started_at: float = field(default_factory=time.monotonic)

    def expired(self) -> bool:
        return (time.monotonic() - self.started_at) > MUSIC_CONTROL_VOTE_SECONDS


class MixedAudioSource(discord.AudioSource):
    """Mistura música + overlays de TTS em PCM sem numpy.

    O Discord chama read() em uma thread de áudio. Por isso, qualquer Future é
    resolvida com call_soon_threadsafe no loop principal.
    """

    def __init__(self, *, loop: asyncio.AbstractEventLoop, music_source: discord.AudioSource, music_volume: float, source_base_volume: float = 1.0, duck_factor: float = MUSIC_TTS_LOCAL_DUCK_FACTOR) -> None:
        self.loop = loop
        self.music_source = music_source
        self.music_volume = float(music_volume)
        self.normal_music_volume = float(music_volume)
        self.duck_factor = max(0.0, min(1.0, float(duck_factor)))
        self.source_base_volume = max(0.001, min(2.0, float(source_base_volume or 1.0)))
        self._current_music_volume = float(music_volume)
        self._overlays: list[TTSOverlay] = []
        self._overlay_lock = threading.RLock()
        self._closed = False
        self._music_ended = False
        self._music_started = False
        self.music_started_future = loop.create_future()

    def is_opus(self) -> bool:
        return False

    @property
    def has_overlays(self) -> bool:
        with self._overlay_lock:
            return bool(self._overlays)

    def _future_set_result_threadsafe(self, future: asyncio.Future, result: object = None) -> None:
        def _set_once() -> None:
            if not future.done():
                future.set_result(result)

        self.loop.call_soon_threadsafe(_set_once)

    def _future_set_exception_threadsafe(self, future: asyncio.Future, error: Exception) -> None:
        def _set_once() -> None:
            if not future.done():
                future.set_exception(error)

        self.loop.call_soon_threadsafe(_set_once)

    def _future_cancel_threadsafe(self, future: asyncio.Future) -> None:
        def _cancel_once() -> None:
            if not future.done():
                future.cancel()

        self.loop.call_soon_threadsafe(_cancel_once)

    def _mark_music_started(self) -> None:
        if self._music_started:
            return
        self._music_started = True
        if not self.music_started_future.done():
            self._future_set_result_threadsafe(self.music_started_future, None)

    def _mark_music_failed_before_start(self, message: str) -> None:
        if self._music_started or self.music_started_future.done():
            return
        self._future_set_exception_threadsafe(self.music_started_future, MusicPlaybackError(message))

    def set_music_volume(self, volume: float) -> None:
        self.normal_music_volume = max(0.0, min(2.0, float(volume)))

    def set_duck_factor(self, factor: float) -> None:
        self.duck_factor = max(0.0, min(1.0, float(factor)))

    def _step_music_volume(self, target: float) -> float:
        target = max(0.0, min(2.0, float(target)))
        self._current_music_volume = target
        return target

    def _limit_sample(self, value: int) -> int:
        if not MUSIC_LIMITER_ENABLED:
            return max(-32768, min(32767, int(value)))
        value = int(value)
        if value > PCM_LIMITER_THRESHOLD:
            value = int(PCM_LIMITER_THRESHOLD + (value - PCM_LIMITER_THRESHOLD) * 0.35)
        elif value < -PCM_LIMITER_THRESHOLD:
            value = int(-PCM_LIMITER_THRESHOLD + (value + PCM_LIMITER_THRESHOLD) * 0.35)
        return max(-32768, min(32767, value))

    def _limit_samples(self, samples: array) -> None:
        if not MUSIC_LIMITER_ENABLED:
            return
        for i, sample in enumerate(samples):
            samples[i] = self._limit_sample(int(sample))

    def add_tts(self, source: discord.AudioSource, *, volume: float) -> asyncio.Future:
        future = self.loop.create_future()
        overlay = TTSOverlay(source=source, volume=max(0.0, min(2.0, float(volume))), future=future)
        with self._overlay_lock:
            self._overlays.append(overlay)
        return future


    def cancel_tts(self, future: asyncio.Future) -> None:
        target = None
        with self._overlay_lock:
            for overlay in self._overlays:
                if overlay.future is future:
                    target = overlay
                    break
            if target is not None:
                self._overlays = [ov for ov in self._overlays if ov is not target]
        if target is not None:
            with contextlib.suppress(Exception):
                target.source.cleanup()
        if not future.done():
            self._future_cancel_threadsafe(future)

    def _finish_overlay(self, overlay: TTSOverlay, error: Exception | None = None) -> None:
        if overlay.ended:
            return
        overlay.ended = True
        with contextlib.suppress(Exception):
            overlay.source.cleanup()
        if not overlay.future.done():
            if error is None:
                self._future_set_result_threadsafe(overlay.future, None)
            else:
                self._future_set_exception_threadsafe(overlay.future, error)

    def _effective_music_scale(self, target_volume: float) -> float:
        # music_source pode já vir com volume base aplicado pelo FFmpeg.
        return max(0.0, min(4.0, float(target_volume) / self.source_base_volume))

    def _apply_volume(self, frame: bytes, volume: float) -> array:
        samples = array("h")
        samples.frombytes(frame)
        if abs(float(volume) - 1.0) > 0.001:
            for i, sample in enumerate(samples):
                samples[i] = self._limit_sample(int(sample * volume))
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
            base[i] = self._limit_sample(int(base[i]) + int(sample))

    def read(self) -> bytes:
        if self._closed:
            return b""

        music_frame = b""
        if not self._music_ended:
            music_frame = self.music_source.read()
            if music_frame:
                self._mark_music_started()
            else:
                self._music_ended = True
                self._mark_music_failed_before_start("FFmpeg encerrou antes de entregar áudio.")
                with contextlib.suppress(Exception):
                    self.music_source.cleanup()

        with self._overlay_lock:
            active_overlays = list(self._overlays)

        if not music_frame and not active_overlays:
            self.cleanup()
            return b""

        if music_frame:
            # Ducking local/yt-dlp: quando há TTS sobreposto, reduza a música
            # para uma fração pequena do volume normal, mas sem alterar o volume
            # persistido do servidor. Quando o último TTS termina, o read() volta
            # automaticamente ao volume normal.
            target_music_volume = self.normal_music_volume * (self.duck_factor if active_overlays else 1.0)
            stepped_volume = self._step_music_volume(target_music_volume)
            scale = self._effective_music_scale(stepped_volume)
            if not active_overlays and abs(scale - 1.0) <= 0.001:
                # Caminho crítico: sem TTS e sem ajuste dinâmico, não processa
                # 96k samples/s em Python. Isso reduz cortes quando yt-dlp/FFmpeg
                # prepara a próxima música em VPS fraca.
                return music_frame
            base = self._apply_volume(music_frame, scale)
        else:
            # Música acabou no mesmo instante em que havia TTS por cima. Mantém silêncio
            # como base para não cortar o TTS no meio da frase.
            base = array("h", [0] * (PCM_FRAME_BYTES // 2))

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

        self._limit_samples(base)
        return base.tobytes()

    def cleanup(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._mark_music_failed_before_start("Playback foi limpo antes do áudio iniciar.")
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
    loop_mode: LoopMode = LoopMode.OFF
    shuffle: bool = False
    stop_requested: bool = False
    paused: bool = False
    current_source: Optional[MixedAudioSource] = None
    current_backend: str = "local"
    current_lavalink_player: Any = None
    current_lavalink_playable: Any = None
    current_resolve_task: Optional[asyncio.Task] = None
    next_resolve_task: Optional[asyncio.Task] = None
    next_resolve_key: str = ""
    next_resolve_active_key: str = ""
    current_status: str = "idle"
    skip_requested: bool = False
    skip_transition_active: bool = False
    skip_history_suppressed_once: bool = False
    now_message: Optional[discord.Message] = None
    panel_track_key: Optional[str] = None
    history: deque[MusicTrack] = field(default_factory=lambda: deque(maxlen=MUSIC_HISTORY_MAXSIZE))
    forward_queue: deque[MusicTrack] = field(default_factory=lambda: deque(maxlen=MUSIC_HISTORY_MAXSIZE))
    voice_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    panel_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    music_owns_voice: bool = False
    tts_voice_touched: bool = False
    last_tts_activity_at: float = 0.0
    lavalink_tts_until: float = 0.0
    lavalink_resume_grace_until: float = 0.0
    music_session_active: bool = False
    music_idle_disconnect_task: Optional[asyncio.Task] = None
    control_votes: dict[str, ControlVote] = field(default_factory=dict)
    control_vote_cleanup_tasks: dict[str, asyncio.Task] = field(default_factory=dict)
    volume_loaded: bool = False
    idle_reason: str = "idle"
    idle_actor_id: Optional[int] = None
    idle_actor_name: str = ""
    idle_channel_name: str = ""
    internal_voice_disconnect_until: float = 0.0
    lavalink_transition_until: float = 0.0
    last_lavalink_error: str = ""
    panel_update_task: Optional[asyncio.Task] = None
    panel_update_create: bool = True
    panel_update_requested_at: float = 0.0
    current_started_at_monotonic: float = 0.0
    auto_bitrate_channel_id: Optional[int] = None
    auto_bitrate_original: Optional[int] = None
    auto_bitrate_boosted: Optional[int] = None
    current_quality_label: str = "Alta"
    current_quality_kbps: int = MUSIC_HIGH_QUALITY_MAX_ABR
    voice_status_channel_id: Optional[int] = None
    voice_status_had_original: bool = False
    voice_status_original: str = ""
    voice_status_last_bot: str = ""
    voice_status_update_task: Optional[asyncio.Task] = None
    voice_status_last_update_at: float = 0.0
    voice_status_last_track_key: str = ""
    voice_status_last_applied_key: str = ""
    voice_status_last_sync_request_key: str = ""
    voice_status_last_sync_request_at: float = 0.0
    voice_status_last_restore_key: str = ""
    voice_status_last_restore_at: float = 0.0
    voice_status_force_task: Optional[asyncio.Task] = None
    voice_status_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def queue_size(self) -> int:
        return self.queue.qsize() + len(self.forward_queue)


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
        self._global_prefetch_active = 0
        self.backends = MusicBackendManager(bot, self.extractor)
        self._lavalink_shadow_tasks: dict[int, asyncio.Task] = {}

    def get_state(self, guild_id: int) -> MusicGuildState:
        state = self._states.get(int(guild_id))
        if state is None:
            state = MusicGuildState()
            self._states[int(guild_id)] = state
        self._load_persisted_volume(int(guild_id), state)
        return state

    def _load_persisted_volume(self, guild_id: int, state: MusicGuildState) -> None:
        if state.volume_loaded:
            return
        state.volume_loaded = True
        settings_db = getattr(self.bot, "settings_db", None)
        try:
            guild_doc = getattr(settings_db, "guild_cache", {}).get(int(guild_id), {}) if settings_db is not None else {}
            raw_volume = guild_doc.get("music_volume")
            if raw_volume is not None:
                state.volume = max(0.0, min(1.5, float(raw_volume)))
        except Exception:
            logger.debug("[music] falha ao carregar volume persistido", exc_info=True)

    def _is_lavalink_voice_client(self, vc: Any) -> bool:
        if vc is None:
            return False
        module = str(getattr(type(vc), "__module__", "") or "")
        qualname = str(getattr(type(vc), "__qualname__", "") or getattr(type(vc), "__name__", "") or "")
        return module.startswith("wavelink") or (qualname == "Player" and hasattr(vc, "node") and hasattr(vc, "play"))

    def _vc_is_connected(self, vc: Any) -> bool:
        if vc is None:
            return False
        if self._is_lavalink_voice_client(vc):
            for attr in ("connected", "is_connected"):
                value = getattr(vc, attr, None)
                try:
                    if callable(value):
                        value = value()
                    if value is not None:
                        return bool(value)
                except Exception:
                    continue
            # Wavelink Player não expõe is_connected em todas as versões. Se ele
            # ainda tem canal/guild, trate como ativo para não disparar falso
            # external_disconnect durante troca de faixa/eventos internos.
            return bool(getattr(vc, "channel", None) is not None or getattr(vc, "guild", None) is not None)
        checker = getattr(vc, "is_connected", None)
        return bool(checker() if callable(checker) else getattr(vc, "connected", False))

    def _vc_is_playing(self, vc: Any) -> bool:
        if vc is None:
            return False
        if self._is_lavalink_voice_client(vc):
            # Wavelink 3 usa propriedades ``playing``/``current`` em vez dos
            # métodos do discord.VoiceClient. Alguns nodes públicos demoram um
            # pouco para atualizar ``playing``; se há ``current`` carregado e o
            # player segue conectado, ainda há uma faixa ativa. Sem isso, o
            # worker achava que a faixa acabou logo após player.play() e pulava
            # várias músicas/status em sequência.
            for attr in ("playing", "is_playing"):
                value = getattr(vc, attr, None)
                try:
                    if callable(value):
                        value = value()
                    if bool(value):
                        return True
                except Exception:
                    continue
            current = getattr(vc, "current", None)
            return bool(current is not None and self._vc_is_connected(vc))
        checker = getattr(vc, "is_playing", None)
        return bool(checker() if callable(checker) else getattr(vc, "playing", False))

    def _vc_is_paused(self, vc: Any) -> bool:
        if vc is None:
            return False
        if self._is_lavalink_voice_client(vc):
            value = getattr(vc, "paused", False)
            try:
                if callable(value):
                    value = value()
            except Exception:
                return False
            return bool(value)
        checker = getattr(vc, "is_paused", None)
        return bool(checker() if callable(checker) else getattr(vc, "paused", False))

    def _vc_is_playing_or_paused(self, vc: Any) -> bool:
        return self._vc_is_playing(vc) or self._vc_is_paused(vc)

    def _vc_lavalink_explicit_playing_or_paused(self, vc: Any) -> bool:
        if vc is None or not self._is_lavalink_voice_client(vc):
            return False
        for attr in ("playing", "is_playing", "paused", "is_paused"):
            value = getattr(vc, attr, None)
            try:
                if callable(value):
                    value = value()
                if bool(value):
                    return True
            except Exception:
                continue
        # Em Wavelink/Lavalink 4.2, alguns players seguem com áudio real
        # enquanto ``playing`` oscila falso. Se há faixa atual e a conexão de voz
        # existe, ainda trate como ativo para não limpar o painel/queue cedo.
        current = getattr(vc, "current", None)
        return bool(current is not None and self._vc_is_connected(vc))

    def _lavalink_tts_active(self, state: MusicGuildState) -> bool:
        return time.monotonic() < float(getattr(state, "lavalink_tts_until", 0.0) or 0.0)

    def _lavalink_resume_grace_active(self, state: MusicGuildState) -> bool:
        return time.monotonic() < float(getattr(state, "lavalink_resume_grace_until", 0.0) or 0.0)

    async def _vc_stop_audio(self, vc: Any) -> None:
        if vc is None:
            return
        if self._is_lavalink_voice_client(vc):
            skipper = getattr(vc, "skip", None)
            if callable(skipper):
                with contextlib.suppress(TypeError):
                    result = skipper(force=True)
                    if asyncio.iscoroutine(result):
                        await result
                    return
                result = skipper()
                if asyncio.iscoroutine(result):
                    await result
                return
            stopper = getattr(vc, "stop", None)
            if callable(stopper):
                result = stopper()
                if asyncio.iscoroutine(result):
                    await result
            return
        stopper = getattr(vc, "stop", None)
        if callable(stopper):
            stopper()

    async def _persist_volume(self, guild_id: int, volume: float) -> None:
        settings_db = getattr(self.bot, "settings_db", None)
        if settings_db is None:
            return
        try:
            get_doc = getattr(settings_db, "_get_guild_doc", None)
            save_doc = getattr(settings_db, "_save_guild_doc", None)
            if not callable(get_doc) or not callable(save_doc):
                return
            doc = get_doc(int(guild_id))
            doc["music_volume"] = max(0.0, min(1.5, float(volume)))
            await save_doc(int(guild_id), doc)
        except Exception:
            logger.debug("[music] falha ao salvar volume persistido", exc_info=True)

    def _auto_bitrate_record_from_doc(self, guild_id: int) -> dict | None:
        settings_db = getattr(self.bot, "settings_db", None)
        if settings_db is None:
            return None
        try:
            doc = getattr(settings_db, "guild_cache", {}).get(int(guild_id), {}) or {}
            raw = doc.get("music_auto_bitrate")
            if isinstance(raw, dict):
                return dict(raw)
        except Exception:
            logger.debug("[music] falha ao ler auto bitrate persistido", exc_info=True)
        return None

    async def _save_auto_bitrate_record(self, guild_id: int, record: dict) -> bool:
        settings_db = getattr(self.bot, "settings_db", None)
        if settings_db is None:
            return False
        try:
            get_doc = getattr(settings_db, "_get_guild_doc", None)
            save_doc = getattr(settings_db, "_save_guild_doc", None)
            if not callable(get_doc) or not callable(save_doc):
                return False
            doc = get_doc(int(guild_id))
            doc["music_auto_bitrate"] = dict(record)
            await save_doc(int(guild_id), doc)
            return True
        except Exception:
            logger.debug("[music] falha ao salvar auto bitrate", exc_info=True)
            return False

    async def _clear_auto_bitrate_record(self, guild_id: int) -> None:
        settings_db = getattr(self.bot, "settings_db", None)
        if settings_db is None:
            return
        try:
            get_doc = getattr(settings_db, "_get_guild_doc", None)
            save_doc = getattr(settings_db, "_save_guild_doc", None)
            if not callable(get_doc) or not callable(save_doc):
                return
            doc = get_doc(int(guild_id))
            doc.pop("music_auto_bitrate", None)
            await save_doc(int(guild_id), doc)
        except Exception:
            logger.debug("[music] falha ao limpar auto bitrate", exc_info=True)

    def _load_auto_bitrate_record_into_state(self, guild_id: int, state: MusicGuildState) -> dict | None:
        record = self._auto_bitrate_record_from_doc(guild_id)
        if not record:
            return None
        try:
            state.auto_bitrate_channel_id = int(record.get("channel_id") or 0) or None
            state.auto_bitrate_original = int(record.get("original_bitrate") or 0) or None
            state.auto_bitrate_boosted = int(record.get("boosted_bitrate") or 0) or None
        except Exception:
            state.auto_bitrate_channel_id = None
            state.auto_bitrate_original = None
            state.auto_bitrate_boosted = None
            return None
        return record

    def _is_normal_voice_channel(self, channel) -> bool:
        if channel is None:
            return False
        stage_type = getattr(discord, "StageChannel", None)
        if stage_type is not None and isinstance(channel, stage_type):
            return False
        return bool(hasattr(channel, "edit") and hasattr(channel, "bitrate") and hasattr(channel, "permissions_for"))

    def _bot_can_manage_voice_channel(self, guild: discord.Guild, channel) -> bool:
        member = getattr(guild, "me", None) or getattr(guild, "self_role", None)
        if member is None:
            return False
        try:
            perms = channel.permissions_for(member)
            return bool(getattr(perms, "manage_channels", False))
        except Exception:
            return False

    def _target_auto_bitrate(self, guild: discord.Guild, channel) -> int:
        current = int(getattr(channel, "bitrate", 0) or 0)
        guild_limit = int(getattr(guild, "bitrate_limit", 0) or 0)
        if guild_limit <= 0:
            guild_limit = max(current, 96000)

        # Quando só uma guild está tocando, usa o máximo permitido pelo servidor.
        # Com várias guilds tocando, mantém bitrate alto o bastante para música,
        # mas evita forçar todos os canais no teto ao mesmo tempo.
        active = max(1, self._active_player_count())
        hard_limit = min(int(MUSIC_AUTO_BITRATE_MAX), guild_limit)
        if active <= MUSIC_HIGH_QUALITY_MAX_ACTIVE_GUILDS:
            target = hard_limit
        elif active >= 3:
            target = min(hard_limit, max(64000, int(MUSIC_HEAVY_LOAD_MAX_ABR) * 1000))
        else:
            target = min(hard_limit, max(64000, int(MUSIC_MAX_AUDIO_BITRATE_STABLE) * 1000))
        return max(8000, int(target))

    async def _boost_auto_bitrate_for_music(self, guild: discord.Guild, channel, state: MusicGuildState) -> None:
        if not MUSIC_AUTO_BITRATE_ENABLED or guild is None or channel is None:
            return
        if not self._is_normal_voice_channel(channel):
            return
        if not self._bot_can_manage_voice_channel(guild, channel):
            return

        # Se a sessão anterior marcou outro canal, restaura antes de mexer no novo.
        if state.auto_bitrate_channel_id and int(state.auto_bitrate_channel_id) != int(getattr(channel, "id", 0) or 0):
            await self._restore_auto_bitrate_for_state(guild, state, reason="channel_change")

        current = int(getattr(channel, "bitrate", 0) or 0)
        target = self._target_auto_bitrate(guild, channel)
        if target <= current + MUSIC_AUTO_BITRATE_MIN_GAIN:
            return

        if state.auto_bitrate_channel_id == int(getattr(channel, "id", 0) or 0) and state.auto_bitrate_boosted == target:
            return

        record = {
            "channel_id": int(getattr(channel, "id", 0) or 0),
            "original_bitrate": current,
            "boosted_bitrate": target,
            "started_at": time.time(),
            "reason": "music_player",
            "active_players": self._active_player_count(),
        }
        # Sem persistência, não altera para evitar canal preso após restart/crash.
        if not await self._save_auto_bitrate_record(guild.id, record):
            return

        try:
            await channel.edit(bitrate=target, reason="Aumentar bitrate temporariamente enquanto o player de música toca")
        except (discord.Forbidden, discord.HTTPException):
            await self._clear_auto_bitrate_record(guild.id)
            logger.debug("[music] auto bitrate não pôde editar canal | guild=%s channel=%s", guild.id, getattr(channel, "id", None), exc_info=True)
            return
        except Exception:
            await self._clear_auto_bitrate_record(guild.id)
            logger.debug("[music] auto bitrate falhou", exc_info=True)
            return

        state.auto_bitrate_channel_id = record["channel_id"]
        state.auto_bitrate_original = record["original_bitrate"]
        state.auto_bitrate_boosted = record["boosted_bitrate"]

    async def _restore_auto_bitrate_for_state(
        self,
        guild: discord.Guild | None,
        state: MusicGuildState,
        *,
        reason: str = "music_finished",
        channel_hint=None,
    ) -> None:
        if not MUSIC_AUTO_BITRATE_ENABLED:
            return
        if guild is None:
            return

        record = None
        if state.auto_bitrate_channel_id and state.auto_bitrate_original and state.auto_bitrate_boosted:
            record = {
                "channel_id": int(state.auto_bitrate_channel_id),
                "original_bitrate": int(state.auto_bitrate_original),
                "boosted_bitrate": int(state.auto_bitrate_boosted),
            }
        else:
            record = self._load_auto_bitrate_record_into_state(guild.id, state)
        if not record:
            return

        try:
            channel_id = int(record.get("channel_id") or 0)
            original = int(record.get("original_bitrate") or 0)
            boosted = int(record.get("boosted_bitrate") or 0)
        except Exception:
            await self._clear_auto_bitrate_record(guild.id)
            return
        if channel_id <= 0 or original <= 0 or boosted <= 0:
            await self._clear_auto_bitrate_record(guild.id)
            return

        channel = channel_hint if channel_hint is not None and int(getattr(channel_hint, "id", 0) or 0) == channel_id else None
        channel = channel or guild.get_channel(channel_id) or self.bot.get_channel(channel_id)
        if channel is None or not self._is_normal_voice_channel(channel):
            await self._clear_auto_bitrate_record(guild.id)
        else:
            current = int(getattr(channel, "bitrate", 0) or 0)
            # Não briga com staff: se alguém alterou manualmente, só limpa a marcação.
            if current == boosted:
                if not self._bot_can_manage_voice_channel(guild, channel):
                    return
                try:
                    await channel.edit(bitrate=original, reason=f"Restaurar bitrate após música ({reason})")
                except (discord.Forbidden, discord.HTTPException):
                    logger.debug("[music] não consegui restaurar bitrate | guild=%s channel=%s", guild.id, channel_id, exc_info=True)
                    return
                except Exception:
                    logger.debug("[music] restauração de bitrate falhou", exc_info=True)
                    return
            await self._clear_auto_bitrate_record(guild.id)

        state.auto_bitrate_channel_id = None
        state.auto_bitrate_original = None
        state.auto_bitrate_boosted = None

    async def reconcile_auto_bitrate_records(self) -> None:
        """Restaura bitrates temporários pendentes após restart do bot."""
        if not MUSIC_AUTO_BITRATE_ENABLED:
            return
        settings_db = getattr(self.bot, "settings_db", None)
        docs = getattr(settings_db, "guild_cache", {}) if settings_db is not None else {}
        for guild_id, doc in list(docs.items()):
            if not isinstance(doc, dict) or not isinstance(doc.get("music_auto_bitrate"), dict):
                continue
            guild = self.bot.get_guild(int(guild_id))
            if guild is None:
                await self._clear_auto_bitrate_record(int(guild_id))
                continue
            state = self.get_state(int(guild_id))
            self._load_auto_bitrate_record_into_state(int(guild_id), state)
            await self._restore_auto_bitrate_for_state(guild, state, reason="restart")

    def _voice_status_record_from_doc(self, guild_id: int) -> dict | None:
        settings_db = getattr(self.bot, "settings_db", None)
        if settings_db is None:
            return None
        try:
            doc = getattr(settings_db, "guild_cache", {}).get(int(guild_id), {}) or {}
            raw = doc.get("music_voice_status_restore")
            if isinstance(raw, dict):
                return dict(raw)
        except Exception:
            logger.debug("[music] falha ao ler status de voz persistido", exc_info=True)
        return None

    async def _save_voice_status_record(self, guild_id: int, record: dict) -> bool:
        settings_db = getattr(self.bot, "settings_db", None)
        if settings_db is None:
            return False
        try:
            get_doc = getattr(settings_db, "_get_guild_doc", None)
            save_doc = getattr(settings_db, "_save_guild_doc", None)
            if not callable(get_doc) or not callable(save_doc):
                return False
            doc = get_doc(int(guild_id))
            doc["music_voice_status_restore"] = dict(record)
            await save_doc(int(guild_id), doc)
            return True
        except Exception:
            logger.debug("[music] falha ao salvar status de voz", exc_info=True)
            return False

    async def _clear_voice_status_record(self, guild_id: int) -> None:
        settings_db = getattr(self.bot, "settings_db", None)
        if settings_db is None:
            return
        try:
            get_doc = getattr(settings_db, "_get_guild_doc", None)
            save_doc = getattr(settings_db, "_save_guild_doc", None)
            if not callable(get_doc) or not callable(save_doc):
                return
            doc = get_doc(int(guild_id))
            doc.pop("music_voice_status_restore", None)
            await save_doc(int(guild_id), doc)
        except Exception:
            logger.debug("[music] falha ao limpar status de voz", exc_info=True)

    def _load_voice_status_record_into_state(self, guild_id: int, state: MusicGuildState) -> dict | None:
        record = self._voice_status_record_from_doc(guild_id)
        if not record:
            return None
        try:
            state.voice_status_channel_id = int(record.get("channel_id") or 0) or None
            state.voice_status_had_original = bool(record.get("had_original_status"))
            state.voice_status_original = str(record.get("original_status") or "")
            state.voice_status_last_bot = str(record.get("last_bot_status") or "")
            state.voice_status_last_track_key = str(record.get("last_track_key") or "")
        except Exception:
            state.voice_status_channel_id = None
            state.voice_status_had_original = False
            state.voice_status_original = ""
            state.voice_status_last_bot = ""
            state.voice_status_last_track_key = ""
            return None
        return record

    def _voice_status_settings_from_doc(self, guild_id: int) -> dict:
        settings_db = getattr(self.bot, "settings_db", None)
        doc = {}
        try:
            doc = getattr(settings_db, "guild_cache", {}).get(int(guild_id), {}) if settings_db is not None else {}
        except Exception:
            doc = {}
        raw = doc.get("music_voice_status") if isinstance(doc, dict) else None
        raw = raw if isinstance(raw, dict) else {}
        enabled = bool(raw.get("enabled", MUSIC_VOICE_STATUS_ENABLED))
        template = str(raw.get("template") or MUSIC_VOICE_STATUS_TEMPLATE).strip() or MUSIC_VOICE_STATUS_TEMPLATE
        idle = str(raw.get("idle") if raw.get("idle") is not None else MUSIC_VOICE_STATUS_IDLE).strip()
        return {"enabled": enabled, "template": template, "idle": idle}

    async def _save_voice_status_settings(self, guild_id: int, settings: dict) -> None:
        settings_db = getattr(self.bot, "settings_db", None)
        if settings_db is None:
            return
        try:
            get_doc = getattr(settings_db, "_get_guild_doc", None)
            save_doc = getattr(settings_db, "_save_guild_doc", None)
            if not callable(get_doc) or not callable(save_doc):
                return
            doc = get_doc(int(guild_id))
            doc["music_voice_status"] = {
                "enabled": bool(settings.get("enabled", MUSIC_VOICE_STATUS_ENABLED)),
                "template": str(settings.get("template") or MUSIC_VOICE_STATUS_TEMPLATE).strip() or MUSIC_VOICE_STATUS_TEMPLATE,
                "idle": str(settings.get("idle") if settings.get("idle") is not None else "").strip(),
            }
            await save_doc(int(guild_id), doc)
        except Exception:
            logger.debug("[music] falha ao salvar configuração de status de voz", exc_info=True)

    def get_voice_status_settings(self, guild_id: int) -> dict:
        return self._voice_status_settings_from_doc(guild_id)

    async def set_voice_status_enabled(self, guild_id: int, enabled: bool) -> dict:
        settings = self._voice_status_settings_from_doc(guild_id)
        settings["enabled"] = bool(enabled)
        await self._save_voice_status_settings(guild_id, settings)
        guild = self.bot.get_guild(int(guild_id))
        state = self.get_state(guild_id)
        if not enabled:
            await self._restore_voice_status_for_state(guild, state, reason="config_disabled")
        elif guild is not None and state.current is not None and state.last_voice_channel_id:
            channel = guild.get_channel(int(state.last_voice_channel_id)) or self.bot.get_channel(int(state.last_voice_channel_id))
            await self._apply_voice_status_for_music(guild, channel, state, state.current, force=True)
        return settings

    async def set_voice_status_template(self, guild_id: int, template: str) -> dict:
        settings = self._voice_status_settings_from_doc(guild_id)
        settings["template"] = self._sanitize_voice_status_template(template)
        await self._save_voice_status_settings(guild_id, settings)
        guild = self.bot.get_guild(int(guild_id))
        state = self.get_state(guild_id)
        if guild is not None and state.current is not None and state.last_voice_channel_id:
            channel = guild.get_channel(int(state.last_voice_channel_id)) or self.bot.get_channel(int(state.last_voice_channel_id))
            await self._apply_voice_status_for_music(guild, channel, state, state.current, force=True)
        return settings

    async def set_voice_status_idle(self, guild_id: int, idle: str) -> dict:
        settings = self._voice_status_settings_from_doc(guild_id)
        settings["idle"] = self._trim_voice_status(str(idle or ""))
        await self._save_voice_status_settings(guild_id, settings)
        return settings

    async def reset_voice_status_settings(self, guild_id: int) -> dict:
        settings = {"enabled": MUSIC_VOICE_STATUS_ENABLED, "template": MUSIC_VOICE_STATUS_TEMPLATE, "idle": MUSIC_VOICE_STATUS_IDLE}
        await self._save_voice_status_settings(guild_id, settings)
        guild = self.bot.get_guild(int(guild_id))
        state = self.get_state(guild_id)
        if guild is not None and state.current is not None and state.last_voice_channel_id:
            channel = guild.get_channel(int(state.last_voice_channel_id)) or self.bot.get_channel(int(state.last_voice_channel_id))
            await self._apply_voice_status_for_music(guild, channel, state, state.current, force=True)
        return settings

    def _sanitize_voice_status_template(self, template: str) -> str:
        template = str(template or "").strip()
        if not template:
            template = MUSIC_VOICE_STATUS_TEMPLATE
        return template[:500]

    def _trim_voice_status(self, status: str) -> str:
        status = " ".join(str(status or "").replace("\n", " ").split())
        return status[:500]

    def _source_key_for_track(self, track: MusicTrack | None) -> str:
        if track is None:
            return ""
        fields = []
        for attr in ("source", "extractor", "original_url", "webpage_url", "display_url", "stream_url"):
            with contextlib.suppress(Exception):
                value = str(getattr(track, attr, "") or "").strip().lower()
                if value:
                    fields.append(value)
        text = " ".join(fields)
        # Prioridade nos links/fontes originais: quando um link Spotify/Deezer
        # cai em fallback tocável do YouTube, o status ainda deve mostrar o
        # emoji da plataforma que o usuário pediu, não um ícone genérico.
        if "spotify" in text:
            return "spotify"
        if "soundcloud" in text or "sound cloud" in text:
            return "soundcloud"
        if "deezer" in text:
            return "deezer"
        if "youtube" in text or "youtu.be" in text or "ytmusic" in text or "yt-dlp" in text:
            return "youtube"
        return ""

    def _source_emoji_for_track(self, track: MusicTrack | None) -> str:
        source_key = self._source_key_for_track(track)
        return MUSIC_SOURCE_EMOJIS.get(source_key) or MUSIC_SOURCE_EMOJI_FALLBACK

    def _voice_status_track_key(self, track: MusicTrack | None) -> str:
        if track is None:
            return ""
        parts: list[str] = []
        for attr in ("webpage_url", "original_url", "display_url", "title", "uploader", "duration"):
            with contextlib.suppress(Exception):
                value = str(getattr(track, attr, "") or "").strip()
                if value:
                    parts.append(value)
        if not parts:
            return str(id(track))
        return "|".join(parts)

    def _voice_status_track_is_current(self, state: MusicGuildState, track: MusicTrack | None, track_key: str) -> bool:
        current = state.current
        if current is None or track is None:
            return False
        if current is track:
            return True
        return bool(track_key and self._voice_status_track_key(current) == track_key)

    def _quality_label_for_cap(self, cap: int | None) -> str:
        if cap is None:
            return "Alta"
        try:
            cap_int = int(cap or 0)
        except Exception:
            cap_int = 0
        if cap_int <= MUSIC_HEAVY_LOAD_MAX_ABR:
            return "Baixa"
        return "Média"

    def _refresh_quality_state(self, state: MusicGuildState, track: MusicTrack | None = None, *, cap: int | None = None) -> None:
        track = track or state.current
        if cap is None:
            cap = self._audio_max_abr_for_load()
        state.current_quality_label = self._quality_label_for_cap(cap)
        kbps = 0
        if track is not None:
            with contextlib.suppress(Exception):
                kbps = int(getattr(track, "resolved_audio_abr", 0) or 0)
            if not kbps:
                with contextlib.suppress(Exception):
                    kbps = int(getattr(track, "resolved_audio_max_abr", 0) or 0)
        if not kbps:
            kbps = int(cap or MUSIC_HIGH_QUALITY_MAX_ABR)
        state.current_quality_kbps = max(1, int(kbps))

    def render_voice_status(self, guild_id: int, track: MusicTrack | None = None, *, template: str | None = None) -> str:
        state = self.get_state(guild_id)
        track = track or state.current
        self._refresh_quality_state(state, track)
        requester = ""
        if track is not None:
            requester = str(getattr(track, "requester_name", "") or f"<@{getattr(track, 'requester_id', 0)}>")
        title = str(getattr(track, "title", "") or "Música sem título")
        author = str(getattr(track, "uploader", "") or getattr(track, "source", "") or getattr(track, "extractor", "") or "fonte desconhecida")
        duration = getattr(track, "duration_label", "desconhecida") if track is not None else "desconhecida"
        elapsed = "0:00"
        remaining = duration
        if track is not None and not getattr(track, "is_live", False):
            started = float(getattr(state, "current_started_at_monotonic", 0.0) or 0.0)
            elapsed_seconds = max(0, int(time.monotonic() - started)) if started else 0
            elapsed = self._format_seconds(elapsed_seconds)
            if getattr(track, "duration", None) is not None:
                remaining = self._format_seconds(max(0, int(float(track.duration) - elapsed_seconds)))
        values = {
            "source_emoji": self._source_emoji_for_track(track),
            "title": title,
            "artist": author,
            "author": author,
            "duration": duration,
            "elapsed": elapsed,
            "remaining": remaining,
            "requester": requester or "alguém",
            "queue": str(state.queue_size()),
            "quality": str(getattr(state, "current_quality_label", "Alta") or "Alta"),
            "kbps": str(int(getattr(state, "current_quality_kbps", MUSIC_HIGH_QUALITY_MAX_ABR) or MUSIC_HIGH_QUALITY_MAX_ABR)),
        }
        raw_template = self._sanitize_voice_status_template(template or self._voice_status_settings_from_doc(guild_id).get("template") or MUSIC_VOICE_STATUS_TEMPLATE)
        for key, value in values.items():
            raw_template = raw_template.replace("{" + key + "}", str(value))
        return self._trim_voice_status(raw_template)

    def preview_voice_status(self, guild_id: int) -> str:
        state = self.get_state(guild_id)
        if state.current is not None:
            return self.render_voice_status(guild_id, state.current)
        sample = MusicTrack(
            title="Compass [Limbus Company]",
            webpage_url="https://www.youtube.com/watch?v=preview",
            requester_id=0,
            requester_name="C.◉.R.E",
            uploader="Mili",
            source="YouTube",
            extractor="Youtube",
            duration=169,
        )
        sample.resolved_audio_abr = int(getattr(state, "current_quality_kbps", MUSIC_HIGH_QUALITY_MAX_ABR) or MUSIC_HIGH_QUALITY_MAX_ABR)
        return self.render_voice_status(guild_id, sample)

    def _format_seconds(self, total: int | float) -> str:
        total = max(0, int(total or 0))
        hours, rem = divmod(total, 3600)
        minutes, seconds = divmod(rem, 60)
        if hours:
            return f"{hours}:{minutes:02d}:{seconds:02d}"
        return f"{minutes}:{seconds:02d}"

    def _bot_can_set_voice_status(self, guild: discord.Guild, channel) -> bool:
        """Best-effort permission check for Discord's voice-status endpoint.

        Older discord.py builds may not expose the newer
        ``set_voice_channel_status`` permission flag yet. In that case we do
        not block the feature here; the raw REST endpoint will be the source of
        truth and will return Forbidden if the bot really lacks permission.
        """
        member = getattr(guild, "me", None)
        if member is None or channel is None:
            return False
        try:
            perms = channel.permissions_for(member)
            if bool(getattr(perms, "manage_channels", False)):
                return True
            voice_status_perm = getattr(perms, "set_voice_channel_status", None)
            if voice_status_perm is None:
                return True
            return bool(voice_status_perm)
        except Exception:
            # If the library cannot evaluate the new permission, still try the
            # endpoint and let Discord answer with 403 when needed.
            return True

    async def _fetch_voice_channel_status(self, channel) -> tuple[bool, str]:
        if channel is None:
            return False, ""
        for attr in ("status", "voice_status"):
            if hasattr(channel, attr):
                value = getattr(channel, attr, None)
                if value:
                    return True, str(value or "")
                # Em versões atuais do discord.py, esses atributos podem existir
                # mas vir vazios/desatualizados mesmo quando o endpoint de
                # voice-status está funcionando. Tratar vazio como "conhecido"
                # fazia o bot achar que staff mudou manualmente e impedia a
                # troca de status quando a próxima música começava.
        try:
            http = getattr(self.bot, "http", None)
            data = None
            get_channel = getattr(http, "get_channel", None)
            if callable(get_channel):
                data = await get_channel(int(getattr(channel, "id", 0)))
            else:
                from discord.http import Route
                request = getattr(http, "request", None)
                if callable(request):
                    data = await request(Route("GET", "/channels/{channel_id}", channel_id=int(getattr(channel, "id", 0))))
            if isinstance(data, dict):
                if data.get("status"):
                    return True, str(data.get("status") or "")
                if data.get("voice_status"):
                    return True, str(data.get("voice_status") or "")
        except (discord.Forbidden, discord.HTTPException):
            return False, ""
        except Exception:
            logger.debug("[music] falha ao ler status atual do canal", exc_info=True)
        return False, ""

    async def _set_voice_channel_status(self, channel, status: str, *, reason: str = "") -> bool:
        try:
            from discord.http import Route
            http = getattr(self.bot, "http", None)
            request = getattr(http, "request", None)
            if not callable(request):
                logger.warning("[music] não consegui alterar status do canal: cliente HTTP indisponível")
                return False
            channel_id = int(getattr(channel, "id", 0) or 0)
            if channel_id <= 0:
                logger.warning("[music] não consegui alterar status do canal: canal inválido")
                return False
            payload_status = self._trim_voice_status(status)
            if payload_status:
                await request(
                    Route("PUT", "/channels/{channel_id}/voice-status", channel_id=channel_id),
                    json={"status": payload_status},
                    reason=reason or None,
                )
            else:
                # O endpoint de status de voz usa PUT. Enviar string vazia é
                # aceito pelo cliente oficial para limpar o status; caso o
                # Discord mude o contrato, tentamos null como fallback.
                try:
                    await request(
                        Route("PUT", "/channels/{channel_id}/voice-status", channel_id=channel_id),
                        json={"status": ""},
                        reason=reason or None,
                    )
                except discord.HTTPException:
                    await request(
                        Route("PUT", "/channels/{channel_id}/voice-status", channel_id=channel_id),
                        json={"status": None},
                        reason=reason or None,
                    )
            return True
        except discord.Forbidden:
            logger.warning("[music] não consegui alterar status do canal: permissão ausente")
            return False
        except discord.HTTPException as exc:
            logger.warning("[music] não consegui alterar status do canal: HTTP %s", getattr(exc, "status", "?"))
            return False
        except Exception:
            logger.warning("[music] falha inesperada ao alterar status do canal", exc_info=True)
            return False

    async def _apply_voice_status_for_music(self, guild: discord.Guild, channel, state: MusicGuildState, track: MusicTrack, *, force: bool = False) -> None:
        if guild is None or channel is None or track is None:
            return
        settings = self._voice_status_settings_from_doc(guild.id)
        if not bool(settings.get("enabled", True)):
            return
        if not self._bot_can_set_voice_status(guild, channel):
            logger.warning("[music] não consegui alterar status do canal: permissão ausente")
            return
        channel_id = int(getattr(channel, "id", 0) or 0)
        if channel_id <= 0:
            return

        track_key = self._voice_status_track_key(track)
        desired = self.render_voice_status(guild.id, track, template=settings.get("template"))
        if not desired:
            return
        desired_key = f"{channel_id}:{track_key}:{desired}"
        # Dedup barato antes de chamar REST: se o status renderizado já é o
        # último aplicado para essa faixa/canal, não faça nova chamada nem log.
        if (
            int(getattr(state, "voice_status_channel_id", 0) or 0) == channel_id
            and str(getattr(state, "voice_status_last_track_key", "") or "") == track_key
            and str(getattr(state, "voice_status_last_bot", "") or "") == desired
        ):
            return
        # Troca de faixa é uma atualização forçada e não deve ser bloqueada
        # pela checagem anti-staff. Porém, se a task antiga terminar depois de
        # a música já ter mudado, ela não pode sobrescrever o status novo.
        if force and not self._voice_status_track_is_current(state, track, track_key):
            return

        async with state.voice_status_lock:
            if force and not self._voice_status_track_is_current(state, track, track_key):
                return
            if state.voice_status_channel_id and int(state.voice_status_channel_id) != channel_id:
                await self._restore_voice_status_for_state(guild, state, reason="channel_change")

            known, current_status = await self._fetch_voice_channel_status(channel)
            if not known:
                # O status atual do canal nem sempre vem no objeto/REST comum do
                # discord.py. Ainda assim aplicamos o status da música; só marcamos
                # que não havia status original conhecido para não inventar restauração.
                current_status = ""
            if state.voice_status_channel_id == channel_id and state.voice_status_last_bot:
                if known and current_status != state.voice_status_last_bot and not force:
                    # Staff mudou manualmente; não briga com a alteração em
                    # atualizações periódicas. Trocas reais de faixa usam force=True.
                    await self._clear_voice_status_record(guild.id)
                    state.voice_status_channel_id = None
                    state.voice_status_had_original = False
                    state.voice_status_original = ""
                    state.voice_status_last_bot = ""
                    state.voice_status_last_track_key = ""
                    return
            else:
                record = {
                    "channel_id": channel_id,
                    "had_original_status": bool(current_status),
                    "original_status": current_status,
                    "last_bot_status": "",
                    "last_track_key": "",
                    "started_at": time.time(),
                    "reason": "music_player",
                }
                if not await self._save_voice_status_record(guild.id, record):
                    return
                state.voice_status_channel_id = channel_id
                state.voice_status_had_original = bool(current_status)
                state.voice_status_original = current_status
                state.voice_status_last_bot = ""
                state.voice_status_last_track_key = ""

            if desired == state.voice_status_last_bot and track_key == state.voice_status_last_track_key:
                return
            if desired_key == str(getattr(state, "voice_status_last_applied_key", "") or ""):
                return
            if force and not self._voice_status_track_is_current(state, track, track_key):
                return
            logger.info("[music] aplicando status do canal | guild=%s channel=%s track=%r", guild.id, channel_id, getattr(track, "title", ""))
            if not await self._set_voice_channel_status(channel, desired, reason="Atualizar status do canal enquanto a música toca"):
                await self._clear_voice_status_record(guild.id)
                state.voice_status_channel_id = None
                state.voice_status_had_original = False
                state.voice_status_original = ""
                state.voice_status_last_bot = ""
                state.voice_status_last_track_key = ""
                return
            logger.info("[music] status do canal aplicado | guild=%s channel=%s", guild.id, channel_id)
            state.voice_status_last_bot = desired
            state.voice_status_last_track_key = track_key
            state.voice_status_last_applied_key = desired_key
            state.voice_status_last_update_at = time.monotonic()
            await self._save_voice_status_record(
                guild.id,
                {
                    "channel_id": channel_id,
                    "had_original_status": bool(state.voice_status_had_original),
                    "original_status": state.voice_status_original,
                    "last_bot_status": desired,
                    "last_track_key": track_key,
                    "started_at": time.time(),
                    "reason": "music_player",
                },
            )
            self._schedule_voice_status_refresh(guild.id, state)

    def _schedule_voice_status_track_sync(self, guild_id: int, *, repeat_after: float = 2.0, reason: str = "track_change") -> None:
        """Sincroniza status do canal para a faixa atual sem bloquear o player.

        Troca real de faixa não usa o mesmo cooldown do refresh periódico.
        O status só deve ser aplicado depois que o playback começou de verdade;
        retries atrasados só devem acontecer quando o chamador pedir explicitamente.
        """
        state = self.get_state(guild_id)
        current_track = getattr(state, "current", None)
        current_key = self._voice_status_track_key(current_track) if current_track is not None else ""
        now = time.monotonic()
        sync_key = f"{current_key}:{reason}"
        # Trocas reais de faixa precisam passar imediatamente, inclusive quando
        # o usuário alterna rápido A → B → A. O cooldown antigo podia bloquear
        # a segunda atualização legítima e deixar o status preso na música
        # anterior até o refresh tardio.
        high_priority_reason = reason in {"playback_started", "lavalink_track_started", "skip", "previous"}
        if (
            not high_priority_reason
            and sync_key
            and sync_key == str(getattr(state, "voice_status_last_sync_request_key", "") or "")
            and now - float(getattr(state, "voice_status_last_sync_request_at", 0.0) or 0.0) < 1.75
        ):
            return
        state.voice_status_last_sync_request_key = sync_key
        state.voice_status_last_sync_request_at = now
        old_task = state.voice_status_force_task
        if old_task is not None and not old_task.done():
            old_task.cancel()

        async def _runner() -> None:
            try:
                guild = self.bot.get_guild(int(guild_id))
                if guild is None or state.current is None or not state.last_voice_channel_id:
                    return
                channel = guild.get_channel(int(state.last_voice_channel_id)) or self.bot.get_channel(int(state.last_voice_channel_id))
                if channel is None:
                    return
                track_key = self._voice_status_track_key(state.current)
                logger.info(
                    "[music] atualizando status do canal por %s | guild=%s track=%r",
                    reason,
                    guild_id,
                    getattr(state.current, "title", ""),
                )
                await self._apply_voice_status_for_music(guild, channel, state, state.current, force=True)
                if repeat_after <= 0:
                    return
                await asyncio.sleep(max(0.0, float(repeat_after)))
                if state.current is None or self._voice_status_track_key(state.current) != track_key:
                    return
                channel = guild.get_channel(int(state.last_voice_channel_id)) or self.bot.get_channel(int(state.last_voice_channel_id))
                if channel is None:
                    return
                await self._apply_voice_status_for_music(guild, channel, state, state.current, force=True)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.debug("[music] sincronização forçada de status de voz falhou", exc_info=True)
            finally:
                if state.voice_status_force_task is asyncio.current_task():
                    state.voice_status_force_task = None

        state.voice_status_force_task = asyncio.create_task(_runner())
        state.voice_status_force_task.add_done_callback(_consume_expected_music_exception)

    def _schedule_voice_status_refresh(self, guild_id: int, state: MusicGuildState) -> None:
        settings = self._voice_status_settings_from_doc(guild_id)
        template = str(settings.get("template") or "")
        if "{elapsed}" not in template and "{remaining}" not in template:
            return
        task = state.voice_status_update_task
        if task is not None and not task.done():
            return

        async def _runner() -> None:
            try:
                while state.current is not None and state.current_status in {"playing", "paused"}:
                    await asyncio.sleep(MUSIC_VOICE_STATUS_UPDATE_INTERVAL_SECONDS)
                    guild = self.bot.get_guild(int(guild_id))
                    if guild is None or state.current is None or not state.last_voice_channel_id:
                        return
                    channel = guild.get_channel(int(state.last_voice_channel_id)) or self.bot.get_channel(int(state.last_voice_channel_id))
                    await self._apply_voice_status_for_music(guild, channel, state, state.current, force=False)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.debug("[music] atualização periódica de status de voz falhou", exc_info=True)
            finally:
                if state.voice_status_update_task is asyncio.current_task():
                    state.voice_status_update_task = None

        state.voice_status_update_task = asyncio.create_task(_runner())
        state.voice_status_update_task.add_done_callback(_consume_expected_music_exception)

    def _cancel_voice_status_refresh(self, state: MusicGuildState) -> None:
        task = state.voice_status_update_task
        if task is not None and not task.done():
            task.cancel()
        state.voice_status_update_task = None
        force_task = state.voice_status_force_task
        current_task = asyncio.current_task()
        if force_task is not None and not force_task.done() and force_task is not current_task:
            force_task.cancel()
        if force_task is not current_task:
            state.voice_status_force_task = None

    async def _restore_voice_status_for_state(
        self,
        guild: discord.Guild | None,
        state: MusicGuildState,
        *,
        reason: str = "music_finished",
        channel_hint=None,
    ) -> None:
        self._cancel_voice_status_refresh(state)
        if guild is None:
            return
        record = None
        if state.voice_status_channel_id:
            record = {
                "channel_id": int(state.voice_status_channel_id),
                "had_original_status": bool(state.voice_status_had_original),
                "original_status": state.voice_status_original,
                "last_bot_status": state.voice_status_last_bot,
            }
        else:
            record = self._load_voice_status_record_into_state(guild.id, state)
        if not record:
            return
        try:
            channel_id = int(record.get("channel_id") or 0)
            original_status = str(record.get("original_status") or "")
            had_original = bool(record.get("had_original_status"))
            last_bot_status = str(record.get("last_bot_status") or "")
        except Exception:
            await self._clear_voice_status_record(guild.id)
            return
        if channel_id <= 0:
            await self._clear_voice_status_record(guild.id)
            return
        channel = channel_hint if channel_hint is not None and int(getattr(channel_hint, "id", 0) or 0) == channel_id else None
        channel = channel or guild.get_channel(channel_id) or self.bot.get_channel(channel_id)
        if channel is None:
            await self._clear_voice_status_record(guild.id)
        else:
            known, current_status = await self._fetch_voice_channel_status(channel)
            if known and last_bot_status and current_status != last_bot_status:
                # Staff mudou manualmente; respeita a alteração e só limpa a marcação.
                await self._clear_voice_status_record(guild.id)
            else:
                if not self._bot_can_set_voice_status(guild, channel):
                    logger.warning("[music] não consegui restaurar status do canal: permissão ausente")
                    return
                target_status = original_status if had_original else str(self._voice_status_settings_from_doc(guild.id).get("idle") or "")
                restore_key = f"{channel_id}:{target_status}:{reason}"
                now = time.monotonic()
                if (
                    restore_key == str(getattr(state, "voice_status_last_restore_key", "") or "")
                    and now - float(getattr(state, "voice_status_last_restore_at", 0.0) or 0.0) < 4.0
                ):
                    return
                state.voice_status_last_restore_key = restore_key
                state.voice_status_last_restore_at = now
                logger.info("[music] restaurando status do canal | guild=%s channel=%s reason=%s", guild.id, channel_id, reason)
                ok = await self._set_voice_channel_status(channel, target_status, reason=f"Restaurar status do canal após música ({reason})")
                if not ok:
                    return
                await self._clear_voice_status_record(guild.id)
        state.voice_status_channel_id = None
        state.voice_status_had_original = False
        state.voice_status_original = ""
        state.voice_status_last_bot = ""
        state.voice_status_last_track_key = ""
        state.voice_status_last_applied_key = ""
        state.voice_status_last_sync_request_key = ""
        state.voice_status_last_sync_request_at = 0.0
        state.voice_status_last_update_at = 0.0

    async def reconcile_voice_status_records(self) -> None:
        """Restaura status temporários de canal pendentes após restart do bot."""
        settings_db = getattr(self.bot, "settings_db", None)
        docs = getattr(settings_db, "guild_cache", {}) if settings_db is not None else {}
        for guild_id, doc in list(docs.items()):
            if not isinstance(doc, dict) or not isinstance(doc.get("music_voice_status_restore"), dict):
                continue
            guild = self.bot.get_guild(int(guild_id))
            if guild is None:
                await self._clear_voice_status_record(int(guild_id))
                continue
            state = self.get_state(int(guild_id))
            self._load_voice_status_record_into_state(int(guild_id), state)
            await self._restore_voice_status_for_state(guild, state, reason="restart")

    def _clear_idle_reason(self, state: MusicGuildState) -> None:
        state.idle_reason = "idle"
        state.idle_actor_id = None
        state.idle_actor_name = ""
        state.idle_channel_name = ""

    def _set_idle_reason(
        self,
        state: MusicGuildState,
        reason: str,
        *,
        actor: discord.abc.User | discord.Member | None = None,
        actor_name: str = "",
        channel_name: str = "",
    ) -> None:
        state.idle_reason = (reason or "idle").strip() or "idle"
        if actor is not None:
            state.idle_actor_id = int(getattr(actor, "id", 0) or 0) or None
            state.idle_actor_name = getattr(actor, "display_name", None) or getattr(actor, "name", None) or str(actor)
        else:
            state.idle_actor_id = None
            state.idle_actor_name = actor_name or ""
        state.idle_channel_name = channel_name or ""

    def _mark_internal_voice_disconnect(self, guild_id: int, *, seconds: float = 8.0) -> None:
        state = self.get_state(guild_id)
        state.internal_voice_disconnect_until = max(state.internal_voice_disconnect_until, time.monotonic() + max(0.0, float(seconds)))

    def _is_internal_voice_disconnect_recent(self, state: MusicGuildState) -> bool:
        return time.monotonic() < float(getattr(state, "internal_voice_disconnect_until", 0.0) or 0.0)

    def _mark_lavalink_transition(self, state: MusicGuildState, *, seconds: float = 12.0) -> None:
        state.lavalink_transition_until = max(
            float(getattr(state, "lavalink_transition_until", 0.0) or 0.0),
            time.monotonic() + max(0.0, float(seconds)),
        )

    def _is_lavalink_transition_recent(self, state: MusicGuildState) -> bool:
        return time.monotonic() < float(getattr(state, "lavalink_transition_until", 0.0) or 0.0)

    def _is_lavalink_strict_mode(self, guild_id: int | None) -> bool:
        mode_getter = getattr(self.backends, "lavalink_mode_for_guild", None)
        if not callable(mode_getter):
            return False
        try:
            return str(mode_getter(guild_id) or "").lower() == "lavalink"
        except Exception:
            return False

    def is_lavalink_active_for_guild(self, guild_id: int | None) -> bool:
        try:
            state = self.get_state(int(guild_id or 0))
        except Exception:
            return False
        return bool(
            state.current_backend == "lavalink"
            or state.current_lavalink_player is not None
            or self._is_lavalink_transition_recent(state)
        )

    def is_music_staff(self, member) -> bool:
        if member is None or getattr(member, "bot", False):
            return False
        guild = getattr(member, "guild", None)
        if guild is not None and getattr(guild, "owner_id", None) == getattr(member, "id", None):
            return True
        perms = getattr(member, "guild_permissions", None)
        return bool(
            getattr(perms, "administrator", False)
            or getattr(perms, "manage_guild", False)
            or getattr(perms, "manage_channels", False)
            or getattr(perms, "move_members", False)
        )

    def _is_current_requester(self, state: MusicGuildState, member) -> bool:
        current = state.current
        return bool(current is not None and member is not None and int(getattr(current, "requester_id", 0) or 0) == int(getattr(member, "id", 0) or 0))

    def _prune_control_votes(self, state: MusicGuildState) -> None:
        stale = [key for key, vote in state.control_votes.items() if vote.expired()]
        for key in stale:
            state.control_votes.pop(key, None)
            task = state.control_vote_cleanup_tasks.pop(key, None)
            if task is not None and not task.done():
                task.cancel()

    def pending_vote_summary(self, guild_id: int) -> list[tuple[str, int, int]]:
        state = self.get_state(guild_id)
        self._prune_control_votes(state)
        labels = {"skip": "Pular", "stop": "Parar", "shuffle": "Shuffle", "loop": "Loop"}
        return [(labels.get(action, action), len(vote.voters), 2) for action, vote in state.control_votes.items() if vote.voters]

    def _schedule_vote_cleanup(self, guild_id: int, action: str) -> None:
        state = self.get_state(guild_id)
        old = state.control_vote_cleanup_tasks.get(action)
        if old is not None and not old.done():
            old.cancel()

        async def _cleanup() -> None:
            try:
                await asyncio.sleep(MUSIC_CONTROL_VOTE_SECONDS + 0.25)
                st = self.get_state(guild_id)
                vote = st.control_votes.get(action)
                if vote is not None and vote.expired():
                    st.control_votes.pop(action, None)
                    await self.update_panel(guild_id, create=bool(st.now_message))
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.debug("[music] falha ao limpar votação", exc_info=True)

        try:
            state.control_vote_cleanup_tasks[action] = asyncio.create_task(_cleanup())
        except RuntimeError:
            pass

    async def _control_or_vote(self, guild_id: int, member, action: str) -> tuple[bool, str, bool]:
        state = self.get_state(guild_id)
        self._prune_control_votes(state)
        if member is None or getattr(member, "bot", False):
            return False, "Bots não podem controlar essa ação.", False
        if self.is_music_staff(member) or self._is_current_requester(state, member):
            state.control_votes.pop(action, None)
            task = state.control_vote_cleanup_tasks.pop(action, None)
            if task is not None and not task.done():
                task.cancel()
            return True, "", False
        vote = state.control_votes.get(action)
        if vote is None or vote.expired():
            vote = ControlVote(action=action)
            state.control_votes[action] = vote
        vote.voters.add(int(member.id))
        if len(vote.voters) >= 2:
            state.control_votes.pop(action, None)
            task = state.control_vote_cleanup_tasks.pop(action, None)
            if task is not None and not task.done():
                task.cancel()
            return True, "", True
        self._schedule_vote_cleanup(guild_id, action)
        self._schedule_panel_update(guild_id, create=bool(state.now_message))
        label = {"skip": "pular", "stop": "parar", "shuffle": "shuffle", "loop": "repetição"}.get(action, action)
        return False, f"`🗳️` Voto registrado para **{label}**: `1/2`.", False

    def is_music_active(self, guild_id: int) -> bool:
        state = self._states.get(int(guild_id))
        return bool(
            state
            and (
                state.current
                or self._has_pending_track(state)
                or state.current_lavalink_player is not None
                or self._is_lavalink_transition_recent(state)
                or (state.worker_task and not state.worker_task.done())
            )
        )

    def should_defer_tts_auto_leave(self, guild_id: int) -> bool:
        state = self._states.get(int(guild_id))
        if state is None:
            return False
        return bool(
            state.music_session_active
            or state.current
            or self._has_pending_track(state)
            or state.current_resolve_task
            or state.current_source
            or state.current_lavalink_player is not None
            or self._is_lavalink_transition_recent(state)
        )

    async def schedule_music_idle_disconnect(self, guild_id: int, *, delay: float | None = None) -> None:
        state = self.get_state(guild_id)
        if state.music_idle_disconnect_task is not None and not state.music_idle_disconnect_task.done():
            return
        delay_seconds = MUSIC_IDLE_DISCONNECT_SECONDS if delay is None else max(0.0, float(delay))

        async def _runner() -> None:
            try:
                await asyncio.sleep(delay_seconds)
                guild = self.bot.get_guild(int(guild_id))
                if guild is not None:
                    await self._maybe_disconnect_idle(guild, self.get_state(guild_id))
            finally:
                st = self.get_state(guild_id)
                if st.music_idle_disconnect_task is task:
                    st.music_idle_disconnect_task = None

        task = asyncio.create_task(_runner())
        task.add_done_callback(_consume_expected_music_exception)
        state.music_idle_disconnect_task = task

    def _cancel_music_idle_disconnect(self, state: MusicGuildState) -> None:
        task = state.music_idle_disconnect_task
        if task is not None and not task.done():
            task.cancel()
        state.music_idle_disconnect_task = None

    async def close(self) -> None:
        for task in list(getattr(self, "_lavalink_shadow_tasks", {}).values()):
            if task is not None and not task.done():
                task.cancel()
        self._lavalink_shadow_tasks.clear()
        for guild_id in list(self._states):
            with contextlib.suppress(Exception):
                await self.stop(guild_id, disconnect=False)
        with contextlib.suppress(Exception):
            await self.backends.close()

    async def backend_status(self, guild_id: int | None = None):
        return await self.backends.status(guild_id=guild_id)

    async def test_lavalink_backend(
        self,
        query: str,
        *,
        requester_id: int = 0,
        requester_name: str = "",
        guild_id: int | None = None,
    ):
        return await self.backends.test_lavalink(
            query,
            requester_id=requester_id,
            requester_name=requester_name,
            guild_id=guild_id,
        )

    async def update_lavalink_node_config(
        self,
        *,
        node_name: str,
        host: str,
        port: int,
        password: str | None,
        secure: bool,
        guild_id: int | None = None,
    ):
        return await self.backends.update_lavalink_node(
            node_name=node_name,
            host=host,
            port=port,
            password=password,
            secure=secure,
            guild_id=guild_id,
        )

    async def set_lavalink_mode(self, mode: str, *, guild_id: int | None = None):
        return await self.backends.set_lavalink_mode(mode, guild_id=guild_id)

    async def clear_lavalink_config(self, *, guild_id: int | None = None):
        return await self.backends.clear_lavalink_config(guild_id=guild_id)

    async def update_lavalink_panel_options(self, **options):
        return await self.backends.update_lavalink_panel_options(**options)

    def lavalink_config_summary(self, guild_id: int | None = None) -> dict:
        return self.backends.lavalink_config_summary(guild_id=guild_id)

    def backend_runtime_summary(self, guild_id: int | None = None) -> dict:
        return self.backends.compact_runtime_summary(guild_id=guild_id)

    def last_lavalink_shadow_result(self, guild_id: int | None = None):
        return self.backends.last_lavalink_shadow_result(guild_id=guild_id)

    def should_run_lavalink_shadow(self, guild_id: int | None = None) -> bool:
        return self.backends.should_shadow_lavalink(guild_id=guild_id)

    def schedule_lavalink_shadow_search(
        self,
        guild_id: int,
        query: str,
        *,
        requester_id: int = 0,
        requester_name: str = "",
        reason: str = "play",
    ) -> bool:
        query = str(query or "").strip()
        if not query or not self.should_run_lavalink_shadow(guild_id):
            return False
        guild_key = int(guild_id)
        current = self._lavalink_shadow_tasks.get(guild_key)
        if current is not None and not current.done():
            logger.debug("[music/lavalink-shadow] já existe teste ativo | guild=%s", guild_id)
            return False

        async def _runner() -> None:
            try:
                await self.backends.shadow_lavalink_search(
                    query,
                    requester_id=requester_id,
                    requester_name=requester_name,
                    guild_id=guild_key,
                    reason=reason,
                )
            finally:
                if self._lavalink_shadow_tasks.get(guild_key) is task:
                    self._lavalink_shadow_tasks.pop(guild_key, None)

        task = asyncio.create_task(_runner())
        task.add_done_callback(_consume_expected_music_exception)
        self._lavalink_shadow_tasks[guild_key] = task
        logger.debug("[music/lavalink-shadow] teste paralelo agendado | guild=%s query=%r", guild_id, query)
        return True

    async def enqueue(self, guild: discord.Guild, voice_channel: discord.abc.Connectable, text_channel: discord.abc.Messageable, tracks: list[MusicTrack]) -> tuple[int, int]:
        if not tracks:
            return 0, 0
        state = self.get_state(guild.id)
        state.last_text_channel_id = getattr(text_channel, "id", None)
        state.last_voice_channel_id = getattr(voice_channel, "id", None)
        state.music_session_active = True
        self._clear_idle_reason(state)
        self._cancel_music_idle_disconnect(state)

        added = 0
        dropped = 0
        seen = self._current_track_keys(state)
        for track in tracks:
            keys = self._track_keys(track)
            if keys and any(key in seen for key in keys):
                dropped += 1
                continue
            if state.queue.full():
                dropped += 1
                continue
            await state.queue.put(track)
            seen.update(keys)
            added += 1
        if added:
            state.stop_requested = False
            self.ensure_music_worker(guild.id)
            if state.current is not None or state.current_source is not None:
                self._start_prefetch_next(guild.id, state)
            self._schedule_panel_update(guild.id, create=True)
        return added, dropped

    def _track_keys(self, track: MusicTrack) -> set[str]:
        keys: set[str] = set()
        url = (track.webpage_url or track.original_url or "").strip().lower()
        if url:
            keys.add("url:" + url)
        title_key = compact_key(track.title)
        if title_key:
            duration_bucket = ""
            if track.duration is not None:
                duration_bucket = str(int(max(0.0, float(track.duration)) // 8))
            keys.add("title:" + title_key + ":" + duration_bucket)
        return keys

    def _current_track_keys(self, state: MusicGuildState) -> set[str]:
        keys: set[str] = set()
        if state.current is not None:
            keys.update(self._track_keys(state.current))
        for item in list(getattr(state, "forward_queue", []) or []):
            keys.update(self._track_keys(item))
        for item in list(getattr(state.queue, "_queue", [])):
            keys.update(self._track_keys(item))
        return keys

    def _pending_items(self, state: MusicGuildState) -> list[MusicTrack]:
        """Retorna próximas músicas na ordem real de avanço.

        ``forward_queue`` guarda músicas que ficaram à frente quando o usuário
        voltou no histórico. Ela precisa ter prioridade sobre o queue normal para
        que o fluxo A → B → voltar A → avançar B funcione mesmo com só 2 músicas.
        """
        items: list[MusicTrack] = []
        with contextlib.suppress(Exception):
            items.extend(list(getattr(state, "forward_queue", []) or []))
        with contextlib.suppress(Exception):
            items.extend(list(getattr(state.queue, "_queue", [])))
        return items

    def _has_pending_track(self, state: MusicGuildState) -> bool:
        return bool(self._pending_items(state))

    async def _get_next_worker_track(self, state: MusicGuildState, *, timeout: float) -> tuple[MusicTrack, bool]:
        """Obtém a próxima música e informa se veio do asyncio.Queue.

        Músicas do ``forward_queue`` não podem chamar ``queue.task_done()``.

        O ``forward_queue`` não acorda tasks bloqueadas em ``asyncio.Queue.get``.
        Isso quebrava o botão ⏮️ quando o player já estava em "Nada tocando":
        a música anterior era colocada na frente, o painel mostrava "Queue
        pronto", mas o worker seguia dormindo no queue normal. Por isso fazemos
        pequenas esperas periódicas, preservando o timeout total de idle.
        """
        deadline = time.monotonic() + max(0.0, float(timeout))
        poll_interval = 0.35
        while True:
            if getattr(state, "forward_queue", None):
                try:
                    return state.forward_queue.popleft(), False
                except IndexError:
                    pass
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise asyncio.TimeoutError
            try:
                return await asyncio.wait_for(state.queue.get(), timeout=min(poll_interval, remaining)), True
            except asyncio.TimeoutError:
                continue

    def _panel_key_for_track(self, track: MusicTrack | None) -> str | None:
        if track is None:
            return None
        url = (track.display_url or track.webpage_url or track.original_url or "").strip().lower()
        if url:
            return "url:" + url
        title_key = compact_key(track.title)
        duration = ""
        if track.duration is not None:
            with contextlib.suppress(Exception):
                duration = str(int(max(0.0, float(track.duration))))
        return f"title:{title_key}:{duration}" if title_key else None

    def _track_resolve_key(self, track: MusicTrack | None, audio_max_abr: int | None = None) -> str:
        if track is None:
            return ""
        url = (track.webpage_url or track.original_url or track.stream_url or "").strip().lower()
        base = "url:" + url if url else ""
        if not base:
            title_key = compact_key(track.title)
            duration = ""
            if track.duration is not None:
                with contextlib.suppress(Exception):
                    duration = str(int(max(0.0, float(track.duration))))
            base = f"title:{title_key}:{duration}" if title_key else ""
        if not base:
            return ""
        try:
            max_abr = int(audio_max_abr or 0)
        except Exception:
            max_abr = 0
        return f"{base}|abr:{max_abr}"

    def _cancel_next_prefetch(self, state: MusicGuildState) -> None:
        task = state.next_resolve_task
        if task is not None and not task.done():
            task.cancel()
        state.next_resolve_task = None
        state.next_resolve_key = ""
        state.next_resolve_active_key = ""

    def _active_player_count(self) -> int:
        total = 0
        for st in self._states.values():
            if st.current or st.current_source or st.current_status in {"resolving", "starting", "playing", "paused"}:
                total += 1
        return total

    def _audio_max_abr_for_load(self) -> int | None:
        """Escolhe qualidade por carga sem aumentar RAM.

        Com um único servidor ativo, retorna None para usar o melhor áudio-only
        disponível, sem teto de abr. Quando há mais guilds tocando, limita
        bitrate para reduzir trabalho do yt-dlp/FFmpeg/rede.
        """
        mode = MUSIC_AUDIO_MODE
        active = max(1, self._active_player_count())
        if mode in {"high", "alta", "quality"}:
            return None
        if mode in {"low", "economy", "economico", "econômico", "stable"}:
            return MUSIC_MAX_AUDIO_BITRATE_STABLE
        if active <= MUSIC_HIGH_QUALITY_MAX_ACTIVE_GUILDS:
            return None
        if active >= 3:
            return MUSIC_HEAVY_LOAD_MAX_ABR
        return MUSIC_MAX_AUDIO_BITRATE_STABLE

    def _track_stream_matches_quality(self, track: MusicTrack | None, audio_max_abr: int | None) -> bool:
        if track is None or not track.stream_url:
            return False
        try:
            requested_abr = int(audio_max_abr or 0)
        except Exception:
            requested_abr = 0
        resolved_abr = int(getattr(track, "resolved_audio_max_abr", 0) or 0)
        is_direct_track = str(getattr(track, "extractor", "") or "").lower() == "direct"
        return bool(is_direct_track or (not requested_abr and not resolved_abr) or (requested_abr and resolved_abr == requested_abr))

    def _prefetch_delay_for_current(self, state: MusicGuildState) -> float:
        delay = float(MUSIC_PREFETCH_MIN_DELAY_SECONDS)
        current = state.current
        if current is None or current.is_live or current.duration is None:
            return delay
        started_at = float(getattr(state, "current_started_at_monotonic", 0.0) or 0.0)
        elapsed = max(0.0, time.monotonic() - started_at) if started_at else 0.0
        remaining = max(0.0, float(current.duration) - elapsed)
        if remaining <= MUSIC_PREFETCH_BEFORE_END_SECONDS:
            return delay
        return max(delay, remaining - MUSIC_PREFETCH_BEFORE_END_SECONDS)

    def _start_prefetch_next(self, guild_id: int, state: MusicGuildState) -> None:
        with contextlib.suppress(Exception):
            if self.backends.should_use_lavalink_real(guild_id):
                # Em modo node de áudio real, a próxima faixa deve ser resolvida
                # pelo node no momento do playback. Prefetch local chamaria yt-dlp e
                # poderia bloquear o event loop/voice heartbeat.
                return
        if not MUSIC_PREFETCH_NEXT or not self._has_pending_track(state):
            return
        if MUSIC_MAX_GLOBAL_PREFETCH <= 0:
            return
        if MUSIC_DISABLE_PREFETCH_ABOVE_PLAYERS and self._active_player_count() > MUSIC_DISABLE_PREFETCH_ABOVE_PLAYERS:
            return
        if self._global_prefetch_active >= MUSIC_MAX_GLOBAL_PREFETCH:
            return
        try:
            next_track = self._pending_items(state)[0]
        except Exception:
            return
        target_abr = self._audio_max_abr_for_load()
        if next_track is None or self._track_stream_matches_quality(next_track, target_abr):
            return
        key = self._track_resolve_key(next_track, target_abr)
        task = state.next_resolve_task
        if task is not None and not task.done() and state.next_resolve_key == key:
            return
        if task is not None and not task.done():
            task.cancel()

        async def _prefetch() -> None:
            counted_active = False
            try:
                # Baixa prioridade: dá tempo ao player atual e evita resolver a próxima
                # música logo no início da faixa, que era quando mais causava cortes.
                await asyncio.sleep(self._prefetch_delay_for_current(state))
                if state.next_resolve_key != key:
                    return
                if MUSIC_DISABLE_PREFETCH_ABOVE_PLAYERS and self._active_player_count() > MUSIC_DISABLE_PREFETCH_ABOVE_PLAYERS:
                    return
                if self._global_prefetch_active >= MUSIC_MAX_GLOBAL_PREFETCH:
                    return
                self._global_prefetch_active += 1
                counted_active = True
                state.next_resolve_active_key = key
                await self.extractor.resolve_stream(next_track, force=False, audio_max_abr=target_abr)
                logger.debug("[music] próxima música pré-resolvida | guild=%s track=%r", guild_id, next_track.title)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.debug("[music] pré-resolução da próxima música falhou | guild=%s track=%r", guild_id, getattr(next_track, "title", ""), exc_info=True)
            finally:
                if state.next_resolve_active_key == key:
                    state.next_resolve_active_key = ""
                if counted_active:
                    self._global_prefetch_active = max(0, self._global_prefetch_active - 1)

        state.next_resolve_key = key
        state.next_resolve_task = asyncio.create_task(_prefetch())
        state.next_resolve_task.add_done_callback(_consume_expected_music_exception)

    async def _resolve_current_track(self, state: MusicGuildState, track: MusicTrack) -> None:
        target_abr = self._audio_max_abr_for_load()
        key = self._track_resolve_key(track, target_abr)
        task = state.next_resolve_task if state.next_resolve_key == key else None
        if task is not None:
            state.next_resolve_task = None
            state.next_resolve_key = ""
            if task.done():
                # Propaga exceção caso a pré-resolução tenha falhado; o fallback abaixo tenta de novo.
                with contextlib.suppress(Exception):
                    task.result()
                if track.stream_url:
                    self._refresh_quality_state(state, track, cap=target_abr)
                    return
            else:
                if state.next_resolve_active_key != key:
                    # A pré-resolução ainda estava apenas aguardando o momento certo.
                    # Quando a música virou atual por fim/skip, cancela o atraso e
                    # resolve imediatamente para não criar pausa gigante entre faixas.
                    task.cancel()
                    state.next_resolve_key = ""
                else:
                    state.current_resolve_task = task
                    try:
                        await task
                        if track.stream_url:
                            self._refresh_quality_state(state, track, cap=target_abr)
                            return
                    finally:
                        if state.current_resolve_task is task:
                            state.current_resolve_task = None
        resolve_task = asyncio.create_task(self.extractor.resolve_stream(track, force=False, audio_max_abr=target_abr))
        resolve_task.add_done_callback(_consume_expected_music_exception)
        state.current_resolve_task = resolve_task
        try:
            await resolve_task
            self._refresh_quality_state(state, track, cap=target_abr)
        finally:
            if state.current_resolve_task is resolve_task:
                state.current_resolve_task = None

    def ensure_music_worker(self, guild_id: int) -> None:
        state = self.get_state(guild_id)
        if state.worker_task is None or state.worker_task.done():
            state.stop_requested = False
            state.worker_task = asyncio.create_task(self._music_worker_loop(int(guild_id)))
            state.worker_task.add_done_callback(_consume_expected_music_exception)

    async def _music_worker_loop(self, guild_id: int) -> None:
        state = self.get_state(guild_id)
        try:
            while not state.stop_requested:
                guild = self.bot.get_guild(guild_id)
                if guild is None:
                    return

                track_from_queue = False
                try:
                    track, track_from_queue = await self._get_next_worker_track(
                        state,
                        timeout=MUSIC_IDLE_DISCONNECT_SECONDS,
                    )
                except asyncio.TimeoutError:
                    await self._maybe_disconnect_idle(guild, state)
                    return

                # Assim que o worker retira a próxima música do queue, ela já vira
                # a música atual do estado. Isso evita uma janela visual durante skip
                # onde o painel via queue vazio + current vazio e mostrava
                # "Nada tocando agora" antes da próxima faixa começar.
                state.current = track
                state.current_status = "resolving"
                state.current_backend = "local"
                state.skip_transition_active = False

                played_ok = False
                state.skip_requested = False
                state.control_votes.clear()
                for task in list(state.control_vote_cleanup_tasks.values()):
                    if task is not None and not task.done():
                        task.cancel()
                state.control_vote_cleanup_tasks.clear()
                try:
                    played_ok = await self._play_track(guild, state, track)
                except Exception as exc:
                    if not state.skip_requested and not state.stop_requested:
                        logger.warning("[music] Falha ao tocar | guild=%s track=%r erro=%s", guild_id, track.title, exc)
                        await self._send_text(guild, state, f"⚠️ Não consegui iniciar **{track.short_title}**. Se houver outra música no queue, vou tentar a próxima.")
                finally:
                    state.current_source = None
                    state.current_resolve_task = None
                    started_before_finish = bool(float(getattr(state, "current_started_at_monotonic", 0.0) or 0.0) > 0.0)
                    should_history_natural = bool(played_ok and not state.stop_requested and not state.skip_requested)
                    should_history_skip = bool(
                        state.skip_requested
                        and not state.stop_requested
                        and started_before_finish
                        and not state.skip_history_suppressed_once
                    )
                    if should_history_natural or should_history_skip:
                        self._push_history(state, track)
                    if played_ok and state.loop_mode is LoopMode.ONE and not state.stop_requested and not state.skip_requested:
                        with contextlib.suppress(Exception):
                            # Repetir a atual antes de qualquer item já presente na fila.
                            state.queue._queue.appendleft(track)
                    elif played_ok and state.loop_mode is LoopMode.ALL and not state.stop_requested and not state.skip_requested:
                        with contextlib.suppress(Exception):
                            await state.queue.put(track)
                    skip_to_next = bool(state.skip_requested and not state.stop_requested and self._has_pending_track(state))
                    state.current = None
                    state.current_started_at_monotonic = 0.0
                    state.current_backend = "local"
                    state.current_lavalink_player = None
                    state.current_lavalink_playable = None
                    state.paused = False
                    state.skip_history_suppressed_once = False
                    state.skip_transition_active = skip_to_next
                    state.current_status = "skipping" if skip_to_next else ("idle" if not self._has_pending_track(state) else "queued")
                    if track_from_queue:
                        with contextlib.suppress(Exception):
                            state.queue.task_done()
                    # Durante um skip com próxima música no queue, não renderiza um
                    # estado intermediário vazio/queued. A próxima iteração já vai
                    # definir current e redesenhar o painel como preparando/tocando.
                    if not skip_to_next:
                        await self.update_panel(guild_id, create=bool(state.now_message))
        finally:
            state.worker_task = None
            if state.stop_requested or not self._has_pending_track(state):
                if not state.stop_requested and not self._has_pending_track(state):
                    self._set_idle_reason(state, "queue_finished")
                state.current = None
                state.current_started_at_monotonic = 0.0
                state.current_backend = "local"
                state.current_lavalink_player = None
                state.current_lavalink_playable = None
                state.skip_transition_active = False
                state.skip_history_suppressed_once = False
                state.current_status = "idle"
                guild = self.bot.get_guild(int(guild_id))
                restore_reason = "queue_finished" if not self._has_pending_track(state) else "stop_requested"
                if state.stop_requested and str(getattr(state, "idle_reason", "") or "") == "manual_stop":
                    restore_reason = "manual_stop"
                elif state.stop_requested:
                    restore_reason = "stop_requested"
                await self._restore_auto_bitrate_for_state(guild, state, reason=restore_reason)
                await self._restore_voice_status_for_state(guild, state, reason=restore_reason)
                self._schedule_panel_update(guild_id, create=False)

    def _track_is_direct_youtube_request(self, track: MusicTrack) -> bool:
        """Link direto do YouTube sempre toca pelo yt-dlp local.

        Resultados de pesquisa do YouTube têm webpage_url do YouTube, mas o
        original_url costuma ser o texto pesquisado; esses ainda podem tentar
        mirror LavaSrc antes do fallback local.
        """
        try:
            original = describe_url(str(getattr(track, "original_url", "") or ""))
            if original.is_youtube:
                return True
        except Exception:
            return False
        return False

    def _track_should_preserve_official_display(self, track: MusicTrack, meta: dict | None = None) -> bool:
        values = " ".join(
            str(value or "").lower()
            for value in (
                getattr(track, "display_source", ""),
                getattr(track, "source", ""),
                getattr(track, "extractor", ""),
                getattr(track, "original_url", ""),
                getattr(track, "webpage_url", ""),
                (meta or {}).get("query", "") if isinstance(meta, dict) else "",
            )
        )
        return bool(
            getattr(track, "display_title", "")
            or "spotify" in values
            or "deezer" in values
            or "apple" in values
            or "metadata" in values
        )

    def _prepare_track_for_local_after_lavalink_failure(self, track: MusicTrack, exc: Exception | None = None) -> bool:
        """Converte mirrors quebrados do LavaSrc em metadata para fallback local.

        SoundCloud/Spotify podem resolver metadata no Lavalink e quebrar só na hora
        do stream real. Nesses casos não adianta entregar a mesma URL ao yt-dlp: ele
        costuma repetir 404 ou erro de extractor. O fallback local deve usar título,
        artista e duração para buscar uma fonte equivalente pelo player local.
        """
        values = " ".join(
            str(value or "").lower()
            for value in (
                getattr(track, "source", ""),
                getattr(track, "extractor", ""),
                getattr(track, "webpage_url", ""),
                getattr(track, "original_url", ""),
            )
        )
        # A mensagem de erro do LavaSrc pode citar SoundCloud mesmo quando o track
        # original era um resultado do YouTube. Só converte para metadata se a
        # própria faixa veio de SoundCloud/Spotify; caso contrário, o fallback
        # local deve tocar a URL original do YouTube selecionada pelo usuário.
        if not any(token in values for token in ("soundcloud", "spotify", "spsearch", "scsearch", "lavasrc")):
            return False
        title = str(getattr(track, "title", "") or "").strip()
        generic_titles = {"", "link", "soundcloud link", "spotify link", "música sem título", "musica sem titulo", "unknown title"}
        if title.lower() in generic_titles:
            return False
        track.stream_url = ""
        track.webpage_url = ""
        track.extractor = "metadata"
        track.lavalink_playable = None
        track.lavalink_encoded = ""
        track.lavalink_resolved = False
        track.resolved_at_monotonic = 0.0
        track.resolved_audio_max_abr = 0
        track.resolved_audio_abr = 0
        track.resolved_audio_ext = ""
        track.resolved_audio_codec = ""
        if "spotify" in values:
            track.fallback_reason = "Spotify"
            if "fallback local" not in str(track.source or "").lower():
                track.source = "Spotify → fallback local"
        elif "soundcloud" in values or "scsearch" in values:
            track.fallback_reason = "SoundCloud"
            if "fallback local" not in str(track.source or "").lower():
                track.source = "SoundCloud → fallback local"
        else:
            track.fallback_reason = "Lavalink"
        return True

    async def _disconnect_lavalink_before_local_fallback(self, guild: discord.Guild, state: MusicGuildState, *, reason: str = "fallback") -> None:
        current_vc = guild.voice_client
        if not self._is_lavalink_voice_client(current_vc):
            return
        self._mark_lavalink_transition(state, seconds=10.0)
        self._mark_internal_voice_disconnect(guild.id, seconds=10.0)
        with contextlib.suppress(Exception):
            await self._vc_stop_audio(current_vc)
        with contextlib.suppress(Exception):
            stopper = getattr(current_vc, "stop", None)
            if callable(stopper):
                result = stopper()
                if asyncio.iscoroutine(result):
                    await result
        with contextlib.suppress(Exception):
            await current_vc.disconnect(force=True)
        # Dá um pequeno tempo para o gateway liberar a sessão de voz antes do
        # VoiceClient local tentar entrar, evitando Already connected/timeout.
        await asyncio.sleep(0.8)

    async def _play_track_lavalink(
        self,
        guild: discord.Guild,
        state: MusicGuildState,
        track: MusicTrack,
        channel: discord.abc.Connectable,
    ) -> bool:
        """Toca uma faixa pelo Lavalink real.

        A queue, painel e status continuam no AudioRouter. Em modo Lavalink o
        erro é tratado sem cair para voice local; em modo Auto, o caller ainda
        pode usar fallback local.
        """
        state.current_backend = "lavalink"
        state.current_source = None
        state.current_lavalink_player = None
        state.current_lavalink_playable = None
        state.last_lavalink_error = ""
        state.current_status = "starting"
        self._mark_lavalink_transition(state, seconds=18.0)
        self._mark_internal_voice_disconnect(guild.id, seconds=18.0)
        await self._boost_auto_bitrate_for_music(guild, channel, state)
        await self.update_panel(guild.id, create=True)

        player, playable, meta = await self.backends.play_lavalink_track(
            guild,
            channel,
            track,
            volume=state.volume,
        )
        state.current_lavalink_player = player
        state.current_lavalink_playable = playable
        self._mark_lavalink_transition(state, seconds=8.0)
        self._mark_internal_voice_disconnect(guild.id, seconds=8.0)
        # Usa metadados reais do node quando eles representam a fonte escolhida.
        # Para Spotify/Deezer/Apple resolvidos por mirror LavaSrc/SoundCloud,
        # preserve a metadata oficial no painel e use o mirror só como áudio.
        if meta:
            preserve_display = self._track_should_preserve_official_display(track, meta)
            raw_duration = meta.get("duration")
            with contextlib.suppress(Exception):
                numeric_duration = float(raw_duration)
                if numeric_duration > 0:
                    # Wavelink geralmente informa duração em ms; MusicTrack
                    # usa segundos. Atualizar isso impede painel final com
                    # "desconhecida" depois que o node já resolveu a faixa.
                    track.duration = numeric_duration / 1000.0 if numeric_duration >= 10000 else numeric_duration
            if preserve_display:
                track.title = str(getattr(track, "display_title", "") or track.title or meta.get("title") or "Música sem título")
                track.uploader = str(getattr(track, "display_uploader", "") or track.uploader or "")
                official_thumb = str(getattr(track, "display_thumbnail", "") or "")
                if official_thumb:
                    track.thumbnail = official_thumb
                # Mantém rastreável que o áudio é via Lavalink sem trocar artista/título.
                if not str(track.source or "").strip():
                    track.source = str(getattr(track, "display_source", "") or "metadata")
            else:
                track.title = str(meta.get("title") or track.title or "Música sem título")
                track.uploader = str(meta.get("author") or track.uploader or "")
                track.source = str(meta.get("source") or track.source or "lavalink")
                artwork = str(meta.get("artwork") or "")
                if artwork:
                    track.thumbnail = artwork
        state.current_status = "playing"
        state.current_started_at_monotonic = time.monotonic()
        state.current_quality_label = "Alta"
        state.current_quality_kbps = MUSIC_HIGH_QUALITY_MAX_ABR
        self._schedule_voice_status_track_sync(guild.id, repeat_after=1.0, reason="lavalink_track_started")
        self._schedule_panel_update(guild.id, create=True)

        # Polling simples e seguro enquanto o backend real ainda não usa eventos
        # Wavelink dedicados no Cog. Em Wavelink 3, ``playing`` pode oscilar logo
        # após play/skip, então usamos ``current`` + uma pequena janela de graça
        # para não concluir a faixa cedo e avançar várias músicas/status em loop.
        started_at = time.monotonic()
        last_explicit_active_at = started_at
        while not state.stop_requested and not state.skip_requested:
            if not self._vc_is_connected(player):
                raise MusicPlaybackError("Player Lavalink desconectou durante a música.")
            now = time.monotonic()
            explicit_active = self._vc_lavalink_explicit_playing_or_paused(player)
            has_current = getattr(player, "current", None) is not None
            if explicit_active:
                last_explicit_active_at = now
                await asyncio.sleep(0.35)
                continue
            if self._lavalink_tts_active(state) or self._lavalink_resume_grace_active(state):
                # Durante TTS via Lavalink, o player troca temporariamente a faixa
                # e ``playing`` pode cair falso na janela de restauração. Não trate
                # isso como fim natural da música.
                await asyncio.sleep(0.20)
                continue
            if has_current and (now - started_at < 4.0 or now - last_explicit_active_at < 3.0):
                await asyncio.sleep(0.20)
                continue
            break
        return not state.stop_requested and not state.skip_requested

    async def _play_track(self, guild: discord.Guild, state: MusicGuildState, track: MusicTrack) -> bool:
        if not state.last_voice_channel_id:
            raise RuntimeError("Canal de voz não definido.")
        channel = guild.get_channel(state.last_voice_channel_id) or self.bot.get_channel(state.last_voice_channel_id)
        if channel is None or not hasattr(channel, "connect"):
            raise RuntimeError("Canal de voz não encontrado.")

        state.current = track
        state.music_session_active = True
        self._clear_idle_reason(state)
        self._cancel_music_idle_disconnect(state)
        state.current_status = "resolving"
        state.paused = False
        state.current_backend = "local"
        state.current_lavalink_player = None
        state.current_lavalink_playable = None
        # Cada música nova ganha um painel novo no fim do chat. Alterações da
        # mesma música continuam editando esse painel.
        await self.update_panel(guild.id, create=True, repost=True)

        use_lavalink_real = bool(self.backends.should_use_lavalink_real(guild.id))
        if use_lavalink_real and self._track_is_direct_youtube_request(track):
            logger.info(
                "[music] YouTube direto tentando mirror LavaSrc antes do yt-dlp local | guild=%s track=%r",
                guild.id,
                getattr(track, "title", ""),
            )

        if use_lavalink_real:
            try:
                return await self._play_track_lavalink(guild, state, track, channel)
            except Exception as exc:
                allow_local_fallback = False
                fallback_getter = getattr(self.backends, "should_lavalink_fallback_to_local", None)
                if callable(fallback_getter):
                    with contextlib.suppress(Exception):
                        allow_local_fallback = bool(fallback_getter(guild.id))
                state.last_lavalink_error = f"{exc.__class__.__name__}: {exc}"
                if not allow_local_fallback:
                    # Modo Lavalink é lavalink-only. Não misture com voice client local
                    # no mesmo fluxo, porque isso causa handshake/local fallback e painel
                    # falso de external_disconnect.
                    logger.warning(
                        "[music/lavalink] playback real falhou em modo lavalink-only | guild=%s track=%r erro=%s",
                        guild.id,
                        getattr(track, "title", ""),
                        exc,
                    )
                    self._mark_lavalink_transition(state, seconds=10.0)
                    self._mark_internal_voice_disconnect(guild.id, seconds=10.0)
                    state.current_status = "queued" if self._has_pending_track(state) else "idle"
                    await self._send_text(
                        guild,
                        state,
                        "⚠️ Lavalink não conseguiu iniciar essa música agora. O node pode estar reconectando/sobrecarregado; tente novamente ou troque o node em `_musicnode`.",
                    )
                    raise MusicPlaybackError(f"Lavalink indisponível em modo lavalink-only: {exc}") from exc

                # Modo auto: fallback local permitido, mas limpo e sem marcar como
                # desconexão externa.
                logger.warning(
                    "[music/lavalink] playback real falhou antes/durante teste | guild=%s track=%r erro=%s; fallback local(auto)",
                    guild.id,
                    getattr(track, "title", ""),
                    exc,
                )
                state.current_backend = "local"
                state.current_lavalink_player = None
                state.current_lavalink_playable = None
                state.current_status = "resolving"
                converted = self._prepare_track_for_local_after_lavalink_failure(track, exc)
                if converted:
                    logger.info(
                        "[music/lavalink] fallback local por metadata | guild=%s track=%r source=%r",
                        guild.id,
                        getattr(track, "title", ""),
                        getattr(track, "source", ""),
                    )
                await self._disconnect_lavalink_before_local_fallback(guild, state, reason="lavalink_failed")

        vc = await self._ensure_voice(guild, channel, state=state)
        if vc is None:
            raise RuntimeError("Não consegui conectar ao canal de voz.")
        await self._boost_auto_bitrate_for_music(guild, channel, state)

        try:
            await self._resolve_current_track(state, track)
        except asyncio.CancelledError as exc:
            raise MusicPlaybackError("Música pulada antes de iniciar o áudio.") from exc

        if state.skip_requested or state.stop_requested:
            raise MusicPlaybackError("Playback cancelado.")
        if not track.stream_url:
            raise MusicExtractionError("A música não retornou URL de stream.")

        state.current_status = "starting"
        await self.update_panel(guild.id, create=True)

        # Se um TTS direto ainda estiver tocando, espera acabar antes da música entrar.
        for _ in range(60):
            if not self._vc_is_playing_or_paused(vc):
                break
            if state.skip_requested or state.stop_requested:
                raise MusicPlaybackError("Playback cancelado.")
            await asyncio.sleep(0.1)

        loop = asyncio.get_running_loop()
        finished: asyncio.Future | None = None
        mixed_source: MixedAudioSource | None = None
        last_start_error: Exception | None = None
        max_attempts = 1 + max(0, MUSIC_STREAM_START_RETRIES)

        for attempt in range(max_attempts):
            if attempt > 0:
                if state.skip_requested or state.stop_requested:
                    raise MusicPlaybackError("Playback cancelado.")
                # URL de stream pode expirar/403 antes do FFmpeg produzir áudio.
                # Força uma nova resolução uma única vez, sem loop infinito.
                track.stream_url = ""
                track.resolved_at_monotonic = 0.0
                track.resolved_audio_max_abr = 0
                track.resolved_audio_abr = 0
                track.resolved_audio_ext = ""
                track.resolved_audio_codec = ""
                retry_cap = self._audio_max_abr_for_load()
                await self.extractor.resolve_stream(track, force=True, audio_max_abr=retry_cap)
                self._refresh_quality_state(state, track, cap=retry_cap)
                if not track.stream_url:
                    raise MusicExtractionError("A música não retornou URL de stream.")

            finished = loop.create_future()
            ffmpeg_options, source_base_volume = _ffmpeg_options_with_base_volume(MUSIC_FFMPEG_OPTIONS, state.volume)
            ffmpeg_source = discord.FFmpegPCMAudio(
                track.stream_url,
                before_options=MUSIC_RECONNECT_BEFORE_OPTIONS,
                options=ffmpeg_options,
            )
            mixed_source = MixedAudioSource(
                loop=loop,
                music_source=ffmpeg_source,
                music_volume=state.volume,
                source_base_volume=source_base_volume,
                duck_factor=MUSIC_TTS_LOCAL_DUCK_FACTOR,
            )
            state.current_source = mixed_source
            mixed_source.music_started_future.add_done_callback(_consume_expected_music_exception)

            def _after(error: Exception | None, finished_ref: asyncio.Future = finished) -> None:
                if error:
                    logger.warning("[music] after playback error | guild=%s erro=%s", guild.id, error)

                def _finish_once() -> None:
                    if not finished_ref.done():
                        finished_ref.set_result(None)

                loop.call_soon_threadsafe(_finish_once)

            async with state.voice_lock:
                if self._vc_is_playing_or_paused(vc):
                    with contextlib.suppress(Exception):
                        vc.stop()
                vc.play(mixed_source, after=_after)

            done, _pending = await asyncio.wait(
                {mixed_source.music_started_future, finished},
                timeout=MUSIC_PLAYBACK_START_TIMEOUT_SECONDS,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if mixed_source.music_started_future in done:
                try:
                    mixed_source.music_started_future.result()
                    last_start_error = None
                    break
                except Exception as exc:
                    last_start_error = exc
            elif finished in done:
                last_start_error = MusicPlaybackError("FFmpeg finalizou antes de iniciar o áudio. A URL recebida provavelmente não era um stream tocável.")
            else:
                last_start_error = MusicPlaybackError("FFmpeg demorou demais para iniciar o áudio.")

            with contextlib.suppress(Exception):
                if self._vc_is_playing_or_paused(vc):
                    vc.stop()
                mixed_source.cleanup()
            state.current_source = None
            if attempt + 1 >= max_attempts:
                raise last_start_error

        if mixed_source is None or finished is None:
            raise last_start_error or MusicPlaybackError("Não consegui iniciar o áudio.")

        state.current_status = "playing"
        state.current_started_at_monotonic = time.monotonic()
        self._refresh_quality_state(state, track)
        self._schedule_voice_status_track_sync(guild.id, repeat_after=1.0, reason="playback_started")
        self._schedule_panel_update(guild.id, create=True)
        self._start_prefetch_next(guild.id, state)
        await finished
        return not state.skip_requested and not state.stop_requested

    async def _ensure_voice(self, guild: discord.Guild, channel: discord.abc.Connectable, *, state: MusicGuildState | None = None) -> Optional[discord.VoiceClient]:
        vc = guild.voice_client
        if vc and self._is_lavalink_voice_client(vc):
            # O backend local não consegue usar o VoiceProtocol do Wavelink.
            # Desconecta apenas quando não está tocando para evitar leave/join em loop.
            if self._vc_is_playing_or_paused(vc):
                raise RuntimeError("Player Lavalink ainda está ativo; não vou roubar a conexão local.")
            with contextlib.suppress(Exception):
                self._mark_internal_voice_disconnect(guild.id, seconds=8.0)
                await vc.disconnect(force=False)
            vc = None
        if vc and self._vc_is_connected(vc):
            if getattr(getattr(vc, "channel", None), "id", None) != getattr(channel, "id", None):
                try:
                    self._mark_internal_voice_disconnect(guild.id, seconds=8.0)
                    await vc.move_to(channel)
                except Exception:
                    self._mark_internal_voice_disconnect(guild.id, seconds=8.0)
                    await vc.disconnect(force=True)
                    vc = None
            if vc and self._vc_is_connected(vc):
                with contextlib.suppress(Exception):
                    await guild.change_voice_state(channel=channel, self_deaf=True)
                if state is not None:
                    state.music_owns_voice = False
                return vc
        try:
            connected = await channel.connect(self_deaf=True)
            if state is not None:
                state.music_owns_voice = True
            return connected
        except Exception as exc:
            logger.warning("[music] falha ao conectar | guild=%s channel=%s erro=%s", guild.id, getattr(channel, "id", None), exc)
            return guild.voice_client if guild.voice_client and self._vc_is_connected(guild.voice_client) else None

    async def _maybe_disconnect_idle(self, guild: discord.Guild, state: MusicGuildState) -> None:
        # Sem modo 24/7 para música: quando a música/fila acabam e o bot fica
        # sozinho ou só com bots, ele sai depois do timeout de música. Se ainda
        # há humanos, a conexão pode continuar para o TTS usar.
        state.current_status = "idle"
        if not state.current and not self._has_pending_track(state) and state.idle_reason == "idle":
            self._set_idle_reason(state, "queue_finished")
        vc = guild.voice_client
        if not vc or not self._vc_is_connected(vc) or getattr(vc, "channel", None) is None:
            state.music_session_active = False
            state.music_owns_voice = False
            await self.update_panel(guild.id, create=False)
            return
        if state.current or self._has_pending_track(state) or state.current_resolve_task or state.current_source:
            return
        # Se algum áudio direto do TTS ainda estiver tocando, não derruba a call.
        if self._vc_is_playing_or_paused(vc):
            await self.schedule_music_idle_disconnect(guild.id, delay=15.0)
            return
        try:
            members = list(getattr(vc.channel, "members", []))
            humans = [m for m in members if not getattr(m, "bot", False)]
            if humans:
                await self.update_panel(guild.id, create=False)
                return
            self._mark_internal_voice_disconnect(guild.id, seconds=8.0)
            await vc.disconnect(force=False)
            state.music_owns_voice = False
            state.music_session_active = False
            await self.update_panel(guild.id, create=False)
        except Exception:
            logger.debug("[music] idle disconnect falhou", exc_info=True)

    def _voice_channel_has_music_state(self, state: MusicGuildState) -> bool:
        return bool(state.music_session_active or state.current or self._has_pending_track(state) or state.current_source or state.current_resolve_task or state.current_lavalink_player)

    async def _find_recent_voice_audit_actor(
        self,
        guild: discord.Guild,
        *,
        disconnected: bool,
        before_channel_id: int | None = None,
        after_channel_id: int | None = None,
    ) -> discord.User | discord.Member | None:
        if guild is None:
            return None
        actions: list[discord.AuditLogAction] = []
        with contextlib.suppress(Exception):
            if disconnected and hasattr(discord.AuditLogAction, "member_disconnect"):
                actions.append(discord.AuditLogAction.member_disconnect)
            if hasattr(discord.AuditLogAction, "member_move"):
                actions.append(discord.AuditLogAction.member_move)
        if not actions:
            return None
        now = datetime.now(timezone.utc)
        for action in actions:
            try:
                async for entry in guild.audit_logs(limit=8, action=action):
                    created_at = getattr(entry, "created_at", None)
                    if created_at is not None:
                        if created_at.tzinfo is None:
                            created_at = created_at.replace(tzinfo=timezone.utc)
                        if abs((now - created_at).total_seconds()) > 12:
                            continue
                    extra = getattr(entry, "extra", None)
                    channel = getattr(extra, "channel", None)
                    channel_id = int(getattr(channel, "id", 0) or 0)
                    if before_channel_id and channel_id and channel_id != int(before_channel_id):
                        # Em member_move, alguns clients registram o canal de origem/destino de forma diferente.
                        if not after_channel_id or channel_id != int(after_channel_id):
                            continue
                    return getattr(entry, "user", None)
            except (discord.Forbidden, discord.HTTPException):
                return None
            except Exception:
                logger.debug("[music] falha ao consultar audit log de voz", exc_info=True)
                return None
        return None

    async def handle_bot_voice_disconnect(
        self,
        guild: discord.Guild,
        before_channel: discord.abc.GuildChannel | None,
        after_channel: discord.abc.GuildChannel | None = None,
    ) -> None:
        if guild is None:
            return
        state = self.get_state(guild.id)
        if self._is_internal_voice_disconnect_recent(state):
            return
        if not self._voice_channel_has_music_state(state):
            return
        # Wavelink pode emitir transições de voice state enquanto o Player ainda
        # está vivo/tocando. Não trate isso como desconexão externa, senão o
        # painel limpa o queue/status mesmo com a música seguindo.
        current_vc = getattr(guild, "voice_client", None)
        lavalink_player = state.current_lavalink_player or current_vc
        if self._is_lavalink_voice_client(lavalink_player) and self._vc_is_connected(lavalink_player):
            logger.debug(
                "[music/lavalink] voice_state disconnect ignorado porque o Player Wavelink ainda está ativo | guild=%s",
                guild.id,
            )
            return
        if (
            self._is_lavalink_strict_mode(guild.id)
            and (state.current_backend == "lavalink" or state.current is not None or self._has_pending_track(state))
            and self._is_lavalink_transition_recent(state)
        ):
            logger.info(
                "[music/lavalink] voice_state disconnect ignorado durante transição Lavalink | guild=%s current=%r pending=%s",
                guild.id,
                getattr(getattr(state, "current", None), "title", None),
                self._has_pending_track(state),
            )
            return

        actor = await self._find_recent_voice_audit_actor(
            guild,
            disconnected=True,
            before_channel_id=int(getattr(before_channel, "id", 0) or 0) or None,
            after_channel_id=int(getattr(after_channel, "id", 0) or 0) or None,
        )
        if state.current_source is not None:
            with contextlib.suppress(Exception):
                state.current_source.cleanup()
        if state.current_resolve_task is not None and not state.current_resolve_task.done():
            state.current_resolve_task.cancel()
        while not state.queue.empty():
            with contextlib.suppress(Exception):
                state.queue.get_nowait()
                state.queue.task_done()
        state.forward_queue.clear()
        state.current = None
        state.current_started_at_monotonic = 0.0
        state.current_source = None
        state.current_resolve_task = None
        state.current_backend = "local"
        state.current_lavalink_player = None
        state.current_lavalink_playable = None
        state.paused = False
        state.stop_requested = True
        state.skip_requested = True
        state.current_status = "idle"
        state.music_session_active = False
        state.music_owns_voice = False
        await self._restore_auto_bitrate_for_state(guild, state, reason="external_disconnect", channel_hint=before_channel)
        await self._restore_voice_status_for_state(guild, state, reason="external_disconnect", channel_hint=before_channel)
        state.control_votes.clear()
        self._cancel_music_idle_disconnect(state)
        self._set_idle_reason(
            state,
            "external_disconnect",
            actor=actor,
            channel_name=getattr(before_channel, "name", "") or "",
        )
        await self.update_panel(guild.id, create=True)

    async def handle_bot_voice_move(
        self,
        guild: discord.Guild,
        before_channel: discord.abc.GuildChannel | None,
        after_channel: discord.abc.GuildChannel | None,
    ) -> None:
        if guild is None or before_channel is None or after_channel is None:
            return
        state = self.get_state(guild.id)
        if self._is_internal_voice_disconnect_recent(state):
            return
        if not self._voice_channel_has_music_state(state):
            return
        current_vc = getattr(guild, "voice_client", None)
        lavalink_player = state.current_lavalink_player or current_vc
        if self._is_lavalink_voice_client(lavalink_player) and self._vc_is_connected(lavalink_player):
            # Se o Wavelink moveu/atualizou voice state internamente, sincroniza o
            # canal conhecido sem marcar external_move nem restaurar status.
            state.last_voice_channel_id = int(getattr(after_channel, "id", 0) or 0) or state.last_voice_channel_id
            logger.debug(
                "[music/lavalink] voice_state move tratado como interno | guild=%s channel=%s",
                guild.id,
                getattr(after_channel, "id", None),
            )
            return
        actor = await self._find_recent_voice_audit_actor(
            guild,
            disconnected=False,
            before_channel_id=int(getattr(before_channel, "id", 0) or 0) or None,
            after_channel_id=int(getattr(after_channel, "id", 0) or 0) or None,
        )
        await self._restore_auto_bitrate_for_state(guild, state, reason="external_move", channel_hint=before_channel)
        await self._restore_voice_status_for_state(guild, state, reason="external_move", channel_hint=before_channel)
        state.last_voice_channel_id = int(getattr(after_channel, "id", 0) or 0) or state.last_voice_channel_id
        await self._boost_auto_bitrate_for_music(guild, after_channel, state)
        if state.current is not None:
            await self._apply_voice_status_for_music(guild, after_channel, state, state.current, force=True)
        self._set_idle_reason(
            state,
            "external_move",
            actor=actor,
            channel_name=getattr(after_channel, "name", "") or "",
        )
        await self._send_text(
            guild,
            state,
            f"`🔀` O player foi movido para **{discord.utils.escape_markdown(getattr(after_channel, 'name', 'outro canal'))}**"
            + (f" por <@{getattr(actor, 'id', 0)}>" if actor else "")
            + ".",
        )
        await self.update_panel(guild.id, create=bool(state.now_message))

    async def _send_text(self, guild: discord.Guild, state: MusicGuildState, content: str) -> None:
        if not state.last_text_channel_id:
            return
        channel = guild.get_channel(state.last_text_channel_id) or self.bot.get_channel(state.last_text_channel_id)
        if channel is None:
            return
        with contextlib.suppress(Exception):
            await channel.send(content, silent=True)

    def _schedule_panel_update(self, guild_id: int, *, create: bool = True) -> None:
        state = self.get_state(guild_id)
        state.panel_update_create = bool(state.panel_update_create or create)
        if state.panel_update_task is not None and not state.panel_update_task.done():
            return

        async def _runner() -> None:
            try:
                await asyncio.sleep(MUSIC_PANEL_UPDATE_THROTTLE_SECONDS)
                st = self.get_state(guild_id)
                create_flag = bool(st.panel_update_create)
                st.panel_update_create = False
                await self.update_panel(guild_id, create=create_flag)
            finally:
                st = self.get_state(guild_id)
                if st.panel_update_task is task:
                    st.panel_update_task = None

        try:
            task = asyncio.create_task(_runner())
            task.add_done_callback(_consume_expected_music_exception)
            state.panel_update_task = task
        except RuntimeError:
            pass

    async def update_panel(self, guild_id: int, *, create: bool = True, repost: bool = False) -> None:
        state = self.get_state(guild_id)
        if not state.last_text_channel_id:
            return
        guild = self.bot.get_guild(int(guild_id))
        channel = None
        if guild is not None:
            channel = guild.get_channel(state.last_text_channel_id)
        channel = channel or self.bot.get_channel(state.last_text_channel_id)
        if channel is None:
            return
        try:
            from .ui import build_player_embeds, MusicPlayerView

            has_player_content = bool(state.current or self._has_pending_track(state))
            state.panel_vote_summary = self.pending_vote_summary(guild_id)
            embeds = build_player_embeds(state)
            # O painel mantém a mesma estrutura de controles mesmo quando a música acaba,
            # é parada ou o bot é desconectado. Os botões ficam visíveis e a view decide
            # quais ações ainda fazem sentido.
            view = MusicPlayerView(self, guild_id)
            current_panel_key = self._panel_key_for_track(state.current)

            async with state.panel_lock:
                should_repost = bool(repost and has_player_content)
                if should_repost and state.now_message is not None:
                    old_message = state.now_message
                    state.now_message = None
                    with contextlib.suppress(Exception):
                        await old_message.delete()
                    # Se não deu para apagar, pelo menos tenta matar os componentes
                    # antigos para evitar dois painéis controlando o player.
                    with contextlib.suppress(Exception):
                        await old_message.edit(view=None)

                if state.now_message is not None and not should_repost:
                    try:
                        await state.now_message.edit(content=None, embeds=embeds, view=view)
                        state.panel_track_key = current_panel_key
                        return
                    except Exception:
                        state.now_message = None
                        state.panel_track_key = None

                if create:
                    state.now_message = await channel.send(embeds=embeds, view=view, silent=True)
                    state.panel_track_key = current_panel_key
        except Exception:
            logger.debug("[music] falha ao atualizar painel", exc_info=True)

    async def _announce_now_playing(self, guild: discord.Guild, state: MusicGuildState, track: MusicTrack) -> None:
        await self.update_panel(guild.id, create=True)


    def _lavalink_tts_candidates_for_path(self, path: str, *, timeout: float = 120.0) -> list[str]:
        candidates: list[str] = []
        public_base = str(getattr(config, "MUSIC_TTS_PUBLIC_BASE_URL", "") or "").strip().rstrip("/")
        if public_base:
            try:
                from webserver import register_tts_audio_file

                token = register_tts_audio_file(
                    path,
                    ttl_seconds=max(float(timeout or 120.0) + 90.0, float(getattr(config, "MUSIC_LAVALINK_TTS_URL_TTL_SECONDS", 240))),
                )
                if token:
                    candidates.append(f"{public_base}/tts-audio/{token}.mp3")
            except Exception:
                logger.debug("[music/lavalink] falha ao registrar URL temporária do TTS", exc_info=True)

        if bool(getattr(config, "MUSIC_LAVALINK_TTS_FILE_FALLBACK", False)):
            with contextlib.suppress(Exception):
                abs_path = os.path.abspath(str(path or ""))
                candidates.append("file://" + quote(abs_path))
                candidates.append(abs_path)
        return [candidate for candidate in candidates if str(candidate or "").strip()]

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
    ) -> dict[str, Any]:
        """Toca TTS integrado ao player de música mantendo o volume normal da música."""
        loop = asyncio.get_running_loop()
        playback_started_at = time.monotonic()
        source_setup_started_at = time.monotonic()
        play_call_ms = 0.0

        guild_id = getattr(guild, "id", None) or getattr(getattr(vc, "guild", None), "id", None)
        state = self.get_state(int(guild_id)) if guild_id is not None else None
        if state is not None:
            state.tts_voice_touched = True
            state.last_tts_activity_at = time.monotonic()
        active_source = state.current_source if state is not None else None

        if state is not None and guild is not None and (state.current_backend == "lavalink" or self._is_lavalink_voice_client(vc)):
            # O arquivo já foi gerado pelo TTS antes de chegar aqui. Só agora o
            # Lavalink resolve uma fonte tocável e substitui temporariamente a
            # música; se qualquer etapa falhar, a faixa atual continua/resume.
            candidates = self._lavalink_tts_candidates_for_path(path, timeout=timeout)
            source_setup_ms = max(0.0, (time.monotonic() - source_setup_started_at) * 1000.0)
            if not candidates:
                logger.warning(
                    "[music/lavalink] TTS via Lavalink sem URL pública configurada | guild=%s",
                    guild_id,
                )
                return {
                    "source_setup_ms": source_setup_ms,
                    "play_call_ms": 0.0,
                    "playback_ms": max(0.0, (time.monotonic() - playback_started_at) * 1000.0),
                    "playback_started_at": playback_started_at,
                    "tts_lavalink_failed": True,
                    "tts_lavalink_missing_source": True,
                }
            tts_window = max(8.0, float(timeout or 120.0) + 6.0)
            try:
                async with state.voice_lock:
                    state.lavalink_tts_until = max(float(getattr(state, "lavalink_tts_until", 0.0) or 0.0), time.monotonic() + tts_window)
                    self._mark_lavalink_transition(state, seconds=tts_window)
                    self._mark_internal_voice_disconnect(int(guild_id), seconds=tts_window)
                    result = await self.backends.play_lavalink_tts(
                        guild,
                        candidates=candidates,
                        volume=TTS_VOLUME,
                        resume_volume=state.volume,
                        resume_playable=state.current_lavalink_playable,
                        timeout=max(1.0, float(timeout or 120.0)),
                        should_resume=lambda: bool(
                            state.current_backend == "lavalink"
                            and not state.skip_requested
                            and not state.stop_requested
                            and state.current is not None
                        ),
                    )
                result.setdefault("source_setup_ms", source_setup_ms)
                result.setdefault("play_call_ms", 0.0)
                result.setdefault("playback_started_at", playback_started_at)
                result.setdefault("playback_ms", max(0.0, (time.monotonic() - playback_started_at) * 1000.0))
                state.last_tts_activity_at = time.monotonic()
                state.lavalink_resume_grace_until = time.monotonic() + 4.0
                state.lavalink_tts_until = min(float(getattr(state, "lavalink_tts_until", 0.0) or 0.0), state.lavalink_resume_grace_until)
                self._mark_lavalink_transition(state, seconds=4.0)
                self._mark_internal_voice_disconnect(int(guild_id), seconds=4.0)
                return result
            except Exception as exc:
                logger.warning("[music/lavalink] TTS via Lavalink falhou; música deve continuar/resumir | guild=%s erro=%s", guild_id, exc)
                state.lavalink_resume_grace_until = time.monotonic() + 6.0
                state.lavalink_tts_until = min(float(getattr(state, "lavalink_tts_until", 0.0) or 0.0), state.lavalink_resume_grace_until)
                self._mark_lavalink_transition(state, seconds=6.0)
                self._mark_internal_voice_disconnect(int(guild_id), seconds=6.0)
                return {
                    "source_setup_ms": source_setup_ms,
                    "play_call_ms": 0.0,
                    "playback_ms": max(0.0, (time.monotonic() - playback_started_at) * 1000.0),
                    "playback_started_at": playback_started_at,
                    "tts_lavalink_failed": True,
                }

        source = discord.FFmpegPCMAudio(path, before_options=before_options, options=options)
        source_setup_ms = max(0.0, (time.monotonic() - source_setup_started_at) * 1000.0)

        if active_source is not None and not getattr(active_source, "_closed", True) and (self._vc_is_playing_or_paused(vc)):
            with contextlib.suppress(Exception):
                active_source.set_duck_factor(MUSIC_TTS_LOCAL_DUCK_FACTOR)
            logger.info(
                "[music] tts_duck_local_start | guild=%s duck_percent=%.1f base_volume=%.3f",
                guild_id,
                MUSIC_TTS_LOCAL_DUCK_FACTOR * 100.0,
                getattr(active_source, "normal_music_volume", state.volume if state is not None else 0.0),
            )
            future = active_source.add_tts(source, volume=TTS_VOLUME)
            try:
                await asyncio.wait_for(future, timeout=max(1.0, float(timeout)))
            except asyncio.TimeoutError:
                active_source.cancel_tts(future)
                playback_ms = max(0.0, (time.monotonic() - playback_started_at) * 1000.0)
                if MUSIC_TTS_OVERLAY_TIMEOUT_IS_NON_FATAL:
                    logger.warning(
                        "[music] TTS overlay excedeu %.1fs; cancelando apenas este TTS e mantendo a música/call",
                        float(timeout),
                    )
                    logger.info("[music] tts_duck_local_restore | guild=%s cancelled=True", guild_id)
                    return {
                        "source_setup_ms": source_setup_ms,
                        "play_call_ms": play_call_ms,
                        "playback_ms": playback_ms,
                        "playback_started_at": playback_started_at,
                        "tts_overlay_cancelled": True,
                        "tts_local_ducked": True,
                        "tts_local_duck_percent": MUSIC_TTS_LOCAL_DUCK_FACTOR * 100.0,
                    }
                raise RuntimeError(f"Playback TTS em overlay excedeu {float(timeout):.1f}s")
            playback_ms = max(0.0, (time.monotonic() - playback_started_at) * 1000.0)
            logger.info("[music] tts_duck_local_restore | guild=%s cancelled=False", guild_id)
            return {
                "source_setup_ms": source_setup_ms,
                "play_call_ms": play_call_ms,
                "playback_ms": playback_ms,
                "playback_started_at": playback_started_at,
                "tts_local_ducked": True,
                "tts_local_duck_percent": MUSIC_TTS_LOCAL_DUCK_FACTOR * 100.0,
            }

        finished = loop.create_future()

        def _after(error: Exception | None) -> None:
            with contextlib.suppress(Exception):
                source.cleanup()

            def _finish_once() -> None:
                if finished.done():
                    return
                if error is None:
                    finished.set_result(None)
                else:
                    finished.set_exception(error)

            loop.call_soon_threadsafe(_finish_once)

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
        state = self.get_state(guild_id)
        if not vc or not self._vc_is_connected(vc) or not self._vc_is_playing(vc) or (state.current is None and state.current_source is None):
            return False
        if self._is_lavalink_voice_client(vc):
            await vc.pause(True)
        else:
            vc.pause()
        state.paused = True
        state.current_status = "paused"
        await self.update_panel(guild_id, create=bool(state.now_message))
        return True

    async def resume(self, guild_id: int) -> bool:
        guild = self.bot.get_guild(int(guild_id))
        vc = guild.voice_client if guild else None
        state = self.get_state(guild_id)
        if not vc or not self._vc_is_connected(vc) or not self._vc_is_paused(vc) or (state.current is None and state.current_source is None):
            return False
        if self._is_lavalink_voice_client(vc):
            await vc.pause(False)
        else:
            vc.resume()
        state.paused = False
        state.current_status = "playing"
        await self.update_panel(guild_id, create=bool(state.now_message))
        return True

    async def skip(self, guild_id: int, *, add_current_to_history: bool = True) -> bool:
        state = self.get_state(guild_id)
        guild = self.bot.get_guild(int(guild_id))
        vc = guild.voice_client if guild else None
        did_anything = False
        state.skip_requested = True
        state.skip_transition_active = True
        state.skip_history_suppressed_once = not bool(add_current_to_history)
        for _vote_action in ("skip", "stop"):
            state.control_votes.pop(_vote_action, None)
            _vote_task = state.control_vote_cleanup_tasks.pop(_vote_action, None)
            if _vote_task is not None and not _vote_task.done():
                _vote_task.cancel()
        if state.current_resolve_task is not None and not state.current_resolve_task.done():
            state.current_resolve_task.cancel()
            did_anything = True
        music_audio_active = state.current is not None or state.current_source is not None or state.current_resolve_task is not None or state.current_lavalink_player is not None
        if music_audio_active and vc and self._vc_is_playing_or_paused(vc):
            with contextlib.suppress(Exception):
                await self._vc_stop_audio(vc)
            did_anything = True
        if state.current is not None or state.current_source is not None or state.current_lavalink_player is not None:
            with contextlib.suppress(Exception):
                if state.current_source is not None:
                    state.current_source.cleanup()
            # Não apaga state.current aqui. O worker ainda precisa saber qual faixa
            # foi pulada para salvar histórico e para o painel não piscar como
            # "Nada tocando agora" enquanto a próxima música está sendo preparada.
            state.current_source = None
            state.current_lavalink_playable = None
            state.paused = False
            state.current_status = "skipping"
            did_anything = True
        else:
            state.current_status = "queued" if self._has_pending_track(state) else "idle"
            if self._has_pending_track(state):
                # Pode acontecer durante uma transição rápida de anterior/avançar:
                # não há faixa ativa no instante do clique, mas ainda existe uma
                # próxima música pronta. Nesse caso ⏭️ deve avançar/iniciar, não
                # responder "não havia música".
                did_anything = True
            if not did_anything:
                state.skip_transition_active = False
                state.skip_history_suppressed_once = False
        self.ensure_music_worker(guild_id)
        self._schedule_panel_update(guild_id, create=bool(state.now_message))
        return did_anything

    async def stop(self, guild_id: int, *, disconnect: bool = True) -> bool:
        state = self.get_state(guild_id)
        state.stop_requested = True
        state.skip_requested = True
        self._cancel_next_prefetch(state)
        self._set_idle_reason(state, "manual_stop")
        for _vote_action in ("skip", "stop"):
            state.control_votes.pop(_vote_action, None)
            _vote_task = state.control_vote_cleanup_tasks.pop(_vote_action, None)
            if _vote_task is not None and not _vote_task.done():
                _vote_task.cancel()
        if state.current_resolve_task is not None and not state.current_resolve_task.done():
            state.current_resolve_task.cancel()
        while not state.queue.empty():
            with contextlib.suppress(Exception):
                state.queue.get_nowait()
                state.queue.task_done()
        state.forward_queue.clear()
        guild = self.bot.get_guild(int(guild_id))
        vc = guild.voice_client if guild else None
        if vc:
            should_stop_audio = state.current is not None or state.current_source is not None or state.current_lavalink_player is not None or not state.tts_voice_touched
            if should_stop_audio:
                with contextlib.suppress(Exception):
                    if self._vc_is_playing_or_paused(vc):
                        await self._vc_stop_audio(vc)
            if disconnect and not state.tts_voice_touched:
                with contextlib.suppress(Exception):
                    self._mark_internal_voice_disconnect(guild_id, seconds=8.0)
                    await vc.disconnect(force=False)
                state.music_owns_voice = False
        state.current = None
        state.current_started_at_monotonic = 0.0
        state.current_source = None
        state.current_resolve_task = None
        state.current_backend = "local"
        state.current_lavalink_player = None
        state.current_lavalink_playable = None
        state.skip_transition_active = False
        state.skip_history_suppressed_once = False
        state.current_status = "idle"
        state.paused = False
        state.music_session_active = False
        self._cancel_music_idle_disconnect(state)
        state.control_votes.clear()
        for _vote_task in list(state.control_vote_cleanup_tasks.values()):
            if _vote_task is not None and not _vote_task.done():
                _vote_task.cancel()
        state.control_vote_cleanup_tasks.clear()
        await self._restore_auto_bitrate_for_state(guild, state, reason="manual_stop")
        await self._restore_voice_status_for_state(guild, state, reason="manual_stop")
        await self.update_panel(guild_id, create=bool(state.now_message))
        return True

    async def set_volume(self, guild_id: int, volume_percent: int) -> float:
        state = self.get_state(guild_id)
        volume = max(0, min(150, int(volume_percent))) / 100.0
        state.volume = volume
        await self._persist_volume(guild_id, volume)
        if state.current_source is not None:
            state.current_source.set_music_volume(volume)
        if state.current_backend == "lavalink":
            await self.backends.set_lavalink_player_volume(guild_id, int(round(volume * 100)))
        self._schedule_panel_update(guild_id, create=False)
        return volume

    async def request_skip(self, guild_id: int, member) -> tuple[bool, str]:
        allowed, pending_message, completed_by_vote = await self._control_or_vote(guild_id, member, "skip")
        if not allowed:
            return False, pending_message
        ok = await self.skip(guild_id)
        if completed_by_vote:
            return ok, "`⏭️` Votação concluída: pulando música." if ok else "Não havia música para pular."
        return ok, "`⏭️` Pulando música." if ok else "Não havia música para pular."

    async def request_stop(self, guild_id: int, member, *, disconnect: bool = True) -> tuple[bool, str]:
        allowed, pending_message, completed_by_vote = await self._control_or_vote(guild_id, member, "stop")
        if not allowed:
            return False, pending_message
        ok = await self.stop(guild_id, disconnect=disconnect)
        if completed_by_vote:
            return ok, "`⏹️` Votação concluída: player encerrado e queue limpo."
        return ok, "`⏹️` Player encerrado e queue limpo."

    async def request_shuffle(self, guild_id: int, member) -> tuple[bool, str]:
        allowed, pending_message, completed_by_vote = await self._control_or_vote(guild_id, member, "shuffle")
        if not allowed:
            return False, pending_message
        enabled = await self.toggle_shuffle(guild_id)
        prefix = "Votação concluída: " if completed_by_vote else ""
        return True, f"`🔀` {prefix}Shuffle {'ativado' if enabled else 'desativado'}."

    async def request_loop(self, guild_id: int, member) -> tuple[bool, str]:
        allowed, pending_message, completed_by_vote = await self._control_or_vote(guild_id, member, "loop")
        if not allowed:
            return False, pending_message
        mode = await self.cycle_loop(guild_id)
        prefix = "Votação concluída: " if completed_by_vote else ""
        return True, f"`🔁` {prefix}Repetição: `{mode.label}`."

    async def toggle_shuffle(self, guild_id: int) -> bool:
        state = self.get_state(guild_id)
        items = self.snapshot_queue(guild_id)
        state.control_votes.pop("shuffle", None)
        state.shuffle = not state.shuffle
        if state.shuffle and len(items) > 1:
            random.shuffle(items)
            await self.replace_queue(guild_id, items)
        else:
            self._schedule_panel_update(guild_id, create=False)
        return state.shuffle

    async def cycle_loop(self, guild_id: int) -> LoopMode:
        state = self.get_state(guild_id)
        state.control_votes.pop("loop", None)
        if state.loop_mode is LoopMode.OFF:
            state.loop_mode = LoopMode.ONE
        elif state.loop_mode is LoopMode.ONE:
            state.loop_mode = LoopMode.ALL
        else:
            state.loop_mode = LoopMode.OFF
        self._schedule_panel_update(guild_id, create=False)
        return state.loop_mode

    def snapshot_queue(self, guild_id: int) -> list[MusicTrack]:
        state = self.get_state(guild_id)
        return self._pending_items(state)

    def history_snapshot(self, guild_id: int) -> list[MusicTrack]:
        state = self.get_state(guild_id)
        return list(state.history)

    def _push_history(self, state: MusicGuildState, track: MusicTrack) -> None:
        # Evita duplicar a mesma música em sequência quando skip/loop dispara rápido.
        try:
            if state.history and state.history[-1].display_url == track.display_url and state.history[-1].title == track.title:
                return
            state.history.append(track)
        except Exception:
            logger.debug("[music] falha ao salvar histórico", exc_info=True)

    def _prepend_queue(self, state: MusicGuildState, track: MusicTrack) -> bool:
        try:
            if state.queue.full():
                return False
            state.queue._queue.appendleft(track)
            return True
        except Exception:
            logger.debug("[music] falha ao colocar música no começo da fila", exc_info=True)
            return False

    async def previous(self, guild_id: int) -> bool:
        state = self.get_state(guild_id)
        if not state.history:
            # Sem histórico, o botão de voltar não reinicia a faixa atual.
            # Ele apenas informa que não existe música anterior.
            return False
        try:
            previous_track = state.history.pop()
        except IndexError:
            return False

        current = state.current
        # ``forward_queue`` tem prioridade sobre o queue normal. Por isso a
        # música anterior precisa entrar nela ANTES da música atual. O fluxo
        # correto com duas músicas é:
        # A tocando → ⏭️ B → ⏮️ A → ⏭️ B.
        # Quando o player já está em "Nada tocando", current é None; nesse caso
        # o ⏮️ precisa acordar o worker e tocar o histórico imediatamente, não
        # apenas mostrar "Queue pronto" parado.
        try:
            if current is not None:
                state.forward_queue.appendleft(current)
            state.forward_queue.appendleft(previous_track)
        except Exception:
            logger.debug("[music] falha ao preparar histórico/avanço", exc_info=True)
            return False

        skipped = await self.skip(guild_id, add_current_to_history=False) if current is not None else False
        if current is None:
            state.stop_requested = False
            state.skip_requested = False
            state.skip_transition_active = True
            state.skip_history_suppressed_once = False
            state.current_status = "skipping"
            self._clear_idle_reason(state)
            self._cancel_music_idle_disconnect(state)
            state.music_session_active = True
        self.ensure_music_worker(guild_id)
        self._schedule_panel_update(guild_id, create=True)
        return True if skipped or previous_track else False

    async def readd_history(self, guild_id: int, *, limit: int = 20) -> int:
        state = self.get_state(guild_id)
        items = list(state.history)[-max(1, int(limit)):]
        if not items:
            return 0
        added = 0
        for track in items:
            if state.queue.full():
                break
            await state.queue.put(track)
            added += 1
        if added:
            state.stop_requested = False
            self.ensure_music_worker(guild_id)
            self._schedule_panel_update(guild_id, create=True)
        return added

    async def skip_to(self, guild_id: int, index_1based: int) -> bool:
        state = self.get_state(guild_id)
        items = self.snapshot_queue(guild_id)
        idx = int(index_1based) - 1
        if idx < 0 or idx >= len(items):
            return False
        selected = items.pop(idx)
        items.insert(0, selected)
        await self.replace_queue(guild_id, items)
        state.stop_requested = False
        if state.current is not None or state.current_source is not None or state.current_resolve_task is not None:
            await self.skip(guild_id)
        else:
            self.ensure_music_worker(guild_id)
        self._schedule_panel_update(guild_id, create=True)
        return True

    async def replace_queue(self, guild_id: int, tracks: list[MusicTrack]) -> None:
        state = self.get_state(guild_id)
        self._cancel_next_prefetch(state)
        while not state.queue.empty():
            with contextlib.suppress(Exception):
                state.queue.get_nowait()
                state.queue.task_done()
        state.forward_queue.clear()
        for track in tracks[:MUSIC_QUEUE_MAXSIZE]:
            await state.queue.put(track)
        if tracks:
            state.music_session_active = True
            self._cancel_music_idle_disconnect(state)
            if state.current is not None or state.current_source is not None:
                self._start_prefetch_next(guild_id, state)
        self._schedule_panel_update(guild_id, create=bool(state.now_message or state.current or tracks))

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
