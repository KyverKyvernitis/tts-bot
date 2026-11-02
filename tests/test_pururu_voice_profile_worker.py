"""Pururu's opt-in acoustic profile stays independent from Teto and request effects."""
from __future__ import annotations

import hashlib
import io
import json
import os
import sys
from types import SimpleNamespace

import pytest

from test_miku_worker_routes import voice_row
from test_phone_worker_tts_lifecycle import audio, tts  # noqa: F401

PROFILE = 'pururu-ptbr-v1'
ACOUSTICS = {'BASE_PITCH': 'C4', 'SPEECH_RATE': '1.0', 'FLAGS': 'g5 B50',
             'VELOCITY': '100', 'MODULATION': '0', 'TEMPO': '125'}


@pytest.fixture
def worker(tts, monkeypatch):
    for key in list(os.environ):
        if key.startswith('PHONE_WORKER_PURURU_'):
            monkeypatch.delenv(key)
    return tts.worker


def test_absent_or_explicit_legacy_profile_retains_the_old_environment(worker, monkeypatch):
    inherited = {'BASE_PITCH': 'F4', 'SPEECH_RATE': '1.25', 'FLAGS': 'g-8 B20',
                 'VELOCITY': '83', 'MODULATION': '11', 'TEMPO': '132'}
    for suffix, value in inherited.items():
        monkeypatch.setenv('PHONE_WORKER_TETO_' + suffix, value)
    absent = worker._pururu_renderer_environment()
    assert 'PHONE_WORKER_TETO_VOICE_PROFILE' not in absent
    for suffix, expected in inherited.items():
        assert absent['PHONE_WORKER_TETO_' + suffix] == expected
    monkeypatch.setenv('PHONE_WORKER_PURURU_PROFILE', 'legacy')
    assert worker._pururu_renderer_environment() == absent
    assert worker._pururu_voice_profile() == 'legacy'


def test_active_profile_owns_acoustics_but_inherits_resource_limits(worker, monkeypatch):
    for suffix in ACOUSTICS:
        monkeypatch.setenv('PHONE_WORKER_TETO_' + suffix, 'foreign-Teto-value')
    monkeypatch.setenv('PHONE_WORKER_TETO_RENDER_THREADS', '3')
    monkeypatch.setenv('PHONE_WORKER_TETO_PCM_CACHE_MB', '192')
    monkeypatch.setenv('PHONE_WORKER_PURURU_PROFILE', PROFILE)
    monkeypatch.setenv('PHONE_WORKER_PURURU_VOICEBANK_DIR', '/native/Púrpura')
    monkeypatch.setenv('PHONE_WORKER_PURURU_RESAMPLER_COMMAND', '/native/straycat-rs')
    monkeypatch.setenv('PHONE_WORKER_PURURU_NATIVE_RESAMPLER_SHA256', 'f' * 64)
    before = dict(os.environ)
    environment = worker._pururu_renderer_environment()
    assert dict(os.environ) == before
    for suffix, value in ACOUSTICS.items():
        assert environment['PHONE_WORKER_TETO_' + suffix] == value
    assert environment['PHONE_WORKER_TETO_VOICE_PROFILE'] == PROFILE
    assert environment['PHONE_WORKER_TETO_RENDER_THREADS'] == '3'
    assert environment['PHONE_WORKER_TETO_PCM_CACHE_MB'] == '192'
    assert environment['PHONE_WORKER_TETO_VOICEBANK_DIR'] == '/native/Púrpura'
    assert environment['PHONE_WORKER_TETO_ENGLISH_VOICEBANK_DIR'] == ''
    assert environment['PHONE_WORKER_TETO_RESAMPLER_COMMAND'] == '/native/straycat-rs'
    assert environment['PHONE_WORKER_TETO_NATIVE_RESAMPLER_SHA256'] == 'f' * 64


