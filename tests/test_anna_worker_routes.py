"""Anna uses the same worker protocols with independent assets and admission."""
from concurrent.futures import ThreadPoolExecutor
import os
import sys
from types import SimpleNamespace

import pytest

from test_miku_worker_routes import voice_row
from test_phone_worker_tts_lifecycle import audio, tts  # noqa: F401


@pytest.fixture
def three_voices(tts, monkeypatch):
    rows = [voice_row(voice) for voice in ('kasane-teto', 'hatsune-miku', 'anna-nyui')]
    tts.deps['teto']['ttv_voices'] = rows
    calls = []

    def factory(voice):
        def synthesize(text, **controls):
            calls.append(dict(voice_id=voice, text=text, **controls))
            data = repr((voice, text, sorted(controls.items()))).encode()
            return dict(audio=data, audio_format='wav', voice_id=voice, voicebank=voice,
                        renderer_version=voice+'-renderer', voicebank_profile='english-cvvc')
        return SimpleNamespace(synthesize=synthesize)

    for name, voice in (('_get_teto_renderer', 'kasane-teto'),
                        ('_get_miku_renderer', 'hatsune-miku'), ('_get_anna_renderer', 'anna-nyui')):
        monkeypatch.setattr(tts.worker, name, lambda voice=voice: factory(voice))
    return SimpleNamespace(**vars(tts), rows=rows, voice_calls=calls)


@pytest.mark.parametrize('language', ['pt-BR', 'en-US'])
@pytest.mark.parametrize('raw', [False, True])
def test_anna_cold_warm_audio_and_controls_do_not_mix_with_miku_or_teto(three_voices, language, raw):
    state = three_voices
    body = dict(engine='teto', text='Olá, Anna.', cache_key='same-client-key',
                ttv_language=language, ttv_speech_rate=1.25, ttv_pitch_semitones=-2)
    result = {}
    for voice in ('kasane-teto', 'hatsune-miku', 'anna-nyui'):
        request = dict(body, ttv_voice_id=voice)
        cold = state.handler._task_tts_agent_synthesize(request, raw_response=raw)
        warm = state.handler._task_tts_agent_synthesize(request, raw_response=raw)
        assert not cold['cache_hit'] and warm['cache_hit']
        assert audio(cold) == audio(warm)
        assert warm['ttv_voice_id'] == voice
        assert warm['ttv_language'] == language
        assert warm['ttv_speech_rate'] == 1.25 and warm['ttv_pitch_semitones'] == -2
        assert warm['teto_backend'] == 'utau'
        result[voice] = warm
    assert len({row['cache_key'] for row in result.values()}) == 3
    assert len({audio(row) for row in result.values()}) == 3
    assert [call['voice_id'] for call in state.voice_calls] == ['kasane-teto', 'hatsune-miku', 'anna-nyui']


@pytest.mark.parametrize('missing_row', [False, True])
def test_anna_missing_assets_reject_before_cache_even_when_other_voices_ready(three_voices, monkeypatch, missing_row):
    state = three_voices
    if missing_row:
        state.rows.pop()
    else:
        state.rows[-1].update(ready=False, available=False, last_error='Anna bank not installed')
    monkeypatch.setattr(state.handler, '_find_tts_cache_file',
                        lambda *_: pytest.fail('unavailable Anna read audio cache'))
    with pytest.raises(RuntimeError, match='Anna Nyui'):
        state.handler._task_tts_agent_synthesize(dict(engine='teto', text='Olá.', ttv_voice_id='anna-nyui'))
    assert not state.voice_calls


def test_anna_does_not_inherit_teto_legacy_advertisement(tts):
    selected = tts.worker._ttv_voice_state(tts.deps['teto'], 'anna-nyui')
    assert not selected['ready'] and not selected['enabled']
    assert 'Anna Nyui' in selected['last_error']


