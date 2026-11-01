"""Native Portuguese Pururu owns her routes, readiness and cache identity."""
from concurrent.futures import ThreadPoolExecutor
import os
import sys
from types import SimpleNamespace

import pytest

from test_miku_worker_routes import voice_row
from test_phone_worker_tts_lifecycle import audio, tts  # noqa: F401


@pytest.fixture
def four_voices(tts, monkeypatch):
    voices = ('kasane-teto', 'hatsune-miku', 'anna-nyui', 'pururu-purpura')
    rows = [voice_row(voice) for voice in voices]
    rows[-1].update(languages=['pt-BR'],
                    language_fingerprints={'pt-BR': 'pururu-pt'},
                    singing_fingerprints={'pt-BR': 'pururu-song'})
    tts.deps['teto']['ttv_voices'] = rows
    calls = []

    def factory(voice):
        def synthesize(text, **controls):
            calls.append(dict(voice_id=voice, text=text, **controls))
            return dict(audio=repr((voice, text, sorted(controls.items()))).encode(),
                        audio_format='wav', voice_id=voice, voicebank=voice)
        return SimpleNamespace(synthesize=synthesize)

    for name, voice in zip(('_get_teto_renderer', '_get_miku_renderer', '_get_anna_renderer',
                             '_get_pururu_renderer'), voices):
        monkeypatch.setattr(tts.worker, name, lambda voice=voice: factory(voice))
    return SimpleNamespace(**vars(tts), rows=rows, voice_calls=calls)


@pytest.mark.parametrize('raw', [False, True])
def test_portuguese_audio_and_controls_do_not_mix_characters_or_read_the_wrong_cache(four_voices, raw):
    state = four_voices
    body = dict(engine='teto', text='Olá, de coração.', cache_key='shared-client-key',
                ttv_language='pt-BR', ttv_speech_rate=.85, ttv_pitch_semitones=1.5)
    rendered = {}
    for row in state.rows:
        voice = row['id']
        request = dict(body, ttv_voice_id=voice)
        cold = state.handler._task_tts_agent_synthesize(request, raw_response=raw)
        warm = state.handler._task_tts_agent_synthesize(request, raw_response=raw)
        assert not cold['cache_hit'] and warm['cache_hit']
        assert audio(cold) == audio(warm)
        assert warm['ttv_voice_id'] == voice and warm['ttv_language'] == 'pt-BR'
        assert warm['ttv_speech_rate'] == .85 and warm['ttv_pitch_semitones'] == 1.5
        rendered[voice] = warm
    assert len({audio(row) for row in rendered.values()}) == 4
    assert len({row['cache_key'] for row in rendered.values()}) == 4
    assert [call['voice_id'] for call in state.voice_calls] == [row['id'] for row in state.rows]


@pytest.mark.parametrize('performance', [False, True])
def test_english_is_rejected_before_status_cache_or_a_native_process(tts, monkeypatch, performance):
    body = dict(engine='teto', text='hello', ttv_voice_id='pururu-purpura', ttv_language='en-US')
    if performance:
        body.update(text='a', ttv_score=dict(version=1, track_index=0, duration_ms=400,
            lyrics_source='message', notes=[dict(midi_note=60, start_ms=0, duration_ms=400, velocity=100)]))
    monkeypatch.setattr(tts.worker, '_teto_status', lambda **_: pytest.fail('status was probed'))
    monkeypatch.setattr(tts.handler, '_find_tts_cache_file', lambda *_: pytest.fail('cache was read'))
    monkeypatch.setattr(tts.worker, '_get_pururu_renderer', lambda: pytest.fail('renderer was created'))
    with pytest.raises(ValueError, match='Pururu Púrpura.*português'):
        tts.handler._task_tts_agent_synthesize(body)


@pytest.mark.parametrize('missing_row', [False, True])
def test_missing_pururu_assets_cannot_borrow_another_character(four_voices, monkeypatch, missing_row):
    state = four_voices
    if missing_row:
        state.rows.pop()
    else:
        state.rows[-1].update(ready=False, available=False, last_error='missing Pururu assets')
    monkeypatch.setattr(state.handler, '_find_tts_cache_file', lambda *_: pytest.fail('cache was read'))
    with pytest.raises(RuntimeError, match='Pururu Púrpura'):
        state.handler._task_tts_agent_synthesize(dict(engine='teto', text='Olá.', ttv_voice_id='pururu-purpura'))
    assert not state.voice_calls


def test_wrong_character_is_rejected_before_storing_pururu_audio(four_voices, monkeypatch):
    state = four_voices
    monkeypatch.setattr(state.worker, '_get_pururu_renderer', lambda: SimpleNamespace(
        synthesize=lambda *_a, **_kw: dict(audio=b'wrong', audio_format='wav', voice_id='kasane-teto')))
    monkeypatch.setattr(state.handler, '_store_tts_agent_standard_cache', lambda **_: pytest.fail('wrong voice cached'))
    with pytest.raises(RuntimeError, match='outra vocaloid'):
        state.handler._task_tts_agent_synthesize(dict(engine='teto', text='Olá.', ttv_voice_id='pururu-purpura'))


