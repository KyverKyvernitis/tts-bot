"""O painel usa o mesmo contrato musical da mensagem, com MP3 privado real."""
from __future__ import annotations

import inspect
import math
from pathlib import Path
import shutil
import struct
import subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock
import wave

import pytest

from test_tts_helpers import tts_audio
from test_ttv_audio import Probe
from test_tts_message_flow import FakeCog as LiveRenderCog, FakeDB, make_message as _base_message

def make_message(content):
    message = _base_message(content)
    message.attachments = []
    message.reply = AsyncMock()
    return message
from test_ttv_performance_bot import midi as demo_midi
DEMO_MIDI = demo_midi()
from cogs.tts.melodies import DEFAULT_MELODY_ID, Melody, list_melodies, score_for_melody
from cogs.tts.performance import performance_hash
from cogs.tts.mensagens.preparacao import preparar_payload_tts_mensagem


def test_catalog_keeps_default_and_each_request_owns_its_score():
    entries = list_melodies()
    assert entries[0].id == DEFAULT_MELODY_ID and len(entries) == 11
    assert len({entry.id for entry in entries}) == 11
    first = score_for_melody()
    first["preset"] = "changed"
    assert score_for_melody() == {
        "version": 1, "track_index": 0, "duration_ms": 4000, "lyrics_source": "message",
        "notes": [
            {"midi_note": pitch, "start_ms": index * 1000, "duration_ms": 1000, "velocity": 100}
            for index, pitch in enumerate((60, 64, 67, 64))
        ],
    }


@pytest.mark.asyncio
async def test_message_freezes_selected_catalog_score_before_later_edits(monkeypatch):
    import cogs.tts.melodies as catalog
    import json
    score = {"version": 1, "track_index": 0, "duration_ms": 1600, "lyrics_source": "message",
             "notes": [{"midi_note": pitch, "start_ms": index * 400, "duration_ms": 400,
                        "velocity": 100} for index, pitch in enumerate([60, 62, 64, 65])]}
    entry = Melody("fixture", "Melodia de teste", "", 1600, 4, score_json=json.dumps(score))
    monkeypatch.setattr(catalog, "_MELODIES", (*list_melodies(), entry))
    cog = LiveRenderCog(db=FakeDB(resolved={"ttv_melody_id": entry.id}))
    payload = await preparar_payload_tts_mensagem(cog, make_message("'/cantar a a a a"),
        guild_defaults={}, active_prefix="'", forced_engine="teto")
    queued = payload.queue_item
    assert queued.ttv_score == score
    digest = performance_hash(text=queued.text, score=score)
    assert queued.ttv_performance_hash == digest
    monkeypatch.setattr(catalog, "_MELODIES", (list_melodies()[0],))
    assert queued.ttv_score == score and queued.ttv_performance_hash == digest


@pytest.mark.asyncio
async def test_user_midi_never_overrides_missing_saved_melody():
    cog = LiveRenderCog(db=FakeDB(resolved={"ttv_melody_id": "removed-melody"}))
    message = make_message("'a ~ ~ ~ a")
    source = SimpleNamespace(filename="melodia.mid", size=len(DEMO_MIDI),
                             read=AsyncMock(return_value=DEMO_MIDI))
    message.attachments = [source]
    payload = await preparar_payload_tts_mensagem(cog, message,
        guild_defaults={}, active_prefix="'", forced_engine="teto")
    assert payload is None
    source.read.assert_not_awaited()


@pytest.mark.asyncio
async def test_unavailable_saved_melody_does_not_change_plain_ttv_speech():
    cog = LiveRenderCog(db=FakeDB(resolved={"ttv_melody_id": "removed-melody"}))
    payload = await preparar_payload_tts_mensagem(cog, make_message("'Olá, mundo."),
        guild_defaults={}, active_prefix="'", forced_engine="teto")
    assert payload.queue_item.ttv_score is None


def make_wav(path):
    with wave.open(str(path), "wb") as output:
        output.setparams((1, 2, 44100, 0, "NONE", "not compressed"))
        output.writeframes(b"".join(struct.pack("<h", round(6000 * math.sin(2 * math.pi * 262 * i / 44100)))
                                  for i in range(44100)))


async def maybe_await(value):
    return await value if inspect.isawaitable(value) else value


@pytest.mark.asyncio
@pytest.mark.parametrize("voice", ["kasane-teto", "hatsune-miku"])
@pytest.mark.parametrize("language", ["pt-BR", "en-US"])
async def test_panel_audio_uses_music_hash_and_snapshot_and_cleans_worker_wav(tmp_path, voice, language):
    if not shutil.which("ffmpeg"):
        pytest.skip("requires FFmpeg to decode the actual MP3")
    path = tmp_path / "private.wav"
    make_wav(path)
    probe = Probe()
    snapshot = {"ttv_voice_id": voice, "ttv_language": language,
                "ttv_pitch_semitones": "-2.0", "ttv_speech_rate": 1.25}
    probe._get_db = lambda: pytest.fail("a captured preview must not reread changed settings")
    probe._maybe_await = maybe_await
    probe._ttv_voice_status = lambda identity: {"online": True, "ready": True,
        "singing_available": True, "languages": ["pt-BR", "en-US"]}
    probe._generate_tts_agent_worker_file = AsyncMock(return_value=str(path))
    data = await probe._cd_preview_mp3(1, 2, settings=snapshot)
    queued = probe._generate_tts_agent_worker_file.await_args.args[0]
    assert queued.guild_id == 1 and queued.author_id == 2 and queued.channel_id == 0
    assert queued.ttv_voice_id == voice and queued.ttv_language == language
    assert queued.ttv_pitch_semitones == "+0.0" and queued.ttv_speech_rate == 1.0
    assert queued.ttv_score == score_for_melody()
    assert queued.ttv_performance_hash == performance_hash(text=queued.text, score=queued.ttv_score)
    assert snapshot["ttv_pitch_semitones"] == "-2.0" and snapshot["ttv_speech_rate"] == 1.25
    assert not path.exists()
    decoded = subprocess.run(["ffmpeg", "-v", "error", "-i", "pipe:0", "-f", "s16le", "pipe:1"],
                             input=data, capture_output=True, timeout=10)
    assert decoded.returncode == 0 and len(decoded.stdout) > 44100


@pytest.mark.asyncio
async def test_panel_preview_refuses_offline_voice_before_synthesis():
    probe = Probe()
    probe._ttv_voice_status = lambda identity: {"online": False, "ready": False}
    probe._generate_tts_agent_worker_file = AsyncMock()
    with pytest.raises(tts_audio.TTVSingingError):
        await probe._cd_preview_mp3(1, 2, settings={})
    probe._generate_tts_agent_worker_file.assert_not_awaited()


@pytest.mark.asyncio
async def test_invalid_worker_audio_is_removed_on_failed_private_conversion(tmp_path):
    path = tmp_path / "invalid.wav"
    path.write_bytes(b"invalid voice audio")
    probe = Probe()
    probe._ttv_voice_status = lambda identity: {"online": True, "ready": True,
        "singing_available": True, "languages": ["pt-BR"]}
    probe._generate_tts_agent_worker_file = AsyncMock(return_value=str(path))
    with pytest.raises(tts_audio.TTVSingingError):
        await probe._cd_preview_mp3(1, 2, settings={})
    assert not path.exists()