def test_explicit_pururu_tuning_overrides_the_profile_even_for_empty_flags(worker, monkeypatch):
    monkeypatch.setenv('PHONE_WORKER_PURURU_PROFILE', PROFILE)
    explicit = dict(BASE_PITCH='D4', SPEECH_RATE='.85', FLAGS='', VELOCITY='150',
                    MODULATION='3', TEMPO='130')
    for suffix, value in explicit.items():
        monkeypatch.setenv('PHONE_WORKER_PURURU_' + suffix, value)
    environment = worker._pururu_renderer_environment()
    for suffix, value in explicit.items():
        assert environment['PHONE_WORKER_TETO_' + suffix] == value
    monkeypatch.setenv('PHONE_WORKER_PURURU_MODULATION', ' ')
    monkeypatch.setenv('PHONE_WORKER_TETO_MODULATION', '99')
    assert worker._pururu_renderer_environment()['PHONE_WORKER_TETO_MODULATION'] == '0'


def test_unknown_profile_is_rejected_without_breaking_other_voice_health(worker, monkeypatch):
    monkeypatch.setenv('PHONE_WORKER_PURURU_PROFILE', 'unverified-profile')
    monkeypatch.setenv('PHONE_WORKER_PURURU_ENABLED', 'true')
    with pytest.raises(ValueError, match='perfil da Pururu desconhecido'):
        worker._pururu_renderer_environment()
    status = worker._ttv_single_voice_status('pururu-purpura')
    assert not status['ready'] and not status['available']
    assert 'perfil da Pururu desconhecido' in status['last_error']
    # Merely configuring another character must never consult Pururu's marker.
    assert worker._miku_renderer_environment()['PHONE_WORKER_TETO_ALIAS_SUFFIX'] == '_D4'
    assert 'PHONE_WORKER_TETO_VOICE_PROFILE' not in worker._anna_renderer_environment()


def test_activation_reloads_only_pururu_factory_and_rollback_restores_legacy(worker, monkeypatch):
    constructed = []
    monkeypatch.setitem(sys.modules, 'teto_renderer', SimpleNamespace(
        TetoRenderer=lambda **kwargs: constructed.append(kwargs) or SimpleNamespace(**kwargs)))
    old = worker._get_pururu_renderer()
    miku = worker._get_miku_renderer()
    anna = worker._get_anna_renderer()
    monkeypatch.setenv('PHONE_WORKER_PURURU_PROFILE', PROFILE)
    new = worker._get_pururu_renderer()
    assert new is not old and worker._get_pururu_renderer() is new
    assert worker._get_miku_renderer() is miku and worker._get_anna_renderer() is anna
    assert new.environment['PHONE_WORKER_TETO_VOICE_PROFILE'] == PROFILE
    monkeypatch.setenv('PHONE_WORKER_PURURU_PROFILE', 'legacy')
    restored = worker._get_pururu_renderer()
    assert restored is not new
    assert 'PHONE_WORKER_TETO_VOICE_PROFILE' not in restored.environment


def test_bad_pururu_profile_leaves_other_character_health_available(tts, worker, monkeypatch):
    monkeypatch.setenv('PHONE_WORKER_PURURU_PROFILE', 'unverified-profile')
    for prefix in ('TETO', 'MIKU', 'ANNA', 'PURURU'):
        monkeypatch.setenv(f'PHONE_WORKER_{prefix}_ENABLED', 'true')
    checked = []

    def renderer(voice):
        checked.append(voice)
        return SimpleNamespace(status=lambda **_: {'ready': True, 'available': True,
            'ttv_languages': ['pt-BR', 'en-US']})

    monkeypatch.setattr(worker, '_get_ttv_renderer', renderer)
    monkeypatch.setattr(worker, '_teto_resource_snapshot', lambda: {'ok': True})
    state = tts.teto_status()
    rows = {row['id']: row for row in state['ttv_voices']}
    assert state['ready']
    assert not rows['pururu-purpura']['ready']
    assert 'perfil da Pururu desconhecido' in rows['pururu-purpura']['last_error']
    assert checked == ['kasane-teto', 'hatsune-miku', 'anna-nyui']
    assert all(rows[voice]['ready'] for voice in checked)


