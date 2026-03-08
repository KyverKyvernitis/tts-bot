import asyncio
import os
from datetime import datetime, timezone

import discord
from discord.ext import commands

import config
from db import SettingsDB


print("BOT.PY INICIOU")


class BotLocal(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.guilds = True
        intents.members = True
        intents.voice_states = True
        intents.messages = True

        super().__init__(
            command_prefix=commands.when_mentioned_or("!"),
            intents=intents,
            help_command=None,
        )

        self.started_at = datetime.now(timezone.utc)
        self.settings_db: SettingsDB | None = None

    async def setup_hook(self):
        print("SETUP_HOOK INICIOU")

        self.settings_db = SettingsDB(
            config.MONGO_URI,
            config.MONGO_DB_NAME,
            config.MONGO_COLLECTION_NAME,
        )
        await self.settings_db.init()

        print("Carregando cogs...")

        loaded = []
        cogs_dir = os.path.join(os.path.dirname(__file__), "cogs")
        for filename in sorted(os.listdir(cogs_dir)):
            if not filename.endswith(".py") or filename.startswith("_"):
                continue

            ext = f"cogs.{filename[:-3]}"
            try:
                await self.load_extension(ext)
                loaded.append(ext)
            except Exception as e:
                print(f"[bot] falha ao carregar {ext}: {e}")
                raise

        synced = await self.tree.sync()
        print(f"[SYNC] Slash commands sincronizados globalmente: {len(synced)}")
        for cmd in synced:
            name = getattr(cmd, "name", None) or str(cmd)
            print(f"[SYNC] /{name}")

    async def on_ready(self):
        print(f"Logado como {self.user} (id: {self.user.id})")
        print(f"Em {len(self.guilds)} servidor(es)")

    async def on_app_command_error(
        self,
        interaction: discord.Interaction,
        error: discord.app_commands.AppCommandError,
    ):
        print(f"[APP_COMMAND_ERROR] {error!r}")
        try:
            if interaction.response.is_done():
                await interaction.followup.send(
                    f"Erro ao executar o comando: {error}",
                    ephemeral=True,
                )
            else:
                await interaction.response.send_message(
                    f"Erro ao executar o comando: {error}",
                    ephemeral=True,
                )
        except Exception as e:
            print(f"[APP_COMMAND_ERROR] Falha ao responder ao usuário: {e!r}")


async def main():
    print("MAIN INICIOU")
    bot = BotLocal()
    await bot.start(config.TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
