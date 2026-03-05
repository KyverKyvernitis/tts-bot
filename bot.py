import os
import asyncio
import threading
from flask import Flask

import discord
from discord.ext import commands
from discord import app_commands

from motor.motor_asyncio import AsyncIOMotorClient

# -------------------------
# VARIÁVEIS DE AMBIENTE
# -------------------------
TOKEN = os.getenv("DISCORD_TOKEN")

# Função original do repo: ao mencionar um cargo, desativa menções por um tempo
TARGET_ROLE_ID = int(os.getenv("ROLE_ID", "0"))
DISABLE_TIME = int(os.getenv("DISABLE_TIME", "14400"))

# Gatilhos de voz (anti-mzk)
TRIGGER_WORD = os.getenv("TRIGGER_WORD", "").lower().strip()               # palavra que desconecta
MUTE_TOGGLE_WORD = os.getenv("MUTE_TOGGLE_WORD", "rola").lower().strip()   # palavra que mute/desmute (toggle)
TARGET_USER_ID = int(os.getenv("TARGET_USER_ID", "0"))                     # ID do usuário alvo

# Porta HTTP (a Render costuma fornecer PORT automaticamente)
PORT = int(os.getenv("PORT", "10000"))

# MongoDB (persistência das configurações)
MONGODB_URI = os.getenv("MONGODB_URI", "").strip()
MONGODB_DB = os.getenv("MONGODB_DB", "chat_revive").strip()
MONGODB_COLLECTION = os.getenv("MONGODB_COLLECTION", "settings").strip()

# Cores dos embeds (hex)
ON_COLOR = discord.Color(0x57F287)   # verde (ativado)
OFF_COLOR = discord.Color(0xED4245)  # vermelho (desativado)

# Servidores para sincronizar slash commands rapidamente
GUILD_IDS = [
    1313883930637762560,
    1349910251117350923,
]

# -------------------------
# SERVIDOR WEB (healthcheck)
# -------------------------
app = Flask(__name__)


@app.get("/")
def home():
    return "OK", 200


@app.get("/health")
def health():
    return "healthy", 200


def run_web():
    # Na Render, o host precisa ser 0.0.0.0
    app.run(host="0.0.0.0", port=PORT)


# -------------------------
# BOT DO DISCORD
# -------------------------
intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True


class MyBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents)

        # Controle do cooldown do cargo (função original)
        self.cooldown_active = False

        # Conexão/coleção do MongoDB
        self.mongo_client: AsyncIOMotorClient | None = None
        self.settings_coll = None

        # Cache em memória das configurações por servidor:
        # { guild_id: {"anti_mzk_enabled": bool} }
        self.guild_settings: dict[int, dict] = {}

    async def setup_hook(self):
        # 1) Conectar no MongoDB
        if not MONGODB_URI:
            raise RuntimeError("Faltou a variável de ambiente: MONGODB_URI")

        self.mongo_client = AsyncIOMotorClient(MONGODB_URI)
        db = self.mongo_client[MONGODB_DB]
        self.settings_coll = db[MONGODB_COLLECTION]

        # 2) Criar índice único por guild_id (recomendado)
        try:
            await self.settings_coll.create_index("guild_id", unique=True)
        except Exception:
            pass

        # 3) Carregar configurações do Mongo para o cache
        await self._load_settings_cache()

        # 4) Sincronizar comandos de barra por guild (aparece rápido)
        for gid in GUILD_IDS:
            guild_obj = discord.Object(id=gid)
            try:
                self.tree.copy_global_to(guild=guild_obj)
                synced = await self.tree.sync(guild=guild_obj)
                print(f"Sincronizados {len(synced)} comandos de barra no servidor {gid}.")
            except Exception as e:
                print(f"Falha ao sincronizar comandos no servidor {gid}: {e}")

    async def _load_settings_cache(self):
        """Carrega as configurações do MongoDB para memória."""
        self.guild_settings.clear()
        cursor = self.settings_coll.find({}, {"_id": 0})
        async for doc in cursor:
            gid = int(doc.get("guild_id"))
            anti = bool(doc.get("anti_mzk_enabled", True))
            self.guild_settings[gid] = {"anti_mzk_enabled": anti}
        print(f"Configurações carregadas do Mongo: {self.guild_settings}")

    def is_anti_mzk_enabled(self, guild_id: int) -> bool:
        """Retorna se a censura anti-mzk está ativada no servidor. Padrão: ativada."""
        return bool(self.guild_settings.get(guild_id, {}).get("anti_mzk_enabled", True))

    async def set_anti_mzk_enabled(self, guild_id: int, value: bool):
        """Salva no Mongo e atualiza o cache."""
        self.guild_settings[guild_id] = {"anti_mzk_enabled": bool(value)}
        await self.settings_coll.update_one(
            {"guild_id": guild_id},
            {"$set": {"guild_id": guild_id, "anti_mzk_enabled": bool(value)}},
            upsert=True,
        )


