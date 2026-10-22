"""MIDI/effect requests preserve their music identity through the real bot flow."""
from __future__ import annotations

import asyncio
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
from pathlib import Path
import struct
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock
import wave

import aiohttp
import discord  # Real modal/view classes before older tests install optional stubs.
import pytest

from test_tts_helpers import tts_audio as audio
from cogs.tts.mensagens.preparacao import preparar_payload_tts_mensagem
from cogs.tts.performance import parse_performance_text, performance_hash
from test_tts_message_flow import FakeCog, FakeDB, make_message
from test_ttv_audio import Probe, item
from test_ttv_http_controls import HTTPProbe


def _vlq(value):
    values = [value & 127]
    while value > 127:
        value >>= 7
        values.insert(0, (value & 127) | 128)
    return bytes(values)


def midi(tracks=1):
    chunks = []
    for _ in range(tracks):
        events = b""
        for note in (60, 62, 64):
            events += b"\0\x90" + bytes((note, 90))
            events += _vlq(480) + b"\x80" + bytes((note, 0))
        events += b"\0\xff\x2f\0"
        chunks.append(b"MTrk" + struct.pack(">I", len(events)) + events)
    return b"MThd" + struct.pack(">IHHH", 6, int(tracks > 1), tracks, 480) + b"".join(chunks)


def attachment(data=None, filename="melodia.mid", size=None):
    data = data or midi()
    return SimpleNamespace(filename=filename, size=len(data) if size is None else size,
                           read=AsyncMock(return_value=data))


def message(content, attachments=()):
    msg = make_message(content)
    msg.attachments = list(attachments)
    msg.reply = AsyncMock(return_value=SimpleNamespace(edit=AsyncMock()))
    return msg


def music_item(**changes):
    kwargs = dict(ttv_score={"version": 1, "track_index": 0, "duration_ms": 1000,
                            "lyrics_source": "message", "notes": [{"midi_note": 60, "start_ms": 0,
                            "duration_ms": 1000, "velocity": 90}]}, ttv_pitch_semitones="-2.0", ttv_speech_rate=1.25)
    kwargs.update(changes)
    return item(**kwargs)


@pytest.mark.asyncio
async def test_catalog_message_retains_voice_language_and_neutralizes_speech_controls():
    from cogs.tts.melodies import score_for_melody
    cog = FakeCog(db=FakeDB(resolved={"ttv_voice_id": "hatsune-miku", "ttv_language": "en-US",
                                     "ttv_pitch_semitones": "-2.0", "ttv_speech_rate": 1.25}))
    msg = message("'/cantar hello Miku ~", [attachment()])
    payload = await preparar_payload_tts_mensagem(cog, msg, guild_defaults={"announce_author": True},
                                                 active_prefix="'", forced_engine="teto")
    queued = payload.queue_item
    assert queued.text == "hello Miku"
    assert queued.ttv_voice_id == "hatsune-miku" and queued.ttv_language == "en-US"
    assert queued.ttv_pitch_semitones == "+0.0" and queued.ttv_speech_rate == 1.0
    assert queued.ttv_score == score_for_melody()
    assert queued.ttv_performance_hash == performance_hash(text=queued.text, score=queued.ttv_score)
    msg.attachments[0].read.assert_not_awaited()


@pytest.mark.asyncio
async def test_midi_only_message_does_not_trigger_singing_or_download():
    cog = FakeCog(db=FakeDB())
    msg = message("'", [attachment()])
    payload = await preparar_payload_tts_mensagem(cog, msg, guild_defaults={},
                                                 active_prefix="'", forced_engine="teto")
    assert payload is None or payload.queue_item.ttv_score is None
    msg.attachments[0].read.assert_not_awaited()


@pytest.mark.asyncio
async def test_attached_midi_does_not_override_saved_catalog_song():
    from cogs.tts.melodies import score_for_melody
    cog = FakeCog(db=FakeDB(resolved={"ttv_melody_id": "senbonzakura"}))
    msg = message("'/cantar a ~ ~ a ~", [attachment(midi(2))])
    payload = await preparar_payload_tts_mensagem(cog, msg, guild_defaults={},
                                                 active_prefix="'", forced_engine="teto")
    assert payload is not None
    assert payload.queue_item.ttv_score == score_for_melody("senbonzakura")
    msg.attachments[0].read.assert_not_awaited()


@pytest.mark.asyncio
async def test_midi_attachment_keeps_other_engines_independent():
    cog = FakeCog(db=FakeDB())
    msg = message(",hello", [attachment()])
    payload = await preparar_payload_tts_mensagem(cog, msg, guild_defaults={},
                                                 active_prefix=",", forced_engine="edge")
    assert payload.queue_item.ttv_score is None
    msg.attachments[0].read.assert_not_awaited()


