"""Shared native processing never shares another character's recordings."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import shlex
import shutil
import sys
from types import SimpleNamespace

import pytest

from test_phone_worker_tts_lifecycle import tts  # noqa: F401


VOICES = [('MIKU', 'hatsune-miku', '_miku_renderer_environment', '_get_miku_renderer'),
          ('ANNA', 'anna-nyui', '_anna_renderer_environment', '_get_anna_renderer'),
          ('PURURU', 'pururu-purpura', '_pururu_renderer_environment', '_get_pururu_renderer')]


@pytest.fixture
def profile(tts, monkeypatch, tmp_path):
    for key in list(os.environ):
        if key.startswith(('PHONE_WORKER_MIKU_', 'PHONE_WORKER_ANNA_', 'PHONE_WORKER_PURURU_')):
            monkeypatch.delenv(key)
    executable = shutil.which('true')
    if not executable or Path(executable).read_bytes()[:4] != b'\x7fELF':
        pytest.skip('a native ELF fixture is required')
    binary = tmp_path / 'Teto native engine' / 'straycat-rs'
    binary.parent.mkdir()
    shutil.copyfile(executable, binary)
    binary.chmod(0o700)
    command = shlex.quote(str(binary))
    digest = hashlib.sha256(binary.read_bytes()).hexdigest()
    monkeypatch.setenv('PHONE_WORKER_TETO_RESAMPLER_COMMAND', command)
    monkeypatch.setenv('PHONE_WORKER_TETO_NATIVE_RESAMPLER_SHA256', digest)
    return SimpleNamespace(**vars(tts), binary=binary, command=command, digest=digest)


@pytest.mark.parametrize('prefix,voice,builder,factory', VOICES)
def test_unset_voice_command_prefers_native_teto_before_default_native_and_legacy_esper(profile, monkeypatch, tmp_path,
                                                                                       prefix, voice, builder, factory):
    home = tmp_path / 'phone-home'
    (home / 'bin').mkdir(parents=True)
    shutil.copyfile(profile.binary, home / 'bin/straycat-rs')
    (home / 'bin/straycat-rs').chmod(0o700)
    (home / '.esper-utau/bin').mkdir(parents=True)
    (home / '.esper-utau/bin/resampler.py').write_text('# legacy bridge\n')
    monkeypatch.setattr(profile.worker.Path, 'home', classmethod(lambda cls: home))
    values = getattr(profile.worker, builder)()
    assert values['PHONE_WORKER_TETO_RESAMPLER_COMMAND'] == profile.command
    assert values['PHONE_WORKER_TETO_NATIVE_RESAMPLER_SHA256'] == profile.digest
    assert values['PHONE_WORKER_TETO_LENGTH_MODE'] == 'auto'
    monkeypatch.delenv('PHONE_WORKER_TETO_RESAMPLER_COMMAND')
    values = getattr(profile.worker, builder)()
    assert shlex.split(values['PHONE_WORKER_TETO_RESAMPLER_COMMAND']) == [str(home / 'bin/straycat-rs')]
    assert values['PHONE_WORKER_TETO_NATIVE_RESAMPLER_SHA256'] == ''


@pytest.mark.parametrize('prefix,voice,builder,factory', VOICES)
def test_explicit_voice_engine_and_digest_take_priority_without_borrowing_teto_trust(profile, monkeypatch,
                                                                                  prefix, voice, builder, factory):
    explicit = 'python /custom/esper/resampler.py --root /custom/runtime'
    monkeypatch.setenv(f'PHONE_WORKER_{prefix}_RESAMPLER_COMMAND', explicit)
    values = getattr(profile.worker, builder)()
    assert values['PHONE_WORKER_TETO_RESAMPLER_COMMAND'] == explicit
    assert values['PHONE_WORKER_TETO_NATIVE_RESAMPLER_SHA256'] == ''
    monkeypatch.setenv(f'PHONE_WORKER_{prefix}_NATIVE_RESAMPLER_SHA256', 'c' * 64)
    assert getattr(profile.worker, builder)()['PHONE_WORKER_TETO_NATIVE_RESAMPLER_SHA256'] == 'c' * 64


@pytest.mark.parametrize('prefix,voice,builder,factory', VOICES)
def test_only_acoustic_and_bounded_cache_defaults_are_inherited_and_overrides_survive(profile, monkeypatch, tmp_path,
                                                                                    prefix, voice, builder, factory):
    baseline = {'BASE_PITCH': 'D4', 'SPEECH_RATE': '1.15', 'FLAGS': 'B12', 'VELOCITY': '90',
                'MODULATION': '10', 'TEMPO': '125', 'RENDER_THREADS': '3',
                'FRAGMENT_CACHE_MB': '192', 'PCM_CACHE_MB': '96', 'STATUS_CACHE_SECONDS': '20',
                'NATIVE_ANALYSIS_CACHE_MB': '384'}
    for suffix, value in baseline.items():
        monkeypatch.setenv('PHONE_WORKER_TETO_' + suffix, value)
    for suffix, value in {'ENABLED': 'true', 'VOICEBANK_DIR': '/teto-only-bank',
                          'ENGLISH_VOICEBANK_DIR': '/teto-only-english', 'ALIAS_SUFFIX': '_TETO',
                          'FRAGMENT_CACHE_DIR': '/teto-only-cache', 'MIN_ALIASES': '9999',
                          'MAX_CHARACTERS': '9999', 'LENGTH_MODE': 'total'}.items():
        monkeypatch.setenv('PHONE_WORKER_TETO_' + suffix, value)
    own_bank = str(tmp_path / voice)
    monkeypatch.setenv(f'PHONE_WORKER_{prefix}_VOICEBANK_DIR', own_bank)
    values = getattr(profile.worker, builder)()
    for suffix, expected in baseline.items():
        assert values['PHONE_WORKER_TETO_' + suffix] == expected
    assert values['PHONE_WORKER_TETO_ENABLED'] == 'false'
    assert values['PHONE_WORKER_TETO_VOICEBANK_DIR'] == own_bank
    assert values['PHONE_WORKER_TETO_FRAGMENT_CACHE_DIR'] != '/teto-only-cache'
    assert values['PHONE_WORKER_TETO_ALIAS_SUFFIX'] != '_TETO'
    assert values['PHONE_WORKER_TETO_MIN_ALIASES'] != '9999'
    assert values['PHONE_WORKER_TETO_MAX_CHARACTERS'] != '9999'
    assert values['PHONE_WORKER_TETO_LENGTH_MODE'] == 'auto'
    assert not {'/teto-only-bank', '/teto-only-english', '/teto-only-cache'} & set(values.values())
    monkeypatch.setenv(f'PHONE_WORKER_{prefix}_BASE_PITCH', 'F4')
    monkeypatch.setenv(f'PHONE_WORKER_{prefix}_FLAGS', '')
    monkeypatch.setenv(f'PHONE_WORKER_{prefix}_LENGTH_MODE', 'post-consonant')
    overridden = getattr(profile.worker, builder)()
    assert overridden['PHONE_WORKER_TETO_BASE_PITCH'] == 'F4'
    assert overridden['PHONE_WORKER_TETO_FLAGS'] == ''
    assert overridden['PHONE_WORKER_TETO_LENGTH_MODE'] == 'post-consonant'


@pytest.mark.parametrize('prefix,voice,builder,factory', VOICES)
def test_bank_profiles_aliases_and_language_identity_are_not_copied_from_teto(profile, monkeypatch,
                                                                          prefix, voice, builder, factory):
    monkeypatch.setenv('PHONE_WORKER_TETO_VOICEBANK_DIR', '/only-teto')
    monkeypatch.setenv('PHONE_WORKER_TETO_ENGLISH_VOICEBANK_DIR', '/only-teto-english')
    monkeypatch.setenv('PHONE_WORKER_TETO_VOICEBANK_MODE', 'standard')
    monkeypatch.setenv('PHONE_WORKER_TETO_ALIAS_SUFFIX', '_foreign')
    values = getattr(profile.worker, builder)()
    if prefix == 'PURURU':
        assert values['PHONE_WORKER_TETO_VOICEBANK_MODE'] == 'standard'
        assert values['PHONE_WORKER_TETO_ENGLISH_VOICEBANK_DIR'] == ''
        assert values['PHONE_WORKER_TETO_ALIAS_SUFFIX'] == ''
        assert values['PHONE_WORKER_TETO_VOICEBANK_DIR'].endswith('voicebanks/pururu-purpura')
        with pytest.raises(ValueError, match='português'):
            profile.worker._ttv_request_settings({'ttv_voice_id': voice, 'ttv_language': 'en-US'})
    else:
        assert values['PHONE_WORKER_TETO_VOICEBANK_MODE'] == 'english'
        assert values['PHONE_WORKER_TETO_ALIAS_SUFFIX'] == ('_D4' if prefix == 'MIKU' else '')
    assert not {'/only-teto', '/only-teto-english'} & set(values.values())


def test_teto_factory_identity_changes_for_native_trust_and_analysis_budget_but_not_authentication(profile, monkeypatch):
    constructed = []
    monkeypatch.setitem(sys.modules, 'teto_renderer', SimpleNamespace(
        TetoRenderer=lambda **kw: constructed.append(kw) or SimpleNamespace(**kw)))
    first = profile.get_teto_renderer()
    assert profile.get_teto_renderer() is first
    before = profile.worker._teto_renderer_config_fingerprint()
    monkeypatch.setenv('CORE_WORKER_TOKEN', 'pairing-secret-does-not-affect-audio')
    monkeypatch.setenv('PHONE_WORKER_TOKEN', 'http-secret-does-not-affect-audio')
    assert profile.worker._teto_renderer_config_fingerprint() == before
    monkeypatch.setenv('PHONE_WORKER_TETO_NATIVE_RESAMPLER_SHA256', 'd' * 64)
    second = profile.get_teto_renderer()
    assert second is not first and len(constructed) == 2
    monkeypatch.setenv('PHONE_WORKER_TETO_NATIVE_ANALYSIS_CACHE_MB', '384')
    assert profile.get_teto_renderer() is not second and len(constructed) == 3


@pytest.mark.parametrize('prefix,voice,builder,factory', VOICES)
def test_voice_factory_reloads_native_trust_but_ignores_other_character_banks(profile, monkeypatch,
                                                                         prefix, voice, builder, factory):
    constructed = []
    monkeypatch.setitem(sys.modules, 'teto_renderer', SimpleNamespace(
        TetoRenderer=lambda **kw: constructed.append(kw) or SimpleNamespace(**kw)))
    get = getattr(profile.worker, factory)
    first = get()
    monkeypatch.setenv('PHONE_WORKER_TETO_VOICEBANK_DIR', '/changed-teto-only-bank')
    assert get() is first
    monkeypatch.setenv('CORE_WORKER_TOKEN', 'another-token')
    assert get() is first
    monkeypatch.setenv(f'PHONE_WORKER_{prefix}_NATIVE_RESAMPLER_SHA256', 'e' * 64)
    second = get()
    assert second is not first and len(constructed) == 2
    assert constructed[-1]['voice_id'] == voice
    assert constructed[-1]['environment']['PHONE_WORKER_TETO_NATIVE_RESAMPLER_SHA256'] == 'e' * 64


@pytest.mark.parametrize('prefix,voice,builder,factory', VOICES)
def test_request_defaults_match_inherited_profile_and_explicit_controls_keep_priority(profile, monkeypatch,
                                                                                   prefix, voice, builder, factory):
    monkeypatch.setenv('PHONE_WORKER_TETO_SPEECH_RATE', '1.25')
    body = {'ttv_voice_id': voice, 'ttv_language': 'pt-BR'}
    assert profile.worker._ttv_request_settings(body)['ttv_speech_rate'] == 1.25
    monkeypatch.setenv(f'PHONE_WORKER_{prefix}_SPEECH_RATE', '.85')
    assert profile.worker._ttv_request_settings(body)['ttv_speech_rate'] == .85
    assert profile.worker._ttv_request_settings({**body, 'ttv_speech_rate': 1.1})['ttv_speech_rate'] == 1.1


def test_aggregate_voice_status_keeps_actual_motor_and_duration_diagnostics(profile, monkeypatch):
    for prefix in ('TETO', 'MIKU', 'ANNA', 'PURURU'):
        monkeypatch.setenv(f'PHONE_WORKER_{prefix}_ENABLED', 'true')
    monkeypatch.setattr(profile.worker, '_get_ttv_renderer', lambda voice: SimpleNamespace(
        status=lambda **_: {'ready': True, 'available': True, 'ttv_languages': ['pt-BR', 'en-US'],
                           'resampler': '/shared/native/straycat-rs', 'length_mode': 'post-consonant',
                           'render_threads': 3, 'native_resampler_verified': True,
                           'voicebank_profile': 'portuguese-cvvc' if voice == 'pururu-purpura' else 'english-cvvc'},
        fingerprint=lambda **_: voice + '-fingerprint',
        singing_fingerprint=lambda **_: voice + '-singing',
        synthesize_performance=lambda *_a, **_kw: None, PERFORMANCE_PROTOCOL=2))
    monkeypatch.setattr(profile.worker, '_teto_resource_snapshot', lambda: {'ok': True})
    state = profile.teto_status(force=True)
    assert len(state['ttv_voices']) == 4
    for row in state['ttv_voices']:
        assert row['resampler'] == '/shared/native/straycat-rs'
        assert row['length_mode'] == 'post-consonant'
        assert row['render_threads'] == 3
        assert row['native_resampler_verified'] is True
        selected = profile.worker._ttv_voice_state(state, row['id'])
        assert selected['resampler'] == row['resampler']
        assert selected['length_mode'] == row['length_mode']
        assert selected['render_threads'] == row['render_threads']
        assert selected['native_resampler_verified'] is True
    native = next(row for row in state['ttv_voices'] if row['id'] == 'pururu-purpura')
    assert native['languages'] == ['pt-BR'] and native['voicebank_profile'] == 'portuguese-cvvc'
