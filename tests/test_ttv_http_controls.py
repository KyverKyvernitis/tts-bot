"""TTV controls survive the real phone HTTP endpoint and bot audio consumer.

Only the native renderer is replaced with a controlled WAV provider. Admission,
cache, HTTP serialization, aiohttp decoding, control checks and MP3 previews use
their production implementations; no bot transport response is mocked.
"""
from __future__ import annotations

import asyncio
from http.server import ThreadingHTTPServer
import io
import math
import os
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import threading
from types import SimpleNamespace
import wave

import aiohttp
import discord  # Load real Discord before the legacy helper installs stubs.
import pytest

from test_phone_worker_tts_lifecycle import tts  # noqa: F401
from test_tts_helpers import QueueItem, tts_audio


class HTTPProbe(tts_audio.TTSAudioMixin):
    def __init__(self, session, base_url, directory):
        self.session = session
        self.base_url = base_url
        self.directory = directory
        self.metrics = {}
        self.results = []
        self.paths = []
        self.route = {
            "ttv_personal_settings": True,
            "ttv_languages": ["pt-BR", "en-US"],
            "teto_fingerprint": "http-fixture-bank",
            "ttv_voices": [{"id": "kasane-teto", "ready": True, "available": True,
                            "supports_personal_rate": True, "languages": ["pt-BR", "en-US"]}],
        }

    def _phone_worker_tts_base_url(self):
        return self.base_url

    async def _get_phone_worker_http_session(self):
        return self.session

    def _tts_agent_route_state(self):
        return self.route

    def _tts_agent_route_available(self):
        return True

    def _tts_phone_worker_online_for_ui(self):
        return True

    def _get_metrics_store(self):
        return self.metrics

    def _get_item_normalized_cache_text(self, item):
        return item.text

    def _make_runtime_temp_file(self, suffix=".mp3"):
        fd, path = tempfile.mkstemp(suffix=suffix, dir=self.directory)
        os.close(fd)
        self.paths.append(Path(path))
        return path

    def _record_tts_agent_synth_success(self, **kwargs):
        self.results.append(dict(kwargs["data"]))
        return super()._record_tts_agent_synth_success(**kwargs)


@pytest.fixture
def http_ttv(tts, monkeypatch, tmp_path):
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setparams((1, 2, 44100, 0, "NONE", "not compressed"))
        wav.writeframes(b"".join(
            struct.pack("<h", int(6000 * math.sin(2 * math.pi * 220 * frame / 44100)))
            for frame in range(11025)))
    raw = buffer.getvalue()
    calls = []
    output_controls = {}

    def synthesize(text, **kwargs):
        calls.append({"text": text, **kwargs})
        return {
            "audio": raw, "audio_format": "wav", "voicebank": "HTTP fixture Teto",
            "voicebank_fingerprint": "http-fixture-bank", "renderer_version": "http-fixture",
            # Real renderer output uses numeric zero; this was serialized as an
            # empty pitch header before the fix, despite successful synthesis.
            "pitch_offset_semitones": kwargs["pitch_offset_semitones"],
            "speech_rate": kwargs["speech_rate"], "reading_language": kwargs["reading_language"],
            **output_controls,
        }

    monkeypatch.setattr(tts.worker, "_get_teto_renderer", lambda: SimpleNamespace(synthesize=synthesize))
    tts.deps["teto"].update({"fingerprint": "http-fixture-bank", "ttv_languages": ["pt-BR", "en-US"]})
    server = ThreadingHTTPServer(("127.0.0.1", 0), tts.worker.WorkerHandler)
    server.daemon_threads = True
    server.worker_token = "local-ttv-test-token"
    server.max_body_bytes = 1024 * 1024
    server.max_output_bytes = 8 * 1024 * 1024
    server.job_timeout = 60
    monkeypatch.setattr(tts_audio, "PHONE_WORKER_TOKEN", server.worker_token)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=.05), daemon=True)
    thread.start()
    try:
        yield SimpleNamespace(base=f"http://127.0.0.1:{server.server_port}", calls=calls,
                              raw=raw, directory=tmp_path, worker=tts.worker, output_controls=output_controls)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        assert not thread.is_alive()


