"""Only a private TTV preview receives a bounded cold-bank analysis budget."""
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import aiohttp
import pytest

from test_phone_worker_tts_lifecycle import tts
from test_ttv_audio import Probe, item
from test_ttv_http_controls import HTTPProbe, http_ttv
from test_tts_helpers import QueueItem, tts_audio


def test_private_preview_marks_its_item_without_changing_ordinary_ttv(tmp_path, monkeypatch):
    probe = Probe()
    source = tmp_path / 'preview.mp3'
    source.write_bytes(b'ID3-native-preview')
    probe._generate_tts_agent_worker_file = AsyncMock(return_value=str(source))
    monkeypatch.setattr(tts_audio, 'TTS_TETO_WORKER_TIMEOUT_SECONDS', 25.0)
    asyncio.run(probe._ttv_preview_mp3(1, 3, {'ttv_voice_id': 'kasane-teto', 'ttv_language': 'pt-BR'}))
    preview = probe._generate_tts_agent_worker_file.await_args.args[0]
    ordinary = item()
    preview_payload = probe._tts_agent_payload_for_item(preview)
    ordinary_payload = probe._tts_agent_payload_for_item(ordinary)
    assert preview_payload['ttv_preview'] is True and preview_payload['timeout_seconds'] == 120
    assert 'ttv_preview' not in ordinary_payload and ordinary_payload['timeout_seconds'] == 25


@pytest.mark.parametrize('engine', ['edge', 'gtts', 'android_native'])
def test_preview_marker_cannot_extend_another_engine_budget(engine):
    probe = Probe()
    queued = item()
    queued.engine = engine
    queued._ttv_preview = True
    payload = probe._tts_agent_payload_for_item(queued)
    assert 'ttv_preview' not in payload
    assert payload['timeout_seconds'] == tts_audio.TTS_WORKER_AGENT_SYNTH_TIMEOUT_SECONDS


@pytest.mark.parametrize('raw_transport', [True, False], ids=['raw', 'json'])
def test_native_pururu_preview_budget_reaches_renderer_over_http_without_changing_message_budget(
        http_ttv, tts, monkeypatch, raw_transport):
    monkeypatch.setattr(tts_audio, 'TTS_WORKER_AGENT_RAW_AUDIO_ENABLED', raw_transport)
    monkeypatch.setattr(tts_audio, 'TTS_TETO_WORKER_TIMEOUT_SECONDS', 25.0)
    row = dict(id='pururu-purpura', name='Pururu Púrpura', ready=True, available=True,
               supports_personal_rate=True, languages=['pt-BR'], fingerprint='pururu-http-bank',
               ttv_language_fingerprints={'pt-BR': 'pururu-http-bank'})
    tts.deps['teto']['ttv_voices'] = [row]
    monkeypatch.setattr(http_ttv.worker, '_get_pururu_renderer', http_ttv.worker._get_teto_renderer)

    async def run():
        async with aiohttp.ClientSession() as session:
            probe = HTTPProbe(session, http_ttv.base, http_ttv.directory)
            probe.route['ttv_voices'] = [row]
            queued = QueueItem(1, 0, 3, 'Olá.', 'teto', 'pururu-purpura', 'pt-BR', '+0%', '+0Hz',
                               ttv_voice_id='pururu-purpura', ttv_language='pt-BR')
            queued._ttv_preview = True
            observed = []
            request = probe._request_phone_worker_tts_audio

            async def observe(**kwargs):
                observed.append(kwargs)
                return await request(**kwargs)

            probe._request_phone_worker_tts_audio = observe
            for preview in (True, False):
                queued._ttv_preview = preview
                path = Path(await probe._generate_tts_agent_worker_file(queued))
                assert path.read_bytes() == http_ttv.raw
                path.unlink()
            assert observed[0]['timeout_seconds'] == 124
            assert observed[0]['payload']['timeout_seconds'] == 120
            assert observed[0]['payload']['ttv_preview'] is True
            assert observed[1]['timeout_seconds'] == 29
            assert observed[1]['payload']['timeout_seconds'] == 25
            assert 'ttv_preview' not in observed[1]['payload']
            assert probe.results[0]['selected_engine'] == 'teto'
            assert probe.results[1]['cache_hit'], 'preview and message must share the same native audio identity'

    asyncio.run(run())
    assert len(http_ttv.calls) == 1
    assert http_ttv.calls[0]['timeout_seconds'] == 120
    assert http_ttv.calls[0]['reading_language'] == 'pt-BR'


@pytest.mark.parametrize(('engine', 'marker', 'requested', 'expected'), [
    ('teto', True, 999, 120), ('teto', True, None, 120),
    ('teto', False, 999, 30), ('teto', 'true', 999, 30),
    ('edge', True, 999, 30), ('gtts', True, 999, 30),
])
@pytest.mark.parametrize('raw', [True, False])
def test_worker_preview_budget_is_strictly_ttv_only_and_capped(tts, monkeypatch, engine, marker, requested, expected, raw):
    delegated = []
    monkeypatch.setattr(tts.handler, '_synthesize_standard_tts_bytes',
                        lambda body, **kwargs: delegated.append(kwargs) or {'ok': True})
    result = tts.handler._task_tts_agent_synthesize(
        dict(engine=engine, text='Olá.', ttv_preview=marker, timeout_seconds=requested), raw_response=raw)
    assert result['ok'] and len(delegated) == 1
    assert delegated[0]['timeout'] == expected
    assert delegated[0]['engine'] == engine


def test_preview_budget_does_not_relax_pururu_language_validation_or_start_fallback(tts, monkeypatch):
    delegated = []
    monkeypatch.setattr(tts.handler, '_synthesize_standard_tts_bytes',
                        lambda body, **kwargs: delegated.append(kwargs) or {'ok': True})
    with pytest.raises(ValueError, match='português brasileiro'):
        tts.handler._task_tts_agent_synthesize(dict(engine='teto', text='Hello.', ttv_preview=True,
            ttv_voice_id='pururu-purpura', ttv_language='en-US', timeout_seconds=120))
    assert not delegated


def test_preview_native_failure_never_falls_back_to_another_engine(tts, monkeypatch):
    attempts = []

    def fail(body, **kwargs):
        attempts.append(kwargs['engine'])
        raise TimeoutError('native analysis expired')

    monkeypatch.setattr(tts.handler, '_synthesize_standard_tts_bytes', fail)
    with pytest.raises(TimeoutError, match='native analysis'):
        tts.handler._task_tts_agent_synthesize(dict(engine='teto', text='Olá.',
            ttv_preview=True, timeout_seconds=120, fallback_engine='gtts'))
    assert attempts == ['teto']
