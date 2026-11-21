from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

from cogs.games.handlers.message_router import GincanaMessageRouterMixin
from cogs.utility import Utility
from utility.help_activity import last_help_category, remember_help_category, remember_help_command
from utility.help_center import HelpCenterView


def _owner(user_id=10):
    return SimpleNamespace(id=user_id, bot=False)


def _event(event_id, *, user=None, guild=None, content=""):
    return SimpleNamespace(
        id=event_id, author=user or _owner(), guild=guild, content=content, channel=AsyncMock()
    )


def _view(**kwargs):
    return HelpCenterView(owner=_owner(), prefixes={"bot_prefix": "!"}, root_ids={"tts": 123}, **kwargs)


def _copy(view):
    return "\n".join(item.content for item in view.walk_children() if isinstance(item, discord.ui.TextDisplay))


def _buttons(view):
    return {item.label: item for item in view.walk_children() if isinstance(item, discord.ui.Button)}


def test_home_shortcuts_and_staff_visibility():
    view = _view()
    assert list(_buttons(view)) == ["Voz na call", "Música", "Jogos e fichas"]
    select = next(item for item in view.walk_children() if isinstance(item, discord.ui.Select))
    assert select.placeholder == "Mais comandos"
    assert [option.label for option in select.options] == ["Conversa e imagens"]
    assert "!help fila" in _copy(view)

    staff = _view(extra_permissions=frozenset({"ticket_staff"}))
    select = next(item for item in staff.walk_children() if isinstance(item, discord.ui.Select))
    assert [option.label for option in select.options] == ["Conversa e imagens", "Servidor"]


@pytest.mark.asyncio
async def test_help_opens_category_and_all_commands_can_be_expanded():
    view = _view(initial_category="music")
    assert (view.target_kind, view.target_key) == ("category", "music")
    assert "!play Basket Case" in _copy(view)
    assert "!queue" in _copy(view)
    assert "!volume" not in _copy(view)

    interaction = SimpleNamespace(response=AsyncMock())
    await _buttons(view)["Ver todos"].callback(interaction)
    assert "!volume" in _copy(view)
    assert "!play <nome ou link>" in _copy(view)
    interaction.response.edit_message.assert_awaited_with(view=view)

    await _buttons(view)["Ver atalhos"].callback(interaction)
    assert "!volume" not in _copy(view)
    await _buttons(view)["Início"].callback(interaction)
    assert view.target_kind == "home"


def test_context_music_help_is_available_when_worker_is_offline():
    unavailable = frozenset({"music"})
    assert "Música" not in _buttons(_view(hidden_categories=unavailable))
    view = _view(initial_category="music", hidden_categories=unavailable)
    assert (view.target_kind, view.target_key) == ("category", "music")
    assert "A música está indisponível agora" in _copy(view)


def test_explicit_subject_and_permission_take_precedence():
    view = _view(subject="fila", initial_category="voice")
    assert (view.target_kind, view.target_key) == ("entry", "queue")
    assert _view(initial_category="server").target_kind == "home"
    staff = _view(initial_category="server", extra_permissions=frozenset({"ticket_staff"}))
    assert staff.target_key == "server"
    assert "!ticket" in _copy(staff)
    assert "!welcome" not in _copy(staff)


def test_voice_examples_and_games_prefix_follow_server_settings():
    voice = _view(initial_category="voice")
    assert "</tts menu:123>" in _copy(voice)
    assert "!ttvmp3 bom dia~~" in _copy(voice)
    games = _view(initial_category="games", games_context={"mode": "commands", "channel_mention": "<#42>"})
    assert "`!ficha`" in _copy(games)
    assert "<#42>" in _copy(games)


def test_latest_attempt_is_ordered_and_scoped_to_person_and_server():
    bot, guild, other_guild = SimpleNamespace(), SimpleNamespace(id=1), SimpleNamespace(id=2)
    user = _owner()
    event = _event(102, user=user, guild=guild)
    remember_help_category(bot, event, "chatbot")
    remember_help_command(bot, event, SimpleNamespace(callback=SimpleNamespace(__module__="cogs.musica.modulo")))
    remember_help_category(bot, _event(101, user=user, guild=guild), "voice")
    assert last_help_category(bot, guild, user, before=103) == "music"
    assert last_help_category(bot, other_guild, user) is None
    assert last_help_category(bot, guild, _owner(11)) is None
    assert last_help_category(bot, guild, user, before=102) is None


@pytest.mark.asyncio
async def test_ficha_is_an_attempt_as_soon_as_games_recognizes_it():
    guild = SimpleNamespace(id=1)
    message = _event(101, guild=guild, content="ficha")
    router = GincanaMessageRouterMixin()
    router.bot = SimpleNamespace()

    async def send_profile(*_):
        assert last_help_category(router.bot, guild, message.author) == "games"

    router._send_chip_profile = AsyncMock(side_effect=send_profile)
    assert await router._handle_text_profile_commands(message)
    assert not await router._handle_text_profile_commands(_event(102, guild=guild, content="como vejo ficha?"))
    router._send_chip_profile.assert_awaited_once()


@pytest.mark.asyncio
async def test_prefix_help_uses_last_category_and_keeps_explicit_search():
    guild = SimpleNamespace(id=1)
    utility = Utility.__new__(Utility)
    utility.bot = SimpleNamespace()
    utility._get_prefix_data = AsyncMock(return_value={"bot_prefix": "!"})
    utility._get_help_prefix_data = utility._get_prefix_data
    utility._fetch_root_command_ids_cached = AsyncMock(return_value={})
    utility._get_help_extra_permissions = AsyncMock(return_value=frozenset())
    utility._help_hidden_categories = AsyncMock(return_value=frozenset())
    utility._get_help_games_context = lambda _: {"mode": "triggers"}
    utility._delete_message_safe = AsyncMock()

    attempt = _event(101, guild=guild, content="ficha")
    remember_help_category(utility.bot, attempt, "games")
    help_message = _event(102, guild=guild, content="!help")
    await utility.on_message(help_message)
    view = help_message.channel.send.await_args.kwargs["view"]
    assert (view.target_kind, view.target_key) == ("category", "games")

    explicit = _event(103, guild=guild, content="!help voz")
    await utility.on_message(explicit)
    view = explicit.channel.send.await_args.kwargs["view"]
    assert (view.target_kind, view.target_key) == ("category", "voice")
    assert last_help_category(utility.bot, guild, attempt.author) == "games"
