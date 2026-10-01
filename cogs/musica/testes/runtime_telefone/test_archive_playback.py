from __future__ import annotations

import asyncio
import json
import shutil
import struct
import time
import wave
from types import SimpleNamespace

import pytest

from cogs.musica.runtime_telefone.agente.audio_segmentado import ArchiveSegmentedPCMSource, ContinuousArchiveDSPSource, ExactSegmentPCMSource
from cogs.musica.runtime_telefone.agente.buffer_pcm import BufferedPCMSource
from cogs.musica.runtime_telefone.agente.estado import AgentTrack, GuildMusicState
from cogs.musica.runtime_telefone.agente.resolucao import ResolucaoMixin


class FakePCM:
    def __init__(self, frames):
        self.frames = list(frames)
        self.closed = False
        self.last_read_had_audio = False

    def set_max_frames(self, value):
        self.max_frames = value

    async def wait_ready(self, **kwargs):
        pass

    def read(self):
        self.last_read_had_audio = bool(self.frames)
        return self.frames.pop(0) if self.frames else b""

    def cleanup(self):
        self.closed = True

    def audio_buffer_metrics(self):
        return {"buffer_underruns": 0}


@pytest.mark.asyncio
async def test_segment_transition_is_one_logical_source_without_silent_frame():
    made = []

    async def create(index, offset):
        source = FakePCM([bytes([index + 1]) * 3840] * 2)
        made.append((index, offset, source))
        return source

    source = ArchiveSegmentedPCMSource(
        segments=[{"offset_seconds": 0, "duration": .04}, {"offset_seconds": .04, "duration": .04}],
        start_index=0, start_offset=0, create_segment=create, loop=asyncio.get_running_loop(),
    )
    track = AgentTrack(duration=.08, archive_segments=source.segments)
    state = GuildMusicState(1, current=track, queue=[AgentTrack(title="próxima")], playback_token=4)
    try:
        await source.wait_ready(timeout=1)
        await asyncio.sleep(0)
        frames = [source.read() for _ in range(4)]
        assert [frame[0] for frame in frames] == [1, 1, 2, 2]
        assert source.read() == b""
        assert state.current is track and state.playback_token == 4 and len(state.queue) == 1
        assert len(made) == 2 and made[0][2].closed
        assert source.audio_buffer_metrics()["archive_transition_wait_frames"] == 0
    finally:
        source.cleanup()
    assert all(entry[2].closed for entry in made)


@pytest.mark.asyncio
async def test_segment_seek_starts_only_the_selected_part_at_its_local_offset():
    calls = []

    async def create(index, offset):
        calls.append((index, offset))
        return FakePCM([b"x" * 3840])

    segments = [{"offset_seconds": 0, "duration": 10}, {"offset_seconds": 10, "duration": 10}]
    index = ResolucaoMixin._archive_segment_at(segments, 14)
    source = ArchiveSegmentedPCMSource(segments=segments, start_index=index, start_offset=4,
                                       create_segment=create, loop=asyncio.get_running_loop())
    try:
        await source.wait_ready(timeout=1)
        assert calls == [(1, 4)] and source.read() == b"x" * 3840
    finally:
        source.cleanup()


@pytest.mark.asyncio
async def test_short_pcm_blocks_are_joined_across_segments_and_only_final_tail_is_padded():
    pieces = [[b"a" * 3840, b"b" * 2592], [b"c" * 3840, b"d" * 1920]]

    async def create(index, offset):
        return FakePCM(pieces[index])

    durations = [sum(len(frame) for frame in part) / 192000 for part in pieces]
    raw = ArchiveSegmentedPCMSource(segments=[{"offset_seconds": 0, "duration": durations[0]},
                                              {"offset_seconds": durations[0], "duration": durations[1]}],
                                       start_index=0, start_offset=0, create_segment=create,
                                       loop=asyncio.get_running_loop())
    try:
        await raw.wait_ready(timeout=1)
        await asyncio.sleep(0)
        frames = [raw.read() for _ in range(4)]
        expected = b"".join(frame for part in pieces for frame in part)
        assert b"".join(frames) == expected.ljust(4 * 3840, b"\0")
        assert raw.read() == b""
        assert all(len(frame) == 3840 for frame in frames)
    finally:
        raw.cleanup()


