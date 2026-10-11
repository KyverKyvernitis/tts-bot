"""Two characters cross the actual worker HTTP endpoint without sharing audio.

Native synthesis is the controlled boundary; routing, cache, serialization,
bot control validation and MP3 encoding are production implementations.
"""
from __future__ import annotations

import asyncio
from http.server import ThreadingHTTPServer
import io
import math
from pathlib import Path
import shutil
import struct
import subprocess
import threading
from types import SimpleNamespace
import wave

import aiohttp
import discord  # Ensure real UI components precede legacy test stubs.
import pytest

from test_phone_worker_tts_lifecycle import tts  # noqa: F401
from test_ttv_http_controls import HTTPProbe, QueueItem, tts_audio


def voice_audio(frequency):
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setparams((1, 2, 44100, 0, "NONE", "not compressed"))
        wav.writeframes(b"".join(
            struct.pack("<h", int(6000 * math.sin(2 * math.pi * frequency * frame / 44100)))
            for frame in range(11025)))
    return buffer.getvalue()


@pytest.fixture
def character_http(tts, monkeypatch, tmp_path):
    raw = {"kasane-teto": voice_audio(220), "hatsune-miku": voice_audio(330)}
    calls = []
    rows = [{"id": voice, "name": voice, "ready": True, "available": True,
             "supports_personal_rate": True, "languages": ["pt-BR", "en-US"],
             "fingerprint": voice + "-bank",
             "language_fingerprints": {"en-US": voice + "-english"}}
            for voice in raw]
    state = {**tts.deps["teto"], "ttv_personal_settings": True,
             "ttv_languages": ["pt-BR", "en-US"], "ttv_voices": rows}
    tts.deps["teto"] = state

    def renderer(voice):
        def synthesize(text, **kwargs):
            calls.append({"voice": voice, "text": text, **kwargs})
            return {"audio": raw[voice], "audio_format": "wav", "voicebank": voice,
                    "voicebank_fingerprint": voice + "-bank", "renderer_version": "fixture",
                    "pitch_offset_semitones": kwargs["pitch_offset_semitones"],
                    "speech_rate": kwargs["speech_rate"],
                    "reading_language": kwargs["reading_language"]}
        return SimpleNamespace(synthesize=synthesize)

    monkeypatch.setattr(tts.worker, "_get_teto_renderer", lambda: renderer("kasane-teto"))
    monkeypatch.setattr(tts.worker, "_get_miku_renderer", lambda: renderer("hatsune-miku"))
    server = ThreadingHTTPServer(("127.0.0.1", 0), tts.worker.WorkerHandler)
    server.daemon_threads = True
    server.worker_token = "local-miku-test-token"
    server.max_body_bytes = 1024 * 1024
    server.max_output_bytes = 8 * 1024 * 1024
    server.job_timeout = 60
    monkeypatch.setattr(tts_audio, "PHONE_WORKER_TOKEN", server.worker_token)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=.05), daemon=True)
    thread.start()
    try:
        yield SimpleNamespace(base=f"http://127.0.0.1:{server.server_port}", worker=tts.worker,
                              directory=tmp_path, calls=calls, raw=raw, state=state)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        assert not thread.is_alive()


def queued(voice, language):
    return QueueItem(1, 2, 3, "Olá." if language == "pt-BR" else "Hello.",
                     "teto", voice, language, "+0%", "+0Hz", ttv_voice_id=voice,
                     ttv_language=language, ttv_pitch_semitones="+0.0", ttv_speech_rate=1.0)


def configure(probe, fixture):
    probe.route.update({"ttv_voices": fixture.state["ttv_voices"],
                        "ttv_languages": ["pt-BR", "en-US"]})
    return probe


