from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[4]


def test_music_agent_router_accepts_prebuilt_overlay_contract() -> None:
    router_text = (ROOT / "cogs/musica/legado/roteador_audio.py").read_text(encoding="utf-8")
    router_tree = ast.parse(router_text)
    methods = [
        node
        for node in ast.walk(router_tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "play_tts_via_music_agent"
    ]
    assert len(methods) == 1
    accepted = {arg.arg for arg in methods[0].args.kwonlyargs}
    assert "prebuilt_audio" in accepted
    assert "prebuilt_audio_source" in accepted

    audio_text = (ROOT / "cogs/tts/audio.py").read_text(encoding="utf-8")
    assert "payload['prebuilt_audio'] = True" in audio_text
    assert "payload['prebuilt_audio_source'] = 'vps-prebuilt'" in audio_text

    method_source = ast.get_source_segment(router_text, methods[0]) or ""
    assert "prebuilt_audio=bool(prebuilt_audio)" in method_source
    assert 'prebuilt_audio_source=str(prebuilt_audio_source or "")' in method_source


def test_music_agent_overlay_prebuilds_audio_on_vps_when_cache_is_cold() -> None:
    audio_text = (ROOT / "cogs/tts/audio.py").read_text(encoding="utf-8")
    audio_tree = ast.parse(audio_text)
    helpers = [
        node
        for node in ast.walk(audio_tree)
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name == "_maybe_attach_prebuilt_direct_tts_audio"
    ]
    assert len(helpers) == 1
    helper_source = ast.get_source_segment(audio_text, helpers[0]) or ""
    assert "generate_if_missing: bool = False" in helper_source
    assert "_resolve_or_generate_singleflight_audio" in helper_source
    assert "WORKER_VOICE_AGENT_DIRECT_TTS_PREBUILD_MAX_MB" in helper_source
    assert "payload['prebuilt_audio'] = True" in helper_source

    integracao = (ROOT / "cogs/musica/integracoes/tts.py").read_text(encoding="utf-8")
    assert "generate_if_missing=True" in integracao
    assert "rotear_item_tts_para_musica" in integracao


@pytest.mark.asyncio
async def test_roteamento_tts_musical_consome_item_e_registra_metricas(monkeypatch) -> None:
    from types import SimpleNamespace
    from cogs.musica.integracoes import tts as integracao

    monkeypatch.setattr(integracao, "deve_rotear_tts_para_agente", lambda *a, **k: True)
    monkeypatch.setattr(integracao, "suporta_cache_tts_agente", lambda *a, **k: False)

    async def tocar(*args, **kwargs):
        return {"ok": True, "playback_started_at": 12.0, "playback_ms": 250.0}

    monkeypatch.setattr(integracao, "tocar_tts_via_agente", tocar)

    class Owner:
        bot = object()
        timings = []
        sessions = []
        def _estimate_playback_timeout(self, item): return 5.0
        def _record_queue_timing(self, **kwargs): self.timings.append(kwargs)
        def _schedule_worker_voice_agent_register_session(self, *args, **kwargs): self.sessions.append((args, kwargs))

    owner = Owner()
    guild = SimpleNamespace(id=7)
    item = SimpleNamespace(
        channel_id=9,
        text="oi",
        engine="gtts",
        voice="",
        language="pt-br",
        rate="+0%",
        pitch="+0Hz",
        enqueued_at_monotonic=11.0,
        _dequeued_at_monotonic=11.5,
        _tts_remote_active=False,
    )
    consumido, task = await integracao.rotear_item_tts_para_musica(owner, guild, item, None)
    assert consumido is True and task is None
    assert owner.timings and owner.timings[0]["playback_ms"] == 250.0
    assert owner.sessions and owner.sessions[0][1]["source"] == "tts_music_agent_route"


@pytest.mark.asyncio
async def test_roteamento_tts_musical_libera_fallback_quando_nao_ha_sessao(monkeypatch) -> None:
    from types import SimpleNamespace
    from cogs.musica.integracoes import tts as integracao

    monkeypatch.setattr(integracao, "deve_rotear_tts_para_agente", lambda *a, **k: True)
    monkeypatch.setattr(integracao, "suporta_cache_tts_agente", lambda *a, **k: False)
    monkeypatch.setattr(integracao, "musica_ativa", lambda *a, **k: False)

    async def falhar(*args, **kwargs):
        raise RuntimeError("sem sessão musical ativa")

    monkeypatch.setattr(integracao, "tocar_tts_via_agente", falhar)

    class Owner:
        bot = object()
        def _estimate_playback_timeout(self, item): return 5.0

    item = SimpleNamespace(
        channel_id=9,
        text="oi",
        engine="gtts",
        voice="",
        language="pt-br",
        rate="+0%",
        pitch="+0Hz",
        enqueued_at_monotonic=1.0,
        _dequeued_at_monotonic=1.0,
        _tts_remote_active=False,
    )
    consumido, task = await integracao.rotear_item_tts_para_musica(Owner(), SimpleNamespace(id=7), item, None)
    assert consumido is False and task is None
    assert item._skip_music_agent_tts_route is True
