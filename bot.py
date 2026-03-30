import asyncio
import os
import threading
from datetime import datetime, timezone

import discord
from discord.ext import commands

import config
from db import SettingsDB
from webserver import run_webserver, set_health_provider


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
            command_prefix=commands.when_mentioned_or(
                getattr(config, "BOT_PREFIX", "_"),
                getattr(config, "PREFIX", "_"),
            ),
            intents=intents,
            help_command=None,
        )

        self.started_at = datetime.now(timezone.utc)
        self.settings_db: SettingsDB | None = None
        self.health_state: dict[str, object] = {
            "status": "starting",
            "healthy": True,
            "starting": True,
            "discord_ready": False,
            "discord_closed": False,
            "guild_count": 0,
            "latency_ms": None,
            "mongo_ok": False,
            "mongo_error": None,
            "last_update": None,
        }
        self._health_task: asyncio.Task | None = None
        set_health_provider(self.get_health_snapshot)

    async def setup_hook(self):
        print("SETUP_HOOK INICIOU")

        mongo_uri = _cfg("MONGODB_URI", "MONGO_URI")
        mongo_db_name = _cfg("MONGODB_DB", "MONGO_DB_NAME", "MONGODB_DB_NAME", default="chat_revive")
        mongo_collection_name = _cfg("MONGODB_COLLECTION", "MONGO_COLLECTION_NAME", "MONGODB_COLLECTION_NAME", default="settings")

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
        extensions = []

        for entry in sorted(os.listdir(cogs_dir)):
            if entry.startswith("_"):
                continue

            full_path = os.path.join(cogs_dir, entry)

            if entry.endswith(".py"):
                module_name = entry[:-3]
                ext = f"cogs.{module_name}"
                extensions.append(ext)
                continue

            if os.path.isdir(full_path):
                init_py = os.path.join(full_path, "__init__.py")
                if entry != "tts" and os.path.isfile(init_py):
                    extensions.append(f"cogs.{entry}")

        # TTS foi reorganizado para um pacote próprio em cogs/tts.
        extensions.extend([
            "cogs.tts.cog",
            "cogs.tts.toggle",
        ])

        for ext in extensions:
            try:
                await self.load_extension(ext)
            except Exception as e:
                print(f"[bot] falha ao carregar {ext}: {e}")
                raise

        should_sync = str(os.getenv("SYNC_SLASH_COMMANDS", "false")).strip().lower() in {"1", "true", "yes", "on"}
        allow_global_sync = str(os.getenv("SYNC_GLOBAL_SLASH_COMMANDS", "false")).strip().lower() in {"1", "true", "yes", "on"}
        if should_sync:
            health_guild_id = 927002914449424404
            guild_ids = {int(gid) for gid in (getattr(config, "GUILD_IDS", []) or []) if gid}
            guild_ids.add(health_guild_id)

            if allow_global_sync:
                synced_global = await self.tree.sync()
                print(f"[SYNC] Slash commands sincronizados globalmente: {len(synced_global)}")
                for cmd in synced_global:
                    name = getattr(cmd, "name", None) or str(cmd)
                    print(f"[SYNC][GLOBAL] /{name}")
            else:
                print("[SYNC] Sync global pulado para preservar o Entry Point da Activity do Discord.")
                print("[SYNC] Use SYNC_GLOBAL_SLASH_COMMANDS=true somente se você souber preservar manualmente o comando Launch.")

            for guild_id in sorted(guild_ids):
                guild_obj = discord.Object(id=guild_id)
                self.tree.copy_global_to(guild=guild_obj)
                synced_guild = await self.tree.sync(guild=guild_obj)
                print(f"[SYNC] Slash commands sincronizados na guild {guild_id}: {len(synced_guild)}")
                for cmd in synced_guild:
                    name = getattr(cmd, "name", None) or str(cmd)
                    print(f"[SYNC][GUILD {guild_id}] /{name}")
        else:
            print("[SYNC] Pulado no boot (defina SYNC_SLASH_COMMANDS=true para sincronizar no startup)")
            print("[SYNC] Observação: comandos limitados por guild, como /health, só aparecem após sync da guild correspondente.")

    def get_health_snapshot(self) -> dict[str, object]:
        snapshot = dict(self.health_state)
        uptime_seconds = (datetime.now(timezone.utc) - self.started_at).total_seconds()
        snapshot["uptime_seconds"] = round(uptime_seconds, 2)
        ready = bool(snapshot.get("discord_ready"))
        closed = bool(snapshot.get("discord_closed"))
        mongo_ok = bool(snapshot.get("mongo_ok"))

        starting = (not ready) and uptime_seconds < 120
        healthy = (ready and not closed and mongo_ok) or starting

        snapshot["starting"] = starting
        snapshot["healthy"] = healthy
        snapshot["status"] = "starting" if starting else ("ok" if healthy else "error")

        tts_cog = self.get_cog("TTSVoice")
        if tts_cog is not None and hasattr(tts_cog, "get_tts_metrics_snapshot"):
            try:
                snapshot["tts_metrics"] = tts_cog.get_tts_metrics_snapshot()
            except Exception as e:
                snapshot["tts_metrics_error"] = str(e)
        return snapshot

    async def _health_monitor_loop(self):
        while not self.is_closed():
            mongo_ok = False
            mongo_error = None
            try:
                if self.settings_db is not None:
                    await self.settings_db.client.admin.command("ping")
                    mongo_ok = True
                else:
                    mongo_error = "settings_db not initialized"
            except Exception as e:
                mongo_error = str(e)

            latency_ms = None
            try:
                latency_ms = round(float(self.latency) * 1000, 2)
            except Exception:
                pass

            self.health_state.update({
                "discord_ready": self.is_ready(),
                "discord_closed": self.is_closed(),
                "guild_count": len(self.guilds),
                "latency_ms": latency_ms,
                "mongo_ok": mongo_ok,
                "mongo_error": mongo_error,
                "last_update": datetime.now(timezone.utc).isoformat(),
            })
            await asyncio.sleep(15)


    async def on_ready(self):
        print(f"Logado como {self.user} (id: {self.user.id})")
        print(f"Em {len(self.guilds)} servidor(es)")
        try:
            await self.change_presence(
                activity=discord.Activity(
                    type=discord.ActivityType.listening,
                    name="/help | _help",
                )
            )
        except Exception as e:
            print(f"[bot] falha ao aplicar presence: {e!r}")
        if self._health_task is None or self._health_task.done():
            self._health_task = asyncio.create_task(self._health_monitor_loop())



    async def on_message(self, message: discord.Message):
        if getattr(message.author, "bot", False):
            return
        try:
            await self.process_commands(message)
        except Exception as e:
            print(f"[bot] falha ao processar comandos: {e!r}")

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