@pytest.mark.asyncio
async def test_effects_render_mentions_and_keep_author_outside_word_fades():
    cog = FakeCog(db=FakeDB())
    cog._render_tts_text = lambda msg, text: text.replace("<@123>", "Ana").strip()
    payload = await preparar_payload_tts_mensagem(cog, message("'<olá <@123>>"), guild_defaults={"announce_author": True},
                                                 active_prefix="'", forced_engine="teto")
    assert payload.text == "Dilma disse olá Ana"
    segments = payload.queue_item.ttv_segments
    assert segments[0]["text"] == "Dilma disse" and not segments[0]["fade_in"]
    assert segments[1]["fade_in"] and segments[-1]["fade_out"]
    assert payload.queue_item.ttv_performance_hash == performance_hash(text=payload.text, segments=segments)


@pytest.mark.asyncio
async def test_effects_are_not_parsed_for_other_engines():
    cog = FakeCog(db=FakeDB())
    payload = await preparar_payload_tts_mensagem(cog, message(",<olá aaa>"), guild_defaults={},
                                                 active_prefix=",", forced_engine="edge")
    assert payload.text == "<olá aaa>" and payload.queue_item.ttv_segments is None


def test_performance_identity_separates_cache_and_dedup_and_avoids_speech_chunks():
    probe = Probe()
    base = item()
    music = music_item()
    assert probe._cache_key(music) != probe._cache_key(base)
    changed = music_item()
    changed.ttv_score["notes"][0]["midi_note"] = 64
    assert probe._cache_key(changed) != probe._cache_key(music)
    clean, segments = parse_performance_text("<Olá.")
    faded = item(ttv_segments=segments)
    faded.text = clean
    assert probe._cache_key(faded) != probe._cache_key(base)
    probe._split_tts_text_chunks = lambda *args, **kwargs: ["split", "would corrupt score"]
    assert probe._expand_tts_queue_item(music) == [music]
    assert probe._expand_tts_queue_item(faded) == [faded]
    assert probe._queue_signature(changed) != probe._queue_signature(music)


def test_performance_payload_neutralizes_midi_speech_controls_and_detects_late_mutation():
    probe = Probe()
    queued = music_item()
    payload = probe._tts_agent_payload_for_item(queued)
    assert payload["ttv_score"] == queued.ttv_score
    assert payload["ttv_performance_hash"] == queued.ttv_performance_hash
    assert payload["ttv_pitch_semitones"] == "+0.0" and payload["ttv_speech_rate"] == 1.0
    queued.ttv_score["notes"][0]["midi_note"] = 70
    with pytest.raises(ValueError, match="mudaram"):
        probe._cache_key(queued)


@pytest.mark.asyncio
async def test_old_worker_cannot_silently_ignore_midi_or_effects():
    probe = Probe()
    probe._request_phone_worker_tts_audio = AsyncMock()
    with pytest.raises(RuntimeError, match="Atualize o worker"):
        await probe._generate_tts_agent_worker_file(music_item())
    probe._request_phone_worker_tts_audio.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("response_hash", [None, "0" * 64])
async def test_missing_or_wrong_performance_identity_removes_audio(tmp_path, response_hash):
    probe = Probe()
    probe.route["ttv_performance_protocol"] = 1
    target = tmp_path / "untrusted.wav"
    target.write_bytes(b"must not play")
    probe._request_phone_worker_tts_audio = AsyncMock(return_value={
        "engine": "teto", "audio_format": "wav", "audio_path": str(target),
        "ttv_performance_hash": response_hash, "ttv_voice_id": "kasane-teto",
        "ttv_pitch_semitones": "+0.0", "ttv_speech_rate": 1.0, "ttv_language": "pt-BR",
    })
    with pytest.raises(RuntimeError, match="notas ou efeitos"):
        await probe._generate_tts_agent_worker_file(music_item())
    assert not target.exists()


@pytest.mark.asyncio
async def test_remote_playback_requires_prebuilt_audio_and_retains_identity(tmp_path):
    probe = Probe()
    probe._get_state = lambda _: SimpleNamespace()
    path = tmp_path / "song.wav"
    path.write_bytes(b"valid earlier synthesised song")
    probe._try_get_cached_path = lambda *args: str(path)
    payload = {}
    queued = music_item()
    await probe._maybe_attach_prebuilt_direct_tts_audio(payload, queued)
    assert payload["audio_b64"] and payload["prebuilt_audio"]
    assert payload["ttv_performance_hash"] == queued.ttv_performance_hash
    probe._try_get_cached_path = lambda *args: None
    with pytest.raises(RuntimeError, match="preparar"):
        await probe._maybe_attach_prebuilt_direct_tts_audio({}, queued)