def test_configuration_attestation_changes_for_profile_and_never_for_tokens(worker, monkeypatch):
    before = worker._ttv_renderer_configuration_digest()
    monkeypatch.setenv('PHONE_WORKER_PURURU_PROFILE', PROFILE)
    active = worker._ttv_renderer_configuration_digest()
    assert active != before
    monkeypatch.setenv('PHONE_WORKER_TOKEN', 'do-not-share-http-token')
    monkeypatch.setenv('CORE_WORKER_TOKEN', 'do-not-share-core-token')
    assert worker._ttv_renderer_configuration_digest() == active


def test_request_defaults_follow_own_profile_and_explicit_personal_controls_survive(worker, monkeypatch):
    body = dict(ttv_voice_id='pururu-purpura', ttv_language='pt-BR')
    monkeypatch.setenv('PHONE_WORKER_TETO_SPEECH_RATE', '1.25')
    assert worker._ttv_request_settings(body)['ttv_speech_rate'] == 1.25
    monkeypatch.setenv('PHONE_WORKER_PURURU_PROFILE', PROFILE)
    assert worker._ttv_request_settings(body)['ttv_speech_rate'] == 1.0
    personal = worker._ttv_request_settings(body | {'ttv_speech_rate': .85, 'ttv_pitch_semitones': 1.5})
    assert personal['ttv_speech_rate'] == .85 and personal['ttv_pitch_semitones'] == 1.5
    monkeypatch.setenv('PHONE_WORKER_PURURU_SPEECH_RATE', '1.15')
    assert worker._ttv_request_settings(body)['ttv_speech_rate'] == 1.15
    with pytest.raises(ValueError, match='português'):
        worker._ttv_request_settings(body | {'ttv_language': 'en-US'})


@pytest.mark.parametrize('marker', ['', 'legacy', PROFILE])
def test_health_reports_only_pururu_profile_and_keeps_native_portuguese(tts, worker, monkeypatch, marker):
    monkeypatch.setenv('PHONE_WORKER_PURURU_PROFILE', marker)
    for prefix in ('TETO', 'MIKU', 'ANNA', 'PURURU'):
        monkeypatch.setenv(f'PHONE_WORKER_{prefix}_ENABLED', 'true')
    monkeypatch.setattr(worker, '_get_ttv_renderer', lambda voice: SimpleNamespace(
        status=lambda **_: {'ready': True, 'available': True, 'ttv_languages': ['pt-BR', 'en-US']},
        fingerprint=lambda **_: voice + '-speech', singing_fingerprint=lambda **_: voice + '-song'))
    monkeypatch.setattr(worker, '_teto_resource_snapshot', lambda: {'ok': True})
    state = tts.teto_status()
    for row in state['ttv_voices']:
        if row['id'] == 'pururu-purpura':
            assert row['voice_profile'] == (marker or 'legacy')
            assert row['languages'] == ['pt-BR']
        else:
            assert 'voice_profile' not in row


@pytest.fixture
def synthesis(tts, worker, monkeypatch):
    row = voice_row('pururu-purpura')
    row.update(languages=['pt-BR'], language_fingerprints={'pt-BR': 'fixed-test-bank'})
    tts.deps['teto']['ttv_voices'] = [row]
    calls = []

    def synthesize(text, **controls):
        marker = worker._pururu_voice_profile()
        calls.append((text, dict(controls), marker))
        return {'audio': ('audio:' + marker).encode(), 'audio_format': 'wav',
                'voice_id': 'pururu-purpura', 'voice_profile': marker,
                'pitch_offset_semitones': controls['pitch_offset_semitones'],
                'speech_rate': controls.get('speech_rate') or 1.0,
                'reading_language': controls.get('reading_language', 'pt-BR')}
    monkeypatch.setattr(worker, '_get_pururu_renderer', lambda: SimpleNamespace(synthesize=synthesize))
    return SimpleNamespace(**vars(tts), row=row, profile_calls=calls)


