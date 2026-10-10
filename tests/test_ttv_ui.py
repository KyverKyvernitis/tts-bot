"""TTV uses real Discord modal components and preserves private user settings."""
from __future__ import annotations

import asyncio
import importlib
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord
import pytest

ROOT = Path(__file__).resolve().parents[1]


def modules():
    name = "ttv_ui_native_checks"
    if name not in sys.modules:
        package = ModuleType(name)
        package.__path__ = [str(ROOT / "cogs" / "tts")]
        sys.modules[name] = package
        common = ModuleType(name + ".common")
        common._shorten = lambda value, limit=100: str(value)[:limit]
        sys.modules[common.__name__] = common
    modal = importlib.import_module(name + ".interface.modais_vozes_online")
    actions = importlib.import_module(name + ".interface.ttv_acoes")
    launcher = importlib.import_module(name + ".interface.visao_lancador_publico")
    return modal, actions, launcher


def cog_for(settings=None, *, ready=False):
    values = dict(settings or {})
    db = SimpleNamespace(
        resolve_tts=lambda guild, user: dict(values),
        get_guild_tts_defaults=lambda guild: {"teto_prefix": "'"},
        get_user_tts=lambda guild, user: dict(values),
    )
    async def save(guild, user, **updates):
        values.update(updates)
    return SimpleNamespace(
        _get_db=lambda: db, _public_panel_states={},
        _resolve_public_panel_message=lambda interaction, source: (source, getattr(source, "id", 0)),
        _resolve_panel_target_user=lambda interaction, **kwargs: (kwargs.get("target_user_id") or interaction.user.id, kwargs.get("target_user_name") or "Usuário", False),
        _set_user_tts_and_refresh=AsyncMock(side_effect=save),
        _make_embed=lambda title, description, **kwargs: discord.Embed(title=title, description=description),
        _edit_panel_message_payload=AsyncMock(),
        _build_public_tts_launcher_view=Mock(return_value=SimpleNamespace()),
        _build_settings_embed=AsyncMock(return_value=discord.Embed()),
        _build_panel_view=Mock(return_value=SimpleNamespace()),
        _member_panel_name=lambda member: "Usuário",
        _ttv_voice_status=lambda: {"id": "kasane-teto", "name": "Kasane Teto", "ready": ready, "online": ready},
        _ttv_preview_mp3=AsyncMock(return_value=b"ID3-real-preview-contract"),
    )


def interaction(*, actor=10, guild=20, admin=False):
    state = {"done": False}
    async def finish(*args, **kwargs):
        state["done"] = True
    response = SimpleNamespace(send_message=AsyncMock(side_effect=finish), defer=AsyncMock(side_effect=finish), send_modal=AsyncMock(side_effect=finish), is_done=lambda: state["done"])
    return SimpleNamespace(
        guild=SimpleNamespace(id=guild) if guild else None,
        user=SimpleNamespace(id=actor, guild_permissions=SimpleNamespace(kick_members=admin)),
        response=response, followup=SimpleNamespace(send=AsyncMock()), message=None,
    )


def test_real_modal_serializes_two_selects_and_radio_preserving_legacy_pitch():
    async def run():
        module, _, _ = modules()
        cog = cog_for({"teto_pitch_semitones": "-2.0", "pitch": "+25Hz", "rate": "+25%"})
        modal = module.ModalConfiguracaoTeto(cog, SimpleNamespace(id=77, guild=SimpleNamespace(id=20)), target_user_id=10)
        payload = modal.to_components()
        assert modal.title == "Configurar TTV"
        assert len(payload) == 3 and all(component["type"] == 18 for component in payload)
        assert [component["component"]["type"] for component in payload] == [3, 3, 21]
        assert payload[0]["label"] == "Escolha sua vocaloid"
        assert payload[0]["component"]["options"] == [{"label": "Kasane Teto", "value": "kasane-teto", "default": True}]
        assert len(payload[1]["component"]["options"]) == 17
        assert [option["value"] for option in payload[1]["component"]["options"] if option["default"]] == ["-2.0"]
        assert next(option for option in payload[1]["component"]["options"] if option["value"] == "+0.0")["label"] == "Original"
        assert [option["value"] for option in payload[2]["component"]["options"] if option["default"]] == ["1.0"]
    asyncio.run(run())


