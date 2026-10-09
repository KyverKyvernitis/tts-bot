"""Keep Teto controls, cache identity and explicit voice selection after removal."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from test_tts_helpers import QueueItem, tts_audio
from test_tts_modularizacao_interface_modais_vozes_online import _carregar_modulo
from test_tts_modularizacao_interface_paineis_principais import _carregar_lancador


def queue_item(engine="teto", pitch="-1.0"):
    item = QueueItem(
        guild_id=1, channel_id=2, author_id=3, text="Olá.", engine=engine,
        voice="kasane-teto-standard", language="pt-br", rate="+0%", pitch="+0Hz",
    )
    item.teto_pitch_semitones = pitch
    return item


@pytest.mark.parametrize("online", (False, True))
def test_explicit_teto_failure_never_speaks_with_local_fallback(online):
    worker = AsyncMock(side_effect=TimeoutError("Teto worker unavailable"))

    async def run(label, callback, **kwargs):
        return await callback()

    probe = SimpleNamespace(
        _tts_agent_route_available=lambda: online,
        _tts_agent_should_try_worker=lambda item: (True, "always_worker_engine"),
        _record_tts_agent_route_sample=Mock(), _run_timed_generation=run,
        _generate_tts_agent_worker_file=worker, _generate_piper_fallback_file=AsyncMock(),
    )
    with pytest.raises(RuntimeError, match="Teto indisponível"):
        asyncio.run(tts_audio.TTSAudioMixin._generate_audio_file(probe, queue_item()))
    probe._generate_piper_fallback_file.assert_not_awaited()
    assert worker.await_count == int(online)


def test_android_still_uses_existing_offline_fallback():
    fallback = AsyncMock(return_value="normal-user-voice.wav")
    probe = SimpleNamespace(
        _tts_agent_route_available=lambda: False,
        _record_tts_agent_route_sample=Mock(), _generate_piper_fallback_file=fallback,
    )
    assert asyncio.run(
        tts_audio.TTSAudioMixin._generate_audio_file(probe, queue_item("android_native"))
    ) == "normal-user-voice.wav"
    fallback.assert_awaited_once()


@pytest.mark.parametrize("ready", (False, True))
def test_health_keeps_teto_identity_even_when_synthesis_not_ready(ready):
    state, metrics = {}, {}
    probe = SimpleNamespace(
        _get_metrics_store=lambda: metrics, _tts_agent_base_configured=lambda: True,
        _phone_worker_tts_base_url=lambda: "http://worker.invalid",
        _tts_agent_route_state=lambda: state, _tts_agent_set_route=Mock(),
        _fetch_tts_agent_light_health=AsyncMock(return_value={"ok": True, "tts_agent": {
            "ok": ready, "available": ready, "synth_ready": ready, "teto": {
                "backend": "utau", "reading_mode": "ptbr", "fingerprint": "bank-and-renderer"}}}),
    )
    asyncio.run(tts_audio.TTSAudioMixin._probe_tts_agent_health_once(probe))
    assert state["teto_backend"] == "utau" and state["teto_reading_mode"] == "ptbr"
    assert state["teto_fingerprint"] == "bank-and-renderer"
    probe._tts_agent_set_route.assert_called_once()


def test_launcher_restores_pitch_controls_even_with_stale_backend_state():
    module = _carregar_lancador()
    cog = SimpleNamespace(
        _get_db=lambda: None, _tts_phone_worker_online_for_ui=lambda: True,
        _tts_agent_route_state=lambda: {"teto_backend": "voicepeak"},
        _member_panel_name=lambda member: "Usuário",
    )
    view = module.VisaoLancadorPublicoTTS(cog, 10, 20)
    assert "semitom" in view._texto_motor(motor="teto")
    assert view.children[-1].label == "Configurar TextToTeto"
    response = SimpleNamespace(send_message=AsyncMock(), send_modal=AsyncMock())
    interaction = SimpleNamespace(
        guild=SimpleNamespace(id=20), user=SimpleNamespace(id=10), message=None, response=response,
    )
    asyncio.run(view._abrir_acao(interaction, "teto"))
    response.send_modal.assert_awaited_once()
    response.send_message.assert_not_awaited()


def test_modal_saves_pitch_even_with_stale_backend_state():
    module, save = _carregar_modulo()
    cog = SimpleNamespace(_tts_agent_route_state=lambda: {"teto_backend": "voicepeak"})
    modal = module.ModalConfiguracaoTeto(cog, None)
    modal.teto_pitch_semitones.value = "2.0"
    response = SimpleNamespace(send_message=AsyncMock())
    asyncio.run(modal.on_submit(SimpleNamespace(response=response)))
    save.assert_awaited_once()
    assert save.await_args.kwargs["updates"] == {"teto_pitch_semitones": "+2.0"}
    response.send_message.assert_not_awaited()


def test_teto_cache_depends_on_pitch_and_worker_bank_renderer_identity():
    state = {"teto_backend": "voicepeak", "teto_fingerprint": "bank-and-renderer-1"}
    probe = SimpleNamespace(
        _snapshot_tts_item=lambda item: None,
        _get_item_normalized_cache_text=lambda item: item.text,
        _tts_agent_route_state=lambda: state,
        _normalize_teto_pitch_semitones=lambda value: (
            tts_audio.TTSAudioMixin._normalize_teto_pitch_semitones(None, value)
        ),
    )
    key = tts_audio.TTSAudioMixin._cache_key(probe, queue_item(pitch="-1.0"))
    assert tts_audio.TTSAudioMixin._cache_key(probe, queue_item(pitch="2.0")) != key
    state["teto_fingerprint"] = "bank-and-renderer-2"
    assert tts_audio.TTSAudioMixin._cache_key(probe, queue_item(pitch="-1.0")) != key
