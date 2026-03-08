import asyncio
from datetime import datetime, timezone
from pathlib import Path
import threading

import discord
from discord.ext import commands

import config
from db import SettingsDB


def _cfg(*names, default=None):
    for name in names:
        if hasattr(config, name):
            value = getattr(config, name)
            if value is not None:
                return value
    return default


def _start_webserver_if_available():
    try:
        import webserver

        if hasattr(webserver, "keep_alive"):
            threading.Thread(target=webserver.keep_alive, daemon=True).start()
            print("WEB SERVER INICIANDO")
            return

        if hasattr(webserver, "run"):
            threading.Thread(target=webserver.run, daemon=True).start()
            print("WEB SERVER INICIANDO")
            return
    except Exception as e:
        print(f"[bot] webserver indisponível: {e}")


class ChatReviveBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.guilds = True
        intents.guild_messages = True
        intents.message_content = True
        intents.members = True
        intents.voice_states = True

        super().__init__(command_prefix=_cfg("PREFIX", default="!"), intents=intents)
        self.started_at = datetime.now(timezone.utc)
        self.settings_db = None

    async def setup_hook(self):
        print("SETUP_HOOK INICIOU")

        mongo_uri = _cfg("MONGO_URI", "MONGODB_URI", "MONGO_URL")
        db_name = _cfg("DB_NAME", "MONGO_DB_NAME", "MONGODB_DB_NAME", default="chat_revive")
        coll_name = _cfg("SETTINGS_COLLECTION", "SETTINGS_COLL_NAME", "SETTINGS_COLLECTION_NAME", default="settings")

        if mongo_uri:
            try:
                self.settings_db = SettingsDB(mongo_uri, db_name, coll_name)
                await self.settings_db.init()
            except Exception as e:
                print(f"[bot] falha ao iniciar SettingsDB: {e}")
                self.settings_db = None
        else:
            print("[bot] MONGO_URI/MONGODB_URI não configurado; SettingsDB desativado.")

        print("Carregando cogs...")
        cogs_dir = Path("cogs")
        loaded = []
        if cogs_dir.exists():
            for file in sorted(cogs_dir.glob("*.py")):
                if file.name.startswith("_"):
                    continue
                ext = f"cogs.{file.stem}"
                try:
                    await self.load_extension(ext)
                    loaded.append(ext)
                except Exception as e:
                    print(f"[bot] falha ao carregar {ext}: {e}")
                    raise
        else:
            print("[bot] pasta cogs não encontrada.")

        try:
            synced = await self.tree.sync()
            print(f"[SYNC] Slash commands sincronizados globalmente: {len(synced)}")
            for cmd in synced:
                print(f"[SYNC] /{cmd.qualified_name}")
        except Exception as e:
            print(f"[bot] falha ao sincronizar slash commands: {e}")
            raise

    async def on_ready(self):
        print(f"Logado como {self.user} (id: {self.user.id})")
        print(f"Em {len(self.guilds)} servidor(es)")

    async def on_app_command_error(self, interaction: discord.Interaction, error):
        print(f"[APP_COMMAND_ERROR] {error!r}")
        try:
            if interaction.response.is_done():
                await interaction.followup.send("Ocorreu um erro.", ephemeral=True)
            else:
                await interaction.response.send_message("Ocorreu um erro.", ephemeral=True)
        except Exception as e:
            print(f"[APP_COMMAND_ERROR] Falha ao responder ao usuário: {e!r}")


async def main():
    print("MAIN INICIOU")
    _start_webserver_if_available()
    bot = ChatReviveBot()
    await bot.start(config.TOKEN)


if __name__ == "__main__":
    print("BOT.PY INICIOU")
    asyncio.run(main())