@pytest.mark.parametrize("speed", (0.85, 1.0, 1.15, 1.075, 1.0005, 0.8505, 1.1505, 1.0751234567))
def test_current_speed_survives_guided_and_text_fallback(speed):
    async def run():
        module, _, _ = modules()
        cog = cog_for({"ttv_speech_rate": speed, "ttv_pitch_semitones": "+0.5"})
        panel = SimpleNamespace(id=77, guild=SimpleNamespace(id=20))
        modal = module.ModalConfiguracaoTeto(cog, panel, target_user_id=10)
        radio = modal.to_components()[2]["component"]["options"]
        assert [float(option["value"]) for option in radio if option["default"]] == [speed]
        fallback = module.ModalConfiguracaoTeto(cog, panel, target_user_id=10, force_text_fallback=True)
        assert len(fallback.children) == 3
        assert fallback.ttv_speech_rate.default == str(speed)
        assert fallback.ttv_pitch_semitones.default == "+0.5"
        assert fallback.ttv_voice_id.default == "kasane-teto"
    asyncio.run(run())


def test_blank_fallback_fields_preserve_saved_preferences():
    async def run():
        module, _, _ = modules()
        cog = cog_for({"teto_pitch_semitones": "-2.0", "ttv_speech_rate": 1.0751234567})
        modal = module.ModalConfiguracaoTeto(cog, None, target_user_id=10, force_text_fallback=True)
        modal.owner_id, modal.guild_id = 10, 20
        for component in modal.children:
            component._value = ""
        await modal.on_submit(interaction())
        cog._set_user_tts_and_refresh.assert_awaited_once_with(
            20, 10, ttv_voice_id="kasane-teto", ttv_pitch_semitones="-2.0", ttv_speech_rate=1.0751234567)
    asyncio.run(run())


def test_two_clicks_during_discord_defer_generate_only_one_sample():
    async def run():
        _, actions, _ = modules()
        cog = cog_for()
        view = actions.VisaoConfirmacaoTTV(cog, 10, 20, 10, "Usuário", None)
        first, second = interaction(), interaction()
        entered, release = asyncio.Event(), asyncio.Event()
        original_defer = first.response.defer.side_effect
        async def blocked_defer(**kwargs):
            entered.set()
            await release.wait()
            await original_defer(**kwargs)
        first.response.defer.side_effect = blocked_defer
        pending = asyncio.create_task(view.ouvir_amostra(first))
        await entered.wait()
        await view.ouvir_amostra(second)
        release.set()
        await pending
        cog._ttv_preview_mp3.assert_awaited_once()
        assert "ainda está sendo gerada" in second.response.send_message.await_args.kwargs["content"]
        assert not view._preview_running
    asyncio.run(run())


def test_partial_component_failure_clears_items_before_three_field_fallback():
    async def run():
        module, _, _ = modules()
        with patch.object(module, "adicionar_radio_modal", return_value=False):
            modal = module.ModalConfiguracaoTeto(cog_for(), None)
        assert len(modal.children) == 3
        assert all(isinstance(child, discord.ui.TextInput) for child in modal.children)
    asyncio.run(run())


def test_submit_saves_three_ttv_fields_together_and_never_changes_edge():
    async def run():
        module, _, _ = modules()
        cog = cog_for({"voice": "pt-BR-AntonioNeural", "pitch": "+25Hz", "rate": "+25%"})
        modal = module.ModalConfiguracaoTeto(cog, None, target_user_id=10)
        modal.owner_id, modal.guild_id = 10, 20
        modal.ttv_voice_id._values = ["kasane-teto"]
        modal.ttv_pitch_semitones._values = ["-0.5"]
        modal.ttv_speech_rate._value = "0.85"
        event = interaction()
        await modal.on_submit(event)
        cog._set_user_tts_and_refresh.assert_awaited_once_with(20, 10, ttv_voice_id="kasane-teto", ttv_pitch_semitones="-0.5", ttv_speech_rate=0.85)
        assert event.response.defer.await_args.kwargs["ephemeral"]
        confirmation = event.followup.send.await_args.kwargs
        assert confirmation["ephemeral"] and "Kasane Teto" in confirmation["content"]
        assert [button.label for button in confirmation["view"].children] == ["Ouvir amostra", "Restaurar padrões"]
        assert cog._get_db().resolve_tts(20, 10)["pitch"] == "+25Hz"
    asyncio.run(run())


