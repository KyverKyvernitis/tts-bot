"""Painéis protegidos e saves parciais de recursos e provedores do chatbot."""
from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
import discord

from cogs.chatbot.commands import ChatbotCommandsMixin
from cogs.chatbot.config import GuildChatbotConfig
from cogs.chatbot.views import ActionAllowlistModal, EditConfigView, ProviderConfigModal


def _interaction(*, user_id=40, guild_id=10):
    guild = SimpleNamespace(id=guild_id, me=object())
    role = SimpleNamespace(id=80, guild=guild)
    channel = SimpleNamespace(id=20, guild=guild)
    guild.get_role = Mock(side_effect=lambda value: role if value == 80 else None)
    guild.get_channel = Mock(side_effect=lambda value: channel if value == 20 else None)
    response = SimpleNamespace(is_done=Mock(return_value=False), send_message=AsyncMock(),
                               send_modal=AsyncMock())

    async def deferred(**kwargs):
        response.is_done.return_value = True

    response.defer = AsyncMock(side_effect=deferred)
    return SimpleNamespace(
        user=SimpleNamespace(id=user_id), guild=guild,
        response=response,
        followup=SimpleNamespace(send=AsyncMock()),
    )


def _cog(initial=None):
    cog = ChatbotCommandsMixin()
    initial = initial or GuildChatbotConfig(guild_id=10, enabled=True, channel_ids=(20,),
                                           audio_reply_chance_percent=77)
    cog._memory = object()
    cog._config = SimpleNamespace(
        get_config=AsyncMock(return_value=initial),
        save_action_allowlists=AsyncMock(return_value=initial),
        save_provider_config=AsyncMock(return_value=initial),
        save_config=AsyncMock(), save_action_config=AsyncMock(), save_audio_config=AsyncMock(),
    )
    return cog


@pytest.mark.asyncio
async def test_live_config_panel_exposes_guarded_allowlist_and_provider_modals():
    cog = _cog()
    interaction = _interaction()
    with patch("cogs.chatbot.commands._staff_check", new=AsyncMock(return_value=True)):
        await cog._do_configurar(interaction)
        view = interaction.followup.send.await_args.kwargs["view"]
        assert [button.label for button in view.children][-2:] == [
            "Autorizar cargos e canais", "Provedor de conversa",
        ]
        for label, modal_type in (("Autorizar cargos e canais", ActionAllowlistModal),
                                  ("Provedor de conversa", ProviderConfigModal)):
            click = _interaction()
            await next(button for button in view.children if button.label == label).callback(click)
            assert isinstance(click.response.send_modal.await_args.args[0], modal_type)


@pytest.mark.asyncio
@pytest.mark.parametrize("label", ["Autorizar cargos e canais", "Provedor de conversa"])
async def test_new_panel_buttons_reject_other_members_and_lost_staff_permission(label):
    authorized = AsyncMock(return_value=True)
    view = EditConfigView(
        requester_id=40, current_config=GuildChatbotConfig(guild_id=10),
        on_submit_config=AsyncMock(), on_submit_allowlists=AsyncMock(), on_submit_provider=AsyncMock(),
        check_authorized=authorized,
    )
    button = next(button for button in view.children if button.label == label)
    other_member = _interaction(user_id=99)
    await button.callback(other_member)
    authorized.assert_not_awaited()
    other_member.response.send_modal.assert_not_awaited()
    authorized.return_value = False
    denied = _interaction()
    await button.callback(denied)
    denied.response.send_modal.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("modal_type", [ActionAllowlistModal, ProviderConfigModal])
async def test_modal_submit_rechecks_requester_and_staff_before_callback(modal_type):
    authorize, save = AsyncMock(return_value=True), AsyncMock()
    modal = modal_type(requester_id=40, current_config=GuildChatbotConfig(guild_id=10),
                       on_submit_config=save, check_authorized=authorize)
    await modal.on_submit(_interaction(user_id=99))
    authorize.assert_not_awaited()
    save.assert_not_awaited()
    authorize.return_value = False
    await modal.on_submit(_interaction())
    save.assert_not_awaited()