@pytest.mark.asyncio
@pytest.mark.parametrize("format", ["raw", "json"])
async def test_actual_http_audio_consumer_validates_performance_identity(format, tmp_path, monkeypatch):
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setparams((1, 2, 44100, 0, "NONE", "not compressed"))
        wav.writeframes(b"\0\0" * 4410)
    raw = buffer.getvalue()
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            calls.append(request)
            digest = request["ttv_performance_hash"]
            metadata = {"ok": True, "engine": "teto", "selected_engine": "teto", "audio_format": "wav",
                        "ttv_voice_id": "kasane-teto", "ttv_language": "pt-BR", "ttv_pitch_semitones": 0,
                        "ttv_speech_rate": 1.0, "ttv_performance_hash": digest, "sha256": hashlib.sha256(raw).hexdigest()}
            self.send_response(200)
            if format == "raw":
                self.send_header("Content-Type", "audio/wav")
                for name, value in metadata.items():
                    self.send_header("X-Core-Worker-" + name.replace("_", "-"), str(value))
                body = raw
            else:
                import base64
                self.send_header("Content-Type", "application/json")
                body = json.dumps({**metadata, "data_b64": base64.b64encode(raw).decode()}).encode()
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(audio, "PHONE_WORKER_ENABLED", True)
    monkeypatch.setattr(audio, "PHONE_WORKER_HOST", "127.0.0.1")
    monkeypatch.setattr(audio, "PHONE_WORKER_TOKEN", "test-performance-token")
    monkeypatch.setattr(audio, "TTS_WORKER_AGENT_RAW_AUDIO_ENABLED", True)
    try:
        async with aiohttp.ClientSession() as session:
            probe = HTTPProbe(session, f"http://127.0.0.1:{server.server_port}", tmp_path)
            probe.route["ttv_performance_protocol"] = 1
            queued = music_item()
            output = Path(await probe._generate_tts_agent_worker_file(queued))
            assert output.read_bytes() == raw
            assert calls[0]["ttv_performance_hash"] == queued.ttv_performance_hash
            assert calls[0]["ttv_pitch_semitones"] == "+0.0" and calls[0]["ttv_speech_rate"] == 1.0
            assert probe.results[-1]["ttv_performance_hash"] == queued.ttv_performance_hash
    finally:
        await asyncio.to_thread(server.shutdown)
        server.server_close()


@pytest.mark.asyncio
async def test_prefix_only_score_survives_real_queue_admission():
    probe = Probe()
    probe.guild_states = {}
    queued = music_item()
    queued.text = ""
    accepted, dropped, duplicate = await probe._enqueue_tts_items(1, [queued])
    assert (accepted, dropped, duplicate) == (True, 0, False)
    assert probe._get_state(1).queue.get_nowait() is queued


def test_midi_lyrics_are_not_truncated_by_speech_payload_limit():
    probe = Probe()
    queued = music_item()
    queued.text = "a " * 1000
    assert probe._tts_agent_payload_for_item(queued)["text"] == queued.text


def test_sung_playback_timeout_tracks_midi_duration_and_audio_budget(tmp_path, monkeypatch):
    probe = Probe()
    probe.guild_states = {}
    queued = music_item()
    queued.ttv_score["duration_ms"] = 60000
    guild = SimpleNamespace(id=1, me=SimpleNamespace(id=9))
    payload = probe._worker_voice_direct_tts_payload(guild, queued)
    assert payload["timeout_seconds"] > 60
    monkeypatch.setattr(audio, "TTS_TETO_MAX_AUDIO_MB", 1)
    assert probe._tts_agent_payload_for_item(queued)["max_audio_bytes"] >= 8 * 1024 * 1024


@pytest.mark.asyncio
async def test_performance_failure_keeps_detail_and_plain_ttv_keeps_legacy_message():
    probe = Probe()
    probe._tts_agent_should_try_worker = lambda _: (True, 'explicit-voice')
    probe._generate_tts_agent_worker_file = AsyncMock(side_effect=RuntimeError("Atualize o worker para usar canto MIDI"))
    with pytest.raises(RuntimeError, match="TTV: Atualize o worker"):
        await probe._generate_audio_file(music_item())
    with pytest.raises(RuntimeError, match="Kasane Teto indisponível no worker de síntese"):
        await probe._generate_audio_file(item())
