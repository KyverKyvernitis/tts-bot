import threading
import asyncio

import discord
from discord.ext import commands

import config
from db import SettingsDB
from webserver import create_app

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.voice_states = True

bot = commands.Bot(command_prefix="!", intents=intents)

def run_web():
    app = create_app()
    app.run(host="0.0.0.0", port=config.PORT)

@bot.event
async def on_ready():
    print(f"Logado como {bot.user} (id: {bot.user.id})")

async def main():
    # Mongo
    if not config.MONGODB_URI:
        raise RuntimeError("Faltou MONGODB_URI")

    bot.settings_db = SettingsDB(config.MONGODB_URI, config.MONGODB_DB, config.MONGODB_COLLECTION)
    await bot.settings_db.init()

    # Carregar cogs
    await bot.load_extension("cogs.role_cooldown")
    await bot.load_extension("cogs.antimzk")
    await bot.load_extension("cogs.tts_voice")

    # Sync rápido por guild
    for gid in config.GUILD_IDS:
        guild_obj = discord.Object(id=gid)
        bot.tree.copy_global_to(guild=guild_obj)
        await bot.tree.sync(guild=guild_obj)

    threading.Thread(target=run_web, daemon=True).start()
    await bot.start(config.TOKEN)

if __name__ == "__main__":
    if not config.TOKEN or config.TARGET_ROLE_ID == 0:
        raise RuntimeError("Faltou DISCORD_TOKEN e/ou ROLE_ID")
    asyncio.run(main())
