import asyncio
from datetime import datetime, timedelta, timezone
from itertools import count
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest
from discord.http import handle_message_parameters

from cogs.games.handlers.message_router import GincanaMessageRouterMixin
from cogs.utility import Utility
from utility import help_center
from utility.commands import help as help_commands
from utility.help_activity import consume_help_category, remember_help_category, remember_help_command
from utility.help_center import HelpCenterView


def _owner(user_id=10):
    return SimpleNamespace(id=user_id, bot=False)


class _CardMessage:
    ids = count(1000)

    def __init__(self, view=None, *, age=0):
        self.id = next(self.ids)
        self.created_at = discord.utils.utcnow() - timedelta(seconds=age)
        self.components = []
        self.edit = AsyncMock(side_effect=self.render)
        if view is not None:
            self.render(view=view)

    def render(self, *, view, **kwargs):
        self.components = [discord.components._component_factory(part) for part in view.to_components()]
        return self


def _event(event_id, *, user=None, guild=None, content=""):
    return SimpleNamespace(
        id=event_id, author=user or _owner(), guild=guild, content=content,
        channel=SimpleNamespace(send=AsyncMock(side_effect=_CardMessage().render)),
    )


def _id_at(ms, *, high=False):
    when = datetime(2026, 11, 21, tzinfo=timezone.utc) + timedelta(milliseconds=ms)
    return discord.utils.time_snowflake(when, high=high)


def _view(*, prefixes=None, **kwargs):
    return HelpCenterView(owner=_owner(), prefixes=prefixes or {"bot_prefix": "!"}, root_ids={"tts": 123}, **kwargs)


def _copy(view):
    return "\n".join(item.content for item in view.walk_children() if isinstance(item, discord.ui.TextDisplay))


def _selectors(view):
    return [item for item in view.walk_children() if isinstance(item, discord.ui.Select)]


def _buttons(view):
    return {item.label: item for item in view.walk_children() if isinstance(item, discord.ui.Button)}


def _control(custom_id, *, user_id=10, kind=discord.InteractionType.component, message=None, **data):
    message = message or _CardMessage()
    return SimpleNamespace(
        type=kind, user=_owner(user_id), guild=SimpleNamespace(id=1), message=message,
        channel=None, app_permissions=discord.Permissions.none(),
        data={"custom_id": custom_id, **data}, response=AsyncMock(),
        edit_original_response=AsyncMock(side_effect=message.render), delete_original_response=AsyncMock(),
    )


def test_slash_index_uses_one_selector_and_filters_staff():
    view = _view()
    select, = _selectors(view)
    assert select.placeholder == "Escolha uma área"
    assert [option.label for option in select.options] == [
        "Voz na call", "Música", "Jogos e fichas", "Conversa e imagens",
    ]
    assert all(option.label in _copy(view) for option in select.options)
    assert not any(option.default for option in select.options)
    assert all(option.emoji is None and not option.description.startswith("[") for option in select.options)
    assert list(_buttons(view)) == [None, "Dashboard"]
    assert "Buscar comando" not in _copy(view)

    staff = _view(extra_permissions=frozenset({"ticket_staff"}))
    select, = _selectors(staff)
    assert select.options[-1].label == "Servidor"


def test_prefix_index_has_copyable_shortcuts_and_working_controls():
    view = _view(source="prefix", extra_permissions=frozenset({"ticket_staff"}))
    assert isinstance(view.children[0], discord.ui.Container)
    assert _copy(view).startswith("## Meus comandos")
    for query in ("voz", "música", "jogos", "chat", "servidor"):
        assert f"`!help {query}`" in _copy(view)
    assert len(_selectors(view)) == 1
    assert list(_buttons(view)) == [None, "Dashboard"]
    assert "!help servidor" not in _copy(_view(source="prefix"))
    assert view.children[0].accent_color is None and _view().children[0].accent_color is None


def _emoji_cache():
    guild = SimpleNamespace(id=77, unavailable=False, me=SimpleNamespace(_roles=discord.utils.SnowflakeList([])))
    state = SimpleNamespace(_get_guild=lambda _: guild)
    records = (
        (1539137127852539944, "osaka", False), (1559374199133315122, "Vinyl_Music_loading", True),
        (1485041721573249135, "emoji_63", False), (1564113432905654353, "emoji_86", False),
        (1500584783779070143, "livro", True), (1548838423169605654, "x_mark", False),
    )
    cache = {key: discord.Emoji(guild=guild, state=state, data={"id": key, "name": name, "animated": animated})
             for key, name, animated in records}
    return guild, cache


