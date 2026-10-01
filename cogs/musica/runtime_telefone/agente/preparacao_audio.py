"""Prepara apenas a próxima faixa, perto da transição, sem ampliar a playlist."""
from __future__ import annotations

import asyncio
import contextlib
import threading
import time
from dataclasses import dataclass, replace
from typing import Any

import discord

from .buffer_pcm import BufferedPCMSource
from .audio_segmentado import ArchiveSegmentedPCMSource, ContinuousArchiveDSPSource, ExactSegmentPCMSource
from .ciclo_vida import consume_task_result, remove_owned_task
from .estado import AgentTrack, GuildMusicState


@dataclass
class AudioPreparado:
    item_id: str
    stream_url: str
    offset: float
    source: Any
    created_at: float
    ready: bool = False
    effects: tuple[int, int, int] = (0, 0, 0)


@dataclass
class AudioInicial:
    item_id: str
    stream_url: str
    source: Any
    created_at: float
    playback_token: int
    queue_reset_generation: int
    voice_channel_id: int
    effects: tuple[int, int, int]
    audio_stream_index: int
    audio_sample_rate: int
    confirmed: bool
    archive_identity: tuple = ()


class PreparacaoAudioMixin:
    def _claim_audio_prepare_slot(self, owner: Any) -> bool:
        """Uma cota compartilhada por início, próxima faixa e próxima parte."""
        if getattr(self, "_audio_prepare_stopping", False):
            return False
        if not hasattr(self, "_audio_extra_owners"):
            self._audio_extra_owners = set()
            self._audio_extra_lock = threading.RLock()
            self._audio_extra_preempting = set()
            self._audio_extra_preempt_tasks = {}
        with self._audio_extra_lock:
            if owner in self._audio_extra_preempting:
                return False
            if owner in self._audio_extra_owners:
                return True
            if len(self._audio_extra_owners) >= getattr(self, "next_audio_prepare_max_sources", 1):
                # Uma parte da música atual tem prioridade sobre aquecer uma
                # faixa futura. A vaga continua ocupada durante o cleanup.
                if isinstance(owner, tuple) and owner[0] == "segment":
                    for optional in list(self._audio_extra_owners):
                        if (isinstance(optional, tuple) and optional[0] == "track"
                                and optional not in self._audio_extra_preempting):
                            self._audio_extra_preempting.add(optional)
                            task = asyncio.create_task(self._preempt_optional_audio(optional))
                            self._audio_extra_preempt_tasks[optional] = task
                            task.add_done_callback(consume_task_result)
                            break
                return False
            self._audio_extra_owners.add(owner)
            return True

    def _release_audio_prepare_slot(self, owner: Any) -> None:
        lock = getattr(self, "_audio_extra_lock", None)
        if lock is not None:
            with lock:
                if owner not in self._audio_extra_preempting:
                    self._audio_extra_owners.discard(owner)

    @staticmethod
    async def _cleanup_audio_off_loop(source: Any) -> None:
        cleanup = asyncio.get_running_loop().run_in_executor(None, source.cleanup)
        cancelled = False
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                # Cancelar a espera não encerra a thread. Até cancelamentos
                # repetidos aguardam seu fim antes de liberar a vaga.
                cancelled = True
        cleanup.result()
        if cancelled:
            raise asyncio.CancelledError

    async def _preempt_optional_audio(self, owner: tuple) -> None:
        guild_id = owner[1]
        prepared = self._prepared_audio.pop(guild_id, None)
        task = self._audio_prepare_tasks.pop(guild_id, None)
        self._audio_prepare_keys.pop(guild_id, None)
        try:
            if task is not None and task is not asyncio.current_task():
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        finally:
            try:
                if prepared is not None:
                    await self._cleanup_audio_off_loop(prepared.source)
            finally:
                with self._audio_extra_lock:
                    self._audio_extra_preempting.discard(owner)
                    self._audio_extra_owners.discard(owner)
                    self._audio_extra_preempt_tasks.pop(owner, None)

    @staticmethod
    def _archive_audio_identity(track: AgentTrack) -> tuple:
        return tuple(tuple(segment.get(key) for key in (
            "guild_id", "channel_id", "message_id", "attachment_id", "duration", "offset_seconds",
            "audio_stream_index", "audio_sample_rate", "audio_channels",
        )) for segment in track.archive_segments)

    async def _prepare_current_pcm(self, guild_id: int, track: AgentTrack, playback_token: int) -> Any:
        st = self.states.setdefault(guild_id, GuildMusicState(guild_id=guild_id))
        effects = (0, st.effect_level("nightcore"), st.effect_level("slowed_reverb"))  # Bassboost atua somente no mixer
        source = self._take_prepared_audio(guild_id, track)
        initial = self._take_initial_audio(guild_id, track, playback_token) if source is None else None
        source = source or initial or self._create_pcm_source(track, effects=effects)

        async def wait_for_audio(candidate: Any) -> Any:
            self._starting_pcm[guild_id] = candidate
            try:
                if isinstance(candidate, (BufferedPCMSource, ArchiveSegmentedPCMSource, ContinuousArchiveDSPSource)):
                    await candidate.wait_ready(timeout=min(12.0, self.prepare_timeout))
                return candidate
            except BaseException:
                candidate.cleanup()
                raise
            finally:
                if self._starting_pcm.get(guild_id) is candidate:
                    self._starting_pcm.pop(guild_id, None)

        try:
            return await wait_for_audio(source)
        except Exception as exc:
            # Um decoder antecipado pode perder a corrida com o CDN. Antes de
            # renovar a URL, tente a mesma URL com a trilha já verificada.
            if initial is None or st.playback_token != playback_token:
                raise
            self.log("initial_audio_retry_verified", guild_id=guild_id, error=type(exc).__name__)
            return await wait_for_audio(self._create_pcm_source(track, effects=effects))

    def _create_pcm_source(
        self, track: AgentTrack, *, effects: tuple[int | bool, ...] = (0, 0, 0),
        first_audio_stream: bool = False, buffer_max_frames: int | None = None,
        preserve_partial_frames: bool = False, preparing: bool = False,
    ) -> Any:
        if track.archive_segments:
            start_index = self._archive_segment_at(track.archive_segments, track.start_offset_seconds)
            start_local = max(0.0, track.start_offset_seconds - float(track.archive_segments[start_index]["offset_seconds"]))
            if len(track.archive_segments) == 1:
                segment = track.archive_segments[0]
                single = replace(track, archive_segments=[], start_offset_seconds=start_local,
                                 audio_stream_index=int(segment.get("audio_stream_index", track.audio_stream_index)),
                                 audio_sample_rate=int(segment.get("audio_sample_rate", track.audio_sample_rate)),
                                 audio_channels=int(segment.get("audio_channels", track.audio_channels)))
                return self._create_pcm_source(single, effects=effects, buffer_max_frames=buffer_max_frames,
                                               preserve_partial_frames=True)

            async def create_segment(index: int, offset: float) -> Any:
                segment = track.archive_segments[index]
                if index == track.archive_segment_index and track.stream_url and not self._track_stream_needs_refresh(track):
                    url = track.stream_url
                else:
                    segment = await self._resolve_archive_segment(track, index)
                    url = segment["url"]
                part = replace(
                    track, archive_segments=[], stream_url=url, start_offset_seconds=offset,
                    attachment_ref={key: int(segment[key]) for key in ("guild_id", "channel_id", "message_id", "attachment_id")},
                    audio_stream_index=int(segment.get("audio_stream_index", 0)),
                    audio_sample_rate=int(segment.get("audio_sample_rate", track.audio_sample_rate)),
                    audio_channels=int(segment.get("audio_channels", track.audio_channels)),
                    duration=float(segment["duration"]),
                )
                # A velocidade/reverb atuam em um único pipe após a união de
                # PCM; assim a cauda e o estado do filtro não reiniciam por parte.
                source = self._create_pcm_source(part, effects=(0, 0, 0), buffer_max_frames=buffer_max_frames,
                                                 preserve_partial_frames=True)
                if not isinstance(source, BufferedPCMSource):
                    source = BufferedPCMSource(source, max_frames=buffer_max_frames or getattr(self, "pcm_buffer_max_frames", 75),
                                               stall_seconds=getattr(self, "pcm_buffer_stall_seconds", 12.0))
                return source

            loop = self._loop or asyncio.get_running_loop()
            raw = ArchiveSegmentedPCMSource(
                segments=track.archive_segments, start_index=start_index, start_offset=start_local,
                create_segment=create_segment, loop=loop,
                max_frames=buffer_max_frames or getattr(self, "pcm_buffer_max_frames", 75),
                stall_seconds=getattr(self, "pcm_buffer_stall_seconds", 12.0),
                preparing=preparing,
                claim_prepare=self._claim_audio_prepare_slot,
                release_prepare=self._release_audio_prepare_slot,
            )
            if any(effects[1:]):
                try:
                    options, _ = self._ffmpeg_options_for_source(48000, effects=effects)
                except BaseException:
                    raw.cleanup()
                    raise
                return ContinuousArchiveDSPSource(
                    raw, executable=self.ffmpeg_executable, options=options, loop=loop,
                    max_frames=buffer_max_frames or getattr(self, "pcm_buffer_max_frames", 75),
                    stall_seconds=getattr(self, "pcm_buffer_stall_seconds", 12.0),
                )
            return raw
        options, _mode = self._ffmpeg_options_for_source(
            track.audio_sample_rate, effects=effects, is_live=track.is_live,
        )
        if first_audio_stream:
            options = f"-map 0:a:0 {options}"
        elif track.attachment_ref and track.audio_stream_index >= 0:
            # Decodifica apenas a trilha confirmada pelo ffprobe, mesmo quando
            # o arquivo tem vídeo, múltiplas trilhas ou capa embutida.
            options = f"-map 0:{track.audio_stream_index} {options}"
        before = self._ffmpeg_before_options_for_offset(track.start_offset_seconds)
        # EOF é normal em músicas. Transmissões ao vivo mantêm a reconexão.
        if track.is_live and "-reconnect_at_eof" not in before:
            before += " -reconnect_at_eof 1"
        pcm = discord.FFmpegPCMAudio(
            track.stream_url, executable=self.ffmpeg_executable,
            before_options=before, options=options,
        )
        if preserve_partial_frames:
            pcm = ExactSegmentPCMSource(pcm)
        if not getattr(self, "pcm_buffer_enabled", True) and not preserve_partial_frames:
            return pcm
        return BufferedPCMSource(
            pcm, max_frames=buffer_max_frames or getattr(self, "pcm_buffer_max_frames", 75),
            stall_seconds=getattr(self, "pcm_buffer_stall_seconds", 12.0),
            **({"preserve_partial_frames": True} if preserve_partial_frames else {}),
        )

    def _cancel_initial_audio(self, guild_id: int, *, item_id: str = "") -> None:
        entry = self._initial_audio.get(guild_id)
        if entry is None or (item_id and entry.item_id != item_id):
            return
        self._initial_audio.pop(guild_id, None)
        entry.source.cleanup()
        self._release_audio_prepare_slot(("initial", guild_id))

    def _begin_initial_audio(self, guild_id: int, track: AgentTrack, *, speculative_first_audio: bool = False) -> bool:
        st = self.states.get(guild_id)
        if (
            not getattr(self, "initial_audio_prepare_enabled", True)
            or (not self.direct_pcm_volume_enabled and not track.archive_segments) or not getattr(self, "pcm_buffer_enabled", True)
            or st is None or not track.stream_url or track.is_live
        ):
            return False
        existing = self._initial_audio.get(guild_id)
        if existing is not None:
            if (existing.item_id == track.queue_item_id and existing.stream_url == track.stream_url
                    and existing.archive_identity == self._archive_audio_identity(track)
                    and existing.effects == (0, st.effect_level("nightcore"), st.effect_level("slowed_reverb"))
                    and existing.playback_token == st.playback_token
                    and existing.voice_channel_id == st.voice_channel_id):
                return True
            self._cancel_initial_audio(guild_id)
        # Divide com o prefetch da próxima faixa a cota de um decoder extra no
        # aparelho. O buffer inicial guarda no máximo 0,5 s de PCM (96 KiB).
        if not self._claim_audio_prepare_slot(("initial", guild_id)):
            return False
        effects = (0, st.effect_level("nightcore"), st.effect_level("slowed_reverb"))
        try:
            source = self._create_pcm_source(
                track, effects=effects, first_audio_stream=speculative_first_audio,
                buffer_max_frames=min(25, self.pcm_buffer_max_frames),
                preparing=True,
            )
        except Exception as exc:
            self._release_audio_prepare_slot(("initial", guild_id))
            self.log("initial_audio_unavailable", guild_id=guild_id, error=type(exc).__name__)
            return False
        if not isinstance(source, (BufferedPCMSource, ArchiveSegmentedPCMSource, ContinuousArchiveDSPSource)):
            self._release_audio_prepare_slot(("initial", guild_id))
            with contextlib.suppress(Exception):
                source.cleanup()
            return False
        self._initial_audio[guild_id] = AudioInicial(
            track.queue_item_id, track.stream_url, source, time.monotonic(),
            st.playback_token, st.queue_reset_generation, st.voice_channel_id,
            effects, track.audio_stream_index, track.audio_sample_rate,
            confirmed=not speculative_first_audio,
            archive_identity=self._archive_audio_identity(track),
        )
        self.log("initial_audio_started", guild_id=guild_id, speculative=speculative_first_audio)
        return True

    def _confirm_initial_audio(
        self, guild_id: int, *, item_id: str, stream_url: str,
        first_audio_stream_index: int | None, audio_stream_index: int, audio_sample_rate: int,
    ) -> None:
        entry = self._initial_audio.get(guild_id)
        if entry is None or entry.item_id != item_id:
            return
        st = self.states.get(guild_id)
        # -map 0:a:0 escolhe a primeira faixa de áudio; ffprobe pode escolher
        # outra marcada como default. Filtros dependentes da taxa precisam
        # produzir as mesmas opções antes e depois da verificação.
        try:
            valid = bool(
                st is not None and entry.stream_url == stream_url
                and entry.playback_token == st.playback_token
                and entry.queue_reset_generation == st.queue_reset_generation
                and entry.voice_channel_id == st.voice_channel_id
                and entry.effects == (0, st.effect_level("nightcore"), st.effect_level("slowed_reverb"))
                and first_audio_stream_index is not None
                and int(first_audio_stream_index) == int(audio_stream_index)
                and self._ffmpeg_options_for_source(0, effects=entry.effects)[0]
                    == self._ffmpeg_options_for_source(audio_sample_rate, effects=entry.effects)[0]
            )
        except (TypeError, ValueError):
            valid = False
        if not valid:
            self.log("initial_audio_discarded", guild_id=guild_id, reason="stream_or_effect_mismatch")
            self._cancel_initial_audio(guild_id, item_id=item_id)
            return
        entry.audio_stream_index = int(audio_stream_index)
        entry.audio_sample_rate = int(audio_sample_rate)
        entry.confirmed = True

    def _take_initial_audio(self, guild_id: int, track: AgentTrack, playback_token: int) -> Any:
        entry = self._initial_audio.pop(guild_id, None)
        if entry is None:
            return None
        self._release_audio_prepare_slot(("initial", guild_id))
        st = self.states.get(guild_id)
        if (
            entry.confirmed and st is not None and st.current is track
            and entry.item_id == track.queue_item_id and entry.stream_url == track.stream_url
            and entry.audio_stream_index == track.audio_stream_index
            and entry.audio_sample_rate == track.audio_sample_rate
            and entry.archive_identity == self._archive_audio_identity(track)
            and entry.effects == (0, st.effect_level("nightcore"), st.effect_level("slowed_reverb"))
            and entry.playback_token + 1 == playback_token == st.playback_token
            and entry.queue_reset_generation == st.queue_reset_generation
            and entry.voice_channel_id == st.voice_channel_id
            and abs(track.start_offset_seconds) < 0.01
            and time.monotonic() - entry.created_at <= 30.0
        ):
            entry.source.set_max_frames(self.pcm_buffer_max_frames)
            activate = getattr(entry.source, "activate_playback", None)
            if callable(activate):
                activate()
            self.log("initial_audio_reused", guild_id=guild_id,
                     warm_ms=round((time.monotonic() - entry.created_at) * 1000.0, 1),
                     **entry.source.audio_buffer_metrics())
            return entry.source
        entry.source.cleanup()
        return None

    def _cancel_audio_preparation(self, guild_id: int, *, keep_item_id: str = "") -> None:
        task = self._audio_prepare_tasks.pop(guild_id, None)
        if task is not None and not task.done() and task is not asyncio.current_task():
            task.cancel()
        self._audio_prepare_keys.pop(guild_id, None)
        prepared = self._prepared_audio.get(guild_id)
        st = self.states.get(guild_id)
        effects = (0, st.effect_level("nightcore"), st.effect_level("slowed_reverb")) if st else (0, 0, 0)
        if prepared is not None and not (prepared.ready and prepared.item_id == keep_item_id and prepared.effects == effects):
            self._prepared_audio.pop(guild_id, None)
            prepared.source.cleanup()
            self._release_audio_prepare_slot(("track", guild_id))

    def _take_prepared_audio(self, guild_id: int, track: AgentTrack) -> BufferedPCMSource | None:
        prepared = self._prepared_audio.pop(guild_id, None)
        if prepared is not None:
            self._release_audio_prepare_slot(("track", guild_id))
        self._cancel_audio_preparation(guild_id)
        if prepared is None:
            return None
        if (
            prepared.ready and prepared.item_id == track.queue_item_id
            and prepared.effects == (0, self.states[guild_id].effect_level("nightcore"), self.states[guild_id].effect_level("slowed_reverb"))
            and prepared.stream_url == track.stream_url
            and abs(prepared.offset - track.start_offset_seconds) < 0.01
            and time.monotonic() - prepared.created_at <= 45.0
        ):
            activate = getattr(prepared.source, "activate_playback", None)
            if callable(activate):
                activate()
            self.log("next_audio_reused", guild_id=guild_id, **prepared.source.audio_buffer_metrics())
            return prepared.source
        prepared.source.cleanup()
        return None

    def _schedule_audio_prepare(self, guild_id: int) -> None:
        st = self.states.get(guild_id)
        if (
            not getattr(self, "next_audio_prepare_enabled", True)
            or not getattr(self, "pcm_buffer_enabled", True)
            or st is None or st.paused or not st.queue or st.queue[0].is_virtual_playlist_marker
            or (not self.direct_pcm_volume_enabled and not st.queue[0].archive_segments)
            or st.current is None or not st.current.duration or not st.started_monotonic
        ):
            self._cancel_audio_preparation(guild_id)
            return
        item_id = st.queue[0].queue_item_id
        effects = (0, st.effect_level("nightcore"), st.effect_level("slowed_reverb"))
        prepare_key = f"{item_id}:n{effects[1]}:r{effects[2]}"
        task = self._audio_prepare_tasks.get(guild_id)
        if task is not None and not task.done() and self._audio_prepare_keys.get(guild_id) == prepare_key:
            return
        prepared = self._prepared_audio.get(guild_id)
        if prepared is not None and prepared.item_id == item_id and prepared.effects == effects and prepared.ready:
            return
        self._cancel_audio_preparation(guild_id)
        token = st.playback_token
        remaining = max(0.0, st.current.duration - st.source_position_seconds()) / st.playback_speed
        delay = max(0.0, remaining - getattr(self, "next_audio_prepare_lead_seconds", 12.0))

        def still_next() -> bool:
            current = self.states.get(guild_id)
            return bool(
                current is st and current.playback_token == token and not current.paused
                and (0, current.effect_level("nightcore"), current.effect_level("slowed_reverb")) == effects
                and current.queue and current.queue[0].queue_item_id == item_id
            )

        async def prepare() -> None:
            owned: AudioPreparado | None = None
            try:
                if delay:
                    await asyncio.sleep(delay)
                if not still_next():
                    return
                track = st.queue[0]
                if not track.stream_url or self._track_stream_needs_refresh(track):
                    previous_track = track
                    meta = track.public()
                    query = self._query_from_track_meta(meta, fallback_query=track.query)
                    if track.stream_url:
                        self._invalidate_track_stream_cache(track)
                    track = await asyncio.wait_for(
                        self.resolve_track(query, track_meta=meta, body={"guild_id": guild_id}, priority=10),
                        timeout=self.prefetch_timeout,
                    )
                    if not still_next():
                        return
                    self._copy_play_trace(previous_track, track)
                    st.queue[0] = track
                if track.is_live or not track.stream_url:
                    return
                # Sem fila oculta de FFmpegs: há no máximo uma preparação extra
                # por padrão em todo o aparelho, além das músicas já tocando.
                if not self._claim_audio_prepare_slot(("track", guild_id)):
                    return
                try:
                    source = self._create_pcm_source(track, effects=effects, preparing=True)
                except BaseException:
                    self._release_audio_prepare_slot(("track", guild_id))
                    raise
                owned = AudioPreparado(item_id, track.stream_url, track.start_offset_seconds, source, time.monotonic(), effects=effects)
                self._prepared_audio[guild_id] = owned
                await source.wait_ready(
                    timeout=min(12.0, self.prepare_timeout),
                    min_frames=getattr(self, "next_audio_prepare_frames", 15),
                )
                if not still_next() or self._prepared_audio.get(guild_id) is not owned:
                    return
                owned.ready = True
                self.log("next_audio_ready", guild_id=guild_id, title=track.title, **source.audio_buffer_metrics())
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                self.log("next_audio_prepare_failed", guild_id=guild_id, error=type(exc).__name__)
            finally:
                try:
                    if owned is not None and not owned.ready:
                        if self._prepared_audio.get(guild_id) is owned:
                            self._prepared_audio.pop(guild_id, None)
                        try:
                            await self._cleanup_audio_off_loop(owned.source)
                        finally:
                            self._release_audio_prepare_slot(("track", guild_id))
                finally:
                    current_task = asyncio.current_task()
                    if self._audio_prepare_tasks.get(guild_id) is current_task:
                        self._audio_prepare_keys.pop(guild_id, None)
                    remove_owned_task(self._audio_prepare_tasks, guild_id, current_task)

        self._audio_prepare_keys[guild_id] = prepare_key
        self._audio_prepare_tasks[guild_id] = asyncio.create_task(prepare())