def test_factory_uses_an_independent_native_portuguese_bank_and_thread_safe_instance(tts, monkeypatch, tmp_path):
    for name in list(os.environ):
        if name.startswith('PHONE_WORKER_PURURU_'):
            monkeypatch.delenv(name)
    monkeypatch.setenv('PHONE_WORKER_TETO_VOICEBANK_DIR', '/unchanged/teto')
    monkeypatch.setenv('PHONE_WORKER_MIKU_VOICEBANK_DIR', '/unchanged/miku')
    monkeypatch.setenv('PHONE_WORKER_ANNA_VOICEBANK_DIR', '/unchanged/anna')
    monkeypatch.setenv('PHONE_WORKER_PURURU_ENABLED', 'true')
    monkeypatch.setenv('PHONE_WORKER_PURURU_VOICEBANK_DIR', str(tmp_path / 'pururu'))
    monkeypatch.setenv('PHONE_WORKER_PURURU_RESAMPLER_COMMAND', 'python /installed/esper/resampler.py')
    before, constructed = dict(os.environ), []
    monkeypatch.setitem(sys.modules, 'teto_renderer', SimpleNamespace(
        TetoRenderer=lambda **kw: constructed.append(kw) or SimpleNamespace(**kw)))
    with ThreadPoolExecutor(max_workers=8) as pool:
        renderers = list(pool.map(lambda _: tts.worker._get_ttv_renderer('pururu-purpura'), range(32)))
    assert len(constructed) == 1 and all(renderer is renderers[0] for renderer in renderers)
    assert os.environ == before
    values = constructed[0]['environment']
    assert constructed[0]['voice_id'] == 'pururu-purpura'
    assert values['PHONE_WORKER_TETO_VOICEBANK_DIR'] == str(tmp_path / 'pururu')
    assert values['PHONE_WORKER_TETO_ENGLISH_VOICEBANK_DIR'] == ''
    assert values['PHONE_WORKER_TETO_VOICEBANK_MODE'] == 'standard'
    assert values['PHONE_WORKER_TETO_ALIAS_SUFFIX'] == ''
    assert not {'/unchanged/teto', '/unchanged/miku', '/unchanged/anna'} & set(values.values())
    monkeypatch.setenv('PHONE_WORKER_PURURU_FLAGS', 'B12')
    assert tts.worker._get_ttv_renderer('pururu-purpura') is not renderers[0]


def test_health_and_singing_profiles_advertise_only_native_portuguese(tts, monkeypatch):
    for prefix in ('TETO', 'MIKU', 'ANNA'):
        monkeypatch.setenv(f'PHONE_WORKER_{prefix}_ENABLED', 'false')
    monkeypatch.setenv('PHONE_WORKER_PURURU_ENABLED', 'true')
    languages = []

    def singing(**controls):
        languages.append(controls['reading_language'])
        assert controls['reading_language'] == 'pt-BR'
        return 'pururu-native-singing'

    monkeypatch.setattr(tts.worker, '_get_pururu_renderer', lambda: SimpleNamespace(
        status=lambda **_: dict(ok=True, ready=True, available=True, ttv_languages=['pt-BR', 'en-US']),
        fingerprint=lambda **kw: 'pururu-' + kw['reading_language'],
        singing_fingerprint=singing, synthesize_performance=lambda *_a, **_kw: None,
        PERFORMANCE_PROTOCOL=2))
    monkeypatch.setattr(tts.worker, '_teto_resource_snapshot', lambda: {'ok': True})
    state = tts.teto_status(force=True)
    row = next(row for row in state['ttv_voices'] if row['id'] == 'pururu-purpura')
    assert row['ready'] and row['name'] == 'Pururu Púrpura'
    assert row['languages'] == ['pt-BR']
    assert row['language_fingerprints'] == {'pt-BR': 'pururu-pt-BR'}
    assert row['singing_fingerprints'] == {'pt-BR': 'pururu-native-singing'}
    assert languages == ['pt-BR']


def test_native_midi_pcm_keeps_pururu_identity_and_cleans_physical_lease(four_voices, monkeypatch):
    state = four_voices
    state.rows[-1]['ttv_performance_protocol'] = 2
    music = dict(version=1, track_index=0, duration_ms=400, lyrics_source='message',
                 notes=[dict(midi_note=64, start_ms=0, duration_ms=400, velocity=100)])
    request = dict(engine='teto', text='a', ttv_voice_id='pururu-purpura', ttv_score=music,
                   ttv_language='pt-BR', ttv_speech_rate=1.5, ttv_pitch_semitones=4)
    policy = state.worker._phone_worker_tts_policy_module()
    digest = policy.performance_request(request, engine='teto')['hash']
    request['ttv_performance_hash'] = digest
    chunks = []

    def perform(text, *, score, segments, emit_pcm, **options):
        assert state.worker._HEAVY_RESOURCE_LOCK.locked()
        assert options == dict(reading_language='pt-BR', pitch_offset_semitones=0, speech_rate=1,
                               timeout_seconds=120, max_audio_bytes=4096)
        emit_pcm(b'\x01\x00' * 100, {'voice_id': 'pururu-purpura'})
        return b'native-pururu', dict(voice_id='pururu-purpura', ttv_performance_hash=digest,
            pitch_offset_semitones=0, speech_rate=1, reading_language='pt-BR')

    monkeypatch.setattr(state.worker, '_get_pururu_renderer', lambda: SimpleNamespace(synthesize_performance=perform))
    result = state.handler._task_tts_agent_synthesize(request, raw_response=True,
        emit_pcm=lambda data, meta: chunks.append((data, meta)))
    assert result['ttv_voice_id'] == 'pururu-purpura' and result['ttv_mode'] == 'singing'
    assert audio(result) == b'native-pururu'
    assert chunks == [(b'\x01\x00' * 100, {'voice_id': 'pururu-purpura'})]
    assert not state.worker._HEAVY_RESOURCE_LOCK.locked()