@pytest.mark.parametrize("source", ["slash", "prefix"])
def test_guild_emojis_in_headings_and_native_options(source):
    guild, cache = _emoji_cache()
    emojis = help_center.available_help_emojis(SimpleNamespace(get_emoji=cache.get), guild, None, discord.Permissions.none())
    view = _view(source=source, emojis=emojis, extra_permissions={"ticket_staff"})
    options = _selectors(view)[0].options
    assert [option.emoji.id for option in options] == [
        1539137127852539944, 1559374199133315122, 1485041721573249135, 1564113432905654353, 1500584783779070143,
    ]
    close, dashboard = _buttons(view).values()
    assert close.style == discord.ButtonStyle.danger and close.label is None
    assert close.emoji.id == 1548838423169605654 and close.emoji.name == "x_mark"
    assert dashboard.style == discord.ButtonStyle.link and dashboard.url == "https://osaka.is-a.bot/"
    assert dashboard.custom_id is None
    assert [option.emoji.animated for option in options] == [False, True, False, False, True]
    assert all("<:" not in o.label and "<a:" not in o.label and "[" not in o.description for o in options)
    assert _copy(view).startswith("## <a:livro:1500584783779070143> Meus comandos")
    for option in options:
        area = _view(source=source, emojis=emojis, subject=option.value, extra_permissions={"ticket_staff"})
        assert _copy(area).startswith(f"## {option.emoji} {option.label}\n")
        assert _copy(area).count(str(option.emoji)) == 1


def test_external_emojis_respect_destination_permissions_and_source_availability():
    guild, cache = _emoji_cache()
    del cache[1564113432905654353]
    cache[1485041721573249135].available = False
    cache[1548838423169605654].available = False
    cache[1539137127852539944]._roles = discord.utils.SnowflakeList([444])
    other = SimpleNamespace(id=88, me=object())
    allow = discord.Permissions(external_emojis=True)
    deny = discord.Permissions.none()
    channel = SimpleNamespace(permissions_for=lambda member: allow if member is other.me else deny)
    bot = SimpleNamespace(get_emoji=cache.get)
    assert not help_center.available_help_emojis(bot, other, channel, deny)
    for destination, perms in ((guild, deny), (other, None), (None, None)):
        icons = help_center.available_help_emojis(bot, destination, channel, perms)
        assert set(icons) == {"home", "server", "music"}
        options = _selectors(_view(emojis=icons))[0].options
        assert options[0].emoji is None and options[1].emoji.animated
        assert options[2].emoji is None and options[3].emoji is None
        assert _buttons(_view(emojis=icons))[None].emoji.name == "✖️"


@pytest.mark.parametrize("source", ["prefix", "slash"])
@pytest.mark.asyncio
async def test_old_card_opens_full_music_list_and_returns_home_on_fresh_cog(source):
    original = _view(source=source, avatar_url="https://cdn.discordapp.com/embed/avatars/0.png")
    with handle_message_parameters(view=original) as params:
        assert params.payload["flags"] & discord.MessageFlags(components_v2=True).value
        assert params.payload["components"][0]["components"][0]["accessory"]["type"] == 11
    select, = _selectors(original)
    message = _CardMessage(original)
    interaction = _control(select.custom_id, values=["category:music"], message=message)
    await _utility().on_interaction(interaction)
    interaction.response.defer.assert_awaited_once()
    view = interaction.edit_original_response.await_args.kwargs["view"]
    assert view.source == source
    assert (view.target_kind, view.target_key) == ("category", "music")
    for command in ("play <nome ou link>", "queue", "volume [0-100]", "readd <posição>"):
        assert f"`!{command}`" in _copy(view)
    assert "`!skip` · Pula a música atual (`!pular`, `!s`)" in _copy(view)
    assert "!nightcore" not in _copy(view)
    assert list(_buttons(view)) == [None, "Início", "Dashboard"]
    select, = _selectors(view)
    assert next(option for option in select.options if option.default).value == "category:music"
    assert [option.value for option in select.options] == [
        "category:voice", "category:music", "category:games", "category:chatbot",
    ]

    home = _control(_buttons(view)["Início"].custom_id, message=message)
    await _utility().on_interaction(home)
    assert home.edit_original_response.await_args.kwargs["view"].target_kind == "home"