def test_anna_wrong_character_cannot_be_cached(three_voices, monkeypatch):
    state = three_voices
    monkeypatch.setattr(state.worker, '_get_anna_renderer', lambda: SimpleNamespace(
        synthesize=lambda *_a, **_kw: dict(audio=b'wrong-voice', audio_format='wav', voice_id='hatsune-miku')))
    monkeypatch.setattr(state.handler, '_store_tts_agent_standard_cache',
                        lambda **_: pytest.fail('wrong character cached'))
    with pytest.raises(RuntimeError, match='outra vocaloid'):
        state.handler._task_tts_agent_synthesize(dict(engine='teto', text='Olá.', ttv_voice_id='anna-nyui'))


def test_anna_factory_is_separate_thread_safe_and_does_not_mutate_other_voices(tts, monkeypatch, tmp_path):
    for name in list(os.environ):
        if name.startswith('PHONE_WORKER_ANNA_'):
            monkeypatch.delenv(name)
    monkeypatch.setenv('PHONE_WORKER_TETO_VOICEBANK_DIR', '/unchanged/teto')
    monkeypatch.setenv('PHONE_WORKER_TETO_BACKEND', 'worldline-r')
    monkeypatch.setenv('PHONE_WORKER_MIKU_VOICEBANK_DIR', '/unchanged/miku')
    monkeypatch.setenv('PHONE_WORKER_MIKU_ALIAS_SUFFIX', '_D4')
    monkeypatch.setenv('PHONE_WORKER_ANNA_ENABLED', 'true')
    monkeypatch.setenv('PHONE_WORKER_ANNA_VOICEBANK_DIR', str(tmp_path / 'anna'))
    monkeypatch.setenv('PHONE_WORKER_ANNA_RESAMPLER_COMMAND', 'python /installed/esper/resampler.py')
    before = dict(os.environ)
    constructed = []
    monkeypatch.setitem(sys.modules, 'teto_renderer', SimpleNamespace(
        TetoRenderer=lambda **kw: constructed.append(kw) or SimpleNamespace(**kw)))
    with ThreadPoolExecutor(max_workers=8) as pool:
        renderers = list(pool.map(lambda _: tts.worker._get_ttv_renderer('anna-nyui'), range(32)))
    assert len(constructed) == 1 and all(renderer is renderers[0] for renderer in renderers)
    assert os.environ == before
    settings = constructed[0]['environment']
    assert constructed[0]['voice_id'] == 'anna-nyui'
    assert settings['PHONE_WORKER_TETO_ENABLED'] == 'true'
    assert settings['PHONE_WORKER_TETO_VOICEBANK_DIR'] == str(tmp_path/'anna')
    assert settings['PHONE_WORKER_TETO_ENGLISH_VOICEBANK_DIR'] == str(tmp_path/'anna')
    assert settings['PHONE_WORKER_TETO_ALIAS_SUFFIX'] == ''
    assert settings['PHONE_WORKER_TETO_FLAGS'] == ''
    assert settings['PHONE_WORKER_TETO_BASE_PITCH'] == 'C4'
    assert settings['PHONE_WORKER_TETO_RESAMPLER_COMMAND'] == 'python /installed/esper/resampler.py'
    monkeypatch.setenv('PHONE_WORKER_ANNA_FLAGS', 'B12')
    assert tts.worker._get_ttv_renderer('anna-nyui') is not renderers[0]
    assert len(constructed) == 2
    assert os.environ['PHONE_WORKER_TETO_VOICEBANK_DIR'] == '/unchanged/teto'


def test_anna_empty_paths_do_not_discover_another_character(tts, monkeypatch):
    monkeypatch.delenv('PHONE_WORKER_ANNA_VOICEBANK_DIR', raising=False)
    monkeypatch.delenv('PHONE_WORKER_ANNA_ENGLISH_VOICEBANK_DIR', raising=False)
    monkeypatch.setenv('PHONE_WORKER_TETO_ENGLISH_VOICEBANK_DIR', '/installed/teto')
    settings = tts.worker._anna_renderer_environment()
    assert settings['PHONE_WORKER_TETO_VOICEBANK_DIR'] == ''
    assert settings['PHONE_WORKER_TETO_ENGLISH_VOICEBANK_DIR'].endswith('/voicebanks/anna-nyui-diva')
    assert '/installed/teto' not in settings.values()