@pytest.mark.parametrize("raw_transport", [True, False], ids=["raw", "json"])
@pytest.mark.parametrize("language", ["pt-BR", "en-US"])
def test_character_switch_keeps_voice_audio_on_cold_and_warm_http(character_http, monkeypatch,
                                                                raw_transport, language):
    monkeypatch.setattr(tts_audio, "TTS_WORKER_AGENT_RAW_AUDIO_ENABLED", raw_transport)

    async def run():
        async with aiohttp.ClientSession() as session:
            probe = configure(HTTPProbe(session, character_http.base, character_http.directory),
                              character_http)
            keys = {}
            for warm in (False, True):
                for voice in ("hatsune-miku", "kasane-teto"):
                    path = Path(await probe._generate_tts_agent_worker_file(queued(voice, language)))
                    try:
                        assert path.read_bytes() == character_http.raw[voice]
                        result = probe.results[-1]
                        assert result["cache_hit"] is warm
                        assert result["ttv_voice_id"] == voice
                        assert result["ttv_language"] == language
                        assert float(result["ttv_pitch_semitones"]) == 0.0
                        assert float(result["ttv_speech_rate"]) == 1.0
                        # Raw responses advertise the cache hit, while JSON
                        # also carries the server key. Both use the bot key
                        # in the request and the server's source identity.
                        cache_key = result.get("cache_key") or probe._cache_key(queued(voice, language))
                        if warm:
                            assert cache_key == keys[voice]
                        else:
                            keys[voice] = cache_key
                    finally:
                        path.unlink()
            assert len(set(keys.values())) == 2
            assert not any(path.exists() for path in probe.paths)

    asyncio.run(run())
    assert [call["voice"] for call in character_http.calls] == ["hatsune-miku", "kasane-teto"]


@pytest.mark.parametrize("language", ["pt-BR", "en-US"])
def test_miku_private_preview_is_decodable_mp3_from_http(character_http, monkeypatch, language):
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg required")
    monkeypatch.setattr(tts_audio, "TTS_WORKER_AGENT_RAW_AUDIO_ENABLED", True)

    async def run():
        async with aiohttp.ClientSession() as session:
            probe = configure(HTTPProbe(session, character_http.base, character_http.directory),
                              character_http)
            data = await probe._ttv_preview_mp3(1, 3, {"ttv_voice_id": "hatsune-miku",
                                                    "ttv_language": language})
            assert probe.results[-1]["ttv_voice_id"] == "hatsune-miku"
            assert not any(path.exists() for path in probe.paths)
            return data

    data = asyncio.run(run())
    decoded = subprocess.run(["ffmpeg", "-v", "error", "-i", "pipe:0", "-f", "s16le", "pipe:1"],
                             input=data, capture_output=True, timeout=10)
    assert decoded.returncode == 0 and len(decoded.stdout) > 20000
    assert len(character_http.calls) == 1
    assert character_http.calls[0]["voice"] == "hatsune-miku"


def test_wrong_character_response_stays_rejected(character_http, monkeypatch):
    monkeypatch.setattr(tts_audio, "TTS_WORKER_AGENT_RAW_AUDIO_ENABLED", True)
    original = character_http.worker.WorkerHandler._synthesize_standard_tts_bytes

    def wrong(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        result["ttv_voice_id"] = "kasane-teto"
        return result

    monkeypatch.setattr(character_http.worker.WorkerHandler, "_synthesize_standard_tts_bytes", wrong)

    async def run():
        async with aiohttp.ClientSession() as session:
            probe = configure(HTTPProbe(session, character_http.base, character_http.directory),
                              character_http)
            with pytest.raises(RuntimeError, match="ajustes de TTV diferentes"):
                await probe._generate_tts_agent_worker_file(queued("hatsune-miku", "pt-BR"))
            assert not probe.results
            assert not any(path.exists() for path in probe.paths)

    asyncio.run(run())


def test_missing_miku_never_uses_healthy_teto(character_http, monkeypatch):
    monkeypatch.setattr(tts_audio, "TTS_WORKER_AGENT_RAW_AUDIO_ENABLED", True)
    character_http.state["ttv_voices"][1].update(ready=False, available=False, languages=[])

    async def run():
        async with aiohttp.ClientSession() as session:
            probe = configure(HTTPProbe(session, character_http.base, character_http.directory),
                              character_http)
            with pytest.raises(RuntimeError):
                await probe._generate_tts_agent_worker_file(queued("hatsune-miku", "pt-BR"))
            assert not probe.results

    asyncio.run(run())
    assert not character_http.calls
