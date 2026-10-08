"""VOICEPEAK selection, failure and cache behavior through the real phone facade."""
import sys
from types import SimpleNamespace

import pytest

from test_phone_worker_tts_lifecycle import audio, tts  # noqa: F401


def test_backend_switch_replaces_singleton_and_never_uses_utau_for_voicepeak(tts, monkeypatch):
    created = []

    def utau(**kwargs):
        created.append(('utau', kwargs))
        return SimpleNamespace(backend='utau', **kwargs)

    def voicepeak(**kwargs):
        created.append(('voicepeak', kwargs))
        return SimpleNamespace(backend='voicepeak', **kwargs)

    monkeypatch.setitem(sys.modules, 'teto_renderer', SimpleNamespace(TetoRenderer=utau))
    monkeypatch.setattr(tts.worker, '_phone_worker_tts_providers_module',
                        lambda: SimpleNamespace(VoicepeakRenderer=voicepeak))
    first = tts.get_teto_renderer()
    assert first.resource_guard is tts.worker._teto_resource_snapshot
    monkeypatch.setenv('PHONE_WORKER_TETO_BACKEND', 'voicepeak')
    monkeypatch.setattr(tts.worker, '_teto_status', lambda **kwargs: dict(tts.deps['teto']))
    second = tts.get_teto_renderer()
    assert second is not first and second.backend == 'voicepeak'
    assert tts.get_teto_renderer() is second
    monkeypatch.setenv('PHONE_WORKER_VOICEPEAK_TEXT_MODE', 'ptbr-kana')
    third = tts.get_teto_renderer()
    assert third is not second
    assert [backend for backend, _ in created] == ['utau', 'voicepeak', 'voicepeak']


def test_unknown_backend_fails_without_loading_utau(tts, monkeypatch):
    monkeypatch.setenv('PHONE_WORKER_TETO_BACKEND', 'typo')
    monkeypatch.setattr(tts.worker, '_phone_worker_tts_providers_module', lambda: pytest.fail('provider loaded'))
    with pytest.raises(ValueError, match='utau ou voicepeak'):
        tts.get_teto_renderer()


def test_remote_status_does_not_gate_on_phone_synthesis_ram(tts, monkeypatch):
    monkeypatch.setenv('PHONE_WORKER_TETO_ENABLED', 'true')
    monkeypatch.setenv('PHONE_WORKER_TETO_BACKEND', 'voicepeak')
    monkeypatch.setattr(tts.worker, '_teto_status', lambda **kwargs: dict(tts.deps['teto']))
    monkeypatch.setenv('PHONE_WORKER_VOICEPEAK_URL', 'http://voicepeak.invalid:8087')
    monkeypatch.setattr(tts.worker, '_teto_resource_snapshot', lambda: pytest.fail('local synthesis resources'))
    monkeypatch.setattr(tts.worker, '_get_teto_renderer',
                        lambda: SimpleNamespace(status=lambda **kw: {'ready': True, 'backend': 'voicepeak'}))
    status = tts.teto_status(force=True)
    assert status['ready'] and status['resources']['ok']
    assert status['resources']['scope'] == 'remote'


def test_missing_voicepeak_returns_clear_error_without_other_voices(tts, monkeypatch):
    monkeypatch.setenv('PHONE_WORKER_TETO_BACKEND', 'voicepeak')
    monkeypatch.setattr(tts.worker, '_teto_status', lambda **kwargs: dict(tts.deps['teto']))
    tts.deps['teto'].update(ready=False, last_error='ponte licenciada indisponível')
    monkeypatch.setattr(tts.worker, '_turbo_dependency_snapshot', lambda: pytest.fail('fallback probed'))
    with pytest.raises(RuntimeError, match='VOICEPEAK.*ponte licenciada'):
        tts.handler._task_tts_agent_synthesize({'engine': 'teto', 'text': 'Olá.'})
    assert not tts.calls


def test_voicepeak_render_error_does_not_synthesize_with_other_voice(tts, monkeypatch):
    monkeypatch.setenv('PHONE_WORKER_TETO_BACKEND', 'voicepeak')
    monkeypatch.setattr(tts.worker, '_teto_status', lambda **kwargs: dict(tts.deps['teto']))
    monkeypatch.setattr(tts.worker, '_turbo_dependency_snapshot', lambda: pytest.fail('fallback probed'))
    def fail(*args, **kwargs):
        raise RuntimeError('VOICEPEAK timeout')
    monkeypatch.setattr(tts.worker, '_get_teto_renderer', lambda: SimpleNamespace(synthesize=fail))
    with pytest.raises(RuntimeError, match='VOICEPEAK timeout'):
        tts.handler._task_tts_agent_synthesize({'engine': 'teto', 'text': 'Olá.'})
    assert not tts.calls and not tts.worker._HEAVY_RESOURCE_LOCK.locked()
    assert tts.worker._TTS_AGENT_FAILED == 1


