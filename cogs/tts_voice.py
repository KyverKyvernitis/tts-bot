from __future__ import annotations

import asyncio
import inspect
from typing import Any, Optional

import discord
import edge_tts
from discord import app_commands
from discord.ext import commands

import config
from config import BLOCK_VOICE_BOT_ID, OFF_COLOR, ON_COLOR
from tts_audio import GuildTTSState, QueueItem, TTSAudioMixin
from tts_helpers import EDGE_DEFAULT_VOICE, get_gtts_languages, make_embed, validate_engine


class TTSVoice(TTSAudioMixin, commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.guild_states: dict[int, GuildTTSState] = {}
        self.edge_voice_names: set[str] = set()
        self.edge_voice_cache: list[str] = []
        self.gtts_languages: dict[str, str] = {}

    async def cog_load(self):
        self.gtts_languages = get_gtts_languages()
        try:
            await self._load_edge_voices()
        except Exception as e:
            self.edge_voice_cache = [EDGE_DEFAULT_VOICE]
            self.edge_voice_names = set(self.edge_voice_cache)
            print(f"[tts_voice] cog_load fallback: {e}")

    async def cog_unload(self):
        for state in self.guild_states.values():
            if state.worker_task and not state.worker_task.done():
                state.worker_task.cancel()

    async def _load_edge_voices(self):
        try:
            voices = await asyncio.wait_for(edge_tts.list_voices(), timeout=15)
            names = sorted({v["ShortName"] for v in voices if "ShortName" in v})
            self.edge_voice_cache = names
            self.edge_voice_names = set(names)
            print(f"[tts_voice] {len(names)} vozes edge carregadas.")
        except Exception as e:
            self.edge_voice_cache = [EDGE_DEFAULT_VOICE]
            self.edge_voice_names = set(self.edge_voice_cache)
            print(f"[tts_voice] Falha ao carregar vozes edge, usando fallback: {e}")

    def _make_embed(self, title: str, description: str, ok: bool = True) -> discord.Embed:
        return make_embed(title, description, ok=ok, on_color=ON_COLOR, off_color=OFF_COLOR)

    def _format_list_block(self, title: str, lines: list[str], footer: str) -> discord.Embed:
        description = f"{title}\n\n" + "\n".join(lines) + f"\n\n{footer}"
        return self._make_embed(title, description, ok=True)

    async def _maybe_await(self, value: Any):
        if inspect.isawaitable(value):
            return await value
        return value

    def _get_db(self):
        return getattr(self.bot, "settings_db", None)

    async def _db_get_user_tts(self, guild_id: int, user_id: int) -> dict[str, Any]:
        db = self._get_db()
        if db is None:
            return {}
        result = await self._maybe_await(db.get_user_tts(guild_id, user_id))
        return result or {}

    async def _db_get_guild_defaults(self, guild_id: int) -> dict[str, Any]:
        db = self._get_db()
        if db is None:
            return {}
        result = await self._maybe_await(db.get_guild_tts_defaults(guild_id))
        return result or {}

    async def _db_resolve_tts(self, guild_id: int, user_id: int) -> dict[str, Any]:
        db = self._get_db()
        if db is None:
            return {}
        result = await self._maybe_await(db.resolve_tts(guild_id, user_id))
        return result or {}

    async def _db_block_voice_bot_enabled(self, guild_id: int) -> bool:
        db = self._get_db()
        if db is None:
            return False
        result = await self._maybe_await(db.block_voice_bot_enabled(guild_id))
        return bool(result)

    async def _respond(
        self,
        interaction: discord.Interaction,
        *,
        content: str | None = None,
        embed: discord.Embed | None = None,
        ephemeral: bool = True,
    ):
        if interaction.response.is_done():
            await interaction.followup.send(content=content, embed=embed, ephemeral=ephemeral)
        else:
            await interaction.response.send_message(content=content, embed=embed, ephemeral=ephemeral)

    def _normalize_rate_value(self, raw: str) -> str | None:
        value = str(raw).strip()
        value = value.replace("％", "%").replace("−", "-").replace("–", "-").replace("—", "-")
        value = value.replace(" ", "")

        if value.endswith("%"):
            value = value[:-1]

        if not value:
            return None

        if value[0] not in "+-":
            value = f"+{value}"

        sign = value[0]
        number = value[1:]
        if not number.isdigit():
            return None

        return f"{sign}{number}%"

    def _normalize_pitch_value(self, raw: str) -> str | None:
        value = str(raw).strip()
        value = value.replace("−", "-").replace("–", "-").replace("—", "-")
        value = value.replace(" ", "")

        if value.lower().endswith("hz"):
            value = value[:-2]

        if not value:
            return None

        if value[0] not in "+-":
            value = f"+{value}"

        sign = value[0]
        number = value[1:]
        if not number.isdigit():
            return None

        return f"{sign}{number}Hz"

    async def _resolved_engine(self, guild_id: int, user_id: int) -> str:
        resolved = await self._db_resolve_tts(guild_id, user_id)
        engine = (resolved.get("engine") or "gtts").strip().lower()
        return engine

    async def _should_block_for_voice_bot(
        self,
        guild: discord.Guild,
        voice_channel: discord.abc.GuildChannel | discord.Thread | Any,
    ) -> bool:
        if not guild or not BLOCK_VOICE_BOT_ID:
            return False

        enabled = await self._db_block_voice_bot_enabled(guild.id)
        if not enabled:
            return False

        for member in getattr(voice_channel, "members", []):
            if member.id == int(BLOCK_VOICE_BOT_ID):
                return True
        return False

    async def _disconnect_if_blocked(self, guild: discord.Guild):
        if not guild or not guild.voice_client or not guild.voice_client.channel:
            return

        if await self._should_block_for_voice_bot(guild, guild.voice_client.channel):
            try:
                await guild.voice_client.disconnect(force=True)
                print(f"[tts_voice] Desconectado da guild {guild.id} por bloqueio de outro bot de voz")
            except Exception as e:
                print(f"[tts_voice] Falha ao desconectar por bloqueio na guild {guild.id}: {e}")

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ):
        if not member.guild or member.bot is False:
            return

        block_bot_id = int(BLOCK_VOICE_BOT_ID) if BLOCK_VOICE_BOT_ID else None
        if block_bot_id is None or member.id != block_bot_id:
            return

        if after.channel is not None:
            await self._disconnect_if_blocked(member.guild)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if not getattr(config, "TTS_ENABLED", True):
            return

        if message.author.bot:
            return

        if not message.guild:
            return

        if not message.content or not message.content.startswith(","):
            return

        author_voice = getattr(message.author, "voice", None)
        if author_voice is None or author_voice.channel is None:
            return

        voice_channel = author_voice.channel

        blocked = await self._should_block_for_voice_bot(message.guild, voice_channel)
        if blocked:
            await self._disconnect_if_blocked(message.guild)
            return

        db = self._get_db()
        if db is None:
            print("[tts_voice] settings_db indisponível")
            return

        resolved = await self._db_resolve_tts(message.guild.id, message.author.id)
        if not resolved:
            print(f"[tts_voice] resolve_tts vazio | guild={message.guild.id} user={message.author.id}")
            return

        text = message.content[1:].strip()
        if not text:
            return

        state = self._get_state(message.guild.id)
        state.last_text_channel_id = getattr(message.channel, "id", None)

        await state.queue.put(
            QueueItem(
                guild_id=message.guild.id,
                channel_id=voice_channel.id,
                author_id=message.author.id,
                text=text,
                engine=resolved.get("engine", "gtts"),
                voice=resolved.get("voice", EDGE_DEFAULT_VOICE),
                language=resolved.get("language", "pt-br"),
                rate=resolved.get("rate", "+0%"),
                pitch=resolved.get("pitch", "+0Hz"),
            )
        )

        print(
            f"[tts_voice] Mensagem enfileirada | guild={message.guild.id} "
            f"user={message.author.id} msg_channel={getattr(message.channel, 'id', None)} "
            f"canal_voz={voice_channel.id} engine={resolved.get('engine', 'gtts')} texto={text!r}"
        )

        self._ensure_worker(message.guild.id)

    @app_commands.command(name="tts_status", description="Mostra as configurações atuais de TTS")
    async def tts_status(self, interaction: discord.Interaction):
        if not interaction.guild:
            await self._respond(interaction, content="Esse comando só pode ser usado em servidor.")
            return

        db = self._get_db()
        if db is None:
            await self._respond(interaction, content="Banco de dados indisponível.")
            return

        user_cfg = await self._db_get_user_tts(interaction.guild.id, interaction.user.id)
        guild_cfg = await self._db_get_guild_defaults(interaction.guild.id)
        resolved = await self._db_resolve_tts(interaction.guild.id, interaction.user.id)
        block_enabled = await self._db_block_voice_bot_enabled(interaction.guild.id)

        block_bot_text = (
            f"ativado ({BLOCK_VOICE_BOT_ID})" if block_enabled and BLOCK_VOICE_BOT_ID else "desativado"
        )

        desc = (
            "**Configuração usada agora**\n"
            f"- Engine: `{resolved.get('engine', '-')}`\n"
            f"- Voz Edge: `{resolved.get('voice', '-')}`\n"
            f"- Idioma gTTS: `{resolved.get('language', '-')}`\n"
            f"- Velocidade: `{resolved.get('rate', '-')}`\n"
            f"- Tom: `{resolved.get('pitch', '-')}`\n\n"
            "**Sua configuração**\n"
            f"- Engine: `{user_cfg.get('engine') or '-'}`\n"
            f"- Voz Edge: `{user_cfg.get('voice') or '-'}`\n"
            f"- Idioma gTTS: `{user_cfg.get('language') or '-'}`\n"
            f"- Velocidade: `{user_cfg.get('rate') or '-'}`\n"
            f"- Tom: `{user_cfg.get('pitch') or '-'}`\n\n"
            "**Padrão do servidor**\n"
            f"- Engine: `{guild_cfg.get('engine') or '-'}`\n"
            f"- Voz Edge: `{guild_cfg.get('voice') or '-'}`\n"
            f"- Idioma gTTS: `{guild_cfg.get('language') or '-'}`\n"
            f"- Velocidade: `{guild_cfg.get('rate') or '-'}`\n"
            f"- Tom: `{guild_cfg.get('pitch') or '-'}`\n\n"
            f"**Bloqueio por outro bot de voz:** `{block_bot_text}`"
        )
        await self._respond(interaction, embed=self._make_embed("Status do TTS", desc, ok=True))

    @app_commands.command(name="voices_edge", description="Mostra as vozes disponíveis do Edge TTS")
    async def voices_edge(self, interaction: discord.Interaction):
        if not self.edge_voice_cache:
            await self._load_edge_voices()

        voices = [v for v in self.edge_voice_cache if v.startswith("pt-")] or self.edge_voice_cache[:40]
        lines = [f"- `{v}`" for v in voices[:40]]
        embed = self._format_list_block("Vozes do Edge TTS", lines, "Use `/set_voice` para escolher uma voz do Edge.")
        await self._respond(interaction, embed=embed)

    @app_commands.command(name="voices_gtts", description="Mostra os idiomas disponíveis do gTTS")
    async def voices_gtts(self, interaction: discord.Interaction):
        if not self.gtts_languages:
            self.gtts_languages = get_gtts_languages()

        items = list(self.gtts_languages.items())[:80]
        lines = [f"- `{code}` — {name}" for code, name in items]
        embed = self._format_list_block("Idiomas do gTTS", lines, "Use `/set_language` para escolher um idioma do gTTS.")
        await self._respond(interaction, embed=embed)

    @app_commands.command(name="set_tts_engine", description="Define qual engine de TTS você quer usar")
    @app_commands.describe(engine="Escolha entre `gtts` e `edge`")
    async def set_tts_engine(self, interaction: discord.Interaction, engine: str):
        if not interaction.guild:
            await self._respond(interaction, content="Esse comando só pode ser usado em servidor.")
            return

        db = self._get_db()
        if db is None:
            await self._respond(interaction, content="Banco de dados indisponível.")
            return

        engine = validate_engine(engine)
        await self._maybe_await(db.set_user_tts(interaction.guild.id, interaction.user.id, engine=engine))

        extra = "• `gtts`: usa idioma com `/set_language`\n• `edge`: permite voz, velocidade e tom"
        await self._respond(
            interaction,
            embed=self._make_embed("Engine atualizada", f"Sua engine de TTS agora é `{engine}`.\n\n{extra}", ok=True),
        )

    @app_commands.command(name="set_server_tts_engine", description="Define a engine de TTS padrão do servidor")
    @app_commands.describe(engine="Escolha entre `gtts` e `edge`")
    @app_commands.default_permissions(manage_guild=True)
    async def set_server_tts_engine(self, interaction: discord.Interaction, engine: str):
        if not interaction.guild:
            await self._respond(interaction, content="Esse comando só pode ser usado em servidor.")
            return
        if not interaction.user.guild_permissions.manage_guild:
            await self._respond(interaction, content="Você precisa da permissão `Gerenciar Servidor`.")
            return

        db = self._get_db()
        if db is None:
            await self._respond(interaction, content="Banco de dados indisponível.")
            return

        engine = validate_engine(engine)
        await self._maybe_await(db.set_guild_tts_defaults(interaction.guild.id, engine=engine))
        await self._respond(
            interaction,
            embed=self._make_embed(
                "Engine padrão atualizada",
                f"A engine padrão do servidor agora é `{engine}`.\n\nEssa configuração será usada por padrão para membros sem configuração própria.",
                ok=True,
            ),
        )

    @app_commands.command(name="set_voice", description="Define sua voz do Edge TTS")
    @app_commands.describe(voice="Exemplo: pt-BR-FranciscaNeural")
    async def set_voice(self, interaction: discord.Interaction, voice: str):
        if not interaction.guild:
            await self._respond(interaction, content="Esse comando só pode ser usado em servidor.")
            return
        db = self._get_db()
        if db is None:
            await self._respond(interaction, content="Banco de dados indisponível.")
            return
        if not self.edge_voice_cache:
            await self._load_edge_voices()

        voice = voice.strip()
        if voice not in self.edge_voice_names:
            await self._respond(
                interaction,
                embed=self._make_embed(
                    "Voz inválida",
                    "Essa voz não existe na lista do Edge TTS.\n\nUse `/voices_edge` para ver as opções disponíveis.",
                    ok=False,
                ),
            )
            return

        await self._maybe_await(db.set_user_tts(interaction.guild.id, interaction.user.id, voice=voice))
        await self._respond(interaction, embed=self._make_embed("Voz atualizada", f"Sua voz do Edge foi definida para `{voice}`.", ok=True))

    @app_commands.command(name="set_server_voice", description="Define a voz padrão do Edge TTS no servidor")
    @app_commands.describe(voice="Exemplo: pt-BR-FranciscaNeural")
    @app_commands.default_permissions(manage_guild=True)
    async def set_server_voice(self, interaction: discord.Interaction, voice: str):
        if not interaction.guild:
            await self._respond(interaction, content="Esse comando só pode ser usado em servidor.")
            return
        if not interaction.user.guild_permissions.manage_guild:
            await self._respond(interaction, content="Você precisa da permissão `Gerenciar Servidor`.")
            return
        db = self._get_db()
        if db is None:
            await self._respond(interaction, content="Banco de dados indisponível.")
            return
        if not self.edge_voice_cache:
            await self._load_edge_voices()

        voice = voice.strip()
        if voice not in self.edge_voice_names:
            await self._respond(
                interaction,
                embed=self._make_embed(
                    "Voz inválida",
                    "Essa voz não existe na lista do Edge TTS.\n\nUse `/voices_edge` para ver as opções disponíveis.",
                    ok=False,
                ),
            )
            return

        await self._maybe_await(db.set_guild_tts_defaults(interaction.guild.id, voice=voice))
        await self._respond(interaction, embed=self._make_embed("Voz padrão atualizada", f"A voz padrão do servidor foi definida para `{voice}`.", ok=True))

    @app_commands.command(name="set_language", description="Define seu idioma do gTTS")
    @app_commands.describe(language="Exemplo: pt-br, en, es, fr")
    async def set_language(self, interaction: discord.Interaction, language: str):
        if not interaction.guild:
            await self._respond(interaction, content="Esse comando só pode ser usado em servidor.")
            return
        db = self._get_db()
        if db is None:
            await self._respond(interaction, content="Banco de dados indisponível.")
            return
        if not self.gtts_languages:
            self.gtts_languages = get_gtts_languages()

        language = language.strip().lower()
        if language not in self.gtts_languages:
            await self._respond(
                interaction,
                embed=self._make_embed(
                    "Idioma inválido",
                    "Esse idioma não existe na lista do gTTS.\n\nUse `/voices_gtts` para ver os idiomas disponíveis.",
                    ok=False,
                ),
            )
            return

        await self._maybe_await(db.set_user_tts(interaction.guild.id, interaction.user.id, language=language))
        await self._respond(interaction, embed=self._make_embed("Idioma atualizado", f"Seu idioma do gTTS foi definido para `{language}` — {self.gtts_languages[language]}.", ok=True))

    @app_commands.command(name="set_server_language", description="Define o idioma padrão do gTTS no servidor")
    @app_commands.describe(language="Exemplo: pt-br, en, es, fr")
    @app_commands.default_permissions(manage_guild=True)
    async def set_server_language(self, interaction: discord.Interaction, language: str):
        if not interaction.guild:
            await self._respond(interaction, content="Esse comando só pode ser usado em servidor.")
            return
        if not interaction.user.guild_permissions.manage_guild:
            await self._respond(interaction, content="Você precisa da permissão `Gerenciar Servidor`.")
            return
        db = self._get_db()
        if db is None:
            await self._respond(interaction, content="Banco de dados indisponível.")
            return
        if not self.gtts_languages:
            self.gtts_languages = get_gtts_languages()

        language = language.strip().lower()
        if language not in self.gtts_languages:
            await self._respond(
                interaction,
                embed=self._make_embed(
                    "Idioma inválido",
                    "Esse idioma não existe na lista do gTTS.\n\nUse `/voices_gtts` para ver os idiomas disponíveis.",
                    ok=False,
                ),
            )
            return

        await self._maybe_await(db.set_guild_tts_defaults(interaction.guild.id, language=language))
        await self._respond(interaction, embed=self._make_embed("Idioma padrão atualizado", f"O idioma padrão do servidor foi definido para `{language}` — {self.gtts_languages[language]}.", ok=True))

    @app_commands.command(name="set_rate", description="Define sua velocidade de fala no Edge TTS")
    @app_commands.describe(rate="Exemplo: 10%, +10%, -10%")
    async def set_rate(self, interaction: discord.Interaction, rate: str):
        await self._set_rate_common(interaction, rate=rate, server=False)

    @app_commands.command(name="set_speed", description="Alias de /set_rate para velocidade de fala")
    @app_commands.describe(speed="Exemplo: 10%, +10%, -10%")
    async def set_speed(self, interaction: discord.Interaction, speed: str):
        await self._set_rate_common(interaction, rate=speed, server=False)

    @app_commands.command(name="set_server_rate", description="Define a velocidade padrão de fala do servidor no Edge TTS")
    @app_commands.describe(rate="Exemplo: 10%, +10%, -10%")
    @app_commands.default_permissions(manage_guild=True)
    async def set_server_rate(self, interaction: discord.Interaction, rate: str):
        await self._set_rate_common(interaction, rate=rate, server=True)

    @app_commands.command(name="set_server_speed", description="Alias de /set_server_rate para velocidade padrão")
    @app_commands.describe(speed="Exemplo: 10%, +10%, -10%")
    @app_commands.default_permissions(manage_guild=True)
    async def set_server_speed(self, interaction: discord.Interaction, speed: str):
        await self._set_rate_common(interaction, rate=speed, server=True)

    async def _set_rate_common(self, interaction: discord.Interaction, *, rate: str, server: bool):
        if not interaction.guild:
            await self._respond(interaction, content="Esse comando só pode ser usado em servidor.")
            return
        if server and not interaction.user.guild_permissions.manage_guild:
            await self._respond(interaction, content="Você precisa da permissão `Gerenciar Servidor`.")
            return
        db = self._get_db()
        if db is None:
            await self._respond(interaction, content="Banco de dados indisponível.")
            return

        current_engine = await self._resolved_engine(interaction.guild.id, interaction.user.id)
        if current_engine != "edge":
            await self._respond(
                interaction,
                embed=self._make_embed(
                    "Engine incompatível",
                    "Esse ajuste só funciona com a engine `edge`.\n\nUse `/set_tts_engine edge` para mudar sua engine.",
                    ok=False,
                ),
            )
            return

        value = self._normalize_rate_value(rate)
        if value is None:
            await self._respond(
                interaction,
                embed=self._make_embed("Velocidade inválida", "Use um valor como `10%`, `+10%` ou `-10%`.", ok=False),
            )
            return

        if server:
            await self._maybe_await(db.set_guild_tts_defaults(interaction.guild.id, rate=value))
            title = "Velocidade padrão atualizada"
            desc = f"A velocidade padrão do servidor foi definida para `{value}`."
        else:
            await self._maybe_await(db.set_user_tts(interaction.guild.id, interaction.user.id, rate=value))
            title = "Velocidade atualizada"
            desc = f"Sua velocidade foi definida para `{value}`."

        await self._respond(interaction, embed=self._make_embed(title, desc, ok=True))

    @app_commands.command(name="set_pitch", description="Define seu tom de voz no Edge TTS")
    @app_commands.describe(pitch="Exemplo: 10Hz, +10Hz, -10Hz")
    async def set_pitch(self, interaction: discord.Interaction, pitch: str):
        await self._set_pitch_common(interaction, pitch=pitch, server=False)

    @app_commands.command(name="set_server_pitch", description="Define o tom de voz padrão do servidor no Edge TTS")
    @app_commands.describe(pitch="Exemplo: 10Hz, +10Hz, -10Hz")
    @app_commands.default_permissions(manage_guild=True)
    async def set_server_pitch(self, interaction: discord.Interaction, pitch: str):
        await self._set_pitch_common(interaction, pitch=pitch, server=True)

    async def _set_pitch_common(self, interaction: discord.Interaction, *, pitch: str, server: bool):
        if not interaction.guild:
            await self._respond(interaction, content="Esse comando só pode ser usado em servidor.")
            return
        if server and not interaction.user.guild_permissions.manage_guild:
            await self._respond(interaction, content="Você precisa da permissão `Gerenciar Servidor`.")
            return
        db = self._get_db()
        if db is None:
            await self._respond(interaction, content="Banco de dados indisponível.")
            return

        current_engine = await self._resolved_engine(interaction.guild.id, interaction.user.id)
        if current_engine != "edge":
            await self._respond(
                interaction,
                embed=self._make_embed(
                    "Engine incompatível",
                    "Esse ajuste só funciona com a engine `edge`.\n\nUse `/set_tts_engine edge` para mudar sua engine.",
                    ok=False,
                ),
            )
            return

        value = self._normalize_pitch_value(pitch)
        if value is None:
            await self._respond(
                interaction,
                embed=self._make_embed("Tom inválido", "Use um valor como `10Hz`, `+10Hz` ou `-10Hz`.", ok=False),
            )
            return

        if server:
            await self._maybe_await(db.set_guild_tts_defaults(interaction.guild.id, pitch=value))
            title = "Tom padrão atualizado"
            desc = f"O tom padrão do servidor foi definido para `{value}`."
        else:
            await self._maybe_await(db.set_user_tts(interaction.guild.id, interaction.user.id, pitch=value))
            title = "Tom atualizado"
            desc = f"Seu tom foi definido para `{value}`."

        await self._respond(interaction, embed=self._make_embed(title, desc, ok=True))

    @app_commands.command(name="set_block_voice_bot", description="Ativa ou desativa o bloqueio quando outro bot de voz estiver na call")
    @app_commands.describe(enabled="Use `true` para ativar ou `false` para desativar")
    @app_commands.default_permissions(manage_guild=True)
    async def set_block_voice_bot(self, interaction: discord.Interaction, enabled: bool):
        if not interaction.guild:
            await self._respond(interaction, content="Esse comando só pode ser usado em servidor.")
            return
        if not interaction.user.guild_permissions.manage_guild:
            await self._respond(interaction, content="Você precisa da permissão `Gerenciar Servidor`.")
            return

        db = self._get_db()
        if db is None:
            await self._respond(interaction, content="Banco de dados indisponível.")
            return

        await self._maybe_await(db.set_block_voice_bot_enabled(interaction.guild.id, enabled))
        if enabled:
            await self._disconnect_if_blocked(interaction.guild)

        bot_info = str(BLOCK_VOICE_BOT_ID) if BLOCK_VOICE_BOT_ID else "não configurado"
        desc = (
            f"O bloqueio por outro bot de voz agora está `{'ativado' if enabled else 'desativado'}`.\n\n"
            f"Bot monitorado: `{bot_info}`\n"
            "Quando ativado, o bot evita entrar e também sai da call se o outro bot entrar no mesmo canal."
        )
        await self._respond(interaction, embed=self._make_embed("Bloqueio atualizado", desc, ok=True))


async def setup(bot: commands.Bot):
    await bot.add_cog(TTSVoice(bot))
