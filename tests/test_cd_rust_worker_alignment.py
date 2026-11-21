"""Check both singing transports against a real Rust worker and synthetic bank."""
import asyncio
import hashlib
import sys
from types import SimpleNamespace

import aiohttp
import pytest

from cogs.tts.arrangement_audio import ArrangementAudioMixin
from cogs.tts.cd_worker_stream import request_pcm_stream
from osaka_cd.render import _preroll
from test_cd_transport_memory import Probe, item
from test_tts_native_worker_platform import bridge, native_binary, native_ttv, performance_body


class RustWorkerProbe(ArrangementAudioMixin, Probe):
    def __init__(self, worker, session):
        super().__init__(None, session)
        self.health = worker._health()

    def _tts_agent_route_state(self):
        teto = self.health['teto']
        return {
            'ttv_pcm_stream_protocol': self.health['ttv_pcm_stream_protocol'],
            'ttv_performance_protocol': self.health['ttv_performance_protocol'],
            'ttv_personal_settings': teto['ttv_personal_settings'],
            'ttv_voices': teto['ttv_voices'],
            'ttv_languages': teto['ttv_languages'],
        }

    def _phone_worker_tts_base_url(self):
        return 'http://native-tts'


def test_rust_worker_preserves_cd_alignment_in_raw_and_streaming_routes(native_ttv, monkeypatch):
    async def check_alignment():
        body = performance_body()
        selected = item()
        del selected._cd_progressive_request
        selected.text = body['text']
        selected.ttv_score = body['ttv_score']
        call = {'text': selected.text, 'score': selected.ttv_score,
                'voice_id': 'kasane-teto', 'language': 'pt-BR'}
        connector = aiohttp.UnixConnector(path=native_ttv.socket_path())
        async with aiohttp.ClientSession(connector=connector) as session:
            cog = RustWorkerProbe(native_ttv, session)
            payload = cog._tts_agent_payload_for_item(selected)

            # Prime the old worker cache to exercise the fallback CD request
            # while the corresponding WAV is present without timeline metadata.
            for expected_hit in (False, True):
                response = await cog._request_phone_worker_raw_audio(
                    payload=payload, timeout_seconds=10, max_audio_mb=2)
                assert response['cache_hit'] is expected_hit
                if expected_hit:
                    assert not response['teto_timeline_mode']

            raw, metadata = await cog._request_cd_stem(selected, call)
            assert metadata['cache_hit'] is False
            assert metadata['sha256'] == hashlib.sha256(raw).hexdigest()
            preroll = _preroll(raw, metadata, selected.ttv_score)

            received = []
            pcm_wav, receipt = await request_pcm_stream(
                cog, selected, lambda pcm, _metadata: received.append(pcm))
            assert received[0] == b'' and any(received[1:])
            assert receipt['native_pcm_stream'] is True
            assert receipt['sha256'] == hashlib.sha256(pcm_wav).hexdigest()
            assert _preroll(pcm_wav, receipt, selected.ttv_score) == pytest.approx(preroll, abs=.03)

    monkeypatch.setitem(sys.modules, 'cogs.tts.audio',
                        SimpleNamespace(PHONE_WORKER_TOKEN='local-memory-token'))
    asyncio.run(check_alignment())
