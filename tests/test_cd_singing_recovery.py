"""Regressões do canto CD: instrução curta e falha discreta."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from cogs.tts.failure_notices import _public_notice, notify_tts_failure
from cogs.tts.melodies import fit_catalog_lyrics_to_score, score_for_melody
from cogs.tts.performance import parse_performance_text


def test_catalog_short_melisma_fits_default_score_without_modifying_original():
    raw, segments = parse_performance_text("a ~ ~ ~ a", singing=True)
    untouched = [part.copy() for part in segments]
    text, aligned = fit_catalog_lyrics_to_score(raw, segments, score_for_melody())
    assert text == "a ~ ~ a"
    assert [entry["text"] for entry in aligned] == ["a", "~", "~", "a"]
    assert segments == untouched


def test_catalog_melisma_underspecified_uses_automatic_alignment():
    text, segments = parse_performance_text("a ~ a", singing=True)
    assert fit_catalog_lyrics_to_score(text, segments, score_for_melody()) == ("a a", None)


def test_catalog_does_not_remove_lyric_words_or_modify_simple_speech():
    score = score_for_melody()
    assert fit_catalog_lyrics_to_score("Olá", None, score) == ("Olá", None)
    with pytest.raises(ValueError, match="mais sílabas"):
        text, segments = parse_performance_text("um dois três quatro cinco ~", singing=True)
        fit_catalog_lyrics_to_score(text, segments, score)


@pytest.mark.asyncio
async def test_singing_error_notice_is_exact_and_anchored_to_original_message():
    guild = Mock()
    guild.id = 11
    guild.me = SimpleNamespace(id=999)
    member = SimpleNamespace(id=22)
    guild.get_member.return_value = member
    channel = Mock()
    channel.id = 44
    channel.guild = guild
    channel.send = AsyncMock()
    channel.permissions_for.return_value = SimpleNamespace(view_channel=True, send_messages=True)
    guild.get_channel_or_thread.return_value = channel
    cog = SimpleNamespace(bot=SimpleNamespace(get_guild=lambda *_: guild))
    item = SimpleNamespace(guild_id=11, channel_id=33, text_channel_id=44,
                           message_id=55, author_id=22, engine="teto", ttv_score=score_for_melody())
    await notify_tts_failure(cog, item, "synthesis_failed", ValueError("PRIVATE SENTENCE https://example.com"))
    sent = channel.send.await_args
    assert sent.args[0] == "-# Por algum motivo não consegui cantar isso :("
    assert sent.kwargs["reference"].message_id == 55
    assert _public_notice("ttv_singing_timeout") == "Por algum motivo não consegui cantar isso :("


def test_singing_backend_keeps_configured_utau_without_forced_esper():
    from pathlib import Path
    worker = (Path(__file__).resolve().parents[1] / "deploy/termux/phone-worker/phone_worker.py").read_text()
    assert 'def _get_teto_singing_renderer' not in worker
    assert 'def _teto_backend() -> str:' in worker
    assert 'result["singing_fingerprints"] = {' in worker
    assert 'result["singing_available"] = any(' in worker
    assert '"singing_available": bool(state.get("singing_available"))' in worker
    renderer = (Path(__file__).resolve().parents[1] / "deploy/termux/phone-worker/teto_renderer/renderer.py").read_text()
    assert "index = self._load_english_index()" in renderer
    assert "self._reading_context.profile = 'english-cvvc'" in renderer


def test_catalog_cache_resolver_returns_singing_fingerprints_even_for_legacy_worker():
    from cogs.tts.ttv import worker_voice_details
    old = worker_voice_details({"ttv_voices": [{"id": "kasane-teto", "ready": True}]}, "kasane-teto")
    assert old["singing_fingerprints"] == {}
    current = worker_voice_details({"ttv_voices": [{"id": "kasane-teto", "ready": True,
        "singing_fingerprints": {"pt-BR": "bank-musical-pt", "en-US": "bank-musical-en"}}]}, "kasane-teto")
    assert current["singing_fingerprints"]["pt-BR"] == "bank-musical-pt"