@pytest.mark.parametrize('raw', [False, True])
def test_actual_profile_survives_cold_warm_and_rollback_without_cross_profile_cache(synthesis, worker, monkeypatch, raw):
    body = dict(engine='teto', text='Olá, de coração.', cache_key='same-client-key',
                ttv_voice_id='pururu-purpura', ttv_language='pt-BR',
                ttv_pitch_semitones=1.5, ttv_speech_rate=.85,
                advanced_slowed_level=3, advanced_reverb_level=2)
    cold = synthesis.handler._task_tts_agent_synthesize(body, raw_response=raw)
    warm = synthesis.handler._task_tts_agent_synthesize(body, raw_response=raw)
    assert cold['voice_profile'] == warm['voice_profile'] == 'legacy'
    assert audio(cold) == audio(warm) == b'audio:legacy' and warm['cache_hit']
    monkeypatch.setenv('PHONE_WORKER_PURURU_PROFILE', PROFILE)
    active = synthesis.handler._task_tts_agent_synthesize(body, raw_response=raw)
    active_warm = synthesis.handler._task_tts_agent_synthesize(body, raw_response=raw)
    assert active['voice_profile'] == active_warm['voice_profile'] == PROFILE
    assert active['cache_key'] != cold['cache_key']
    assert audio(active) == audio(active_warm) == b'audio:' + PROFILE.encode()
    assert not active['cache_hit'] and active_warm['cache_hit']
    assert active['ttv_pitch_semitones'] == 1.5 and active['ttv_speech_rate'] == .85
    monkeypatch.setenv('PHONE_WORKER_PURURU_PROFILE', 'legacy')
    restored = synthesis.handler._task_tts_agent_synthesize(body, raw_response=raw)
    assert restored['voice_profile'] == 'legacy' and restored['cache_hit']
    assert audio(restored) == audio(cold)
    assert len(synthesis.profile_calls) == 2
    assert all('advanced_slowed_level' not in options for _, options, _ in synthesis.profile_calls)


@pytest.mark.parametrize('reported', [None, 'legacy', 'another-profile'])
def test_unconfirmed_profile_is_rejected_before_cache_store_or_fallback(synthesis, worker, monkeypatch, reported):
    monkeypatch.setenv('PHONE_WORKER_PURURU_PROFILE', PROFILE)
    calls = []

    def synthesize(text, **controls):
        calls.append(text)
        metadata = dict(audio=b'wrong profile', audio_format='wav', voice_id='pururu-purpura')
        if reported is not None:
            metadata['voice_profile'] = reported
        return metadata
    monkeypatch.setattr(worker, '_get_pururu_renderer', lambda: SimpleNamespace(synthesize=synthesize))
    monkeypatch.setattr(synthesis.handler, '_store_tts_agent_standard_cache',
                        lambda **_: pytest.fail('unconfirmed profile was cached'))
    with pytest.raises(RuntimeError, match='outro perfil da Pururu'):
        synthesis.handler._task_tts_agent_synthesize(dict(engine='teto', text='Olá.', ttv_voice_id='pururu-purpura'))
    assert calls == ['Olá.']


def test_raw_audio_headers_report_actual_voice_profile(worker):
    headers = {}
    handler = SimpleNamespace(send_response=lambda _: None,
        send_header=lambda key, value: headers.update({key: value}), end_headers=lambda: None,
        wfile=io.BytesIO())
    worker._audio_response(handler, 200, b'RIFF-pcm', {'engine': 'teto', 'audio_format': 'wav',
        'ttv_voice_id': 'pururu-purpura', 'voice_profile': PROFILE})
    assert headers['X-Core-Worker-TTV-Voice-Profile'] == PROFILE
    assert handler.wfile.getvalue() == b'RIFF-pcm'