@pytest.mark.parametrize("raw_transport", [True, False], ids=["raw", "json"])
@pytest.mark.parametrize("language", ["pt-BR", "en-US"])
@pytest.mark.parametrize(("pitch", "rate"), [
    ("-4.0", .5), ("-2.0", .75), ("+0.0", 1.0), ("+2.0", 1.25), ("+4.0", 1.5),
])
def test_ttv_http_cold_and_warm_audio_preserves_all_presets(
        http_ttv, monkeypatch, raw_transport, language, pitch, rate):
    monkeypatch.setattr(tts_audio, "TTS_WORKER_AGENT_RAW_AUDIO_ENABLED", raw_transport)

    async def run():
        async with aiohttp.ClientSession() as session:
            probe = HTTPProbe(session, http_ttv.base, http_ttv.directory)
            queued = QueueItem(1, 2, 3, "Olá." if language == "pt-BR" else "Hello.",
                               "teto", "kasane-teto", language, "+0%", "+0Hz",
                               ttv_language=language, ttv_pitch_semitones=pitch, ttv_speech_rate=rate)
            for expected_hit in (False, True):
                path = Path(await probe._generate_tts_agent_worker_file(queued))
                try:
                    assert path.read_bytes() == http_ttv.raw
                    result = probe.results[-1]
                    assert result["cache_hit"] is expected_hit
                    assert result["engine"] == result["selected_engine"] == "teto"
                    assert result["ttv_voice_id"] == "kasane-teto"
                    assert float(result["ttv_pitch_semitones"]) == float(pitch)
                    assert float(result["ttv_speech_rate"]) == rate
                    assert result["ttv_language"] == language
                finally:
                    path.unlink()
            assert probe.metrics["tts_agent_synth_ok"] == 2
            assert not any(path.exists() for path in probe.paths)

    asyncio.run(run())
    assert len(http_ttv.calls) == 1, "warm HTTP request should use the real worker cache"
    assert http_ttv.calls[0]["reading_language"] == language
    assert http_ttv.calls[0]["pitch_offset_semitones"] == float(pitch)
    assert http_ttv.calls[0]["speech_rate"] == rate


@pytest.mark.parametrize("numeric_zero", [0, 0.0, -0.0], ids=["integer-zero", "float-zero", "negative-zero"])
def test_numeric_zero_variants_arrive_as_nonempty_pitch_headers(http_ttv, numeric_zero):
    http_ttv.output_controls["pitch_offset_semitones"] = numeric_zero

    async def run():
        async with aiohttp.ClientSession() as session:
            probe = HTTPProbe(session, http_ttv.base, http_ttv.directory)
            queued = QueueItem(1, 2, 3, "Olá.", "teto", "kasane-teto", "pt-BR", "+0%", "+0Hz")
            # Exercise the wire parser directly as well as the production
            # generator: JSON numbers and signed zero must survive as text.
            result = await probe._request_phone_worker_raw_audio(
                payload=probe._tts_agent_payload_for_item(queued), timeout_seconds=5,
                max_audio_mb=8, stream_to_file=False)
            assert result["raw_audio"] == http_ttv.raw
            assert result["ttv_pitch_semitones"] == str(numeric_zero)
            assert result["ttv_language"] == "pt-BR"

    asyncio.run(run())


@pytest.mark.parametrize("language", ["pt-BR", "en-US"])
def test_prior_empty_zero_header_is_rejected_without_retaining_download(http_ttv, monkeypatch, language):
    monkeypatch.setattr(tts_audio, "TTS_WORKER_AGENT_RAW_AUDIO_ENABLED", True)

    def prior_header_ascii(value, *, limit=180):
        return http_ttv.worker._short_text(value, limit=limit, default="").encode("ascii", errors="ignore").decode("ascii")

    # Recreate the previous faulty wire encoder only. This verifies that the
    # fix preserves the bot's checks instead of accepting unconfirmed controls.
    monkeypatch.setattr(http_ttv.worker, "_header_ascii", prior_header_ascii)

    async def run():
        async with aiohttp.ClientSession() as session:
            probe = HTTPProbe(session, http_ttv.base, http_ttv.directory)
            queued = QueueItem(1, 2, 3, "Hello.", "teto", "kasane-teto", language, "+0%", "+0Hz",
                               ttv_language=language)
            with pytest.raises(RuntimeError, match="ajustes de TTV diferentes"):
                await probe._generate_tts_agent_worker_file(queued)
            assert not probe.results
            assert probe.paths and not any(path.exists() for path in probe.paths)
            assert probe.metrics["tts_agent_synth_failed"] == 1

    asyncio.run(run())
    assert len(http_ttv.calls) == 1, "synthesis succeeds before the broken header is rejected"


@pytest.mark.parametrize("language", ["pt-BR", "en-US"])
def test_default_pitch_private_preview_returns_playable_mp3_over_raw_http(http_ttv, monkeypatch, language):
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg required to verify the actual private preview")
    monkeypatch.setattr(tts_audio, "TTS_WORKER_AGENT_RAW_AUDIO_ENABLED", True)

    async def run():
        async with aiohttp.ClientSession() as session:
            probe = HTTPProbe(session, http_ttv.base, http_ttv.directory)
            data = await probe._ttv_preview_mp3(1, 3, {"ttv_language": language})
            assert len(data) > 1000
            assert probe.results[-1]["ttv_language"] == language
            assert float(probe.results[-1]["ttv_pitch_semitones"]) == 0.0
            assert not any(path.exists() for path in probe.paths)
            return data

    data = asyncio.run(run())
    decoded = subprocess.run(["ffmpeg", "-v", "error", "-i", "pipe:0", "-f", "s16le", "pipe:1"],
                             input=data, capture_output=True, timeout=10)
    assert decoded.returncode == 0 and len(decoded.stdout) > 20000
    assert len(http_ttv.calls) == 1
    assert http_ttv.calls[0]["text"] == ("Hello. How are you?" if language == "en-US" else "Olá. Como você está?")
    assert http_ttv.calls[0]["reading_language"] == language
    assert http_ttv.calls[0]["pitch_offset_semitones"] == 0.0
    assert http_ttv.calls[0]["speech_rate"] == 1.0
