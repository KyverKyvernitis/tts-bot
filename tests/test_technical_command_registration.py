from __future__ import annotations

import asyncio
from types import SimpleNamespace

import discord
from discord.ext import commands

from cogs.application_presence_admin import ApplicationPresenceAdminCog
from cogs.utility import Utility
from utility.application_presence import ApplicationPresenceService
from utility.technical_commands import TECHNICAL_COMMAND_GUILD_ID


def test_utility_and_presence_register_together_without_vps(tmp_path, monkeypatch):
    # Loading the real cogs verifies registration and alias ownership, without
    # starting the worker wake service or connecting to Discord.
    monkeypatch.setattr(Utility, "_start_core_worker_auto_wake_task", lambda self: None)

    async def scenario():
        bot = commands.Bot(
            command_prefix=commands.when_mentioned_or("_", "!"),
            intents=discord.Intents.none(), help_command=None,
        )
        bot._connection.user = SimpleNamespace(id=9001)
        bot.application_presence = ApplicationPresenceService(
            bot, tmp_path / "runtime.json", tmp_path / "presence.json",
        )
        utility = Utility(bot)
        presence = ApplicationPresenceAdminCog(bot)
        try:
            await bot.add_cog(utility)
            await bot.add_cog(presence)
            assert bot.get_command("base").cog is utility
            assert bot.get_command("status").cog is utility
            assert bot.get_command("base").hidden
            assert bot.get_command("status").hidden
            assert bot.get_command("presenca").cog is presence
            assert bot.get_command("presence") is bot.get_command("presenca")
            assert bot.tree.get_command("vps") is None
            assert bot.tree.get_command("vps", guild=discord.Object(id=TECHNICAL_COMMAND_GUILD_ID)) is None
            for content in ("_base", "_status tts", "!status", "<@9001> base", "_presenca", "_presence"):
                message = SimpleNamespace(
                    content=content, author=SimpleNamespace(id=123),
                    _state=bot._connection,
                )
                context = await bot.get_context(message)
                assert context.valid, content
        finally:
            await bot.close()

    asyncio.run(scenario())
