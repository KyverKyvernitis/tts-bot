"""Preserve offline recovery while refusing text-only singing fallbacks."""
from __future__ import annotations

import asyncio
import base64
from types import SimpleNamespace

import pytest

from cogs.musica.agente_telefone.modelos import MusicWorkerUnavailable
from cogs.musica.integracoes import tts
from cogs.tts.audio import QueueItem, TTSAudioMixin, TTVSingingError


def item_with_performance(field="ttv_score"):
    item = QueueItem(
        guild_id=1, channel_id=2, author_id=3, text="Meu pai tem pão",
        engine="teto", voice="kasane-teto", language="pt-BR",
        rate="+0%", pitch="+0Hz", ttv_performance_hash="verified-performance",
    )
    setattr(item, field, {"version": 1, "preset": "ttv-demo-v1"}
            if field == "ttv_score" else [{"text": "Meu pai tem pão", "hold": False}])
    return item


def route_owner(prepare):
    router = SimpleNamespace(
        should_route_tts_to_music_agent=lambda *_: True,
        is_music_active=lambda *_: True,
        _supports_tts_cached_audio=True,
    )
    return SimpleNamespace(
        bot=SimpleNamespace(audio_router=router),
        _maybe_attach_prebuilt_direct_tts_audio=prepare,
        _cache_key=lambda item: "musical-cache",
        _estimate_playback_timeout=lambda item: 60,
        _record_queue_timing=lambda **kwargs: None,
        _schedule_worker_voice_agent_register_session=lambda *args, **kwargs: None,
    )


def verified_audio(payload, item):
    payload.update(
        audio_b64=base64.b64encode(b"verified-rendered-audio").decode("ascii"),
        audio_format="mp3", prebuilt_audio=True,
        ttv_performance_hash=item.ttv_performance_hash,
        ttv_score=item.ttv_score, ttv_segments=item.ttv_segments,
    )


@pytest.mark.parametrize("field", ["ttv_score", "ttv_segments"])
@pytest.mark.parametrize("invalid", ["cold-failed", "invalid-base64", "missing-hash", "wrong-hash", "no-prebuilt"])
def test_failed_or_unverified_prebuild_never_calls_text_only_agent(monkeypatch, field, invalid):
    calls = []

    async def prepare(payload, item, **kwargs):
        assert kwargs["generate_if_missing"] is True
        if invalid == "cold-failed":
            # The ordinary prebuild helper returns None when cold synthesis
            # fails. The routing boundary must enforce the musical obligation.
            return None
        verified_audio(payload, item)
        if invalid == "invalid-base64":
            payload["audio_b64"] = "%%%"
        elif invalid == "missing-hash":
            payload.pop("ttv_performance_hash")
        elif invalid == "wrong-hash":
            payload["ttv_performance_hash"] = "different-performance"
        elif invalid == "no-prebuilt":
            payload["prebuilt_audio"] = False

    async def agent(*args, **kwargs):
        calls.append(kwargs)
        return {"ok": True}

    monkeypatch.setattr(tts, "tocar_tts_via_agente", agent)
    item = item_with_performance(field)
    with pytest.raises(RuntimeError, match="preparar o áudio") as failure:
        asyncio.run(tts.rotear_item_tts_para_musica(
            route_owner(prepare), SimpleNamespace(id=1), item, None))
    assert failure.value.code == "ttv_singing_failed"
    assert calls == []
    assert item._tts_remote_active is False


@pytest.mark.parametrize("field", ["ttv_score", "ttv_segments"])
def test_verified_prebuilt_audio_keeps_performance_identity_in_music_route(monkeypatch, field):
    calls = []

    async def prepare(payload, item, **kwargs):
        verified_audio(payload, item)

    async def agent(*args, **kwargs):
        calls.append(kwargs)
        return {"ok": True, "playback_ms": 4000}

    monkeypatch.setattr(tts, "tocar_tts_via_agente", agent)
    item = item_with_performance(field)
    result = asyncio.run(tts.rotear_item_tts_para_musica(
        route_owner(prepare), SimpleNamespace(id=1), item, None))
    assert result == (True, None)
    assert len(calls) == 1
    assert base64.b64decode(calls[0]["audio_b64"], validate=True) == b"verified-rendered-audio"
    assert calls[0]["ttv_performance_hash"] == item.ttv_performance_hash
    assert calls[0][field] is getattr(item, field)
    assert calls[0]["engine"] == "teto"
    assert item._tts_remote_active is False


def test_performance_playback_failure_is_explicit_without_silent_consumption(monkeypatch):
    async def prepare(payload, item, **kwargs):
        verified_audio(payload, item)

    async def agent(*args, **kwargs):
        return {"ok": False, "error": "secret provider detail"}

    monkeypatch.setattr(tts, "tocar_tts_via_agente", agent)
    with pytest.raises(RuntimeError, match="reproduzir o canto") as failure:
        asyncio.run(tts.rotear_item_tts_para_musica(
            route_owner(prepare), SimpleNamespace(id=1), item_with_performance(), None))
    assert failure.value.code == "ttv_singing_failed"
    assert "secret" not in str(failure.value)


def test_offline_worker_marks_singing_item_without_text_fallback(monkeypatch):
    async def prepare(payload, item, **kwargs):
        verified_audio(payload, item)

    async def agent(*args, **kwargs):
        raise MusicWorkerUnavailable("Nenhum worker online")

    monkeypatch.setattr(tts, "tocar_tts_via_agente", agent)
    item = item_with_performance()
    with pytest.raises(RuntimeError, match="reproduzir o canto"):
        asyncio.run(tts.rotear_item_tts_para_musica(
            route_owner(prepare), SimpleNamespace(id=1), item, None))
    assert item._tts_music_worker_unavailable is True
    assert not getattr(item, "_skip_music_agent_tts_route", False)


def test_offline_edge_still_releases_item_to_local_recovery(monkeypatch):
    async def unused_prepare(*args, **kwargs):
        pytest.fail("unsupported prebuild must not run")

    async def agent(*args, **kwargs):
        raise MusicWorkerUnavailable("Nenhum worker online")

    owner = route_owner(unused_prepare)
    owner.bot.audio_router._supports_tts_cached_audio = False
    item = QueueItem(
        guild_id=1, channel_id=2, author_id=3, text="Teste de fala",
        engine="edge", voice="pt-BR-AntonioNeural", language="pt-BR",
        rate="+0%", pitch="+0Hz",
    )
    monkeypatch.setattr(tts, "tocar_tts_via_agente", agent)
    result = asyncio.run(tts.rotear_item_tts_para_musica(
        owner, SimpleNamespace(id=1), item, None))
    assert result == (False, None)
    assert item._skip_music_agent_tts_route is True
    assert item._tts_music_worker_unavailable is True


def test_confirmed_offline_teto_never_uses_gtts_fallback():
    class OfflineAudio(TTSAudioMixin):
        def _tts_agent_route_available(self):
            return True

        def _record_tts_agent_route_sample(self, use_agent):
            assert use_agent is False

        async def _generate_gtts_file_with_priority(self, *args, **kwargs):
            pytest.fail("offline singing must not be spoken with gTTS")

    item = item_with_performance()
    item._tts_music_worker_unavailable = True
    with pytest.raises(TTVSingingError, match="worker de canto está offline"):
        asyncio.run(OfflineAudio()._generate_audio_file(item))
