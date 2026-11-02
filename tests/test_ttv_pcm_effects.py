"""Progressive mixes retain playback DSP, bounded cleanup and frame timing."""
from __future__ import annotations

import array
from concurrent.futures import ThreadPoolExecutor
import math
import shutil
import threading

import pytest

from cogs.tts.cd_stream import PCMStreamBuffer
from cogs.tts.pcm_effects import filtered_pcm_source


class AudioSource:
    pass


def tone(milliseconds):
    values = array.array('h')
    for sample in range(round(milliseconds * 48)):
        value = round(4000 * math.sin(2 * math.pi * 440 * sample / 48000))
        values.extend((value, value))
    return values.tobytes()


def drain(source):
    frames = []
    while True:
        frame = source.read()
        if not frame:
            return b''.join(frames)
        assert len(frame) == 3840
        frames.append(frame)


def test_neutral_progressive_source_preserves_pcm_without_starting_ffmpeg(monkeypatch):
    stream = PCMStreamBuffer(200)
    pcm = tone(200)
    stream.append(pcm)
    stream.finish()
    monkeypatch.setattr('cogs.tts.pcm_effects.subprocess.Popen',
                        lambda *args, **kwargs: pytest.fail('neutral PCM must not start FFmpeg'))
    source = filtered_pcm_source(stream, AudioSource, '')
    try:
        assert isinstance(source, AudioSource)
        assert source.is_opus() is False
        assert drain(source) == pcm
    finally:
        source.cleanup()
    assert stream.closed


@pytest.mark.skipif(not shutil.which('ffmpeg'), reason='FFmpeg is required for real DSP')
@pytest.mark.parametrize('graph,seconds', [
    ('aresample=48000,asetrate=36480,aresample=48000', .2 / .76),
    ('aresample=48000,asetrate=62400,aresample=48000', .2 / 1.3),
    ('aecho=0.82:0.58:55|120|250:0.20|0.12|0.07', .45),
])
def test_real_dsp_changes_complete_mix_duration_and_pads_only_final_frame(graph, seconds):
    stream = PCMStreamBuffer(200)
    stream.append(tone(200))
    stream.finish()
    source = filtered_pcm_source(stream, AudioSource, graph)
    try:
        assert isinstance(source, AudioSource)
        actual = drain(source)
        assert any(actual)
        assert len(actual) / 192000 == pytest.approx(seconds, abs=.021)
        assert source.read() == b''
        assert source._process.poll() == 0
    finally:
        source.cleanup()
    assert not source._feeder.is_alive()


@pytest.mark.skipif(not shutil.which('ffmpeg'), reason='FFmpeg is required for real DSP')
def test_effects_deliver_audio_before_native_production_finishes():
    stream = PCMStreamBuffer(1000)
    stream.append(tone(200))
    source = filtered_pcm_source(stream, AudioSource, 'volume=0.8')
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            first = pool.submit(source.read)
            assert len(first.result(timeout=3)) == 3840
            assert not stream.done
        stream.append(tone(800))
        stream.finish()
        assert drain(source)
    finally:
        source.cleanup()


@pytest.mark.skipif(not shutil.which('ffmpeg'), reason='FFmpeg is required for real DSP')
def test_cleanup_unblocks_pcm_reader_and_feeder_and_reaps_ffmpeg():
    stream = PCMStreamBuffer(1000)
    source = filtered_pcm_source(stream, AudioSource, 'volume=0.8')
    with ThreadPoolExecutor(max_workers=1) as pool:
        reader = pool.submit(source.read)
        source.cleanup()
        assert reader.result(timeout=3) == b''
    source.cleanup()
    assert stream.closed
    assert source._process.poll() is not None
    assert not source._feeder.is_alive()


@pytest.mark.skipif(not shutil.which('ffmpeg'), reason='FFmpeg is required for real DSP')
def test_producer_error_reaches_audio_player_instead_of_silent_success():
    stream = PCMStreamBuffer(200)
    stream.finish(RuntimeError('native producer failed'))
    source = filtered_pcm_source(stream, AudioSource, 'volume=0.8')
    try:
        with pytest.raises(RuntimeError, match='native producer failed'):
            drain(source)
    finally:
        source.cleanup()


@pytest.mark.skipif(not shutil.which('ffmpeg'), reason='FFmpeg is required for real DSP')
def test_invalid_filter_surfaces_playback_failure():
    stream = PCMStreamBuffer(200)
    stream.append(tone(200))
    stream.finish()
    source = filtered_pcm_source(stream, AudioSource, 'this_filter_does_not_exist')
    try:
        with pytest.raises((RuntimeError, BrokenPipeError)):
            drain(source)
    finally:
        source.cleanup()


def test_admission_accounts_for_effect_consumption_speed():
    streams = [PCMStreamBuffer(10000, prebuffer_ms=0, safety_ms=0, playback_rate=rate)
               for rate in (.76, 1.0, 1.3)]
    for stream in streams:
        stream.observe_generation(1000, 900, 1000)
        stream.append(tone(1000))
    slow, neutral, nightcore = streams
    assert slow.required_prebuffer_ms < neutral.required_prebuffer_ms < nightcore.required_prebuffer_ms
    assert slow.generation_ratio == pytest.approx(.9 * .76)
    assert nightcore.generation_ratio == pytest.approx(.9 * 1.3)
    assert nightcore.admission_snapshot()['playback_rate'] == 1.3


@pytest.mark.parametrize('rate', [0, -1, float('nan'), float('inf'), True, '1.0'])
def test_invalid_playback_rate_is_rejected(rate):
    with pytest.raises(ValueError, match='playback_rate'):
        PCMStreamBuffer(1000, playback_rate=rate)