def test_voicepeak_cache_preserves_backend_and_does_not_apply_utau_pitch(tts, monkeypatch):
    monkeypatch.setenv('PHONE_WORKER_TETO_BACKEND', 'voicepeak')
    monkeypatch.setattr(tts.worker, '_teto_status', lambda **kwargs: dict(tts.deps['teto']))
    tts.deps['teto'].update(backend='voicepeak', fingerprint='voicepeak-preset-1',
                           narrator='重音テト', reading_mode='ptbr-kana',
                           renderer_version='voicepeak-cli-v1', voicebank_profile='voicepeak-ptbr-kana')
    calls = []
    def render(text, **kwargs):
        calls.append(kwargs)
        return {'audio': b'voicepeak-wave', 'audio_format': 'wav', 'backend': 'voicepeak',
                'narrator': '重音テト', 'reading_mode': 'ptbr-kana',
                'renderer_version': 'voicepeak-cli-v1', 'voicebank_profile': 'voicepeak-ptbr-kana',
                'pitch_offset_semitones': 0.0}
    monkeypatch.setattr(tts.worker, '_get_teto_renderer', lambda: SimpleNamespace(synthesize=render))
    body = {'engine': 'teto', 'text': 'Olá.', 'teto_pitch_semitones': -1}
    cold = tts.handler._task_tts_agent_synthesize(body)
    warm = tts.handler._task_tts_agent_synthesize({**body, 'teto_pitch_semitones': 2})
    assert len(calls) == 1 and calls[0]['pitch_offset_semitones'] == 0.0
    assert warm['cache_hit'] and audio(warm) == audio(cold) == b'voicepeak-wave'
    for result in [cold, warm]:
        assert result['teto_backend'] == 'voicepeak'
        assert result['teto_reading_mode'] == 'ptbr-kana'
        assert result['teto_narrator'] == '重音テト'
        assert result['teto_portuguese_experimental']
        assert result['teto_pitch_mode'] == 'voicepeak-native'
    assert cold['teto_requested_utau_pitch_semitones'] == -1
    tts.deps['teto']['fingerprint'] = 'voicepeak-preset-2'
    changed = tts.handler._task_tts_agent_synthesize(body)
    assert not changed['cache_hit'] and len(calls) == 2


def test_voicepeak_caps_default_worker_output_budget_to_bridge_limit(tts, monkeypatch):
    monkeypatch.setenv('PHONE_WORKER_TETO_BACKEND', 'voicepeak')
    monkeypatch.setattr(tts.worker, '_teto_status', lambda **kwargs: dict(tts.deps['teto']))
    received = []
    def render(text, **kwargs):
        received.append(kwargs['max_audio_bytes'])
        return {'audio': b'wave', 'audio_format': 'wav', 'backend': 'voicepeak'}
    monkeypatch.setattr(tts.worker, '_get_teto_renderer', lambda: SimpleNamespace(synthesize=render))
    tts.handler.server.max_output_bytes = 32 * 1024 * 1024
    result = tts.handler._task_tts_agent_synthesize({'engine': 'teto', 'text': 'Olá.'})
    assert result['ok'] and received == [8 * 1024 * 1024]


def test_voicepeak_preflight_consumes_the_same_request_deadline(tts, monkeypatch):
    monkeypatch.setenv('PHONE_WORKER_TETO_BACKEND', 'voicepeak')
    clock = [100.0]
    tts.worker.time.monotonic = lambda: clock[0]
    probes, renders = [], []

    def status(**kwargs):
        probes.append(kwargs['timeout_seconds'])
        clock[0] += 1.5
        return dict(tts.deps['teto'])

    def render(text, **kwargs):
        renders.append(kwargs['timeout_seconds'])
        return {'audio': b'wave', 'audio_format': 'wav', 'backend': 'voicepeak'}

    monkeypatch.setattr(tts.worker, '_teto_status', status)
    monkeypatch.setattr(tts.worker, '_get_teto_renderer', lambda: SimpleNamespace(synthesize=render))
    result = tts.handler._task_tts_agent_synthesize({'engine': 'teto', 'text': 'Olá.', 'timeout_seconds': 2})
    assert result['ok'] and probes == [2.0] and renders == [0.5]


@pytest.mark.parametrize('scenario', ('prebuild-failed', 'prebuild-disabled', 'other-voice', 'teto-ready'))
def test_direct_playback_requires_successful_teto_synthesis(tts, monkeypatch, scenario):
    monkeypatch.setenv('PHONE_WORKER_TETO_BACKEND', 'voicepeak')
    monkeypatch.setenv('PHONE_WORKER_VOICE_AGENT_DIRECT_TTS_PREBUILD_WITH_TTS_AGENT',
                       'false' if scenario == 'prebuild-disabled' else 'true')
    tts.worker.time.perf_counter = lambda: 100.0
    monkeypatch.setattr(tts.worker, '_voice_agent_prune_transfers', lambda: {})
    monkeypatch.setattr(tts.worker, '_voice_agent_set_connection', lambda *args, **kwargs: dict(kwargs))
    monkeypatch.setattr(tts.worker, '_music_agent_snapshot', lambda: {})
    monkeypatch.setattr(tts.worker, '_tts_agent_snapshot', lambda: {})
    monkeypatch.setattr(tts.worker, '_voice_agent_snapshot', lambda **kwargs: {})
    calls = []
    monkeypatch.setattr(tts.handler, '_task_music_agent_proxy',
                        lambda body: calls.append(body) or {'ok': True, 'engine': 'teto'})

    def synthesize(body):
        if scenario == 'prebuild-failed':
            raise RuntimeError('VOICEPEAK unavailable')
        return {'ok': True, 'data_b64': 'd2F2ZQ==', 'audio_format': 'wav',
                'selected_engine': 'gtts' if scenario == 'other-voice' else 'teto'}

    monkeypatch.setattr(tts.handler, '_task_tts_agent_synthesize', synthesize)
    body = {'guild_id': 1, 'channel_id': 2, 'engine': 'teto', 'text': 'Olá.'}
    if scenario == 'teto-ready':
        result = tts.handler._task_voice_agent_play_tts(body)
        assert result['ok'] and len(calls) == 1 and calls[0]['audio_b64'] == 'd2F2ZQ=='
    else:
        with pytest.raises(RuntimeError, match='Teto VOICEPEAK'):
            tts.handler._task_voice_agent_play_tts(body)
        assert not calls
