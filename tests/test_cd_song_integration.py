"""Every catalog selection must reach the actual musical clock and worker wire.

The local resampler fixture tests notes and PCM, not the native phone timbre.
"""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from cogs.tts import melodies as catalog
from cogs.tts.mensagens.preparacao import preparar_payload_tts_mensagem
from cogs.tts.performance import performance_hash
from test_cd_singing import make_wav
from test_ttv_audio import Probe
from test_cd_singing import DEMO_MIDI, FakeDB, LiveRenderCog, make_message
from test_ttv_performance_renderer import renderer, pcm
from teto_renderer.phonemizer import phonemize
from teto_renderer.prosody import align_singing_syllables, validate_performance


def songs():
    entries = tuple(item for item in catalog.list_melodies() if item.id != catalog.DEFAULT_MELODY_ID)
    assert len(entries) == 10
    return entries


@pytest.mark.parametrize("language", ["pt-BR", "en-US"])
def test_every_song_preview_lyrics_attack_every_note_without_crossing_rests(language):
    for entry in songs():
        score = catalog.score_for_melody(entry.id)
        text = catalog.preview_text_for_melody(entry.id, language)
        validate_performance(text, score=score)
        moras = phonemize(text, max_moras=1025, voicebank_profile="english-cvvc", reading_language=language)
        nuclei = [mora for mora in moras if mora.role == "nucleus"]
        assert len(nuclei) == len(score["notes"]) == entry.note_count
        groups = align_singing_syllables(moras, score)
        assert groups == [[number] for number in range(entry.note_count)]
        assert 8 <= entry.note_count <= 30 and score["duration_ms"] <= 12000


@pytest.mark.asyncio
@pytest.mark.parametrize("voice", ["kasane-teto", "hatsune-miku"])
@pytest.mark.parametrize("language", ["pt-BR", "en-US"])
async def test_all_catalog_selections_freeze_explicit_score_in_discord_worker_payload(voice, language):
    hashes, cache_keys = set(), set()
    probe = Probe()
    probe.route["ttv_performance_protocol"] = 2
    for entry in songs():
        text = " ".join(["a"] * entry.note_count)
        cog = LiveRenderCog(db=FakeDB(resolved={"ttv_melody_id": entry.id,
            "ttv_voice_id": voice, "ttv_language": language,
            "ttv_pitch_semitones": "-2.0", "ttv_speech_rate": 1.25}))
        prepared = await preparar_payload_tts_mensagem(cog, make_message("'/cantar " + text + " ~"),
            guild_defaults={}, active_prefix="'", forced_engine="teto")
        queued = prepared.queue_item
        assert queued is not None
        score = catalog.score_for_melody(entry.id)
        assert queued.ttv_score == score and "preset" not in queued.ttv_score
        assert queued.ttv_voice_id == voice and queued.ttv_language == language
        assert queued.ttv_pitch_semitones == "+0.0" and queued.ttv_speech_rate == 1.0
        wire = probe._tts_agent_payload_for_item(queued)
        assert wire["ttv_score"] == score and wire["text"] == text
        assert wire["ttv_voice_id"] == voice and wire["ttv_language"] == language
        digest = performance_hash(text=text, score=score)
        assert wire["ttv_performance_hash"] == queued.ttv_performance_hash == digest
        hashes.add(digest)
        cache_keys.add(probe._cache_key(queued))
        score["notes"][0]["midi_note"] = 36
        assert queued.ttv_score == catalog.score_for_melody(entry.id)
    assert len(hashes) == len(cache_keys) == 10


