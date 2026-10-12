"""A score must never disappear into the music agent's text-only endpoint."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from cogs.musica.integracoes import tts


@pytest.mark.parametrize("field", ["ttv_score", "ttv_segments"])
def test_music_text_only_route_refuses_performance_before_discarding_audio(field):
    owner = SimpleNamespace(bot=SimpleNamespace(audio_router=SimpleNamespace(
        should_route_tts_to_music_agent=lambda *_: True,
        _supports_tts_cached_audio=False,
    )))
    item = SimpleNamespace(channel_id=2, **{field: {"version": 1}})
    # This sentinel has no task API: a failure here would reveal that routing
    # consumed/cancelled the caller's pending audio before checking capability.
    pending_audio = object()
    with pytest.raises(RuntimeError, match="áudio pronto"):
        asyncio.run(tts.rotear_item_tts_para_musica(
            owner, SimpleNamespace(id=1), item, pending_audio))


def test_nonmusical_route_retains_performance_for_local_voice_playback():
    owner = SimpleNamespace(bot=SimpleNamespace(audio_router=SimpleNamespace(
        should_route_tts_to_music_agent=lambda *_: False,
    )))
    pending_audio = object()
    item = SimpleNamespace(channel_id=2, ttv_score={"version": 1})
    result = asyncio.run(tts.rotear_item_tts_para_musica(
        owner, SimpleNamespace(id=1), item, pending_audio))
    assert result == (False, pending_audio)