@pytest.mark.asyncio
async def test_allowlist_modal_changes_only_target_allowlists_in_callback_configuration():
    initial = GuildChatbotConfig(guild_id=10, enabled=True, channel_ids=(20,),
                                 action_staff_role_ids=(90,), text_provider_order=("gemini", "groq"))
    save = AsyncMock()
    modal = ActionAllowlistModal(requester_id=40, current_config=initial, on_submit_config=save,
                                check_authorized=AsyncMock(return_value=True))
    modal.allowed_roles._values = [SimpleNamespace(id=80)]
    modal.allowed_channels._values = [SimpleNamespace(id=20)]
    await modal.on_submit(_interaction())
    submitted = save.await_args.args[1]
    assert submitted == replace(initial, action_allowed_role_ids=(80,), action_allowed_channel_ids=(20,))
    assert modal.allowed_roles.min_values == modal.allowed_channels.min_values == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["groq", "gemini"])
async def test_provider_modal_keeps_fallback_and_changes_only_provider_order(provider):
    initial = GuildChatbotConfig(guild_id=10, channel_ids=(20,), action_allowed_role_ids=(80,))
    save = AsyncMock()
    modal = ProviderConfigModal(requester_id=40, current_config=initial, on_submit_config=save,
                                check_authorized=AsyncMock(return_value=True))
    assert [option.value for option in modal.provider_group.options if option.default] == ["groq"]
    modal.provider_group._value = provider
    await modal.on_submit(_interaction())
    expected = ("groq", "gemini") if provider == "groq" else ("gemini", "groq")
    assert save.await_args.args[1] == replace(initial, text_provider_order=expected)


@pytest.mark.asyncio
async def test_invalid_provider_selection_is_not_saved():
    save = AsyncMock()
    modal = ProviderConfigModal(requester_id=40, current_config=GuildChatbotConfig(guild_id=10),
                                on_submit_config=save, check_authorized=AsyncMock(return_value=True))
    modal.provider_group._value = "unknown"
    await modal.on_submit(_interaction())
    save.assert_not_awaited()


@pytest.mark.asyncio
async def test_allowlist_handler_uses_current_guild_roles_policy_and_partial_save():
    cog = _cog()
    proposed = replace(await cog._config.get_config(10), action_allowed_role_ids=(80,), action_allowed_channel_ids=(20,))
    interaction = _interaction()
    with patch("cogs.chatbot.commands._staff_check", new=AsyncMock(return_value=True)), \
         patch("cogs.chatbot.action_policy.is_safe_assignable_role", new=Mock(return_value=True), create=True) as safe_role:
        await cog._handle_allowlists_modal(interaction, proposed)
    safe_role.assert_called_once_with(interaction.guild, interaction.guild.get_role(80), interaction.guild.me)
    cog._config.save_action_allowlists.assert_awaited_once_with(
        guild_id=10, action_allowed_role_ids=(80,), action_allowed_channel_ids=(20,), updated_by=40,
    )
    cog._config.save_config.assert_not_awaited()
    cog._config.save_action_config.assert_not_awaited()
    cog._config.save_audio_config.assert_not_awaited()
    cog._config.save_provider_config.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["missing_role", "unsafe_role", "staff_role", "missing_channel", "foreign_channel"])
async def test_invalid_or_dangerous_allowlists_are_never_saved(failure):
    initial = GuildChatbotConfig(guild_id=10, action_staff_role_ids=(80,) if failure == "staff_role" else ())
    cog = _cog(initial)
    proposed = replace(initial, action_allowed_role_ids=(81,) if failure == "missing_role" else (80,),
                       action_allowed_channel_ids=(21,) if failure == "missing_channel" else (20,))
    interaction = _interaction()
    if failure == "foreign_channel":
        interaction.guild.get_channel = Mock(return_value=SimpleNamespace(id=20, guild=SimpleNamespace(id=99)))
    with patch("cogs.chatbot.commands._staff_check", new=AsyncMock(return_value=True)), \
         patch("cogs.chatbot.action_policy.is_safe_assignable_role", new=Mock(return_value=failure != "unsafe_role"), create=True):
        await cog._handle_allowlists_modal(interaction, proposed)
    cog._config.save_action_allowlists.assert_not_awaited()


