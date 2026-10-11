"""The selected character stays isolated across UI, cache and audio validation."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
import pytest

from cogs.tts import ttv
from test_ttv_audio import Probe, item
from test_ttv_ui import cog_for, interaction, modules
from test_tts_message_flow import FakeCog, FakeDB, analyze_message_for_tts, dispatch_message_tts, make_message


def miku_row(**changes):
    return {
        "id": "hatsune-miku", "name": "Hatsune Miku", "ready": True,
        "available": True, "supports_personal_rate": True,
        "languages": ["pt-BR", "en-US"], "fingerprint": "miku-bank-a",
        "language_fingerprints": {"en-US": "miku-english-a"}, **changes,
    }


def miku_probe(**changes):
    probe = Probe()
    probe.route["ttv_voices"].append(miku_row(**changes))
    return probe


def test_miku_preferences_keep_character_and_controls_without_changing_teto_default():
    assert ttv.resolve_preferences({})["ttv_voice_id"] == "kasane-teto"
    settings = {"ttv_voice_id": "hatsune-miku", "ttv_language": "en-US",
                "ttv_pitch_semitones": "-1.5", "ttv_speech_rate": .85}
    assert ttv.resolve_preferences(settings) == settings
    assert ttv.get_voice("hatsune-miku").engine == "teto"
    assert ttv.voice_label("hatsune-miku") == "Hatsune Miku"


def test_legacy_prefix_message_carries_selected_miku_through_real_dispatch():
    async def run():
        cog = FakeCog(db=FakeDB(guild_defaults={"teto_prefix": "'"}, resolved={
            "engine": "edge", "voice": "pt-BR-FranciscaNeural", "language": "pt-br",
            "ttv_voice_id": "hatsune-miku", "ttv_language": "en-US",
            "ttv_pitch_semitones": "-2.0", "ttv_speech_rate": .75,
        }))
        message = make_message("'Hello Miku")
        decision = await analyze_message_for_tts(cog, message)
        result = await dispatch_message_tts(cog, message, guild_defaults=decision.guild_defaults,
                                            active_prefix=decision.active_prefix, forced_engine=decision.forced_engine)
        assert result.enqueued and len(cog.enqueue_calls) == 1
        queued = cog.enqueue_calls[0][1]
        assert queued.engine == "teto" and queued.voice == queued.ttv_voice_id == "hatsune-miku"
        assert queued.language == queued.ttv_language == "en-US"
        assert queued.ttv_pitch_semitones == "-2.0" and queued.ttv_speech_rate == .75
        assert queued.piper_fallback_voice == "pt-BR-FranciscaNeural"
    asyncio.run(run())


def test_missing_miku_never_inherits_teto_languages_fingerprints_or_readiness():
    probe = Probe()
    probe.route.update(ttv_languages=["pt-BR", "en-US"],
                       ttv_language_fingerprints={"en-US": "teto-english"})
    state = probe._ttv_voice_status("hatsune-miku")
    assert not state["ready"] and not state["available"]
    assert state["languages"] == [] and state["language_fingerprints"] == {}
    assert state["fingerprint"] == "unavailable"
    assert probe._ttv_voice_status()["languages"] == ["pt-BR", "en-US"]


@pytest.mark.parametrize("fingerprint_key", ["language_fingerprints", "ttv_language_fingerprints"])
def test_cache_tracks_selected_voice_bank_and_language_only(fingerprint_key):
    probe = miku_probe()
    row = probe.route["ttv_voices"][1]
    row[fingerprint_key] = row.pop("language_fingerprints") if fingerprint_key != "language_fingerprints" else row[fingerprint_key]
    english = probe._cache_key(item(ttv_voice_id="hatsune-miku", ttv_language="en-US"))
    portuguese = probe._cache_key(item(ttv_voice_id="hatsune-miku"))
    teto = probe._cache_key(item())
    probe.route.update(teto_fingerprint="teto-new", ttv_language_fingerprints={"en-US": "teto-new-en"})
    assert probe._cache_key(item(ttv_voice_id="hatsune-miku", ttv_language="en-US")) == english
    assert probe._cache_key(item(ttv_voice_id="hatsune-miku")) == portuguese
    row[fingerprint_key]["en-US"] = "miku-english-b"
    assert probe._cache_key(item(ttv_voice_id="hatsune-miku", ttv_language="en-US")) != english
    assert probe._cache_key(item(ttv_voice_id="hatsune-miku")) == portuguese
    row["fingerprint"] = "miku-bank-b"
    assert probe._cache_key(item(ttv_voice_id="hatsune-miku")) != portuguese
    assert teto != probe._cache_key(item())


@pytest.mark.parametrize("changes", [{"ready": False}, {"available": False}, None])
def test_uninstalled_or_unready_miku_is_rejected_before_transport(changes):
    probe = Probe() if changes is None else miku_probe(**changes)
    probe._request_phone_worker_tts_audio = AsyncMock()
    with pytest.raises(RuntimeError, match="Hatsune Miku está indisponível"):
        asyncio.run(probe._generate_tts_agent_worker_file(item(ttv_voice_id="hatsune-miku")))
    probe._request_phone_worker_tts_audio.assert_not_awaited()


def test_miku_english_uses_own_advertisement_when_teto_has_no_english(tmp_path):
    probe = miku_probe()
    assert "en-US" not in (probe.route.get("ttv_languages") or [])
    path = tmp_path / "miku.wav"
    path.write_bytes(b"test consumer contract")
    probe._get_metrics_store = lambda: {}
    probe._record_tts_agent_synth_success = Mock()
    probe._request_phone_worker_tts_audio = AsyncMock(return_value={
        "engine": "teto", "audio_format": "wav", "audio_path": str(path),
        "ttv_voice_id": "hatsune-miku", "ttv_language": "en-US",
        "ttv_pitch_semitones": 0, "ttv_speech_rate": 1.0,
    })
    queued = item(ttv_voice_id="hatsune-miku", ttv_language="en-US")
    assert asyncio.run(probe._generate_tts_agent_worker_file(queued)) == str(path)
    payload = probe._request_phone_worker_tts_audio.await_args.kwargs["payload"]
    assert payload["voice"] == payload["ttv_voice_id"] == "hatsune-miku"
    assert payload["language"] == payload["ttv_language"] == "en-US"
    assert payload["engine"] == payload["preferred_engine"] == "teto"


@pytest.mark.parametrize("returned", ["kasane-teto", None, "other"])
def test_miku_response_with_teto_or_missing_voice_metadata_is_discarded(tmp_path, returned):
    probe = miku_probe()
    path = tmp_path / "wrong-voice.wav"
    path.write_bytes(b"must never play")
    probe._get_metrics_store = lambda: {}
    probe._mark_tts_agent_synth_failure = Mock()
    probe._request_phone_worker_tts_audio = AsyncMock(return_value={
        "engine": "teto", "audio_format": "wav", "audio_path": str(path),
        "ttv_voice_id": returned, "ttv_language": "pt-BR",
        "ttv_pitch_semitones": 0, "ttv_speech_rate": 1.0,
    })
    with pytest.raises(RuntimeError, match="ajustes de TTV diferentes"):
        asyncio.run(probe._generate_tts_agent_worker_file(item(ttv_voice_id="hatsune-miku")))
    assert not path.exists()


@pytest.mark.parametrize("language,text", [("pt-BR", "Olá. Como você está?"), ("en-US", "Hello. How are you?")])
def test_miku_private_preview_uses_selected_character_language_and_personal_controls(tmp_path, language, text):
    source = tmp_path / "preview.mp3"
    source.write_bytes(b"preview passthrough")
    probe = miku_probe()
    probe._generate_tts_agent_worker_file = AsyncMock(return_value=str(source))
    settings = {"ttv_voice_id": "hatsune-miku", "ttv_language": language,
                "ttv_pitch_semitones": "-2.0", "ttv_speech_rate": .75}
    assert asyncio.run(probe._ttv_preview_mp3(1, 3, settings)) == b"preview passthrough"
    queued = probe._generate_tts_agent_worker_file.await_args.args[0]
    assert queued.voice == queued.ttv_voice_id == "hatsune-miku"
    assert queued.text == text and queued.ttv_language == language
    assert queued.ttv_pitch_semitones == "-2.0" and queued.ttv_speech_rate == .75
    assert queued.channel_id == 0 and queued.author_id == 3 and not source.exists()


@pytest.mark.parametrize("installed", [True, False])
def test_modal_only_offers_installed_ready_miku_and_preserves_existing_four_fields(installed):
    async def run():
        modal_module, _, _ = modules()
        cog = cog_for(ready=True)
        cog._ttv_voice_status = miku_probe(ready=installed, available=installed)._ttv_voice_status
        modal = modal_module.ModalConfiguracaoTeto(cog, None, target_user_id=10)
        components = modal.to_components()
        assert len(components) == 4
        assert [row["component"]["type"] for row in components] == [3, 21, 21, 21]
        assert not any(row.get("description") for row in components)
        options = components[0]["component"]["options"]
        assert [option["value"] for option in options] == (["kasane-teto", "hatsune-miku"] if installed else ["kasane-teto"])
        assert [option["value"] for option in options if option["default"]] == ["kasane-teto"]
        assert len(components[2]["component"]["options"]) == len(components[3]["component"]["options"]) == 5
    asyncio.run(run())


def test_missing_saved_miku_stays_visible_and_cannot_be_silently_saved_as_teto():
    async def run():
        modal_module, _, _ = modules()
        cog = cog_for({"ttv_voice_id": "hatsune-miku", "ttv_language": "en-US"}, ready=True)
        cog._ttv_voice_status = Probe()._ttv_voice_status
        modal = modal_module.ModalConfiguracaoTeto(cog, None, target_user_id=10)
        modal.owner_id, modal.guild_id = 10, 20
        current = modal.to_components()[0]["component"]["options"][0]
        assert current == {"label": "Hatsune Miku (indisponível)", "value": "hatsune-miku", "default": True}
        selected = interaction()
        await modal.on_submit(selected)
        cog._set_user_tts_and_refresh.assert_not_awaited()
        response = selected.response.send_message.await_args.kwargs
        assert response["ephemeral"] and "Hatsune Miku está indisponível" in response["embed"].description
    asyncio.run(run())


def test_ready_miku_modal_saves_selection_and_reset_preserves_character_and_language():
    async def run():
        modal_module, actions, _ = modules()
        cog = cog_for({"ttv_voice_id": "hatsune-miku", "ttv_language": "en-US",
                       "ttv_pitch_semitones": "-2.0", "ttv_speech_rate": .75}, ready=True)
        cog._ttv_voice_status = miku_probe()._ttv_voice_status
        modal = modal_module.ModalConfiguracaoTeto(cog, None, target_user_id=10)
        modal.owner_id, modal.guild_id = 10, 20
        await modal.on_submit(interaction())
        cog._set_user_tts_and_refresh.assert_awaited_with(
            20, 10, ttv_voice_id="hatsune-miku", ttv_language="en-US", ttv_pitch_semitones="-2.0", ttv_speech_rate=.75)
        confirmation = actions.VisaoConfirmacaoTTV(cog, 10, 20, 10, "Usuário", None)
        await confirmation.restaurar_padroes(interaction())
        cog._set_user_tts_and_refresh.assert_awaited_with(
            20, 10, ttv_voice_id="hatsune-miku", ttv_language="en-US", ttv_pitch_semitones="+0.0", ttv_speech_rate=1.0)
    asyncio.run(run())