bot = MyBot()


async def get_target_member(guild: discord.Guild, user_id: int):
    """Tenta pegar o membro do cache; se não achar, busca pela API."""
    target = guild.get_member(user_id)
    if target is not None:
        return target
    try:
        return await guild.fetch_member(user_id)
    except (discord.NotFound, discord.HTTPException):
        return None


@bot.event
async def on_ready():
    print(f"Logado como {bot.user} (id: {bot.user.id})")


# -------------------------
# COMANDO DE BARRA: /antimzk
# -------------------------
@bot.tree.command(name="antimzk", description="Ativa/desativa a censura anti-mzk (voz).")
@app_commands.checks.has_permissions(move_members=True)
async def antimzk(interaction: discord.Interaction):
    # Garante que está em um servidor
    if interaction.guild is None:
        return await interaction.response.send_message("Use esse comando em um servidor.", ephemeral=True)

    gid = interaction.guild.id
    novo_valor = not bot.is_anti_mzk_enabled(gid)

    # Persistir no Mongo
    try:
        await bot.set_anti_mzk_enabled(gid, novo_valor)
    except Exception as e:
        print(f"Falha ao salvar /antimzk no Mongo: {e}")
        return await interaction.response.send_message(
            "Não consegui salvar a configuração no banco agora.",
            ephemeral=True,
        )

    # Resposta em embed
    embed = discord.Embed(
        description="✅ Censura anti-mzk ativada" if novo_valor else "❌ Censura anti-mzk desativada",
        color=ON_COLOR if novo_valor else OFF_COLOR,
    )
    await interaction.response.send_message(embed=embed)


@antimzk.error
async def antimzk_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message(
            "Você não tem permissão para usar esse comando (precisa de **Mover Membros**).",
            ephemeral=True,
        )
    else:
        try:
            await interaction.response.send_message("Ocorreu um erro ao executar o comando.", ephemeral=True)
        except Exception:
            pass
        print(f"Erro no /antimzk: {error}")


# -------------------------
# EVENTO: ao receber mensagem
# -------------------------
@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or not message.guild:
        return

    # 1) Função original: ao mencionar um cargo, desativa menção por um tempo
    role = message.guild.get_role(TARGET_ROLE_ID)

    if role and role in message.role_mentions and not bot.cooldown_active:
        bot.cooldown_active = True
        try:
            if role.mentionable:
                await role.edit(mentionable=False, reason="Cargo mencionado; desativando menções automaticamente")
        except discord.Forbidden:
            print("Sem permissão para editar cargo (Gerenciar Cargos / hierarquia).")
        except discord.HTTPException as e:
            print(f"Falha ao editar cargo: {e}")

        await asyncio.sleep(DISABLE_TIME)

        role = message.guild.get_role(TARGET_ROLE_ID)
        if role:
            try:
                await role.edit(mentionable=True, reason="Cooldown acabou; reativando menções automaticamente")
            except Exception as e:
                print(f"Falha ao reativar menções do cargo: {e}")

        bot.cooldown_active = False

    # 2) Gatilhos de voz (anti-mzk), apenas se estiver ativado no servidor
    if (
        bot.is_anti_mzk_enabled(message.guild.id)
        and TARGET_USER_ID
        and (TRIGGER_WORD or MUTE_TOGGLE_WORD)
        and isinstance(message.channel, discord.VoiceChannel)  # só chat do canal de voz
    ):
        # Autor precisa estar conectado no MESMO canal de voz
        author_voice = getattr(message.author, "voice", None)
        if author_voice and author_voice.channel and author_voice.channel.id == message.channel.id:
            content = (message.content or "").lower()
            target = await get_target_member(message.guild, TARGET_USER_ID)

            # A) Desconectar
            if TRIGGER_WORD and TRIGGER_WORD in content:
                if target and target.voice and target.voice.channel:
                    try:
                        await target.move_to(None, reason="Palavra gatilho detectada (desconectar)")
                    except discord.Forbidden:
                        print("Sem permissão para mover membros (Mover Membros).")
                    except discord.HTTPException as e:
                        print(f"Falha ao desconectar o usuário alvo: {e}")

            # B) Mute/Desmute (toggle) - server mute
            if MUTE_TOGGLE_WORD and MUTE_TOGGLE_WORD in content:
                if target and target.voice and target.voice.channel:
                    try:
                        muted_atual = bool(target.voice.mute)
                        await target.edit(mute=not muted_atual, reason="Palavra gatilho detectada (toggle mute)")
                    except discord.Forbidden:
                        print("Sem permissão para mutar membros (Mutar Membros).")
                    except discord.HTTPException as e:
                        print(f"Falha ao alternar mute: {e}")

    # Mantém outros comandos prefixados funcionando (se você tiver)
    await bot.process_commands(message)