@pytest.mark.parametrize("source", ["prefix", "slash"])
def test_context_music_help_is_available_when_worker_is_offline(source):
    unavailable = frozenset({"music"})
    home = _view(source=source, hidden_categories=unavailable)
    assert "category:music" not in [option.value for select in _selectors(home) for option in select.options]
    assert "!help música" not in _copy(home)
    view = _view(source=source, initial_category="music", hidden_categories=unavailable)
    assert (view.target_kind, view.target_key) == ("category", "music")
    assert "!queue" in _copy(view) and "!volume" in _copy(view)
    assert "sem conexão com o player" in _copy(view)


def test_explicit_subject_and_permission_take_precedence():
    view = _view(subject="fila", initial_category="voice")
    assert (view.target_kind, view.target_key) == ("entry", "queue")
    assert _view(initial_category="server").target_kind == "home"
    staff = _view(initial_category="server", extra_permissions=frozenset({"ticket_staff"}))
    assert staff.target_key == "server"
    assert "!ticket" in _copy(staff)
    assert "!welcome" not in _copy(staff)
    assert _view(subject="!panel_server").target_key == "panel_server"
    assert _view(subject="!economia").target_key == "economia"


def test_slash_mentions_and_prefix_instructions_show_real_syntax():
    slash = _view(initial_category="voice")
    assert "</tts menu:123>" in _copy(slash)
    assert "`!ttvmp3 texto~~`" in _copy(slash)
    prefix = _view(source="prefix", initial_category="voice")
    assert "`/tts menu`" in _copy(prefix)
    assert "</tts menu:" not in _copy(prefix)
    assert "Pra voltar pelo chat" not in _copy(prefix)
    assert len(_selectors(prefix)) == 1
    assert "`/imagem <prompt>`" in _copy(_view(subject="/imagem"))


@pytest.mark.parametrize("source", ["prefix", "slash"])
def test_area_and_command_use_area_color_and_home_has_no_accent(source):
    colors = {
        "voice": discord.Color.green(), "music": discord.Color.blurple(), "games": discord.Color.orange(),
        "chatbot": discord.Color.fuchsia(), "server": discord.Color.gold(),
    }
    for area, color in colors.items():
        entry = next(e for e in help_center.ENTRIES if e.category == area)
        for subject in (f"category:{area}", f"entry:{entry.key}"):
            view = _view(source=source, subject=subject, extra_permissions={"ticket_staff"})
            assert view.to_components()[0]["accent_color"] == color.value
            assert "Pra voltar pelo chat" not in _copy(view)
            assert list(_buttons(view)) == [None, "Início", "Dashboard"]
    for subject in (None, "início", "zzzzzz"):
        view = _view(source=source, subject=subject)
        assert view.to_components()[0].get("accent_color") is None
        assert "Pra voltar pelo chat" not in _copy(view)


def test_help_and_registered_commands_can_have_different_prefixes():
    prefixes = {"bot_prefix": "!!", "registered_prefix": "!"}
    home = _view(source="prefix", prefixes=prefixes)
    assert "`!!help música`" in _copy(home)
    music = _view(source="prefix", prefixes=prefixes, subject="música")
    assert "`!queue`" in _copy(music)
    assert "`!!help início`" not in _copy(music)
    voice = _view(source="prefix", prefixes=prefixes, subject="voz")
    assert "`!!join`" in _copy(voice)
    assert _view(source="prefix", prefixes=prefixes, subject="!queue").target_key == "queue"
    assert _view(source="prefix", prefixes=prefixes, subject="!!join").target_key == "join"


def test_text_prefix_can_match_the_first_letter_of_an_area_or_command():
    prefixes = {"bot_prefix": "m", "registered_prefix": "f"}
    assert _view(source="prefix", prefixes=prefixes, subject="música").target_key == "music"
    games = _view(source="prefix", prefixes=prefixes, subject="jogos", games_context={"mode": "commands"})
    assert "`fficha`" in _copy(games)


@pytest.mark.parametrize(("mode", "ficha"), [("commands", "!ficha"), ("triggers", "ficha")])
def test_full_games_list_uses_correct_prefix_for_each_command(mode, ficha):
    body = _copy(_view(initial_category="games", games_context={"mode": mode, "channel_mention": "<#42>"}))
    assert f"`{ficha}`" in body
    assert "`!coinflip`" in body and "`!re`" in body
    assert "`!perfil`" in body and "`!saldo`" in body
    assert "<#42>" in body


