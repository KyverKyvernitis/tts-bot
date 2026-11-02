"""Keep TTV voice identity and personal DSP on legacy music playback routes."""
from __future__ import annotations

import ast
import asyncio
import contextlib
import hashlib
import logging
import os
from pathlib import Path
import shutil
import subprocess
import time
from types import SimpleNamespace
import uuid

import pytest

from cogs.musica.runtime_telefone.agente.efeitos import filtros_tts, payload_efeitos_tts


SOURCE = Path(__file__).resolve().parents[1] / 'cogs/musica/legado/roteador_audio.py'


@pytest.fixture
def router_module():
    tree = ast.parse(SOURCE.read_text())
    source_class = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'AudioRouter')
    names = {'_convert_tts_audio_for_lavalink', '_lavalink_tts_source_paths_for_playback',
             '_tts_transition_filter', 'play_tts'}
    methods = [node for node in source_class.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
               and node.name in names]
    assert {node.name for node in methods} == names
    future = ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0)
    cls = ast.ClassDef(name='Router', bases=[], keywords=[], body=methods, decorator_list=[])
    namespace = dict(asyncio=asyncio, contextlib=contextlib, hashlib=hashlib, logging=logging,
                     os=os, shutil=shutil, time=time, uuid=uuid,
                     logger=logging.getLogger(__name__), payload_efeitos_tts=payload_efeitos_tts,
                     MUSIC_TTS_PREROLL_SILENCE_MS=0, MUSIC_TTS_POSTROLL_SILENCE_MS=0,
                     MUSIC_TTS_FADE_IN_MS=0, MUSIC_TTS_FADE_OUT_MS=0,
                     MUSIC_TTS_MP3_BITRATE='128k', MUSIC_TTS_OPUS_BITRATE='128k',
                     MUSIC_TTS_OPUS_SAMPLE_RATE=48000, MUSIC_TTS_OPUS_CHANNELS=2,
                     MUSIC_TTS_CONVERT_TIMEOUT_SECONDS=10,
                     MUSIC_TTS_AUDIO_FORMAT='opus', MUSIC_TTS_AUDIO_FALLBACK_FORMAT='mp3',
                     MUSIC_TTS_FFMPEG_OPTIONS='-vn', MUSIC_AGENT_TTS_ROUTE_ENABLED=True,
                     MUSIC_AGENT_TTS_TIMEOUT_SECONDS=120, MUSIC_LAVALINK_TTS_TIMEOUT_PADDING_SECONDS=2,
                     MUSIC_TTS_SESSION_CLEANUP_GRACE_SECONDS=2, TTS_VOLUME=1)
    exec(compile(ast.fix_missing_locations(ast.Module(body=[future, cls], type_ignores=[])),
                 str(SOURCE), 'exec'), namespace)
    return namespace


@pytest.fixture
def conversion_router(router_module):
    router = router_module['Router']()
    async def probe(*args):
        return 1.0
    async def no_phone(*args, **kwargs):
        return False
    router._probe_audio_duration_seconds = probe
    router._phone_worker_convert_tts_audio = no_phone
    return router


def decoded_seconds(path):
    result = subprocess.run(['ffmpeg', '-v', 'error', '-i', str(path), '-f', 's16le',
                             '-ar', '48000', '-ac', '2', 'pipe:1'], check=True,
                            capture_output=True, timeout=10)
    assert any(result.stdout)
    return len(result.stdout) / 192000


