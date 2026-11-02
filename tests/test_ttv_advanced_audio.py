"""Personal advanced DSP is shared by speech, singing, previews and music routes."""
from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
import subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from cogs.tts import audio
from cogs.tts.performance import parse_performance_text
from cogs.tts.cd_stream import PCMStreamBuffer
from cogs.musica.runtime_telefone.agente.efeitos import filtros_tts
from cogs.musica.runtime_telefone.agente.tts import TTSMixin


def item(**changes):
    fields = dict(guild_id=1, channel_id=2, author_id=3, text='Olá.', engine='teto',
        voice='pururu-purpura', language='pt-BR', rate='1.0', pitch='+0Hz',
        ttv_voice_id='pururu-purpura', ttv_language='pt-BR', ttv_pitch_semitones='+1.5',
        ttv_speech_rate=1.25)
    fields.update(changes)
    return audio.QueueItem(**fields)


class Probe(audio.TTSAudioMixin):
    def __init__(self):
        self.guild_states = {}
        self.bot = SimpleNamespace(audio_router=None, get_guild=lambda _: None)

    def _tts_agent_route_state(self):
        return {'ttv_voices': [{'id':'pururu-purpura', 'ready':True, 'available':True,
            'languages':['pt-BR'], 'fingerprint':'pururu-native', 'singing_available':True}],
            'ttv_performance_protocol':2}


def score():
    return {'version':1, 'track_index':0, 'duration_ms':1000, 'lyrics_source':'message',
        'notes':[{'midi_note':60, 'start_ms':0, 'duration_ms':1000, 'velocity':100}]}


@pytest.mark.parametrize('kind', ['speech', 'score', 'holds'])
def test_user_effects_are_frozen_and_applied_at_playback_without_changing_native_identity(kind):
    probe = Probe()
    options = {}
    if kind == 'score': options['ttv_score'] = score()
    if kind == 'holds':
        text, segments = parse_performance_text('Olá~')
        options.update(text=text, ttv_segments=segments)
    original = item(**options)
    effected = replace(original, advanced_slowed_level=2, advanced_reverb_level=3)
    key = probe._cache_key(original)
    assert probe._cache_key(effected) == key, 'raw synthesis cache must remain reusable'
    assert (effected.advanced_slowed_level, effected.advanced_reverb_level) == (2,3)
    graph = probe._tts_effect_filter(effected)
    assert graph == filtros_tts(engine='edge', slowed_level=2, reverb_level=3)
    assert graph.count('asetrate=') == graph.count('aecho=') == 1
    native = probe._tts_agent_payload_for_item(effected)
    assert native['ttv_voice_id'] == 'pururu-purpura'
    assert native['ttv_pitch_semitones'] == ('+0.0' if kind == 'score' else '+1.5')
    assert native['ttv_speech_rate'] == (1.0 if kind == 'score' else 1.25)
    assert not any(key.startswith('advanced_') for key in native), 'worker synthesis must stay raw'
    before = graph
    effected.advanced_slowed_level = 1  # separate items retain their personal playback graph
    assert probe._tts_effect_filter(original) == ''
    assert probe._tts_effect_filter(effected) != before


def test_cached_ttv_opus_with_effects_is_transcoded_once_and_neutral_can_copy():
    probe = Probe()
    probe._edge_stream_handle_for_path = lambda _: None
    with patch.object(audio, 'TTS_OPUS_PLAYBACK_ENABLED', True), \
            patch.object(audio, 'TTS_OPUS_PLAYBACK_COPY_CODEC', True), \
            patch.object(audio.discord, 'FFmpegOpusAudio', Mock()) as factory:
        changed = item(advanced_nightcore_level=2, advanced_reverb_level=1)
        source, route = probe._make_discord_tts_source('cached.ogg', item=changed)
        assert route == 'ffmpeg_opus'
        assert 'codec' not in factory.call_args.kwargs
        assert factory.call_args.kwargs['options'].count('-af') == 1
        assert 'asetrate=57600' in factory.call_args.kwargs['options']
        factory.reset_mock()
        source, route = probe._make_discord_tts_source('cached.ogg', item=item())
        assert route == 'ffmpeg_opus_copy'
        assert factory.call_args.kwargs['codec'] == 'copy'


def test_cd_ram_stream_uses_same_effect_graph_once_and_raw_buffer_stays_raw(monkeypatch):
    probe = Probe()
    queued = item(advanced_nightcore_level=3, advanced_reverb_level=2)
    buffer = PCMStreamBuffer(200, prebuffer_ms=0, safety_ms=0)
    buffer.append(b'\1\0\1\0' * 9600)
    buffer.finish()
    probe._cd_stream_for_path = lambda _: SimpleNamespace(buffer=buffer)
    sentinel = object()
    import cogs.tts.pcm_effects as pcm_effects
    wrap = Mock(return_value=sentinel)
    monkeypatch.setattr(pcm_effects, 'filtered_pcm_source', wrap)
    source, route = probe._make_discord_tts_source_for_item('cd.cdpcm', queued)
    assert source is sentinel and route == 'cd_pcm_effects'
    wrap.assert_called_once_with(buffer, audio._DiscordAudioSourceBase, probe._tts_effect_filter(queued))
    assert buffer.buffered_ms == 200
    buffer.cleanup()


def test_slowed_song_timeout_covers_effect_duration_and_reverb_tail():
    probe = Probe()
    song = item(ttv_score=score())
    song.ttv_score['duration_ms'] = 100000
    slowed = replace(song, advanced_slowed_level=3, advanced_reverb_level=3)
    expected = 100 / .76 + audio.TTS_PLAYBACK_TIMEOUT_BASE_SECONDS + .25
    assert probe._estimate_playback_timeout(slowed) >= expected
    assert probe._estimate_playback_timeout(slowed) > probe._estimate_playback_timeout(song)