def test_music_staff_sees_effects_and_chat_index_deduplicates_mentions():
    music = _view(initial_category="music", extra_permissions={"music_staff"})
    for command in ("nightcore [1-3]", "bassboost [1-6]", "reverb [1-3]", "voicestatus [ação] [valor]"):
        assert f"`!{command}`" in _copy(music)
    assert music.content_length() <= 4000
    assert _copy(_view(initial_category="chatbot")).count("`@bot mensagem`") == 1


def test_latest_attempt_is_ordered_and_scoped_to_person_and_server():
    bot, guild, other_guild = SimpleNamespace(), SimpleNamespace(id=1), SimpleNamespace(id=2)
    user = _owner()
    event = _event(102, user=user, guild=guild)
    remember_help_category(bot, event, "chatbot")
    remember_help_command(bot, event, SimpleNamespace(callback=SimpleNamespace(__module__="cogs.musica.modulo")))
    remember_help_category(bot, _event(101, user=user, guild=guild), "voice")
    assert consume_help_category(bot, _event(103, user=user, guild=other_guild)) is None
    assert consume_help_category(bot, _event(103, user=_owner(11), guild=guild)) is None
    assert consume_help_category(bot, _event(101, user=user, guild=guild)) is None
    assert consume_help_category(bot, _event(103, user=user, guild=guild)) == "music"
    remember_help_category(bot, event, "voice", registered=True)
    remember_help_category(bot, _event(103, user=user, guild=guild), "chatbot", registered=True)
    assert consume_help_category(bot, _event(104, user=user, guild=guild)) is None


@pytest.mark.parametrize(("age_ms", "expected"), [(299000, "voice"), (299999, "voice"), (300000, None), (300001, None)])
def test_five_minutes_count_from_the_most_recent_attempt(age_ms, expected):
    guild, bot = SimpleNamespace(id=1), SimpleNamespace()
    remember_help_category(bot, _event(_id_at(0), guild=guild), "music")
    remember_help_category(bot, _event(_id_at(180000, high=True), guild=guild), "voice")
    assert consume_help_category(bot, _event(_id_at(180000 + age_ms), guild=guild)) == expected
    assert consume_help_category(bot, _event(_id_at(180001 + age_ms), guild=guild)) is None


@pytest.mark.asyncio
async def test_ficha_is_an_attempt_as_soon_as_games_recognizes_it():
    guild = SimpleNamespace(id=1)
    message = _event(101, guild=guild, content="ficha")
    router = GincanaMessageRouterMixin()
    router.bot = SimpleNamespace()

    async def send_profile(*_):
        assert consume_help_category(router.bot, _event(102, guild=guild)) == "games"

    router._send_chip_profile = AsyncMock(side_effect=send_profile)
    assert await router._handle_text_profile_commands(message)
    assert not await router._handle_text_profile_commands(_event(102, guild=guild, content="como vejo ficha?"))
    router._send_chip_profile.assert_awaited_once()


def _utility():
    utility = Utility.__new__(Utility)
    utility.bot = SimpleNamespace(get_emoji=lambda _: None)
    utility._get_prefix_data = AsyncMock(return_value={"bot_prefix": "!"})
    utility._get_help_prefix_data = utility._get_prefix_data
    utility._fetch_root_command_ids_cached = AsyncMock(return_value={"tts": 123})
    utility._get_help_extra_permissions = AsyncMock(return_value=frozenset())
    utility._help_hidden_categories = AsyncMock(return_value=frozenset())
    utility._get_help_games_context = lambda _: {"mode": "triggers"}
    utility._delete_message_safe = AsyncMock()
    return utility


def _slash(event_id, guild):
    return SimpleNamespace(
        id=event_id, guild=guild, user=_owner(), channel=None, app_permissions=discord.Permissions.none(),
        response=SimpleNamespace(is_done=lambda: False, defer=AsyncMock()),
        edit_original_response=AsyncMock(side_effect=_CardMessage().render),
    )