@pytest.mark.skipif(not shutil.which('ffmpeg'), reason='real FFmpeg required')
def test_real_filter_variants_have_separate_cache_and_expected_audio_duration(conversion_router, tmp_path):
    source = tmp_path / 'voice.wav'
    subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'sine=frequency=440:duration=1',
                    '-ar', '48000', '-ac', '2', str(source)], check=True, capture_output=True, timeout=5)
    raw_before = source.read_bytes()
    async def run():
        night = filtros_tts(engine='teto', nightcore_level=3)
        slow = filtros_tts(engine='teto', slowed_level=3, reverb_level=2)
        assert night and slow
        fast_path = await conversion_router._convert_tts_audio_for_lavalink(str(source), 'opus', audio_filter=night)
        slow_path = await conversion_router._convert_tts_audio_for_lavalink(str(source), 'opus', audio_filter=slow)
        neutral_path = await conversion_router._convert_tts_audio_for_lavalink(str(source), 'opus')
        assert fast_path and slow_path and neutral_path
        assert len({fast_path, slow_path, neutral_path}) == 3
        assert '.fx_' in fast_path and '.fx_' in slow_path and '.fx_' not in neutral_path
        old = Path(fast_path).stat().st_mtime_ns
        assert await conversion_router._convert_tts_audio_for_lavalink(str(source), 'opus', audio_filter=night) == fast_path
        assert Path(fast_path).stat().st_mtime_ns == old
        return fast_path, slow_path, neutral_path
    fast, slow, neutral = asyncio.run(run())
    assert decoded_seconds(fast) == pytest.approx(1 / 1.3, abs=.035)
    assert decoded_seconds(slow) == pytest.approx(1 / .76 + .25, abs=.04)
    assert decoded_seconds(neutral) == pytest.approx(1, abs=.025)
    assert source.read_bytes() == raw_before
    assert not list(tmp_path.glob('*.part.*'))


def test_failed_effect_conversion_cannot_fall_back_to_unfiltered_audio(conversion_router, tmp_path):
    source = tmp_path / 'voice.mp3'
    source.write_bytes(b'original voice audio')
    calls = []
    async def failed(path, fmt, **kwargs):
        calls.append((fmt, kwargs))
        return None
    conversion_router._convert_tts_audio_for_lavalink = failed
    assert asyncio.run(conversion_router._lavalink_tts_source_paths_for_playback(
        str(source), audio_filter='volume=0.5')) == []
    assert calls and all(kwargs == {'audio_filter': 'volume=0.5'} for fmt, kwargs in calls)
    calls.clear()
    assert asyncio.run(conversion_router._lavalink_tts_source_paths_for_playback(str(source))) == [str(source)]
    assert all(not kwargs for fmt, kwargs in calls)


def test_cancelled_conversion_drains_process_and_discards_partial_variant(conversion_router, router_module, tmp_path, monkeypatch):
    source = tmp_path / 'voice.wav'
    source.write_bytes(b'source remains immutable')
    entered = asyncio.Event()
    class Process:
        returncode = None
        killed = False
        async def communicate(self):
            entered.set()
            if not self.killed:
                await asyncio.Event().wait()
            self.returncode = -9
            return b'', b''
        def kill(self):
            self.killed = True
    process = Process()
    async def start(*args, **kwargs):
        Path(args[-1]).write_bytes(b'partial encoded audio')
        return process
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', start)
    async def run():
        task = asyncio.create_task(conversion_router._convert_tts_audio_for_lavalink(
            str(source), 'opus', audio_filter='volume=0.5'))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(run())
    assert process.killed
    assert list(tmp_path.iterdir()) == [source]


def _route(router_module, backend):
    router = router_module['Router']()
    guild = SimpleNamespace(id=1)
    vc = SimpleNamespace(guild=guild, channel=SimpleNamespace(id=2))
    state = SimpleNamespace(current_backend=backend, current_source=None, current=object(),
        last_voice_channel_id=2, voice_lock=asyncio.Lock(), volume=100, current_lavalink_playable=None,
        skip_requested=False, stop_requested=False, lavalink_tts_until=0)
    router.get_state = lambda _: state
    router._is_lavalink_voice_client = lambda _: False
    paths = []
    async def variants(path, **kwargs):
        paths.append((path, kwargs))
        return ['filtered-mix.ogg']
    async def accessible(candidates):
        return candidates
    router._lavalink_tts_source_paths_for_playback = variants
    router._lavalink_tts_candidates_for_path = lambda path, **kwargs: ['https://audio.invalid/' + path]
    router._filter_lavalink_tts_candidates = accessible
    router._mark_lavalink_transition = lambda *args, **kwargs: None
    router._mark_internal_voice_disconnect = lambda *args, **kwargs: None
    return router, guild, vc, paths


