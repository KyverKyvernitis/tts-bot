"""Contrato real de Components V2 e ações privadas do painel de canto."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord
from discord.http import handle_message_parameters
import pytest

from cogs.tts.interface import painel_canto as panel
from cogs.tts.melodies import DEFAULT_MELODY_ID, Melody


def cog_for(settings=None, *, online=True, ready=True, status=None):
    values = dict(settings or {})
    db = SimpleNamespace(resolve_tts=lambda guild, user: dict(values))
    state = dict(online=online, ready=ready, available=ready, singing_available=True,
                 performance_protocol=1, languages=["pt-BR", "en-US"])
    state.update(status or {})

    async def save(guild, user, **updates):
        values.update(updates)

    return SimpleNamespace(
        _get_db=lambda: db,
        _set_user_tts_and_refresh=AsyncMock(side_effect=save),
        _ttv_voice_status=Mock(return_value=state),
        _tts_phone_worker_online_for_ui=Mock(return_value=online),
        _cd_preview_mp3=AsyncMock(return_value=b"ID3-private-native-canto-contract"),
        _get_panel_prefix_hint=AsyncMock(return_value="`!cd`"),
        _test_values=values,
    )


def interaction(*, actor=10, guild=20, done=False):
    state = {"done": done}

    async def finish(*args, **kwargs):
        state["done"] = True

    message = SimpleNamespace(edit=AsyncMock())
    return SimpleNamespace(
        user=SimpleNamespace(id=actor), guild=SimpleNamespace(id=guild) if guild else None,
        response=SimpleNamespace(
            is_done=lambda: state["done"], send_message=AsyncMock(side_effect=finish),
            edit_message=AsyncMock(side_effect=finish), defer=AsyncMock(side_effect=finish),
        ),
        followup=SimpleNamespace(send=AsyncMock()), message=message,
    )


def walk(components):
    for component in components:
        yield component
        yield from walk(component.get("components", []))
        if "accessory" in component:
            yield component["accessory"]


def content_sent(inter):
    calls = list(inter.response.send_message.call_args_list) + list(inter.followup.send.call_args_list)
    return "\n".join(str(call.args[0] if call.args else call.kwargs.get("content", "")) for call in calls)


def with_second_melody():
    original = panel.get_melody(DEFAULT_MELODY_ID)
    other = Melody(id="test-catalog-song", name="Faixa do catálogo", description="Trecho de teste",
                   duration_ms=5500, note_count=5)

    def get(value):
        if value == other.id:
            return other
        if value in (None, "", original.id):
            return original
        raise ValueError("invalid_catalog_id")

    return patch.multiple(panel, list_melodies=lambda: (original, other), get_melody=get), other


def test_panel_serializes_real_discord_v2_without_content_embed_or_fake_songs():
    async def run():
        view = panel.CDPanelView(cog_for(), 10, 20)
        await view.refresh()
        assert isinstance(view, discord.ui.LayoutView)
        components = view.to_components()
        assert len(components) == 1 and components[0]["type"] == 17
        assert components[0]["accent_color"] == discord.Color.blurple().value
        parts = list(walk(components))
        assert len(parts) <= 12
        assert not any(item["type"] in (9, 11, 12) for item in parts)
        selects = [item for item in parts if item["type"] == 3]
        assert len(selects) == 1
        options = selects[0]["options"]
        assert len(options) == 11 and options[0]["label"] == "Padrão"
        assert options[0]["value"] == DEFAULT_MELODY_ID and options[0]["default"]
        assert sum(bool(option.get("default")) for option in options) == 1
        assert all("description" not in option for option in options)
        assert {option["value"] for option in options} == {item.id for item in panel.list_melodies()}
        buttons = [item for item in parts if item["type"] == 2]
        assert [item["label"] for item in buttons] == ["Ouvir amostra", "CD já em uso"]
        assert buttons[0]["disabled"] is False and buttons[1]["disabled"] is True
        with handle_message_parameters(view=view, allowed_mentions=discord.AllowedMentions.none()) as params:
            assert params.payload["flags"] & 32768
            assert params.payload.get("content") is None
            assert not params.payload.get("embeds")
            assert params.payload["allowed_mentions"]["parse"] == []
    asyncio.run(run())


def test_real_thumbnail_section_only_appears_with_catalog_metadata():
    async def run():
        melody = Melody(id=DEFAULT_MELODY_ID, name="Padrão", description="Melodia padrão",
                        duration_ms=4000, note_count=4, thumbnail_url="https://cdn.example.org/cover.webp")
        with patch.object(panel, "get_melody", return_value=melody), patch.object(panel, "list_melodies", return_value=(melody,)):
            view = panel.CDPanelView(cog_for(), 10, 20)
            await view.refresh()
            parts = list(walk(view.to_components()))
            section = next(item for item in parts if item["type"] == 9)
            assert section["accessory"]["type"] == 11
            assert section["accessory"]["media"]["url"] == melody.thumbnail_url
    asyncio.run(run())


def test_default_melody_uses_existing_bot_discord_avatar_without_download():
    async def run():
        cog = cog_for()
        avatar = discord.Asset._from_default_avatar(None, 0)
        cog.bot = SimpleNamespace(user=SimpleNamespace(display_avatar=avatar))
        view = panel.CDPanelView(cog, 10, 20)
        await view.refresh()
        section = next(item for item in walk(view.to_components()) if item["type"] == 9)
        assert section["accessory"]["media"]["url"] == avatar.url
        assert section["accessory"]["description"] == "Painel de canto"
        cog._cd_preview_mp3.assert_not_awaited()
    asyncio.run(run())


def test_panel_reads_current_voice_and_language_without_network_or_call():
    async def run():
        cog = cog_for({"ttv_voice_id": "hatsune-miku", "ttv_language": "en-US"})
        cog._get_db().resolve_tts = AsyncMock(return_value=dict(cog._test_values))
        view = panel.CDPanelView(cog, 10, 20)
        await view.refresh()
        text = "\n".join(item["content"] for item in walk(view.to_components()) if item["type"] == 10)
        assert "Hatsune Miku" not in text and "Inglês" not in text
        assert "Menu de CDs" in text
        assert "Escolha seu CD, o bot vai ler e no modo de canto vai cantar no ritmo dele." in text
        assert "Em uso:" not in text and "Sua escolha neste servidor" not in text
        assert "ryo (supercell)" not in text
        cog._ttv_voice_status.assert_called_once_with("hatsune-miku")
        cog._cd_preview_mp3.assert_not_awaited()
        cog._set_user_tts_and_refresh.assert_not_awaited()
    asyncio.run(run())


def test_offline_selection_is_temporary_and_apply_saves_only_melody():
    async def run():
        catalog_patch, other = with_second_melody()
        with catalog_patch:
            values = {"ttv_voice_id": "hatsune-miku", "ttv_language": "en-US",
                      "ttv_speech_rate": 1.25, "ttv_pitch_semitones": "-2.0"}
            cog = cog_for(values, online=False)
            view = panel.CDPanelView(cog, 10, 20)
            await view.refresh()
            assert not view.melody_select.disabled
            assert view.preview_button.disabled
            view.melody_select._values = [other.id]
            selected = interaction()
            await view.select_melody(selected)
            assert view.selected_melody_id == other.id and view.active_melody_id == DEFAULT_MELODY_ID
            selected.response.edit_message.assert_awaited_once()
            cog._set_user_tts_and_refresh.assert_not_awaited()
            assert not view.apply_button.disabled
            applied = interaction()
            await view.apply_melody(applied)
            cog._set_user_tts_and_refresh.assert_awaited_once_with(20, 10, ttv_melody_id=other.id)
            assert cog._test_values == {**values, "ttv_melody_id": other.id}
            assert view.apply_button.disabled and view.active_melody_id == other.id
            reopened = panel.CDPanelView(cog, 10, 20)
            await reopened.refresh()
            assert reopened.selected_melody_id == other.id
    asyncio.run(run())


def test_unknown_select_id_cannot_save_or_generate_audio():
    async def run():
        cog = cog_for()
        view = panel.CDPanelView(cog, 10, 20)
        await view.refresh()
        view.melody_select._values = ["../../other-user"]
        inter = interaction()
        await view.select_melody(inter)
        assert "não está disponível" in content_sent(inter)
        assert view.selected_melody_id == DEFAULT_MELODY_ID
        cog._set_user_tts_and_refresh.assert_not_awaited()
        cog._cd_preview_mp3.assert_not_awaited()
    asyncio.run(run())


def test_current_melody_is_disabled_and_direct_apply_is_a_noop():
    async def run():
        cog = cog_for()
        view = panel.CDPanelView(cog, 10, 20)
        await view.refresh()
        inter = interaction()
        await view.apply_melody(inter)
        assert "já está em uso" in content_sent(inter)
        cog._set_user_tts_and_refresh.assert_not_awaited()
    asyncio.run(run())


@pytest.mark.parametrize("actor,guild", ((11, 20), (10, 21), (10, None)))
@pytest.mark.parametrize("action", ("select_melody", "apply_melody", "preview_melody"))
def test_other_user_guild_and_dm_cannot_mutate_or_preview(actor, guild, action):
    async def run():
        cog = cog_for()
        view = panel.CDPanelView(cog, 10, 20)
        await view.refresh()
        inter = interaction(actor=actor, guild=guild)
        await getattr(view, action)(inter)
        cog._set_user_tts_and_refresh.assert_not_awaited()
        cog._cd_preview_mp3.assert_not_awaited()
        assert content_sent(inter)
        assert inter.response.send_message.call_args.kwargs["ephemeral"]
    asyncio.run(run())


def test_expired_panel_mentions_cd_with_guild_prefix_and_denies_actions():
    async def run():
        cog = cog_for()
        view = panel.CDPanelView(cog, 10, 20)
        await view.refresh()
        view.expires_at_monotonic = 0
        inter = interaction(done=True)
        await view.preview_melody(inter)
        assert "`!cd`" in content_sent(inter) and "tts" not in content_sent(inter)
        cog._get_panel_prefix_hint.assert_awaited_once_with(20, "cd")
        cog._cd_preview_mp3.assert_not_awaited()
        await view.on_timeout()
        assert view.melody_select.disabled and view.preview_button.disabled and view.apply_button.disabled
    asyncio.run(run())


def test_preview_is_private_mp3_and_never_changes_preferences_or_queue():
    async def run():
        cog = cog_for({"ttv_voice_id": "hatsune-miku", "ttv_language": "en-US"})
        view = panel.CDPanelView(cog, 10, 20)
        await view.refresh()
        inter = interaction()
        view.message = inter.message
        await view.preview_melody(inter)
        cog._cd_preview_mp3.assert_awaited_once_with(20, 10, melody_id=DEFAULT_MELODY_ID,
                                                  settings=panel.resolve_preferences(cog._test_values))
        cog._set_user_tts_and_refresh.assert_not_awaited()
        inter.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
        kwargs = inter.followup.send.call_args.kwargs
        assert kwargs["ephemeral"] is True
        assert kwargs["file"].filename == "cd-amostra.mp3"
        assert kwargs["file"].fp.read() == b"ID3-private-native-canto-contract"
        assert kwargs["allowed_mentions"].to_dict()["parse"] == []
        assert "Hatsune Miku" in kwargs["content"] and "Inglês" in kwargs["content"]
        assert not view._preview_running and not view.preview_button.disabled
        assert inter.message.edit.await_count == 2
    asyncio.run(run())


def test_removed_saved_melody_can_be_restored_without_silent_change():
    async def run():
        cog = cog_for({"ttv_melody_id": "removed-song-with-private-title"}, online=False)
        view = panel.CDPanelView(cog, 10, 20)
        await view.refresh()
        assert view.active_melody_id == "removed-song-with-private-title"
        assert view.selected_melody_id == DEFAULT_MELODY_ID
        assert not view.apply_button.disabled
        content = "\n".join(item["content"] for item in walk(view.to_components()) if item["type"] == 10)
        assert "Melodia indisponível" in content and "removed-song" not in content
        cog._set_user_tts_and_refresh.assert_not_awaited()
        await view.apply_melody(interaction())
        cog._set_user_tts_and_refresh.assert_awaited_once_with(20, 10, ttv_melody_id=DEFAULT_MELODY_ID)
        assert view.active_melody_id == DEFAULT_MELODY_ID and view.apply_button.disabled
    asyncio.run(run())


@pytest.mark.parametrize("online,ready,status", (
    (False, True, {}), (True, False, {}), (True, True, {"available": False}),
    (True, True, {"singing_available": False}), (True, True, {"performance_protocol": 0}),
    (True, True, {"languages": ["en-US"]}),
))
def test_preview_rechecks_local_availability_before_audio(online, ready, status):
    async def run():
        cog = cog_for(online=online, ready=ready, status=status)
        view = panel.CDPanelView(cog, 10, 20)
        await view.refresh()
        assert view.preview_button.disabled and not view.melody_select.disabled
        inter = interaction()
        await view.preview_melody(inter)
        cog._cd_preview_mp3.assert_not_awaited()
        assert content_sent(inter)
    asyncio.run(run())


@pytest.mark.parametrize("error", (
    ValueError("SECRET token=https://private.example/path?password=unsafe"),
    RuntimeError("SECRET local-path and user lyrics"),
    TimeoutError("SECRET private runtime"),
))
def test_preview_failures_are_safe_and_restore_controls(error):
    async def run():
        cog = cog_for()
        cog._cd_preview_mp3.side_effect = error
        view = panel.CDPanelView(cog, 10, 20)
        await view.refresh()
        inter = interaction()
        await view.preview_melody(inter)
        sent = content_sent(inter)
        assert sent and all(value not in sent for value in ("SECRET", "https://", "token=", "local-path"))
        assert not view._preview_running and not view.preview_button.disabled
        cog._set_user_tts_and_refresh.assert_not_awaited()
    asyncio.run(run())


def test_typed_singing_failure_uses_existing_notice_without_exception_text():
    async def run():
        cog = cog_for()
        error = RuntimeError("SECRET")
        error.code = "ttv_singing_bank_missing"
        cog._cd_preview_mp3.side_effect = error
        view = panel.CDPanelView(cog, 10, 20)
        await view.refresh()
        inter = interaction()
        await view.preview_melody(inter)
        assert "-# Não consegui cantar isso pois essa voz está desativada, ou só por um erro msm sla" in content_sent(inter) and "SECRET" not in content_sent(inter)
    asyncio.run(run())


def test_duplicate_click_only_generates_one_preview_and_cancel_restores_controls():
    async def run():
        cog = cog_for()
        started = asyncio.Event()
        release = asyncio.Event()

        async def generate(*args, **kwargs):
            started.set()
            await release.wait()
            return b"ID3"

        cog._cd_preview_mp3.side_effect = generate
        view = panel.CDPanelView(cog, 10, 20)
        await view.refresh()
        first = interaction()
        task = asyncio.create_task(view.preview_melody(first))
        await started.wait()
        assert view.preview_button.disabled and view.melody_select.disabled
        second = interaction()
        await view.preview_melody(second)
        assert "anterior" in content_sent(second)
        cog._cd_preview_mp3.assert_awaited_once()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not view._preview_running and not view.preview_button.disabled and not view.melody_select.disabled
        first.followup.send.assert_not_awaited()
    asyncio.run(run())


def test_database_failure_keeps_preview_and_save_disabled_with_safe_notice():
    async def run():
        cog = cog_for()
        cog._get_db = Mock(side_effect=RuntimeError("SECRET DB"))
        view = panel.CDPanelView(cog, 10, 20)
        await view.refresh()
        assert view.preview_button.disabled and view.apply_button.disabled
        inter = interaction()
        await view.apply_melody(inter)
        assert "SECRET" not in content_sent(inter)
        cog._set_user_tts_and_refresh.assert_not_awaited()
    asyncio.run(run())