@pytest.mark.asyncio
async def test_prefix_help_uses_last_category_and_keeps_explicit_search():
    guild = SimpleNamespace(id=1)
    utility = _utility()

    attempt = _event(101, guild=guild, content="ficha")
    remember_help_category(utility.bot, attempt, "games")
    help_message = _event(102, guild=guild, content="!help")
    await utility.on_message(help_message)
    view = help_message.channel.send.await_args.kwargs["view"]
    assert (view.target_kind, view.target_key) == ("category", "games")
    assert view.source == "prefix"
    assert len(_selectors(view)) == 1
    assert "`!coinflip`" in _copy(view)

    remember_help_category(utility.bot, _event(103, guild=guild), "games")
    explicit = _event(104, guild=guild, content="!help voz")
    await utility.on_message(explicit)
    view = explicit.channel.send.await_args.kwargs["view"]
    assert (view.target_kind, view.target_key) == ("category", "voice")
    second = _event(105, guild=guild, content="!help")
    await utility.on_message(second)
    assert second.channel.send.await_args.kwargs["view"].target_kind == "home"
    utility._fetch_root_command_ids_cached.assert_not_awaited()


@pytest.mark.asyncio
async def test_slash_help_uses_same_activity_but_keeps_its_layout():
    guild = SimpleNamespace(id=1)
    utility = _utility()
    attempt = _event(101, guild=guild)
    remember_help_category(utility.bot, attempt, "voice")

    interaction = _slash(102, guild)
    await utility.help_command.callback(utility, interaction)
    interaction.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
    view = interaction.edit_original_response.await_args.kwargs["view"]
    assert (view.source, view.target_kind, view.target_key) == ("slash", "category", "voice")
    assert len(_selectors(view)) == 1
    assert "</tts menu:123>" in _copy(view)
    again = _event(103, guild=guild, content="!help")
    await utility.on_message(again)
    assert again.channel.send.await_args.kwargs["view"].target_kind == "home"


@pytest.mark.asyncio
async def test_concurrent_help_consumes_once_and_preserves_later_attempt():
    utility, guild = _utility(), SimpleNamespace(id=1)
    remember_help_category(utility.bot, _event(100, guild=guild), "music")
    waiting, release = asyncio.Event(), asyncio.Event()
    first = _event(101, guild=guild, content="!help")

    async def hold_card(**kwargs):
        assert kwargs["view"].target_key == "music"
        waiting.set()
        await release.wait()
        return _CardMessage(kwargs["view"])

    first.channel.send.side_effect = hold_card
    task = asyncio.create_task(utility.on_message(first))
    try:
        await asyncio.wait_for(waiting.wait(), 2)
        second = _slash(102, guild)
        await utility.help_command.callback(utility, second)
        assert second.edit_original_response.await_args.kwargs["view"].target_kind == "home"
        remember_help_category(utility.bot, _event(103, guild=guild), "games")
    finally:
        release.set()
        await task
    third = _slash(104, guild)
    await utility.help_command.callback(utility, third)
    assert third.edit_original_response.await_args.kwargs["view"].target_key == "games"


@pytest.mark.asyncio
async def test_old_card_navigation_is_independent_of_next_help():
    utility, guild = _utility(), SimpleNamespace(id=1)
    remember_help_category(utility.bot, _event(_id_at(0), guild=guild), "music")
    first = _event(_id_at(1000), guild=guild, content="!help")
    await utility.on_message(first)
    card = first.channel.send.await_args.kwargs["view"]
    assert card.target_key == "music"
    remember_help_category(utility.bot, _event(_id_at(59000), guild=guild), "games")
    message = next(iter(utility.bot._open_help_cards.values())).message
    click = _control(_selectors(card)[0].custom_id, values=["category:voice"], message=message)
    click.id = _id_at(60000)
    await utility.on_interaction(click)
    assert click.edit_original_response.await_args.kwargs["view"].target_key == "voice"
    new = _slash(_id_at(61000), guild)
    await utility.help_command.callback(utility, new)
    assert new.edit_original_response.await_args.kwargs["view"].target_key == "games"


@pytest.mark.asyncio
async def test_explicit_slash_home_discards_the_pending_attempt():
    utility, guild = _utility(), SimpleNamespace(id=1)
    remember_help_category(utility.bot, _event(101, guild=guild), "music")
    explicit = _slash(102, guild)
    await utility.help_command.callback(utility, explicit, "início")
    assert explicit.edit_original_response.await_args.kwargs["view"].target_kind == "home"
    again = _event(103, guild=guild, content="!help")
    await utility.on_message(again)
    assert again.channel.send.await_args.kwargs["view"].target_kind == "home"