@pytest.mark.parametrize("field,value", (("ttv_voice_id", "future-uninstalled"), ("ttv_pitch_semitones", "-4.5"), ("ttv_pitch_semitones", "0.25"), ("ttv_speech_rate", "nan"), ("ttv_speech_rate", "2.0")))
def test_invalid_submissions_never_save(field, value):
    async def run():
        module, _, _ = modules()
        cog = cog_for()
        modal = module.ModalConfiguracaoTeto(cog, None, force_text_fallback=True)
        getattr(modal, field)._value = value
        event = interaction()
        await modal.on_submit(event)
        cog._set_user_tts_and_refresh.assert_not_awaited()
        assert event.response.send_message.await_args.kwargs["ephemeral"]
    asyncio.run(run())


@pytest.mark.parametrize("actor,guild,admin,target,allowed", ((11, 20, True, 10, False), (10, 21, True, 10, False), (10, 0, True, 10, False), (10, 20, False, 99, False), (10, 20, True, 99, True), (10, 20, False, 10, True)))
def test_sample_buttons_guard_owner_guild_and_target_permission(actor, guild, admin, target, allowed):
    async def run():
        _, actions, _ = modules()
        cog = cog_for()
        view = actions.VisaoConfirmacaoTTV(cog, 10, 20, target, "Alvo", None)
        event = interaction(actor=actor, guild=guild, admin=admin)
        await view.ouvir_amostra(event)
        assert bool(cog._ttv_preview_mp3.await_count) == allowed
        if allowed:
            delivered = event.followup.send.await_args.kwargs
            assert delivered["ephemeral"] and delivered["file"].filename == "ttv-amostra.mp3"
            assert delivered["file"].fp.read() == b"ID3-real-preview-contract"
    asyncio.run(run())


def test_offline_sample_returns_private_error_and_preserves_settings():
    async def run():
        _, actions, _ = modules()
        cog = cog_for({"ttv_pitch_semitones": "-2.0", "ttv_speech_rate": 1.15})
        cog._ttv_preview_mp3.side_effect = RuntimeError("Kasane Teto indisponível no momento.")
        view = actions.VisaoConfirmacaoTTV(cog, 10, 20, 10, "Usuário", None)
        event = interaction()
        await view.ouvir_amostra(event)
        result = event.followup.send.await_args.kwargs
        assert result["ephemeral"] and "indisponível" in result["content"] and "file" not in result
        cog._set_user_tts_and_refresh.assert_not_awaited()
        assert not view._preview_running
    asyncio.run(run())


def test_reset_retains_voice_and_restores_only_ttv_tone_and_speed():
    async def run():
        _, actions, _ = modules()
        cog = cog_for({"ttv_voice_id": "kasane-teto", "ttv_pitch_semitones": "-2.0", "ttv_speech_rate": 1.15, "pitch": "+50Hz"})
        view = actions.VisaoConfirmacaoTTV(cog, 10, 20, 10, "Usuário", None)
        event = interaction()
        await view.restaurar_padroes(event)
        cog._set_user_tts_and_refresh.assert_awaited_once_with(20, 10, ttv_voice_id="kasane-teto", ttv_pitch_semitones="+0.0", ttv_speech_rate=1.0)
        assert cog._get_db().resolve_tts(20, 10)["pitch"] == "+50Hz"
        assert "Original" in event.followup.send.await_args.kwargs["content"]
    asyncio.run(run())


def test_saved_preference_updates_existing_launcher_then_sends_private_confirmation():
    async def run():
        _, actions, _ = modules()
        cog = cog_for()
        panel = SimpleNamespace(id=77, guild=SimpleNamespace(id=20))
        cog._public_panel_states[77] = {"panel_kind": "launcher", "owner_id": 10, "target_user_id": 99, "target_user_name": "Alvo"}
        event = interaction(admin=True)
        await actions.salvar_configuracao_ttv(cog, event, source_panel_message=panel, settings={"ttv_voice_id": "kasane-teto", "ttv_pitch_semitones": "-2.0", "ttv_speech_rate": 1.15}, target_user_id=99, target_user_name="Alvo", owner_id=10, guild_id=20)
        cog._build_public_tts_launcher_view.assert_called_once_with(20, owner_id=10, timeout=300, target_user_id=99, target_user_name="Alvo")
        cog._edit_panel_message_payload.assert_awaited_once()
        assert event.followup.send.await_args.kwargs["ephemeral"]
    asyncio.run(run())


