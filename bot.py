import asyncio
import os
import threading
from datetime import datetime, timezone

import discord
from discord.ext import commands

import config
from db import SettingsDB
from webserver import run_webserver


print("BOT.PY INICIOU")


def _cfg(*names: str, default=None):
    for name in names:
        if hasattr(config, name):
            return getattr(config, name)
    return default


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

        mongo_uri = _cfg("MONGODB_URI", "MONGO_URI")
        mongo_db_name = _cfg("MONGO_DB_NAME", "MONGODB_DB_NAME", default="chat_revive")
        mongo_collection_name = _cfg("MONGO_COLLECTION_NAME", "MONGODB_COLLECTION_NAME", default="settings")

        if not mongo_uri:
            raise RuntimeError("Nenhuma URI do MongoDB encontrada no config.py (MONGODB_URI/MONGO_URI).")

        self.settings_db = SettingsDB(
            mongo_uri,
            mongo_db_name,
            mongo_collection_name,
        )
        await self.settings_db.init()

        print("Carregando cogs...")

        cogs_dir = os.path.join(os.path.dirname(__file__), "cogs")
        for filename in sorted(os.listdir(cogs_dir)):
            if not filename.endswith(".py") or filename.startswith("_"):
                continue

            ext = f"cogs.{filename[:-3]}"
            try:
                await self.load_extension(ext)
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

    web_thread = threading.Thread(target=run_webserver, daemon=True)
    web_thread.start()

    bot = BotLocal()
    await bot.start(config.TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