def test_native_ttv_slow_speech_combines_with_slowed_without_shortening_fast_margin():
    probe = Probe()
    plain = item(text='a' * 180, ttv_speech_rate=1.0)
    slower = replace(plain, ttv_speech_rate=.5, advanced_slowed_level=3, advanced_reverb_level=3)
    expected = (audio.TTS_PLAYBACK_TIMEOUT_BASE_SECONDS +
        180 * audio.TTS_PLAYBACK_TIMEOUT_PER_CHAR_SECONDS / (.5 * .76) + .25)
    assert probe._estimate_playback_timeout(slower) >= expected
    assert probe._estimate_playback_timeout(slower) > probe._estimate_playback_timeout(plain) * 1.5
    faster = replace(plain, ttv_speech_rate=1.5, advanced_nightcore_level=3)
    assert probe._estimate_playback_timeout(faster) == probe._estimate_playback_timeout(plain)


def test_remote_music_overlay_uses_same_ttv_filter_as_local_playback():
    queued = item(advanced_slowed_level=3, advanced_reverb_level=2)
    body = audio.payload_efeitos_tts(0,3,2) | {'engine':'teto'}
    options, graph = TTSMixin()._tts_ffmpeg_options_from_body(body)
    assert graph == Probe()._tts_effect_filter(queued)
    assert options.count('-af') == 1 and options.endswith(graph)


def test_actual_unsupported_engine_keeps_previous_effect_exclusion():
    probe = Probe()
    queued = item(advanced_nightcore_level=3, advanced_reverb_level=2)
    queued._tts_actual_engine = 'android_native'
    assert probe._tts_effect_filter(queued) == ''
    assert probe._tts_effect_timing(queued) == (1.0,0.0)


@pytest.fixture
def source_mp3(tmp_path):
    target = tmp_path / 'source.mp3'
    subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','sine=frequency=440:duration=0.4',
        '-ac','2','-c:a','libmp3lame',str(target)],check=True,capture_output=True,timeout=10)
    return target.read_bytes()


def decoded_seconds(raw):
    result = subprocess.run(['ffmpeg','-v','error','-i','pipe:0','-ar','48000','-ac','2',
        '-f','s16le','pipe:1'],input=raw,capture_output=True,check=True,timeout=10)
    return len(result.stdout)/192000


@pytest.mark.parametrize('preview', ['speech','singing'])
def test_private_mp3_samples_honor_personal_effects_once(preview, source_mp3, tmp_path, monkeypatch):
    probe = Probe()
    generated = []
    probe._ttv_voice_status = lambda _: dict(online=True, ready=True, languages=['pt-BR'],
        supports_personal_rate=True, singing_available=True)

    async def generate(queued):
        generated.append(queued)
        path = tmp_path / f'generated-{len(generated)}.mp3'
        path.write_bytes(source_mp3)
        return str(path)
    probe._generate_tts_agent_worker_file = generate
    monkeypatch.setattr(audio,'score_for_melody',lambda _: score())
    monkeypatch.setattr(audio,'preview_text_for_melody',lambda *_: 'Olá.')
    monkeypatch.setattr(audio,'arrangement_for_melody',lambda *_,**__:None)
    settings = {'ttv_voice_id':'pururu-purpura','ttv_language':'pt-BR',
        'ttv_pitch_semitones':'+1.5','ttv_speech_rate':1.25}
    method = probe._ttv_preview_mp3 if preview=='speech' else probe._cd_preview_mp3
    plain = asyncio.run(method(1,3,settings=settings))
    effected = asyncio.run(method(1,3,settings=settings | {'advanced_slowed_level':2,'advanced_reverb_level':3}))
    assert decoded_seconds(effected) == pytest.approx(decoded_seconds(plain)/.84 + .25, abs=.12)
    assert generated[0].advanced_slowed_level == 0
    assert generated[1].advanced_slowed_level == 2
    assert generated[1].advanced_reverb_level == 3
    assert not list(tmp_path.glob('generated-*.mp3'))


@pytest.mark.parametrize('engine', ['teto','edge','gtts'])
def test_advanced_command_opens_personal_panel_for_supported_modes(engine):
    import ast
    source = ast.parse((Path(audio.__file__).with_name('cog.py')).read_text())
    function = next(node for node in ast.walk(source)
        if isinstance(node, ast.AsyncFunctionDef) and node.name=='_prefix_advanced')
    namespace = {'VisaoEfeitosAvancadosTTS': Mock()}
    module = ast.Module(body=[ast.ImportFrom(module='__future__',names=[ast.alias(name='annotations')],level=0),function],type_ignores=[])
    exec(compile(ast.fix_missing_locations(module),'cog.py','exec'),namespace)
    view = namespace['VisaoEfeitosAvancadosTTS'].return_value
    view.send_for_message = AsyncMock()
    cog = SimpleNamespace(_get_db=lambda:SimpleNamespace(resolve_tts=lambda *_:{'engine':engine}),
        _maybe_await=AsyncMock(side_effect=lambda value:value))
    message = SimpleNamespace(guild=SimpleNamespace(id=1),author=SimpleNamespace(id=3),
        channel=SimpleNamespace(send=AsyncMock()))
    asyncio.run(namespace['_prefix_advanced'](cog,message))
    view.send_for_message.assert_awaited_once_with(message)
    message.channel.send.assert_not_awaited()