@pytest.mark.asyncio
async def test_catalog_song_does_not_rewrite_speech_or_read_user_midi():
    entry = songs()[0]
    settings = {"ttv_melody_id": entry.id, "ttv_pitch_semitones": "-2.0", "ttv_speech_rate": 1.25}
    cog = LiveRenderCog(db=FakeDB(resolved=settings))
    speech = await preparar_payload_tts_mensagem(cog, make_message("'Olá, mundo."),
        guild_defaults={}, active_prefix="'", forced_engine="teto")
    assert speech.queue_item.ttv_score is None
    assert speech.queue_item.ttv_pitch_semitones == "-2.0"
    assert speech.queue_item.ttv_speech_rate == 1.25
    attached = make_message("'a ~ ~ ~ a ~")
    user_midi = SimpleNamespace(filename="melodia.mid", size=len(DEMO_MIDI),
        read=AsyncMock(return_value=DEMO_MIDI))
    attached.attachments = [user_midi]
    musical = await preparar_payload_tts_mensagem(cog, attached,
        guild_defaults={}, active_prefix="'", forced_engine="teto")
    assert musical.queue_item.ttv_score == catalog.score_for_melody(entry.id)
    user_midi.read.assert_not_awaited()
    assert settings["ttv_melody_id"] == entry.id


@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["pt-BR", "en-US"])
async def test_each_private_song_preview_uses_selected_score_and_snapshot(tmp_path, language):
    if not shutil.which("ffmpeg"):
        pytest.skip("requires FFmpeg to encode the actual private MP3")
    probe = Probe()
    snapshot = {"ttv_voice_id": "hatsune-miku", "ttv_language": language,
        "ttv_pitch_semitones": "-2.0", "ttv_speech_rate": 1.25}
    original = deepcopy(snapshot)
    probe._get_db = lambda: pytest.fail("captured preview must not reread changed preferences")
    probe._ttv_voice_status = lambda identity: {"online": True, "ready": True,
        "singing_available": True, "languages": ["pt-BR", "en-US"]}
    for entry in songs():
        source = tmp_path / (entry.id + ".wav")
        make_wav(source)
        probe._generate_tts_agent_worker_file = AsyncMock(return_value=str(source))
        raw = await probe._cd_preview_mp3(20, 10, melody_id=entry.id, settings=snapshot)
        queued = probe._generate_tts_agent_worker_file.await_args.args[0]
        assert queued.ttv_score == catalog.score_for_melody(entry.id)
        assert queued.text == catalog.preview_text_for_melody(entry.id, language)
        assert queued.ttv_voice_id == "hatsune-miku" and queued.ttv_language == language
        assert queued.ttv_pitch_semitones == "+0.0" and queued.ttv_speech_rate == 1.0
        assert queued.ttv_performance_hash == performance_hash(text=queued.text, score=queued.ttv_score)
        assert not source.exists() and snapshot == original
        decoded = subprocess.run(["ffmpeg", "-v", "error", "-i", "pipe:0", "-f", "s16le", "pipe:1"],
            input=raw, capture_output=True, timeout=10)
        assert decoded.returncode == 0 and len(decoded.stdout) > 44100


def test_too_many_lyric_syllables_fail_before_fragment_rendering(renderer, monkeypatch):
    for entry in songs():
        text = " ".join(["a"] * (entry.note_count + 1))
        monkeypatch.setattr(renderer, "_compose_english_cvvc", lambda *args, **kwargs:
            pytest.fail("invalid lyric alignment must not start fragment rendering"))
        with pytest.raises(ValueError, match=rf"{entry.note_count + 1} sílabas para {entry.note_count} notas"):
            renderer.synthesize_performance(text, score=catalog.score_for_melody(entry.id))


def test_selected_song_uses_actual_note_clock_and_cached_pcm(renderer):
    entry = min(songs(), key=lambda item: item.note_count)
    score = catalog.score_for_melody(entry.id)
    unchanged = deepcopy(score)
    text = " ".join(["a"] * entry.note_count)
    raw, metadata = renderer.synthesize_performance(text, score=score)
    samples, rate = pcm(raw)
    assert score == unchanged and metadata["midi_notes"] == len(score["notes"])
    assert metadata["singing_nuclei"] == entry.note_count
    assert metadata["ttv_performance_hash"] == performance_hash(text=text, score=score)
    assert len(samples) / rate == pytest.approx((score["duration_ms"] + metadata["midi_preroll_ms"]) / 1000, abs=1/rate)
    repeated, cached = renderer.synthesize_performance(text, score=score)
    assert repeated == raw and cached["resampler_calls"] == 0