def test_remote_agent_ttv_plays_prebuilt_mix_and_never_resynthesizes_bare_text(router_module):
    router, guild, vc, paths = _route(router_module, 'agent')
    commands = []
    async def command(action, **kwargs):
        commands.append((action, kwargs))
        return {'ok': True}
    router_module['_music_agent_command'] = command
    item = SimpleNamespace(engine='teto', text='letra sincronizada', ttv_voice_id='pururu-purpura',
        ttv_score={'notes': [{'midi_note': 60}]}, advanced_slowed_level=3, advanced_reverb_level=2)
    result = asyncio.run(router.play_tts(guild=guild, vc=vc, path='verified-raw-mix.mp3',
        options='-vn -af "aresample=48000,asetrate=36480,aresample=48000"', item=item))
    assert result['ok']
    assert paths == [('verified-raw-mix.mp3', {'audio_filter': 'aresample=48000,asetrate=36480,aresample=48000'})]
    action, payload = commands[0]
    assert action == 'tts' and payload['engine'] == 'prebuilt-url'
    assert payload['audio_url'] == 'https://audio.invalid/filtered-mix.ogg'
    assert 'text' not in payload and not any(key.startswith('advanced_') for key in payload)


def test_lavalink_route_receives_personal_filter_in_prepared_media(router_module):
    router, guild, vc, paths = _route(router_module, 'lavalink')
    plays = []
    async def play(*args, **kwargs):
        plays.append(kwargs)
        return {'ok': True}
    router.backends = SimpleNamespace(play_lavalink_tts=play)
    result = asyncio.run(router.play_tts(guild=guild, vc=vc, path='verified-raw-mix.mp3',
        options='-vn -af "volume=0.8,aecho=0.82:0.58:55:0.2"'))
    assert result['ok']
    assert paths == [('verified-raw-mix.mp3', {'audio_filter': 'volume=0.8,aecho=0.82:0.58:55:0.2'})]
    assert plays[0]['candidates'] == ['https://audio.invalid/filtered-mix.ogg']


def test_transition_and_personal_effects_are_sent_as_one_graph(conversion_router, tmp_path):
    source = tmp_path / 'voice.mp3'
    source.write_bytes(b'original waveform')
    conversion_router._tts_transition_filter = lambda **kwargs: 'afade=t=out:st=0.95:d=0.05'
    requests = []
    async def phone(path, target, **kwargs):
        requests.append(kwargs)
        Path(target).write_bytes(b'complete filtered encoding')
        return True
    conversion_router._phone_worker_convert_tts_audio = phone
    result = asyncio.run(conversion_router._convert_tts_audio_for_lavalink(
        str(source), 'mp3', audio_filter='aresample=48000,asetrate=36480,aresample=48000'))
    assert result != str(source)
    arguments = requests[0]['ffmpeg_args']
    assert arguments.count('-af') == 1
    assert arguments[arguments.index('-af') + 1] == (
        'afade=t=out:st=0.95:d=0.05,aresample=48000,asetrate=36480,aresample=48000')


def test_remote_agent_edge_keeps_existing_synthesis_and_effect_payload(router_module):
    router, guild, vc, paths = _route(router_module, 'agent')
    commands = []
    async def command(action, **kwargs):
        commands.append((action, kwargs))
        return {'ok': True}
    router_module['_music_agent_command'] = command
    item = SimpleNamespace(engine='edge', text='fala normal', advanced_slowed_level=2,
                           advanced_reverb_level=3)
    result = asyncio.run(router.play_tts(guild=guild, vc=vc, path='unused.mp3', item=item))
    assert result['ok'] and not paths
    action, payload = commands[0]
    assert payload['text'] == 'fala normal' and payload['engine'] == 'edge'
    assert payload['advanced_slowed_level'] == 2 and payload['advanced_reverb_level'] == 3
    assert 'audio_url' not in payload
