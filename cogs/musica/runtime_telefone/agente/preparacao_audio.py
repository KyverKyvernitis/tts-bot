"""Prepara apenas a próxima faixa, perto da transição, sem ampliar a playlist."""
from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass
from typing import Any

import discord

from .buffer_pcm import BufferedPCMSource
from .ciclo_vida import remove_owned_task
from .estado import AgentTrack, GuildMusicState


@dataclass
class AudioPreparado:
    item_id: str
    stream_url: str
    offset: float
    source: BufferedPCMSource
    created_at: float
    ready: bool = False
    effects: tuple[bool, bool, bool] = (False, False, False)


class PreparacaoAudioMixin:
    async def _prepare_current_pcm(self, guild_id: int, track: AgentTrack, playback_token: int) -> Any:
        st = self.states.setdefault(guild_id, GuildMusicState(guild_id=guild_id))
        effects = (False, st.nightcore, st.slowed_reverb)  # Bassboost atua somente no mixer
        source = self._take_prepared_audio(guild_id, track) or self._create_pcm_source(track, effects=effects)
        self._starting_pcm[guild_id] = source
        try:
            if isinstance(source, BufferedPCMSource):
                await source.wait_ready(timeout=min(12.0, self.prepare_timeout))
            return source
        except BaseException:
            source.cleanup()
            raise
        finally:
            if self._starting_pcm.get(guild_id) is source:
                self._starting_pcm.pop(guild_id, None)

    def _create_pcm_source(self, track: AgentTrack, *, effects: tuple[bool, ...] = (False, False, False)) -> Any:
        options, _mode = self._ffmpeg_options_for_source(
            track.audio_sample_rate, effects=effects, is_live=track.is_live,
        )
        if track.attachment_ref and track.audio_stream_index >= 0:
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
        if not getattr(self, "pcm_buffer_enabled", True):
            return pcm
        return BufferedPCMSource(
            pcm, max_frames=getattr(self, "pcm_buffer_max_frames", 75),
            stall_seconds=getattr(self, "pcm_buffer_stall_seconds", 12.0),
        )

    def _cancel_audio_preparation(self, guild_id: int, *, keep_item_id: str = "") -> None:
        task = self._audio_prepare_tasks.pop(guild_id, None)
        if task is not None and not task.done() and task is not asyncio.current_task():
            task.cancel()
        self._audio_prepare_keys.pop(guild_id, None)
        prepared = self._prepared_audio.get(guild_id)
        st = self.states.get(guild_id)
        effects = (False, st.nightcore, st.slowed_reverb) if st else (False, False, False)
        if prepared is not None and not (prepared.ready and prepared.item_id == keep_item_id and prepared.effects == effects):
            self._prepared_audio.pop(guild_id, None)
            prepared.source.cleanup()

    def _take_prepared_audio(self, guild_id: int, track: AgentTrack) -> BufferedPCMSource | None:
        prepared = self._prepared_audio.pop(guild_id, None)
        self._cancel_audio_preparation(guild_id)
        if prepared is None:
            return None
        if (
            prepared.ready and prepared.item_id == track.queue_item_id
            and prepared.effects == (False, self.states[guild_id].nightcore, self.states[guild_id].slowed_reverb)
            and prepared.stream_url == track.stream_url
            and abs(prepared.offset - track.start_offset_seconds) < 0.01
            and time.monotonic() - prepared.created_at <= 45.0
        ):
            self.log("next_audio_reused", guild_id=guild_id, **prepared.source.audio_buffer_metrics())
            return prepared.source
        prepared.source.cleanup()
        return None

    def _schedule_audio_prepare(self, guild_id: int) -> None:
        st = self.states.get(guild_id)
        if (
            not getattr(self, "next_audio_prepare_enabled", True)
            or not self.direct_pcm_volume_enabled or not getattr(self, "pcm_buffer_enabled", True)
            or st is None or st.paused or not st.queue or st.queue[0].is_virtual_playlist_marker
            or st.current is None or not st.current.duration or not st.started_monotonic
        ):
            self._cancel_audio_preparation(guild_id)
            return
        item_id = st.queue[0].queue_item_id
        effects = (False, st.nightcore, st.slowed_reverb)
        prepare_key = f"{item_id}:{int(effects[1])}:{int(effects[2])}"
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
                and (False, current.nightcore, current.slowed_reverb) == effects
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
                    st.queue[0] = track
                if track.is_live or not track.stream_url:
                    return
                # Sem fila oculta de FFmpegs: há no máximo uma preparação extra
                # por padrão em todo o aparelho, além das músicas já tocando.
                if len(self._prepared_audio) >= getattr(self, "next_audio_prepare_max_sources", 1):
                    return
                source = self._create_pcm_source(track, effects=effects)
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
                if owned is not None and not owned.ready:
                    if self._prepared_audio.get(guild_id) is owned:
                        self._prepared_audio.pop(guild_id, None)
                    owned.source.cleanup()
                current_task = asyncio.current_task()
                if self._audio_prepare_tasks.get(guild_id) is current_task:
                    self._audio_prepare_keys.pop(guild_id, None)
                remove_owned_task(self._audio_prepare_tasks, guild_id, current_task)

        self._audio_prepare_keys[guild_id] = prepare_key
        self._audio_prepare_tasks[guild_id] = asyncio.create_task(prepare())
