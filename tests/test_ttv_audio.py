import asyncio
import hashlib
import json
import math
import shutil
import struct
import subprocess
import wave
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import discord

from test_tts_helpers import QueueItem, tts_audio


class Probe(tts_audio.TTSAudioMixin):
    def __init__(self):
        self.route = {"teto_fingerprint": "bank-and-esper-v4", "ttv_personal_settings": True,
                      "ttv_voices": [{"id": "kasane-teto", "ready": True, "available": True,
                                      "supports_personal_rate": True}]}

    def _tts_agent_route_state(self):
        return self.route

    def _tts_agent_route_available(self):
        return True

    def _tts_phone_worker_online_for_ui(self):
        return True

    def _get_item_normalized_cache_text(self, item):
        return item.text


def item(**kwargs):
    return QueueItem(1, 2, 3, "Olá.", "teto", "kasane-teto-standard", "pt-br", "+0%", "+0Hz", **kwargs)


def test_snapshot_preserves_legacy_tone_and_transports_independent_personal_controls():
    probe = Probe()
    legacy = item(teto_pitch_semitones="-2.0")
    payload = probe._tts_agent_payload_for_item(legacy)
    assert payload["ttv_pitch_semitones"] == payload["teto_pitch_semitones"] == "-2.0"
    assert payload["ttv_voice_id"] == "kasane-teto" and payload["ttv_speech_rate"] == 1.0
    customized = item(ttv_pitch_semitones="+1.5", ttv_speech_rate=.85)
    payload = probe._tts_agent_payload_for_item(customized)
    assert payload["ttv_pitch_semitones"] == "+1.5" and payload["ttv_speech_rate"] == .85
    assert customized.rate == "+0%" and customized.pitch == "+0Hz"


def test_cache_separates_tone_speed_and_engine_implementation():
    probe = Probe()
    base = probe._cache_key(item())
    assert probe._cache_key(item(ttv_speech_rate=.85)) != base
    assert probe._cache_key(item(ttv_pitch_semitones="-2.0")) != base
    assert probe._cache_key(item(ttv_speech_rate=1.00000001)) != probe._cache_key(item(ttv_speech_rate=1.00000002))
    probe.route["teto_fingerprint"] = "new-voicebank"
    assert probe._cache_key(item()) != base
    with pytest.raises(ValueError, match="desconhecida"):
        probe._cache_key(item(ttv_voice_id="another-uninstalled-character"))


def test_voice_readiness_requires_actual_renderer_and_support_not_only_phone_online():
    probe = Probe()
    assert probe._ttv_voice_status()["ready"]
    probe.route["ttv_voices"][0]["ready"] = False
    assert not probe._ttv_voice_status()["ready"]
    probe.route.pop("ttv_personal_settings")
    assert not probe._ttv_voice_status()["supports_personal_rate"]


def test_preview_offline_preserves_preferences_without_synthesizing():
    probe = Probe()
    probe.route["ttv_voices"][0]["ready"] = False
    probe._generate_tts_agent_worker_file = AsyncMock()
    with pytest.raises(RuntimeError, match="continuam salvos"):
        asyncio.run(probe._ttv_preview_mp3(1, 3, {"teto_pitch_semitones": "-2.0"}))
    probe._generate_tts_agent_worker_file.assert_not_awaited()


def test_private_preview_encodes_real_wav_and_cleans_temp_file(tmp_path):
    if not shutil.which("ffmpeg"):
        pytest.skip("requires ffmpeg to verify playable MP3")
    source = tmp_path / "preview.wav"
    with wave.open(str(source), "wb") as audio:
        audio.setparams((1, 2, 44100, 0, "NONE", "not compressed"))
        audio.writeframes(b"".join(struct.pack("<h", int(8000 * math.sin(2 * math.pi * 220 * n / 44100)))
                                 for n in range(22050)))
    probe = Probe()
    probe._generate_tts_agent_worker_file = AsyncMock(return_value=str(source))
    data = asyncio.run(probe._ttv_preview_mp3(1, 3, {"ttv_speech_rate": 1.15, "teto_pitch_semitones": "-2.0"}))
    queued = probe._generate_tts_agent_worker_file.await_args.args[0]
    assert queued.author_id == 3 and queued.channel_id == 0
    assert queued.ttv_speech_rate == 1.15 and queued.ttv_pitch_semitones == "-2.0"
    assert not source.exists() and 1000 < len(data) < 1024 * 1024
    decoded = subprocess.run(["ffmpeg", "-v", "error", "-i", "pipe:0", "-f", "s16le", "pipe:1"],
                             input=data, capture_output=True, timeout=10)
    assert decoded.returncode == 0 and len(decoded.stdout) > 30000


@pytest.mark.parametrize("changed", [{"ttv_voice_id": "unknown"}, {"ttv_speech_rate": .85},
                                      {"ttv_pitch_semitones": "-2.0"}, {"ttv_speech_rate": None}])
def test_worker_audio_with_wrong_controls_is_rejected_and_file_removed(tmp_path, changed):
    probe = Probe()
    path = tmp_path / "wrong.wav"
    path.write_bytes(b"incorrect voice should not play")
    result = {"engine": "teto", "audio_format": "wav", "audio_path": str(path),
              "ttv_voice_id": "kasane-teto", "ttv_pitch_semitones": "+0.0", "ttv_speech_rate": 1.0}
    result.update(changed)
    probe._get_metrics_store = lambda: {}
    probe._request_phone_worker_tts_audio = AsyncMock(return_value=result)
    probe._mark_tts_agent_synth_failure = Mock()
    with pytest.raises((RuntimeError, ValueError)):
        asyncio.run(probe._generate_tts_agent_worker_file(item()))
    assert not path.exists()


def test_old_worker_cannot_silently_ignore_personal_ttv_controls():
    probe = Probe()
    probe.route["ttv_personal_settings"] = False
    probe._request_phone_worker_tts_audio = AsyncMock()
    with pytest.raises(RuntimeError, match="Atualize o worker"):
        asyncio.run(probe._generate_tts_agent_worker_file(item(ttv_speech_rate=.85)))
    probe._request_phone_worker_tts_audio.assert_not_awaited()
