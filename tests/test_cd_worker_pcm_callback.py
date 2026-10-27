"""Native PCM callbacks traverse normal TTV admission and never encoded caches."""
from __future__ import annotations

from http.client import HTTPResponse, IncompleteRead
import importlib.util
import io
import json
from pathlib import Path
import struct
import sys
import wave
from types import SimpleNamespace

import pytest

from test_phone_worker_tts_lifecycle import audio, tts  # noqa: F401
from test_ttv_performance_worker import performer, request, score  # noqa: F401


def streaming_renderer(tts, performer, monkeypatch, chunks):
    original = performer.renderer.synthesize_performance

    def perform(text, **options):
        callback = options.pop('emit_pcm')
        assert tts.worker._HEAVY_RESOURCE_LOCK.locked()
        callback(b'\x01\x00' * 4, {'voice_id': 'kasane-teto'})
        chunks.append('rendered')
        return original(text, **options)

    monkeypatch.setattr(performer.renderer, 'synthesize_performance', perform)


def test_stream_callback_bypasses_encoded_cache_and_preserves_physical_lease(tts, performer, monkeypatch):
    body = request(tts, ttv_score=score(), cache_mode='prefer')
    warm = tts.handler._task_tts_agent_synthesize(body, raw_response=True)
    observed, chunks = [], []
    streaming_renderer(tts, performer, monkeypatch, chunks)
    monkeypatch.setattr(tts.handler, '_find_tts_cache_file',
                        lambda *_: pytest.fail('encoded cache read in native stream'))
    result = tts.handler._task_tts_agent_synthesize(body, raw_response=True,
        emit_pcm=lambda pcm, meta: observed.append((pcm, meta)))
    assert not result['cache_hit']
    assert audio(result) == audio(warm)
    assert observed == [(b'\x01\x00' * 4, {'voice_id': 'kasane-teto'})]
    assert chunks == ['rendered']
    assert body['cache_mode'] == 'prefer', 'Caller settings remain immutable'
    assert not tts.worker._HEAVY_RESOURCE_LOCK.locked()


@pytest.mark.parametrize('body', ({'engine': 'gtts', 'text': 'a'}, {'engine': 'teto', 'text': 'a'}))
def test_pcm_callback_requires_singing_before_provider_discovery(tts, monkeypatch, body):
    monkeypatch.setattr(tts.worker, '_teto_dependency_snapshot_fast',
                        lambda: pytest.fail('discovery before stream validation'))
    with pytest.raises(ValueError, match='partitura TTV'):
        tts.handler._task_tts_agent_synthesize(body, emit_pcm=lambda *_: None)
    assert not tts.calls


def test_callback_failure_releases_physical_lease_and_never_enters_audio_cache(tts, performer, monkeypatch):
    streaming_renderer(tts, performer, monkeypatch, [])
    failure = RuntimeError('consumer disconnected')

    def disconnected(*_):
        raise failure

    with pytest.raises(RuntimeError, match='consumer disconnected'):
        tts.handler._task_tts_agent_synthesize(request(tts, ttv_score=score()),
                                             raw_response=True, emit_pcm=disconnected)
    assert not tts.worker._HEAVY_RESOURCE_LOCK.locked()
    assert not tts.root.exists()


def test_ordinary_json_cannot_supply_an_executable_pcm_callback(tts, performer):
    result = tts.handler._task_tts_agent_synthesize(request(tts, ttv_score=score(),
        emit_pcm='untrusted JSON field', cache_mode='bypass'), raw_response=True)
    assert result['ok']
    assert 'emit_pcm' not in performer.calls[0]


@pytest.mark.parametrize('truncated', [False, True])
def test_real_http_handler_pcm_endpoint_and_stream_terminator_without_network(tts, performer, monkeypatch, truncated):
    path = Path(tts.worker.__file__).with_name('tts_transport.py')
    spec = importlib.util.spec_from_file_location('pcm_callback_real_transport', path)
    transport = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(transport)
    monkeypatch.setattr(tts.worker, '_tts_transport_module', lambda: transport)
    monkeypatch.setitem(sys.modules, tts.worker.__name__, tts.worker)
    assert tts.worker._tts_agent_snapshot()['ttv_pcm_stream_protocol'] == 1
    body = request(tts, ttv_score=score(), cache_mode='prefer', timeout_seconds=30)
    original = performer.renderer.synthesize_performance

    def render(text, **options):
        emit = options.pop('emit_pcm')
        wav, meta = original(text, **options)
        with wave.open(io.BytesIO(wav), 'rb') as source:
            pcm = source.readframes(source.getnframes())
            frames = source.getnframes()
        config = dict(voice_id='kasane-teto', reading_language='pt-BR',
            ttv_performance_hash=body['ttv_performance_hash'], sample_rate=44100,
            channels=1, sample_width_bytes=2, normalization_gain=1.0,
            midi_preroll_ms=0, expected_frames=frames)
        emit(pcm[:4096], config)
        if truncated:
            raise RuntimeError('native failure after a prefix')
        emit(pcm[4096:], config)
        return wav, meta

    monkeypatch.setattr(performer.renderer, 'synthesize_performance', render)
    data = json.dumps(body).encode()

    class Socket:
        def __init__(self, incoming):
            self.incoming, self.outgoing = io.BytesIO(incoming), bytearray()

        def makefile(self, *_args):
            return self.incoming

        def sendall(self, chunk):
            self.outgoing.extend(chunk)

        def settimeout(self, _timeout):
            pass

    wire = Socket(b'POST /tts-agent/singing.pcm HTTP/1.1\r\nHost: fixture\r\n'
                  + f'Content-Length: {len(data)}\r\n\r\n'.encode() + data)
    tts.worker.WorkerHandler(wire, ('127.0.0.1', 42), tts.handler.server)
    reader = HTTPResponse(Socket(bytes(wire.outgoing)))
    reader.begin()
    assert reader.status == 200
    assert reader.getheader('Transfer-Encoding') == 'chunked'
    assert reader.getheader('X-Core-Worker-TTV-PCM-Protocol') == '1'
    if truncated:
        with pytest.raises(IncompleteRead):
            reader.read()
    else:
        records, stream = [], io.BytesIO(reader.read())
        while kind := stream.read(1):
            length = struct.unpack('!I', stream.read(4))[0]
            payload = stream.read(length)
            assert len(payload) == length
            records.append((kind, payload))
        assert records[0][0] == b'M' and records[-1][0] == b'E'
        assert json.loads(records[-1][1])['ok'] is True
        assert len(b''.join(value for kind, value in records if kind == b'P')) == 400 * 44100 * 2 // 1000
    assert not tts.worker._HEAVY_RESOURCE_LOCK.locked()
    assert not tts.root.exists()