def test_launcher_uses_same_compact_structure_as_other_engines():
    async def run():
        _, _, module = modules()
        cog = cog_for({"teto_pitch_semitones": "-2.0"}, ready=True)
        view = module.VisaoLancadorPublicoTTS(cog, 10, 20)
        text = view._texto_motor(motor="teto")
        assert "**TTV (TextToVocaloid)**" in text
        assert text.splitlines() == [
            "**TTV (TextToVocaloid)**",
            "Voz da vocaloid escolhida · Prefixo `'`",
            "-# Voz: `Kasane Teto` · Tom: `-2st`",
        ]
        assert "Velocidade" not in text and "Amostra indisponível" not in text
        event = interaction()
        await view._abrir_acao(event, "teto")
        modal = event.response.send_modal.await_args.args[0]
        assert modal.owner_id == 10 and modal.guild_id == 20
        assert modal.current_pitch == "-2.0"
    asyncio.run(run())


@pytest.mark.parametrize("online,ready", [(False, False), (False, True), (True, False), (True, True)])
def test_real_launcher_hides_ttv_offline_and_keeps_other_sections(online, ready):
    async def run():
        _, _, module = modules()
        cog = cog_for(ready=ready)
        cog._tts_phone_worker_online_for_ui = lambda: online
        view = module.VisaoLancadorPublicoTTS(cog, 10, 20)
        container = view.to_components()[0]["components"]
        sections = [item for item in container if item["type"] == 9]
        texts = [item["components"][0]["content"] for item in sections]
        assert texts[0].startswith("**Edge**") and texts[1].startswith("**gTTS**")
        assert len(sections) == (3 if online else 2)
        assert any(text.startswith("**TTV (TextToVocaloid)**") for text in texts) == online
        assert all(section["accessory"]["label"] == "Configurar" for section in sections)
        # Cada separador tem uma seção/footer seguinte; TTV offline não deixa espaço vazio.
        assert all(next_item["type"] != 14 for item, next_item in zip(container, container[1:]) if item["type"] == 14)
    asyncio.run(run())


def test_compact_summary_keeps_only_changed_controls_and_saved_values():
    async def run():
        _, _, module = modules()
        defaults = module.VisaoLancadorPublicoTTS(cog_for(ready=True), 10, 20)
        assert defaults._resumo_ttv() == "Voz: `Kasane Teto`"
        settings = {"teto_pitch_semitones": "-1.5", "ttv_speech_rate": 1.075}
        cog = cog_for(settings, ready=True)
        view = module.VisaoLancadorPublicoTTS(cog, 10, 20)
        assert view._resumo_ttv() == "Voz: `Kasane Teto` · Tom: `-1,5st` · Velocidade: `107,5%`"
        assert cog._get_db().get_user_tts(20, 10) == settings
        cog._set_user_tts_and_refresh.assert_not_awaited()
    asyncio.run(run())


def test_stale_launcher_rechecks_worker_before_opening_ttv_modal():
    async def run():
        _, _, module = modules()
        state = {"online": True}
        cog = cog_for(ready=True)
        cog._tts_phone_worker_online_for_ui = lambda: state["online"]
        view = module.VisaoLancadorPublicoTTS(cog, 10, 20)
        assert view._teto_disponivel()
        state["online"] = False
        event = interaction()
        await view._abrir_acao(event, "teto")
        event.response.send_modal.assert_not_awaited()
        reply = event.response.send_message.await_args.kwargs
        assert reply["ephemeral"] and "offline" in event.response.send_message.await_args.args[0]
        cog._set_user_tts_and_refresh.assert_not_awaited()
    asyncio.run(run())


def test_worker_status_failure_hides_ttv_without_using_stale_readiness():
    async def run():
        _, _, module = modules()
        cog = cog_for(ready=True)
        cog._tts_phone_worker_online_for_ui = Mock(side_effect=RuntimeError("status unavailable"))
        view = module.VisaoLancadorPublicoTTS(cog, 10, 20)
        sections = [item for item in view.to_components()[0]["components"] if item["type"] == 9]
        assert len(sections) == 2 and not view._teto_disponivel()
    asyncio.run(run())
