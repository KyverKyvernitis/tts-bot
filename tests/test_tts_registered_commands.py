from __future__ import annotations

import asyncio
from types import SimpleNamespace

import discord
from discord.ext import commands
import pytest

import config
from cogs.tts.mensagens.triagem import analisar_mensagem_para_tts


class _Cog:
    def __init__(self, bot, defaults):
        self.bot = bot
        self.defaults = defaults

    def _get_db(self):
        return self

    async def get_guild_tts_defaults(self, guild_id):
        return self.defaults

    async def _maybe_await(self, value):
        return await value


def _message(bot, content):
    return SimpleNamespace(
        content=content,
        author=SimpleNamespace(id=123, bot=False),
        guild=SimpleNamespace(id=7),
        _state=getattr(bot, "_connection", None),
    )


@pytest.mark.parametrize(
    "content",
    ["_base", "_status", "_status tts", "_presenca", "_presence", "!base", "<@9001> status", "<@!9001> base"],
)
def test_registered_commands_are_silent_even_with_speech_prefix_conflict(content, monkeypatch):
    monkeypatch.setattr(config, "TTS_ENABLED", True)

    async def scenario():
        bot = commands.Bot(
            command_prefix=commands.when_mentioned_or("_", "!"),
            intents=discord.Intents.none(),
            help_command=None,
        )
        bot._connection.user = SimpleNamespace(id=9001)
        invoked = []
        checked = []

        async def owner_check(ctx):
            checked.append(ctx)
            return False

        async def callback(ctx):
            invoked.append(ctx)

        for name, aliases in (("base", []), ("status", []), ("presenca", ["presence"])):
            command = commands.Command(callback, name=name, aliases=aliases)
            command.add_check(owner_check)
            bot.add_command(command)
        cog = _Cog(bot, {"bot_prefix": "!", "gtts_prefix": "_", "tts_prefix": "_"})
        try:
            decision = await analisar_mensagem_para_tts(cog, _message(bot, content))
            assert decision.reason == "registered_bot_command"
            assert not decision.should_process_tts
            assert not decision.should_dispatch_prefix_command
            assert decision.prefix_command is None
            assert not invoked
            assert not checked
        finally:
            await bot.close()

    asyncio.run(scenario())


def test_unregistered_speech_still_uses_the_configured_engine(monkeypatch):
    monkeypatch.setattr(config, "TTS_ENABLED", True)

    async def scenario():
        bot = commands.Bot(command_prefix="_", intents=discord.Intents.none(), help_command=None)
        bot._connection.user = SimpleNamespace(id=9001)
        cog = _Cog(bot, {"bot_prefix": "!", "gtts_prefix": "_"})
        try:
            decision = await analisar_mensagem_para_tts(cog, _message(bot, "_baseado em fatos"))
            assert decision.should_process_tts
            assert not decision.should_dispatch_prefix_command
            assert decision.forced_engine == "gtts"
        finally:
            await bot.close()

    asyncio.run(scenario())


def test_missing_context_parser_keeps_existing_tts_behavior(monkeypatch):
    monkeypatch.setattr(config, "TTS_ENABLED", True)

    async def scenario():
        bot = SimpleNamespace()
        cog = _Cog(bot, {})
        decision = await analisar_mensagem_para_tts(cog, _message(bot, ".olá"))
        assert decision.should_process_tts
        assert decision.forced_engine == "gtts"

    asyncio.run(scenario())


def test_tts_controls_keep_their_dispatcher_before_general_command_parsing(monkeypatch):
    monkeypatch.setattr(config, "TTS_ENABLED", True)

    async def get_context(message):
        raise AssertionError("o controle TTS já deveria ter sido reconhecido")

    async def scenario():
        bot = SimpleNamespace(get_context=get_context)
        cog = _Cog(bot, {"bot_prefix": "_"})
        decision = await analisar_mensagem_para_tts(cog, _message(bot, "_join"))
        assert not decision.should_process_tts
        assert decision.should_dispatch_prefix_command
        assert decision.prefix_command.kind == "join"

    asyncio.run(scenario())
