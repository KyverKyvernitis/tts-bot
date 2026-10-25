"""Prepara apenas a próxima faixa, perto da transição, sem ampliar a playlist."""
from __future__ import annotations

import asyncio
import contextlib
import threading
import time
from types import SimpleNamespace
from dataclasses import dataclass, replace
from typing import Any

import discord

from .buffer_pcm import BufferedOpusPCMSource, BufferedPCMSource, OpusPCMDecoderSource
from .audio_segmentado import ArchiveOpusPCMSource, ArchiveSegmentedPCMSource, ContinuousArchiveDSPSource, ExactSegmentPCMSource
from .ciclo_vida import consume_task_result, remove_owned_task
from .estado import AgentTrack, GuildMusicState
from .configuracao import (
    effective_opus_direct_mode, opus_passthrough_eligibility,
    opus_preparation_eligibility, prepared_opus_cache_key,
)


@dataclass
class AudioPreparado:
    item_id: str
    stream_url: str
    offset: float
    source: Any
    created_at: float
    ready: bool = False
    effects: tuple[int, int, int] = (0, 0, 0)
    archive_identity: tuple = ()


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
    def _opus_preparation_preflight(self, guild_id: int, track: AgentTrack) -> dict[str, Any]:
        """Prepare finite media only when its output could actually be copied."""
        st = self.states.get(guild_id)
        if effective_opus_direct_mode(self, st, guild_id=guild_id) != "auto":
            return {"candidate": False, "reason": "disabled"}
        if getattr(track, "_opus_prepared_failed", False):
            return {"candidate": False, "reason": "previous_preparation_failure"}
        channel_id = getattr(st, "voice_channel_id", 0)
        getter = getattr(getattr(self, "client", None), "get_channel", None)
        channel = getter(channel_id) if callable(getter) else None
        if channel is None:
            channel = getattr(getattr(st, "player", None), "channel", None)
        if not channel or not int(getattr(channel, "bitrate", 0) or 0):
            return {"candidate": False, "reason": "channel_bitrate_unknown"}
        policy = self._audio_output_policy(SimpleNamespace(channel=channel), track, st)
        capacity = policy.get("copy_bitrate_kbps", policy.get("bitrate_kbps", 0))
        result = opus_preparation_eligibility(
            self, track, state=st, output_bitrate_kbps=capacity,
            volume_percent=getattr(st, "volume_percent", self.default_volume_percent),
            effects=st.effect_signature() if st is not None else (),
        )
        has_tts = getattr(self, "_tts_active_for_guild", None)
        if callable(has_tts) and has_tts(guild_id):
            result.update(candidate=False, reason="tts_active")
        if result.get("candidate"):
            # A 20 ms packet cannot exceed 1275 bytes: 510 kbps is the
            # largest exact CBR payload below that physical Opus limit.
            result["bitrate_kbps"] = min(510, int(capacity))
            result["fec"] = policy.get("fec", True)
            result["expected_packet_loss"] = policy.get("expected_packet_loss", 0.15)
            quality = getattr(self, "_ffmpeg_quality_resample_filter", None)
            result["resample_filter"] = (quality() if callable(quality)
                                         and getattr(self, "resample_quality_enabled", True) else "")
        return result

    def _create_prepared_opus_source(
        self, guild_id: int, track: AgentTrack, *, skip_cache: bool = False, **kwargs: Any,
    ) -> Any | None:
        """Stream one encoding pass, or read an already completed local version."""
        preflight = self._opus_preparation_preflight(guild_id, track)
        if not preflight.get("candidate"):
            return None
        bitrate = preflight["bitrate_kbps"]
        codec = str(getattr(track, "audio_codec", "") or "")
        protection = {"fec": preflight["fec"], "expected_packet_loss": preflight["expected_packet_loss"]}
        signature = {**protection, "resample_filter": preflight["resample_filter"]}
        cache = None if kwargs.get("first_audio_stream") else getattr(self, "prepared_opus_cache", None)
        key, path, writer = None, None, None
        if cache is not None:
            with contextlib.suppress(Exception):
                key = prepared_opus_cache_key(track, bitrate, **signature)
                if not skip_cache:
                    path = cache.get(key)

        def invalidate() -> None:
            if cache is not None and key is not None:
                with contextlib.suppress(Exception):
                    cache.invalidate(key)

        def wrap(raw: Any, *, cache_hit: bool) -> Any:
            try:
                source = BufferedOpusPCMSource(
                    raw, max_frames=kwargs.get("buffer_max_frames") or getattr(self, "pcm_buffer_max_frames", 75),
                    stall_seconds=getattr(self, "pcm_buffer_stall_seconds", 12.0),
                )
                source._prepared_opus_source = True
                source._prepared_opus_cache_key = key
                source._prepared_opus_cache_hit = cache_hit
                self._install_original_pcm_request(guild_id, track, source)
                return source
            except BaseException:
                raw.cleanup()
                raise

        if path is not None:
            try:
                raw = OpusPCMDecoderSource.from_prepared_file(
                    path, bitrate_kbps=bitrate, source_codec=codec, on_error=invalidate, **protection)
                return wrap(raw, cache_hit=True)
            except Exception:
                invalidate()  # A missing/replaced cache file is not an audio failure.
        if cache is not None:
            with contextlib.suppress(Exception):
                writer = cache.writer(key)
        raw, pcm = None, None
        try:
            if len(track.archive_segments) > 1:
                archived = replace(track, archive_segments=[dict(part) for part in track.archive_segments],
                                   archive_ref=dict(track.archive_ref), attachment_ref=dict(track.attachment_ref))
                # Decode the original parts continuously using the already
                # verified archive path, then encode that joined signal once.
                pcm = self._create_pcm_source(archived, **kwargs)
                raw = OpusPCMDecoderSource.from_prepared_pcm(
                    pcm, bitrate_kbps=bitrate, source_codec=codec, executable=self.ffmpeg_executable,
                    cache_writer=writer, stall_seconds=getattr(self, "pcm_buffer_stall_seconds", 12.0),
                    **protection)
            else:
                stream_index = (-1 if kwargs.get("first_audio_stream") else track.audio_stream_index
                                if track.attachment_ref or track.archive_ref else -1)
                raw = OpusPCMDecoderSource.from_prepared_ffmpeg(
                    track.stream_url, bitrate_kbps=bitrate, source_codec=codec,
                    executable=self.ffmpeg_executable,
                    before_options=self._ffmpeg_before_options_for_offset(0),
                    audio_stream_index=stream_index, cache_writer=writer,
                    resample_filter=preflight["resample_filter"] if track.audio_sample_rate not in (0, 48000) else "",
                    **protection)
            return wrap(raw, cache_hit=False)
        except BaseException:
            if raw is not None:
                raw.cleanup()
            elif pcm is not None:
                pcm.cleanup()
            if writer is not None:
                with contextlib.suppress(Exception):
                    writer.abort()
            raise

    def _install_original_pcm_request(self, guild_id: int, track: AgentTrack, source: Any) -> None:
        """Keep original media and an actual-consumer clock beside prepared Opus.

        The producer may buffer ahead; only frames consumed by the mixer count.
        The voice thread merely posts a request, never opens media or waits.
        """
        source._original_pcm_track = replace(
            track, archive_segments=[dict(part) for part in track.archive_segments],
            archive_ref=dict(track.archive_ref), attachment_ref=dict(track.attachment_ref),
        )
        source._original_pcm_owner_track = track
        if callable(getattr(source, "original_pcm_position_seconds", None)):
            return  # Rebind a reused warm source without wrapping/counting twice.
        clock_lock, samples = threading.RLock(), 0
        original_read = getattr(source, "read", None)
        start = float(track.start_offset_seconds or 0)

        def position() -> float:
            metadata = getattr(source, "opus_metadata", {})
            preskip = int(metadata.get("pre_skip_samples", 0)) if isinstance(metadata, dict) else 0
            with clock_lock:
                return start + max(0, samples - preskip) / 48000.0

        if callable(original_read):
            def counted_read() -> bytes:
                nonlocal samples
                frame = original_read()
                if frame and getattr(source, "last_read_had_audio", True):
                    with clock_lock:
                        samples += len(frame) // 4  # signed 16-bit stereo at 48 kHz
                return frame
            source.read = counted_read
        source.original_pcm_position_seconds = position
        loop = getattr(self, "_loop", None)
        if loop is None:
            with contextlib.suppress(RuntimeError):
                loop = asyncio.get_running_loop()

        def request(reason: str) -> None:
            if loop is not None and not loop.is_closed():
                with contextlib.suppress(RuntimeError):
                    loop.call_soon_threadsafe(self._request_original_pcm_handoff, guild_id,
                                             source._original_pcm_owner_track, source, reason)
        source.request_original_pcm = request

    def _request_original_pcm_handoff(self, guild_id: int, track: AgentTrack, source: Any, reason: str) -> None:
        if getattr(self, "_audio_prepare_stopping", False):
            return
        st = self.states.get(guild_id)
        mixer = getattr(getattr(st, "player", None), "source", None)
        if (st is None or st.current is not track or st.transport != "direct"
                or getattr(mixer, "music_source", None) is not source
                or not getattr(source, "_prepared_opus_source", False)):
            return
        tasks = getattr(self, "_original_pcm_handoff_tasks", None)
        if tasks is None:
            tasks = self._original_pcm_handoff_tasks = {}
        previous = tasks.get(guild_id)
        if previous is not None and not previous.done():
            if getattr(previous, "_original_pcm_source", source) is not source:
                # A skipped track may still own an off-loop cleanup. Serialize
                # its successor instead of consuming the successor's request.
                previous._original_pcm_retry = (guild_id, track, source, reason)
            return
        task = asyncio.create_task(self._original_pcm_handoff(guild_id, track, source, reason))
        task._original_pcm_source = source
        tasks[guild_id] = task
        task.add_done_callback(consume_task_result)
        task.add_done_callback(lambda finished: remove_owned_task(tasks, guild_id, finished))
        def retry_after_cleanup(finished: Any) -> None:
            retry = getattr(finished, "_original_pcm_retry", None)
            if retry is not None:
                self._request_original_pcm_handoff(*retry)
        task.add_done_callback(retry_after_cleanup)

    def _cancel_original_pcm_handoff(self, guild_id: int) -> None:
        task = getattr(self, "_original_pcm_handoff_tasks", {}).get(guild_id)
        if task is not None and not task.done() and task is not asyncio.current_task():
            task.cancel()

    async def _original_pcm_handoff(self, guild_id: int, track: AgentTrack, source: Any, reason: str) -> bool:
        """Replace prepared decoding with original PCM, keeping healthy audio live.

        Warming/catching up is bounded. Ownership passes only at a complete
        mixer read boundary; cleanup and cancellation cannot close a live source.
        """
        st = self.states.get(guild_id)
        player = getattr(st, "player", None)
        mixer = getattr(player, "source", None)
        if st is None or getattr(mixer, "music_source", None) is not source:
            return False
        token = st.playback_token
        effects = tuple(st.effect_signature()[1:])
        deadline = time.monotonic() + 4.0
        owner = ("original", guild_id, id(source))
        claimed = queued = committed = False
        transfer = {"accepted": False}
        candidate = None
        outcome = "unavailable"
        resumed_at = 0.0

        def valid() -> bool:
            return bool(
                not getattr(self, "_audio_prepare_stopping", False)
                and self.states.get(guild_id) is st and st.current is track
                and st.playback_token == token and st.player is player
                and getattr(player, "source", None) is mixer
                and st.transport == "direct" and not getattr(mixer, "music_ended", False)
                and not getattr(mixer, "_closed", False)
                and tuple(st.effect_signature()[1:]) == effects
            )

        def remaining() -> float:
            left = deadline - time.monotonic()
            if left <= 0:
                raise asyncio.TimeoutError
            return left

        try:
            if not valid() or not callable(getattr(mixer, "queue_music_source_handoff", None)):
                return False
            while not self._claim_audio_prepare_slot(owner):
                if not valid():
                    outcome = "stale"
                    return False
                await asyncio.sleep(min(0.02, remaining()))
            claimed = True
            original = source._original_pcm_track
            selected = replace(original, archive_segments=[dict(part) for part in original.archive_segments],
                               archive_ref=dict(original.archive_ref), attachment_ref=dict(original.attachment_ref))
            selected.start_offset_seconds = source.original_pcm_position_seconds()
            # Existing verified URLs need no additional REST/metadata request.
            if selected.attachment_ref and self._track_stream_needs_refresh(selected):
                refreshed = await asyncio.wait_for(self._resolve_discord_attachment(
                    track_meta=selected.public(), body={"guild_id": guild_id}), remaining())
                selected.stream_url = refreshed.stream_url
                selected.stream_resolved_monotonic = refreshed.stream_resolved_monotonic
                selected.archive_segment_index = refreshed.archive_segment_index
                selected.archive_segments = refreshed.archive_segments
            if not valid() or getattr(mixer, "music_source", None) is not source:
                outcome = "stale"
                return False
            resumed_at = selected.start_offset_seconds = source.original_pcm_position_seconds()
            candidate = self._create_pcm_source(selected, effects=(0, *effects), preparing=True)
            if not callable(getattr(candidate, "wait_ready", None)):
                candidate = BufferedPCMSource(candidate, max_frames=10,
                    stall_seconds=getattr(self, "pcm_buffer_stall_seconds", 12.0))
            await asyncio.wait_for(candidate.wait_ready(timeout=remaining(), min_frames=1), remaining())
            # The current source continues playing while the original warms.
            # Discard only real decoded frames to catch its moving media clock.
            discarded_samples = 0
            while True:
                remaining()
                if not valid() or getattr(mixer, "music_source", None) is not source:
                    outcome = "stale"
                    return False
                position = resumed_at + discarded_samples / 48000.0
                if position >= source.original_pcm_position_seconds() - 0.02:
                    break
                frame = candidate.read()
                if not frame:
                    raise ValueError("original_pcm_eof")
                if getattr(candidate, "last_read_had_audio", True):
                    discarded_samples += len(frame) // 4
                    await asyncio.sleep(0)
                else:
                    await asyncio.sleep(min(0.005, remaining()))
            candidate._original_pcm_handoff = True
            activate = getattr(candidate, "activate_playback", None)
            if callable(activate):
                activate()
            loop = asyncio.get_running_loop()
            result = loop.create_future()

            def completed(accepted: bool) -> None:
                # Record ownership synchronously on the voice thread. A later
                # skip can replace the candidate before its loop callback runs.
                transfer["accepted"] = accepted
                def publish() -> None:
                    if not result.done():
                        result.set_result(accepted)
                if not loop.is_closed():
                    with contextlib.suppress(RuntimeError):
                        loop.call_soon_threadsafe(publish)

            queued = mixer.queue_music_source_handoff(source, candidate, completed, validate=valid)
            if not queued:
                outcome = "stale"
                return False
            committed = await asyncio.wait_for(asyncio.shield(result), remaining())
            outcome = "success" if committed else "stale"
            return committed
        except asyncio.TimeoutError:
            outcome = "timeout"
            return False
        except asyncio.CancelledError:
            outcome = "cancelled"
            raise
        except Exception as exc:
            # No URLs, filenames, cookies or exception text in this telemetry.
            self.log("original_pcm_handoff_error", guild_id=guild_id, error_type=type(exc).__name__)
            return False
        finally:
            if queued and candidate is not None:
                mixer.cancel_music_source_handoff(source, candidate)
            # A voice-thread commit can win a cancellation/timeout race. The
            # candidate is then owned by the mixer even before its callback runs.
            committed = committed or transfer["accepted"] or (
                candidate is not None and getattr(mixer, "music_source", None) is candidate)
            try:
                if committed:
                    outcome = "success"
                    if valid():
                        with contextlib.suppress(Exception):
                            self._arm_music_packet_trace(player, mixer, guild_id=guild_id, track=track,
                                token=token, pcm_source=candidate, output_refresh=True)
                    await self._cleanup_audio_off_loop(source)
                elif candidate is not None:
                    await self._cleanup_audio_off_loop(candidate)
            finally:
                if claimed:
                    self._release_audio_prepare_slot(owner)
                if not committed:
                    reset = getattr(mixer, "reset_original_pcm_request", None)
                    if callable(reset):
                        reset(source)
                self.log("original_pcm_handoff", guild_id=guild_id, outcome=outcome,
                         offset_seconds=round(resumed_at, 3))

    def _opus_direct_preflight(self, guild_id: int, track: AgentTrack) -> dict[str, Any]:
        st = self.states.get(guild_id)
        mode = effective_opus_direct_mode(self, st, guild_id=guild_id)
        if mode != "auto":
            return {"candidate": False, "reason": "disabled"}
        if getattr(track, "_opus_direct_failed", False):
            return {"candidate": False, "reason": "previous_copy_failure"}
        channel_id = getattr(st, "voice_channel_id", 0)
        get_channel = getattr(getattr(self, "client", None), "get_channel", None)
        channel = get_channel(channel_id) if callable(get_channel) else None
        if channel is None:
            channel = getattr(getattr(st, "player", None), "channel", None)
        if not channel or not int(getattr(channel, "bitrate", 0) or 0):
            return {"candidate": False, "reason": "channel_bitrate_unknown"}
        policy = self._audio_output_policy(SimpleNamespace(channel=channel), track, st)
        result = opus_passthrough_eligibility(
            self, track, state=st, output_bitrate_kbps=policy.get("copy_bitrate_kbps", policy["bitrate_kbps"]),
            volume_percent=getattr(st, "volume_percent", self.default_volume_percent),
            effects=st.effect_signature() if st is not None else (),
        )
        has_tts = getattr(self, "_tts_active_for_guild", None)
        if callable(has_tts) and has_tts(guild_id):
            result.update(candidate=False, reason="tts_active")
        return result

    def _create_music_pcm_source(self, guild_id: int, track: AgentTrack, **kwargs: Any) -> Any:
        """Prefer a validated copy reader only for an explicitly opted-in track.

        It still supplies continuously decoded PCM to the same mixer. Startup
        validation failure is retried by _prepare_current_pcm through FFmpeg's
        ordinary PCM decoder, without trusting media metadata as proof.
        """
        if not self._opus_direct_preflight(guild_id, track).get("candidate"):
            try:
                prepared = self._create_prepared_opus_source(guild_id, track, **kwargs)
                if prepared is not None:
                    return prepared
            except Exception as exc:
                track._opus_prepared_failed = True
                self.log("opus_preparation_fallback", guild_id=guild_id, error_type=type(exc).__name__)
            return self._create_pcm_source(track, **kwargs)
        try:
            if len(track.archive_segments) > 1:
                # The stream proof belongs to one immutable manifest revision.
                # A queue refresh may replace metadata while a producer runs.
                archived = replace(track, archive_segments=[dict(part) for part in track.archive_segments],
                                   archive_ref=dict(track.archive_ref), attachment_ref=dict(track.attachment_ref))
                async def resolve_segment(index: int, *, force_refresh: bool = False) -> dict:
                    if (index == 0 and not force_refresh and archived.archive_segment_index == 0
                            and archived.stream_url and not self._track_stream_needs_refresh(archived)):
                        # This URL was verified when the manifest/root attachment
                        # resolved; do not repeat its REST request on first audio.
                        return {**archived.archive_segments[0], "url": archived.stream_url}
                    return await self._resolve_archive_segment(archived, index, force_refresh=force_refresh)

                async def create_fallback(offset: float) -> Any:
                    resumed = replace(archived, start_offset_seconds=offset)
                    return self._create_pcm_source(resumed, **{**kwargs, "preparing": False})

                return ArchiveOpusPCMSource(
                    segments=archived.archive_segments, resolve_segment=resolve_segment,
                    create_fallback=create_fallback, loop=self._loop or asyncio.get_running_loop(),
                    max_frames=kwargs.get("buffer_max_frames") or getattr(self, "pcm_buffer_max_frames", 75),
                    stall_seconds=getattr(self, "pcm_buffer_stall_seconds", 12.0),
                    preparing=kwargs.get("preparing", False),
                    claim_prepare=self._claim_audio_prepare_slot,
                    release_prepare=self._release_audio_prepare_slot,
                    on_fallback=lambda offset: self.log("archive_opus_pcm_fallback", guild_id=guild_id,
                                                       offset_seconds=round(offset, 6)),
                )
            raw = OpusPCMDecoderSource.from_ffmpeg(
                track.stream_url, executable=self.ffmpeg_executable,
                before_options=self._ffmpeg_before_options_for_offset(0),
                audio_stream_index=track.audio_stream_index if track.attachment_ref else -1,
            )
        except Exception as exc:
            track._opus_direct_failed = True
            self.log("opus_direct_prepare_fallback", guild_id=guild_id, error_type=type(exc).__name__)
            try:
                prepared = self._create_prepared_opus_source(guild_id, track, **kwargs)
                if prepared is not None:
                    return prepared
            except Exception as prepared_exc:
                track._opus_prepared_failed = True
                self.log("opus_preparation_fallback", guild_id=guild_id, error_type=type(prepared_exc).__name__)
            return self._create_pcm_source(track, **kwargs)
        try:
            return BufferedOpusPCMSource(
                raw, max_frames=kwargs.get("buffer_max_frames") or getattr(self, "pcm_buffer_max_frames", 75),
                stall_seconds=getattr(self, "pcm_buffer_stall_seconds", 12.0),
            )
        except BaseException:
            raw.cleanup()
            raise

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
                if isinstance(owner, tuple) and owner[0] in {"segment", "original"}:
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
        if not track.archive_segments:
            return ()
        reference = getattr(track, "archive_ref", {}) or {}
        return (getattr(track, "archive_key", ""), tuple(reference.get(key) for key in (
            "guild_id", "channel_id", "message_id", "attachment_id", "manifest_attachment_id")),
            tuple(tuple(segment.get(key) for key in (
            "guild_id", "channel_id", "message_id", "attachment_id", "duration", "offset_seconds",
            "audio_stream_index", "audio_sample_rate", "audio_channels", "audio_codec",
            "sha256", "size_bytes", "filename", "order",
        )) for segment in track.archive_segments))

    async def _prepare_current_pcm(self, guild_id: int, track: AgentTrack, playback_token: int) -> Any:
        st = self.states.setdefault(guild_id, GuildMusicState(guild_id=guild_id))
        effects = (0, st.effect_level("nightcore"), st.effect_level("slowed_reverb"))  # Bassboost atua somente no mixer
        source = self._take_prepared_audio(guild_id, track)
        initial = self._take_initial_audio(guild_id, track, playback_token) if source is None else None
        source = source or initial
        if source is not None:
            copy_candidate = self._opus_direct_preflight(guild_id, track).get("candidate")
            prepared_policy = self._opus_preparation_preflight(guild_id, track)
            replace_source = bool(not getattr(source, "opus_direct_capable", False)
                                  and (copy_candidate or prepared_policy.get("candidate")))
            if getattr(source, "_prepared_opus_source", False):
                metadata = getattr(source, "opus_metadata", {})
                replace_source = bool(not prepared_policy.get("candidate")
                                      or metadata.get("prepared_bitrate_kbps") != prepared_policy["bitrate_kbps"])
            elif not copy_candidate and prepared_policy.get("candidate"):
                replace_source = True
            if replace_source:
                source.cleanup()
                source = None
        source = source or self._create_music_pcm_source(guild_id, track, effects=effects)
        if getattr(source, "_prepared_opus_source", False):
            # Initial warming may have used a provisional AgentTrack before
            # probe/resolve completed. The active track now owns the request.
            self._install_original_pcm_request(guild_id, track, source)

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

        def still_current() -> bool:
            return self.states.get(guild_id) is st and st.playback_token == playback_token

        async def recover_direct_source(failed: Any, error: Exception) -> Any:
            if not still_current():
                raise error
            prepared = bool(getattr(failed, "_prepared_opus_source", False))
            cached = bool(getattr(failed, "_prepared_opus_cache_hit", False))
            if prepared and cached:
                cache = getattr(self, "prepared_opus_cache", None)
                if cache is not None:
                    with contextlib.suppress(Exception):
                        cache.invalidate(getattr(failed, "_prepared_opus_cache_key", None))
            elif prepared:
                track._opus_prepared_failed = True
            else:
                track._opus_direct_failed = True
            self.log("opus_preparation_fallback" if prepared else "opus_direct_prepare_fallback",
                     guild_id=guild_id, error_type=type(error).__name__)
            if not getattr(track, "_opus_prepared_failed", False):
                try:
                    replacement = self._create_prepared_opus_source(
                        guild_id, track, skip_cache=cached, effects=effects)
                    if replacement is not None:
                        return await wait_for_audio(replacement)
                except Exception as replacement_error:
                    if not still_current():
                        raise
                    track._opus_prepared_failed = True
                    self.log("opus_preparation_fallback", guild_id=guild_id,
                             error_type=type(replacement_error).__name__)
            if not still_current():
                raise error
            return await wait_for_audio(self._create_pcm_source(track, effects=effects))

        try:
            result = await wait_for_audio(source)
        except Exception as exc:
            if getattr(source, "opus_direct_capable", False):
                return await recover_direct_source(source, exc)
            # Um decoder antecipado pode perder a corrida com o CDN. Antes de
            # renovar a URL, tente a mesma URL com a trilha já verificada.
            if initial is None or not still_current():
                raise
            self.log("initial_audio_retry_verified", guild_id=guild_id, error=type(exc).__name__)
            return await wait_for_audio(self._create_pcm_source(track, effects=effects))
        reason = getattr(getattr(source, "source", None), "passthrough_reason", "")
        if getattr(source, "opus_direct_capable", False) and reason:
            source.cleanup()
            return await recover_direct_source(source, ValueError("pacotes exigem preparação Opus"))
        return result

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
            source = self._create_music_pcm_source(
                guild_id, track, effects=effects, first_audio_stream=speculative_first_audio,
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
            and getattr(prepared, "archive_identity", ()) == self._archive_audio_identity(track)
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
        if (prepared is not None and prepared.item_id == item_id and prepared.effects == effects and prepared.ready
                and getattr(prepared, "archive_identity", ()) == self._archive_audio_identity(st.queue[0])):
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
                    source = self._create_music_pcm_source(guild_id, track, effects=effects, preparing=True)
                except BaseException:
                    self._release_audio_prepare_slot(("track", guild_id))
                    raise
                owned = AudioPreparado(item_id, track.stream_url, track.start_offset_seconds, source, time.monotonic(),
                                       effects=effects, archive_identity=self._archive_audio_identity(track))
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