def main():
    # Inicia o Flask em thread separada e roda o bot normalmente
    threading.Thread(target=run_web, daemon=True).start()
    bot.run(TOKEN)


if __name__ == "__main__":
    if not TOKEN or TARGET_ROLE_ID == 0:
        raise RuntimeError("Faltou DISCORD_TOKEN e/ou ROLE_ID nas variáveis de ambiente")
    main()]

# ---- WEB SERVER ----
app = Flask(__name__)


@app.get("/")
def home():
    return "OK", 200


@app.get("/health")
def health():
    return "healthy", 200


def run_web():
    app.run(host="0.0.0.0", port=PORT)


# ---- DISCORD BOT ----
intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True


class MyBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents)
        self.cooldown_active = False

        # Mongo handles
        self.mongo_client: AsyncIOMotorClient | None = None
        self.settings_coll = None

        # Cache de config por guild
        # { guild_id: {"anti_mzk_enabled": bool} }
        self.guild_settings: dict[int, dict] = {}

    async def setup_hook(self):
        # 1) Conectar no Mongo
        if not MONGODB_URI:
            raise RuntimeError("Missing env var: MONGODB_URI")

        self.mongo_client = AsyncIOMotorClient(MONGODB_URI)
        db = self.mongo_client[MONGODB_DB]
        self.settings_coll = db[MONGODB_COLLECTION]

        # índice único por guild_id (opcional, mas recomendado)
        try:
            await self.settings_coll.create_index("guild_id", unique=True)
        except Exception:
            pass

        # 2) Carregar settings pro cache
        await self._load_settings_cache()

        # 3) Sync por guild (rápido)
        for gid in GUILD_IDS:
            guild_obj = discord.Object(id=gid)
            try:
                self.tree.copy_global_to(guild=guild_obj)
                synced = await self.tree.sync(guild=guild_obj)
                print(f"Synced {len(synced)} app commands to guild {gid}.")
            except Exception as e:
                print(f"Failed to sync commands to guild {gid}: {e}")

    async def _load_settings_cache(self):
        """Carrega as configs do Mongo para memória."""
        self.guild_settings.clear()
        cursor = self.settings_coll.find({}, {"_id": 0})
        async for doc in cursor:
            gid = int(doc.get("guild_id"))
            anti = bool(doc.get("anti_mzk_enabled", True))
            self.guild_settings[gid] = {"anti_mzk_enabled": anti}
        print(f"Loaded guild settings from Mongo: {self.guild_settings}")

    def is_anti_mzk_enabled(self, guild_id: int) -> bool:
        # default ON se não existir no banco
        return bool(self.guild_settings.get(guild_id, {}).get("anti_mzk_enabled", True))

    async def set_anti_mzk_enabled(self, guild_id: int, value: bool):
        """Salva no Mongo e atualiza cache."""
        self.guild_settings[guild_id] = {"anti_mzk_enabled": bool(value)}
        await self.settings_coll.update_one(
            {"guild_id": guild_id},
            {"$set": {"guild_id": guild_id, "anti_mzk_enabled": bool(value)}},
            upsert=True,
        )


bot = MyBot()


async def get_target_member(guild: discord.Guild, user_id: int):
    target = guild.get_member(user_id)
    if target is not None:
        return target
    try:
        return await guild.fetch_member(user_id)
    except (discord.NotFound, discord.HTTPException):
        return None


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (id: {bot.user.id})")