def test_anna_status_independent_of_invalid_teto_backend(tts, monkeypatch):
    monkeypatch.setenv('PHONE_WORKER_TETO_ENABLED', 'false')
    monkeypatch.setenv('PHONE_WORKER_TETO_BACKEND', 'invalid')
    monkeypatch.setenv('PHONE_WORKER_MIKU_ENABLED', 'false')
    monkeypatch.setenv('PHONE_WORKER_PURURU_ENABLED', 'false')
    monkeypatch.setenv('PHONE_WORKER_ANNA_ENABLED', 'true')
    monkeypatch.setattr(tts.worker, '_get_anna_renderer', lambda: SimpleNamespace(
        status=lambda **_: dict(ok=True, ready=True, available=True, ttv_languages=['pt-BR','en-US']),
        fingerprint=lambda **kw: 'anna-'+kw['reading_language'],
        singing_fingerprint=lambda **kw: 'anna-singing-'+kw['reading_language'],
        synthesize_performance=lambda *_a, **_kw: None, PERFORMANCE_PROTOCOL=2))
    monkeypatch.setattr(tts.worker, '_teto_resource_snapshot', lambda: {'ok':True})
    status = tts.teto_status(force=True)
    rows = {row['id']: row for row in status['ttv_voices']}
    assert set(rows) == {'kasane-teto', 'hatsune-miku', 'anna-nyui', 'pururu-purpura'}
    assert status['ready'] and status['available'] and status['enabled']
    assert rows['anna-nyui']['ready'] and rows['anna-nyui']['backend'] == 'utau'
    assert rows['anna-nyui']['name'] == 'Anna Nyui'
    assert rows['anna-nyui']['language_fingerprints']['pt-BR'] == 'anna-pt-BR'
    assert rows['anna-nyui']['singing_fingerprints']['pt-BR'] == 'anna-singing-pt-BR'
    assert rows['anna-nyui']['ttv_performance_protocol'] == 2
    assert not rows['kasane-teto']['ready'] and not rows['hatsune-miku']['ready']


def test_anna_profile_and_request_defaults_do_not_use_other_character_controls(three_voices, monkeypatch):
    state = three_voices
    monkeypatch.setenv('PHONE_WORKER_TETO_SPEECH_RATE', '.8')
    monkeypatch.setenv('PHONE_WORKER_MIKU_SPEECH_RATE', '1.4')
    monkeypatch.setenv('PHONE_WORKER_ANNA_SPEECH_RATE', '1.2')
    assert state.worker._ttv_request_settings({'ttv_voice_id':'anna-nyui'})['ttv_speech_rate'] == 1.2
    body = dict(engine='teto', text='Olá.', ttv_voice_id='anna-nyui', ttv_language='pt-BR')
    original = state.handler._tts_agent_standard_cache_key(body, engine='teto')
    state.rows[-1]['language_fingerprints']['pt-BR'] = 'updated-anna-bank'
    assert state.handler._tts_agent_standard_cache_key(body, engine='teto') != original
    stable = state.handler._tts_agent_standard_cache_key(body, engine='teto')
    state.rows[0]['language_fingerprints']['pt-BR'] = 'updated-teto-bank'
    assert state.handler._tts_agent_standard_cache_key(body, engine='teto') == stable


def test_anna_personal_voice_prebuild_forwards_all_controls(three_voices, monkeypatch):
    state = three_voices
    state.worker.time.perf_counter = lambda: 100.0
    for name in ('_voice_agent_prune_transfers', '_music_agent_snapshot', '_tts_agent_snapshot'):
        monkeypatch.setattr(state.worker, name, lambda: {})
    monkeypatch.setattr(state.worker, '_voice_agent_snapshot', lambda **_: {})
    monkeypatch.setattr(state.worker, '_voice_agent_set_connection', lambda *_a, **kw: dict(kw))
    forwarded = []
    monkeypatch.setattr(state.handler, '_task_music_agent_proxy', lambda body: forwarded.append(body) or {'ok':True})
    body = dict(guild_id=1, channel_id=2, engine='teto', text='Olá.', ttv_voice_id='anna-nyui',
                ttv_language='pt-BR', ttv_speech_rate=.75, ttv_pitch_semitones=2)
    assert state.handler._task_voice_agent_play_tts(body)['ok']
    assert state.voice_calls[0]['voice_id'] == 'anna-nyui'
    assert forwarded[0]['prebuilt_audio']
    for control in ('ttv_voice_id','ttv_language','ttv_speech_rate','ttv_pitch_semitones'):
        assert forwarded[0][control] == body[control]