@pytest.mark.asyncio
async def test_allowlist_handler_rechecks_staff_after_loading_fresh_configuration():
    cog = _cog()
    proposed = replace(await cog._config.get_config(10), action_allowed_role_ids=(80,))
    with patch("cogs.chatbot.commands._staff_check", new=AsyncMock(side_effect=[True, False])), \
         patch("cogs.chatbot.action_policy.is_safe_assignable_role", new=Mock(return_value=True), create=True):
        await cog._handle_allowlists_modal(_interaction(), proposed)
    cog._config.get_config.assert_any_await(10, fresh=True)
    cog._config.save_action_allowlists.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("handler", ["_handle_allowlists_modal", "_handle_provider_modal"])
async def test_new_handlers_reject_forms_from_other_guilds(handler):
    cog = _cog()
    with patch("cogs.chatbot.commands._staff_check", new=AsyncMock(return_value=True)):
        await getattr(cog, handler)(_interaction(guild_id=99), GuildChatbotConfig(guild_id=10))
    cog._config.save_action_allowlists.assert_not_awaited()
    cog._config.save_provider_config.assert_not_awaited()


@pytest.mark.asyncio
async def test_provider_handler_writes_only_provider_order():
    cog = _cog()
    proposed = replace(await cog._config.get_config(10), text_provider_order=("gemini", "groq"))
    with patch("cogs.chatbot.commands._staff_check", new=AsyncMock(return_value=True)):
        await cog._handle_provider_modal(_interaction(), proposed)
    cog._config.save_provider_config.assert_awaited_once_with(
        guild_id=10, text_provider_order=("gemini", "groq"), updated_by=40,
    )
    cog._config.save_config.assert_not_awaited()
    cog._config.save_action_config.assert_not_awaited()
    cog._config.save_action_allowlists.assert_not_awaited()


def test_configuration_summary_explains_empty_allowlists_and_groq_first_default():
    rendered = ChatbotCommandsMixin._format_config(GuildChatbotConfig(guild_id=10))
    assert "alterações de cargos desativadas" in rendered
    assert "alterações e limpeza desativadas" in rendered
    assert "Groq → Gemini" in rendered
    assert ".env" not in rendered


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["safe", "managed", "administrator", "default", "foreign", "above_bot"])
async def test_allowlist_handler_integrates_real_discord_role_safety_policy(kind):
    cog, interaction = _cog(), _interaction()
    guild = interaction.guild

    def make_role(identifier, *, role_guild=guild, position=1, managed=False, permissions=0):
        return discord.Role(guild=role_guild, state=None, data={
            "id": str(identifier), "name": "cargo", "permissions": str(permissions),
            "position": position, "color": 0, "hoist": False, "managed": managed,
            "mentionable": False,
        })

    identifier = guild.id if kind == "default" else 80
    role = make_role(identifier, role_guild=SimpleNamespace(id=99) if kind == "foreign" else guild,
                     position=20 if kind == "above_bot" else 1, managed=kind == "managed",
                     permissions=discord.Permissions(administrator=True).value if kind == "administrator" else 0)
    guild.get_role = Mock(side_effect=lambda value: role if value == identifier else None)
    guild.me = SimpleNamespace(top_role=make_role(90, position=10))
    proposed = GuildChatbotConfig(guild_id=10, action_allowed_role_ids=(identifier,))
    with patch("cogs.chatbot.commands._staff_check", new=AsyncMock(return_value=True)):
        await cog._handle_allowlists_modal(interaction, proposed)
    assert cog._config.save_action_allowlists.await_count == (1 if kind == "safe" else 0)