@pytest.mark.asyncio
@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg indisponível")
async def test_real_ffmpeg_preserves_all_samples_at_non_20ms_segment_boundaries(tmp_path):
    import discord

    paths, expected, segments, offset = [], b"", [], 0.0
    for index, sample_count in enumerate((48312, 48888, 48600)):
        pcm = struct.pack("<h", (index + 1) * 1000) * (sample_count * 2)
        path = tmp_path / f"part{index}.wav"
        with wave.open(str(path), "wb") as output:
            output.setnchannels(2)
            output.setsampwidth(2)
            output.setframerate(48000)
            output.writeframes(pcm)
        paths.append(path)
        seconds = sample_count / 48000
        segments.append({"offset_seconds": offset, "duration": seconds})
        offset += seconds
        expected += pcm

    async def create(index, offset):
        decoder = discord.FFmpegPCMAudio(str(paths[index]), options="-vn")
        return BufferedPCMSource(ExactSegmentPCMSource(decoder), max_frames=75,
                                 stall_seconds=2, preserve_partial_frames=True)

    raw = ArchiveSegmentedPCMSource(segments=segments, start_index=0, start_offset=0,
                                   create_segment=create, loop=asyncio.get_running_loop(), stall_seconds=2)
    frames = []
    try:
        await raw.wait_ready(timeout=2)
        deadline = asyncio.get_running_loop().time() + 3
        while asyncio.get_running_loop().time() < deadline:
            frame = raw.read()
            if not frame:
                break
            if raw.last_read_had_audio:
                frames.append(frame)
            await asyncio.sleep(.001)
        padded_size = ((len(expected) + 3839) // 3840) * 3840
        assert b"".join(frames) == expected.ljust(padded_size, b"\0")
    finally:
        raw.cleanup()


@pytest.mark.asyncio
async def test_early_segment_eof_is_recoverable_error_instead_of_advancing_queue():
    async def create(index, offset):
        return FakePCM([b"x" * 3840])

    source = ArchiveSegmentedPCMSource(segments=[{"offset_seconds": 0, "duration": 5}], start_index=0,
                                       start_offset=0, create_segment=create, loop=asyncio.get_running_loop())
    try:
        await source.wait_ready(timeout=1)
        source.read()
        with pytest.raises(RuntimeError, match="encerrou antes"):
            source.read()
    finally:
        source.cleanup()


@pytest.mark.asyncio
async def test_cleanup_cancels_inflight_decoder_and_never_confirms_synthetic_audio():
    ready = asyncio.Event()
    made = FakePCM([])

    async def blocked(**kwargs):
        ready.set()
        await asyncio.Event().wait()

    made.wait_ready = blocked

    async def create(index, offset):
        return made

    source = ArchiveSegmentedPCMSource(segments=[{"offset_seconds": 0, "duration": 5}], start_index=0,
                                       start_offset=0, create_segment=create, loop=asyncio.get_running_loop())
    await ready.wait()
    assert source.read() == bytes(3840) and not source.last_read_had_audio
    source.cleanup()
    await asyncio.gather(source._initial_task, return_exceptions=True)
    assert made.closed and source.read() == b""


@pytest.mark.asyncio
@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg indisponível")
async def test_reverb_tail_occurs_once_for_the_whole_multipart_track():
    made = []

    async def create(index, offset):
        frame = struct.pack("<h", 5000) * 1920
        source = FakePCM([frame] * 20)
        made.append(source)
        return source

    loop = asyncio.get_running_loop()
    raw = ArchiveSegmentedPCMSource(segments=[{"offset_seconds": 0, "duration": .4},
                                              {"offset_seconds": .4, "duration": .4}], start_index=0,
                                       start_offset=0, create_segment=create, loop=loop)
    source = ContinuousArchiveDSPSource(raw, executable="ffmpeg", options="-af aecho=0.8:0.9:520:0.3",
                                        max_frames=75, stall_seconds=2, loop=loop)
    frames = []
    try:
        await source.wait_ready(timeout=2)
        deadline = loop.time() + 2
        while loop.time() < deadline:
            frame = source.read()
            if not frame:
                break
            if source.last_read_had_audio:
                frames.append(frame)
            else:
                await asyncio.sleep(.005)
        assert 1.3 <= len(frames) * .02 <= 1.34  # .8 s + uma única cauda de .52 s
        assert raw.audio_buffer_metrics()["archive_transition_wait_frames"] == 0
        assert source.audio_buffer_metrics()["archive_continuous_dsp"]
    finally:
        source.cleanup()
    assert len(made) == 2 and all(candidate.closed for candidate in made)


class ArchiveResolver(ResolucaoMixin):
    def _agent_track_from_metadata(self, metadata, *, body):
        return AgentTrack(title="Teste", start_offset_seconds=float(metadata.get("start_offset_seconds") or 0))


@pytest.mark.asyncio
async def test_multipart_pcm_route_preserves_default_volume_and_mixer_without_global_change():
    from cogs.musica.runtime_telefone.agente.reproducao import ReproducaoMixin

    agent = ReproducaoMixin()
    agent.direct_pcm_volume_enabled = False
    agent.default_volume_percent = 55
    agent.duck_volume_percent = 8
    agent.ffmpeg_before_options = "-nostdin"
    agent.ffmpeg_options = "-vn"
    agent._loop = asyncio.get_running_loop()
    pcm = FakePCM([struct.pack("<h", 1000) * 1920])
    source = agent._build_ffmpeg_source("unused", pcm_source=pcm, source_sample_rate=48000,
                                       force_pcm=True, on_music_end=lambda *args: None)
    try:
        assert source.normal_music_volume == .55 and source.persistent and source.music_source is pcm
        assert agent.direct_pcm_volume_enabled is False
        source.set_music_volume(.7)
        assert source.normal_music_volume == .7
    finally:
        source.cleanup()


@pytest.mark.asyncio
async def test_archive_url_uses_signed_expiry_and_singleflight_beyond_90_seconds():
    key = "a" * 32
    ref = {"guild_id": 1, "forum_id": 20, "channel_id": 40, "message_id": 30, "attachment_id": 9}
    calls = []
    expiry = format(int(time.time() + 600), "x")

    async def get_message(channel, message):
        calls.append((channel, message))
        await asyncio.sleep(0)
        return {"author": {"id": "5"}, "guild_id": "1", "channel_id": "40",
                "embeds": [{"url": f"https://youtu.be/test#music-archive-v7-{key}",
                            "footer": {"text": "Arquivo de músicas"},
                            "description": "🎵 YouTube • 3:55\nOPUS · OGG · 48 kHz • ≈128 kbps"}],
                "attachments": [{"id": "9", "filename": "faixa.ogg", "url":
                                  f"https://cdn.discordapp.com/attachments/40/9/faixa.ogg?ex={expiry}&hm=sign"}]}

    agent = ArchiveResolver()
    agent.client = SimpleNamespace(user=SimpleNamespace(id=5), http=SimpleNamespace(get_message=get_message))
    metadata = {"title": "Teste", "archive_key": key, "archive_ref": ref}
    first, second = await asyncio.gather(*(agent._resolve_archive_attachment(track_meta=metadata, body={"guild_id": 1}) for _ in range(2)))
    assert first.stream_url == second.stream_url and len(calls) == 1
    deadline = next(iter(agent._archive_url_cache.values()))[0]
    assert 500 < deadline - time.monotonic() < 600
    await agent._resolve_archive_attachment(track_meta=metadata, body={"guild_id": 1}, force_refresh=True)
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_multipart_manifest_resolves_selected_segment_and_rejects_wrong_author():
    key = "b" * 32
    base = {"guild_id": 1, "forum_id": 20, "channel_id": 40}
    ref = {**base, "message_id": 30, "attachment_id": 9}
    technical = {"audio_codec": "opus", "audio_sample_rate": 48000, "audio_channels": 2,
                 "audio_stream_index": 0, "audio_abr": 160}
    segments = [{**technical, "order": order, "offset_seconds": order * 10, "duration": 10,
                 "filename": f"part{order}.ogg", "sha256": "c" * 64, "size_bytes": 123,
                 "reference": {**base, "message_id": 30 + order, "attachment_id": 9 + order}}
                for order in range(2)]
    manifest = {"version": 1, "archive_key": key, "duration": 20, "sha256": "c" * 64,
                **technical, "segments": segments}
    expiry = format(int(time.time() + 600), "x")
    wrong_author = False

    async def get_message(channel, message):
        order = message - 30
        raw = {"author": {"id": "6" if wrong_author and order else "5"}, "guild_id": "1", "channel_id": "40",
               "content": f"music-archive-segment-v1:{key}:{order}",
               "attachments": [{"id": str(9 + order), "filename": f"part{order}.ogg", "size": 123,
                                "url": f"https://cdn.discordapp.com/attachments/40/{9 + order}/part{order}.ogg?ex={expiry}&hm=sign"}]}
        if not order:
            raw["embeds"] = [{"url": f"https://youtu.be/test#music-archive-v8-{key}",
                              "footer": {"text": "Arquivo de músicas"},
                              "description": "🎵 YouTube • 0:20\nOPUS · OGG · 48 kHz • ≈160 kbps",
                              "fields": [{"name": "Manifesto 1/1", "value": json.dumps(manifest)}]}]
        return raw

    agent = ArchiveResolver()
    agent.client = SimpleNamespace(user=SimpleNamespace(id=5), http=SimpleNamespace(get_message=get_message))
    track = await agent._resolve_archive_attachment(track_meta={"archive_key": key, "archive_ref": ref,
                                                               "start_offset_seconds": 14}, body={"guild_id": 1})
    assert track.duration == 20 and track.archive_segment_index == 1 and track.audio_channels == 2
    assert track.archive_ref == ref and track.attachment_ref["attachment_id"] == 10
    assert all("url" not in segment for segment in track.public()["archive_segments"])
    wrong_author = True
    with pytest.raises(ValueError, match="não pertence"):
        await agent._resolve_archive_segment(track, 1, force_refresh=True)