@pytest.mark.asyncio
async def test_discord_emoji_rejection_retries_same_category_in_plain_text():
    guild, cache = _emoji_cache()
    utility = _utility()
    utility.bot.get_emoji = cache.get
    remember_help_category(utility.bot, _event(101, guild=guild), "music")
    first = _slash(102, guild)
    invalid = discord.HTTPException(
        SimpleNamespace(status=400, reason="Bad Request"),
        {"code": 50035, "message": "Invalid Form Body", "errors": {"emoji": {"_errors": [{"message": "Invalid emoji"}]}}},
    )
    calls = []

    def send(*, view, **kwargs):
        calls.append((_copy(view), view.target_key))
        if len(calls) == 1:
            raise invalid
        return _CardMessage(view)

    first.edit_original_response.side_effect = send
    await utility.help_command.callback(utility, first)
    (decorated_text, first_key), (plain_text, second_key) = calls
    assert first_key == second_key == "music"
    assert decorated_text.startswith("## <a:Vinyl_Music_loading:")
    assert plain_text.startswith("## Música")
    plain = first.edit_original_response.await_args.kwargs["view"]
    assert all(opt.emoji is None for opt in _selectors(plain)[0].options)
    assert _buttons(plain)[None].emoji.name == "✖️"
    assert consume_help_category(utility.bot, _event(103, guild=guild)) is None


@pytest.mark.parametrize("source", ["prefix", "slash"])
@pytest.mark.asyncio
async def test_close_deletes_message_and_cancels_expiry(source):
    utility = _utility()
    view = _view(source=source, subject="cantar")
    message = _CardMessage(view)
    card = utility._track_help_card(message, message.edit)
    close = _control(_buttons(view)[None].custom_id, message=message)
    await utility.on_interaction(close)
    close.response.defer.assert_awaited_once_with()
    close.delete_original_response.assert_awaited_once_with()
    close.edit_original_response.assert_not_called()
    assert card.closed and not utility.bot._open_help_cards
    await asyncio.gather(card.timer, return_exceptions=True)
    message.edit.assert_not_awaited()


@pytest.mark.asyncio
async def test_other_members_cannot_change_or_close_a_public_card():
    view = _view(source="prefix", subject="voz")
    ids = [button.custom_id for button in _buttons(view).values() if button.custom_id] + [_selectors(view)[0].custom_id]
    for custom_id in ids:
        click = _control(custom_id, user_id=11)
        await _utility().on_interaction(click)
        assert click.response.send_message.await_args.kwargs["ephemeral"]
        click.response.send_modal.assert_not_called()
        click.edit_original_response.assert_not_called()
        click.delete_original_response.assert_not_called()


@pytest.mark.asyncio
async def test_click_rechecks_permissions_and_preserves_recent_offline_music():
    utility = _utility()
    utility._help_hidden_categories.return_value = frozenset({"music"})
    old_staff = _view(extra_permissions={"ticket_staff"}, initial_category="music")
    select, = _selectors(old_staff)
    revoked = _control(select.custom_id, values=["category:server"])
    await utility.on_interaction(revoked)
    home = revoked.edit_original_response.await_args.kwargs["view"]
    assert home.target_kind == "home"
    assert "Servidor" not in _copy(home)
    music = _control(_selectors(home)[0].custom_id, values=["category:music"])
    await utility.on_interaction(music)
    assert "sem conexão com o player" in _copy(music.edit_original_response.await_args.kwargs["view"])


def _controls(view):
    return [item for item in view.walk_children() if isinstance(item, (discord.ui.Select, discord.ui.Button))]


@pytest.mark.parametrize("source", ["prefix", "slash"])
@pytest.mark.asyncio
async def test_timeout_disables_latest_page_including_dashboard_without_another_click(source, monkeypatch):
    assert help_commands.DURACAO_PAINEL_PUBLICO_TTS == 300
    monkeypatch.setattr(help_commands, "DURACAO_PAINEL_PUBLICO_TTS", 0.5)
    utility, guild = _utility(), SimpleNamespace(id=1)
    if source == "slash":
        await utility.help_command.callback(utility, _slash(101, guild))
    else:
        await utility.on_message(_event(101, guild=guild, content="!help"))
    card, = utility.bot._open_help_cards.values()
    deadline = card.deadline
    first = discord.ui.LayoutView.from_message(card.message)
    click = _control(_selectors(first)[0].custom_id, message=card.message, values=["category:games"])
    await utility.on_interaction(click)
    assert card.deadline == deadline
    before = discord.ui.LayoutView.from_message(card.message)
    await asyncio.wait_for(card.timer, 3)
    assert card.closed and not utility.bot._open_help_cards
    after = card.message.edit.await_args.kwargs["view"]
    assert _copy(after) == _copy(before) and "Jogos e fichas" in _copy(after)
    assert before.to_components()[0] == after.to_components()[0]
    assert not any(item.disabled for item in _controls(before))
    assert all(item.disabled for item in _controls(after))
    assert _buttons(after)["Dashboard"].url == "https://osaka.is-a.bot/"
    assert _selectors(after)[0].options[2].default
    card.message.edit.assert_awaited_once()