# -------------------------
# Slash command: /antimzk
# -------------------------
@bot.tree.command(name="antimzk", description="Ativa/desativa a censura anti-mzk (voz).")
@app_commands.checks.has_permissions(move_members=True)
async def antimzk(interaction: discord.Interaction):
    if interaction.guild is None:
        return await interaction.response.send_message("Use esse comando em um servidor.", ephemeral=True)

    gid = interaction.guild.id
    new_value = not bot.is_anti_mzk_enabled(gid)

    try:
        await bot.set_anti_mzk_enabled(gid, new_value)
    except Exception as e:
        print(f"Failed to persist /antimzk in Mongo: {e}")
        return await interaction.response.send_message(
            "Não consegui salvar a configuração no banco agora.",
            ephemeral=True,
        )

    embed = discord.Embed(
        description="✅ Censura anti-mzk ativada" if new_value else "❌ Censura anti-mzk desativada",
        color=ON_COLOR if new_value else OFF_COLOR,
    )
    await interaction.response.send_message(embed=embed)


@antimzk.error
async def antimzk_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message(
            "Você não tem permissão para usar esse comando (precisa de **Mover Membros**).",
            ephemeral=True,
        )
    else:
        try:
            await interaction.response.send_message("Ocorreu um erro ao executar o comando.", ephemeral=True)
        except Exception:
            pass
        print(f"Error in /antimzk: {error}")


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or not message.guild:
        return

    # -------------------------
    # 1) Role mention cooldown
    # -------------------------
    role = message.guild.get_role(TARGET_ROLE_ID)

    if role and role in message.role_mentions and not bot.cooldown_active:
        bot.cooldown_active = True
        try:
            if role.mentionable:
                await role.edit(mentionable=False, reason="Role mentioned; auto-disable mentions")
        except discord.Forbidden:
            print("Missing permissions to edit role (Manage Roles / role hierarchy).")
        except discord.HTTPException as e:
            print(f"Failed to edit role: {e}")

        await asyncio.sleep(DISABLE_TIME)

        role = message.guild.get_role(TARGET_ROLE_ID)
        if role:
            try:
                await role.edit(mentionable=True, reason="Cooldown finished; auto re-enable mentions")
            except Exception as e:
                print(f"Failed to re-enable role mentions: {e}")

        bot.cooldown_active = False

    # -------------------------------------------------------
    # 2) Voice triggers (lido do Mongo via cache)
    # -------------------------------------------------------
    if (
        bot.is_anti_mzk_enabled(message.guild.id)
        and TARGET_USER_ID
        and (TRIGGER_WORD or MUTE_TOGGLE_WORD)
        and isinstance(message.channel, discord.VoiceChannel)
    ):
        author_voice = getattr(message.author, "voice", None)
        if author_voice and author_voice.channel and author_voice.channel.id == message.channel.id:
            content = (message.content or "").lower()
            target = await get_target_member(message.guild, TARGET_USER_ID)

            # A) Desconectar
            if TRIGGER_WORD and TRIGGER_WORD in content:
                if target and target.voice and target.voice.channel:
                    try:
                        await target.move_to(None, reason="Trigger word detected (disconnect)")
                    except discord.Forbidden:
                        print("Missing permissions to move members (Move Members).")
                    except discord.HTTPException as e:
                        print(f"Failed to disconnect target user: {e}")

            # B) Mute/desmute (toggle)
            if MUTE_TOGGLE_WORD and MUTE_TOGGLE_WORD in content:
                if target and target.voice and target.voice.channel:
                    try:
                        currently_muted = bool(target.voice.mute)  # server mute
                        await target.edit(mute=not currently_muted, reason="Toggle mute trigger word detected")
                    except discord.Forbidden:
                        print("Missing permissions to mute members (Mute Members).")
                    except discord.HTTPException as e:
                        print(f"Failed to toggle mute: {e}")

    await bot.process_commands(message)


def main():
    threading.Thread(target=run_web, daemon=True).start()
    bot.run(TOKEN)


if __name__ == "__main__":
    if not TOKEN or TARGET_ROLE_ID == 0:
        raise RuntimeError("Missing env vars: DISCORD_TOKEN and/or ROLE_ID")
    main()