@pytest.mark.parametrize('language', ['pt-BR', 'en-US'])
def test_anna_midi_pcm_callback_preserves_voice_and_physical_lease(three_voices, monkeypatch, language):
    state = three_voices
    state.rows[-1]['ttv_performance_protocol'] = 2
    music = dict(version=1, track_index=0, duration_ms=400, lyrics_source='message',
                 notes=[dict(midi_note=64, start_ms=0, duration_ms=400, velocity=100)])
    request = dict(engine='teto', text='a', ttv_voice_id='anna-nyui', ttv_score=music,
                   ttv_language=language, ttv_speech_rate=1.5, ttv_pitch_semitones=4)
    policy = state.worker._phone_worker_tts_policy_module()
    digest = policy.performance_request(request, engine='teto')['hash']
    request['ttv_performance_hash'] = digest
    payload = b'anna-native-wave'
    chunks, controls = [], []

    def perform(text, *, score, segments, emit_pcm, **options):
        assert state.worker._HEAVY_RESOURCE_LOCK.locked()
        assert text == 'a' and score == music and segments is None
        controls.append(options)
        emit_pcm(b'\x01\x00' * 100, {'voice_id':'anna-nyui'})
        return payload, dict(voice_id='anna-nyui', ttv_performance_hash=digest,
                             pitch_offset_semitones=options['pitch_offset_semitones'],
                             speech_rate=options['speech_rate'], reading_language=options['reading_language'])

    monkeypatch.setattr(state.worker, '_get_anna_renderer', lambda: SimpleNamespace(synthesize_performance=perform))
    monkeypatch.setattr(state.handler, '_find_tts_cache_file', lambda *_: pytest.fail('PCM stream used encoded cache'))
    result = state.handler._task_tts_agent_synthesize(request, raw_response=True,
        emit_pcm=lambda data, metadata: chunks.append((data,metadata)))
    assert result['ttv_voice_id'] == 'anna-nyui' and result['ttv_mode'] == 'singing'
    assert result['ttv_speech_rate'] == controls[0]['speech_rate'] == 1
    assert result['ttv_pitch_semitones'] == controls[0]['pitch_offset_semitones'] == 0
    assert controls[0]['reading_language'] == language
    assert chunks == [(b'\x01\x00' * 100, {'voice_id':'anna-nyui'})]
    assert audio(result) == payload
    assert not state.worker._HEAVY_RESOURCE_LOCK.locked()
    assert not state.root.exists()


def test_anna_enabled_with_missing_bank_does_not_advertise_ready(tts, monkeypatch):
    monkeypatch.setenv('PHONE_WORKER_ANNA_ENABLED', 'true')
    monkeypatch.setenv('PHONE_WORKER_TETO_ENABLED', 'false')
    monkeypatch.setenv('PHONE_WORKER_MIKU_ENABLED', 'false')
    monkeypatch.setattr(tts.worker, '_get_anna_renderer', lambda: SimpleNamespace(
        status=lambda **_: dict(ok=False, ready=False, available=False, last_error='missing Anna assets')))
    monkeypatch.setattr(tts.worker, '_teto_resource_snapshot', lambda: {'ok':True})
    status = tts.teto_status(force=True)
    selected = next(row for row in status['ttv_voices'] if row['id'] == 'anna-nyui')
    assert selected['enabled'] and not selected['ready'] and not selected['available']
    assert 'missing Anna assets' in selected['last_error']
    assert not status['ready'] and not status['available']