@pytest.mark.parametrize("action", ["area", "close", "search"])
@pytest.mark.asyncio
async def test_overdue_message_on_fresh_bot_freezes_in_place_even_for_legacy_controls(action):
    utility = _utility()
    view = _view(source="prefix", subject="música")
    message = _CardMessage(view, age=300)
    custom_id = _buttons(view)[None].custom_id.removesuffix(":close") + ":" + action
    click = _control(custom_id, message=message, values=["category:voice"])
    await utility.on_interaction(click)
    frozen = click.edit_original_response.await_args.kwargs["view"]
    assert _copy(frozen) == _copy(view)
    assert all(item.disabled for item in _controls(frozen))
    click.delete_original_response.assert_not_awaited()
    utility._get_help_prefix_data.assert_not_awaited()
    assert not utility.bot._open_help_cards


@pytest.mark.asyncio
async def test_navigation_in_flight_cannot_reactivate_expired_message(monkeypatch):
    utility = _utility()
    view = _view(source="prefix", subject="música")
    message = _CardMessage(view)
    entered, release = asyncio.Event(), asyncio.Event()
    monkeypatch.setattr(help_commands, "DURACAO_PAINEL_PUBLICO_TTS", 0.25)
    card = utility._track_help_card(message, message.edit)

    async def hold_build(**kwargs):
        entered.set()
        await release.wait()
        return _view(subject=kwargs["subject"])

    utility._build_help_view = hold_build
    click = _control(_selectors(view)[0].custom_id, message=message, values=["category:voice"])
    task = asyncio.create_task(utility.on_interaction(click))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        await asyncio.wait_for(card.timer, 2)
    finally:
        release.set()
        await task
    click.edit_original_response.assert_not_awaited()
    frozen = message.edit.await_args.kwargs["view"]
    assert _copy(frozen) == _copy(view) and all(item.disabled for item in _controls(frozen))


@pytest.mark.asyncio
async def test_removed_emoji_does_not_block_disabling_the_dashboard_and_close():
    utility = _utility()
    guild, cache = _emoji_cache()
    icons = help_center.available_help_emojis(SimpleNamespace(get_emoji=cache.get), guild, None, discord.Permissions.none())
    message = _CardMessage(_view(emojis=icons, subject="jogos"), age=300)
    calls = []

    def edit(*, view, **kwargs):
        close = _buttons(view)[None]
        calls.append((close.emoji.id, close.style, all(control.disabled for control in _controls(view))))
        if close.emoji.id:
            raise discord.HTTPException(SimpleNamespace(status=400, reason="Bad Request"),
                                        {"code": 10014, "message": "Unknown Emoji"})
        return message.render(view=view)

    card = utility._track_help_card(message, AsyncMock(side_effect=edit))
    await asyncio.wait_for(card.timer, 1)
    assert calls == [(1548838423169605654, discord.ButtonStyle.danger, True), (None, discord.ButtonStyle.danger, True)]
    assert not utility.bot._open_help_cards


@pytest.mark.asyncio
async def test_already_acknowledged_slash_expires_its_followup():
    utility, guild = _utility(), SimpleNamespace(id=1)
    slash = _slash(101, guild)
    slash.response.is_done = lambda: True
    followup = _CardMessage(age=300)
    slash.followup = SimpleNamespace(send=AsyncMock(side_effect=followup.render))
    await utility.help_command.callback(utility, slash)
    sent = slash.followup.send.await_args.kwargs
    assert sent["ephemeral"] and sent["wait"]
    await asyncio.wait_for(utility.bot._open_help_cards[followup.id].timer, 1)
    assert all(item.disabled for item in _controls(followup.edit.await_args.kwargs["view"]))
    slash.edit_original_response.assert_not_awaited()
