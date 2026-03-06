from __future__ import annotations

import asyncio
import inspect
from typing import Any

import discord
import edge_tts
from discord import app_commands
from discord.ext import commands

import config
from config import BLOCK_VOICE_BOT_ID, OFF_COLOR, ON_COLOR
from tts_audio import GuildTTSState, QueueItem, TTSAudioMixin
from tts_helpers import (
    EDGE_DEFAULT_VOICE,
    PITCH_RE,
    RATE_RE,
    get_gtts_languages,
    make_embed,
    validate_engine,
)


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

    def _get_db(self):
        return getattr(self.bot, "settings_db", None)

    async def _maybe_await(self, value: Any):
        if inspect.isawaitable(value):
            return await value
        return value

    async def _defer_ephemeral(self, interaction: discord.Interaction):
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)

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

    def _make_embed(self, title: str, description: str, ok: bool = True) -> discord.Embed:
        return make_embed(title, description, ok=ok, on_color=ON_COLOR, off_color=OFF_COLOR)

    def _format_list_block(self, title: str, lines: list[str], footer: str) -> discord.Embed:
        description = f"{title}\n\n" + "\n".join(lines) + f"\n\n{footer}"
        return self._make_embed(title, description, ok=True)

    def _normalize_rate_value(self, raw: str) -> str | None:
        value = str(raw).strip().replace("％", "%").replace("−", "-").replace("–", "-").replace("—", "-")
        value = value.replace(" ", "")
        if value.endswith("%"):
            value = value[:-1]
        if not value:
            return None
        if value[0] not in "+-":
            value = f"+{value}"
        sign, number = value[0], value[1:]
        if not number.isdigit():
            return None
        return f"{sign}{number}%"

    def _normalize_pitch_value(self, raw: str) -> str | None:
        value = str(raw).strip().replace("−", "-").replace("–", "-").replace("—", "-")
        value = value.replace(" ", "")
        if value.lower().endswith("hz"):
            value = value[:-2]
        if not value:
            return None
        if value[0] not in "+-":
            value = f"+{value}"
        sign, number = value[0], value[1:]
        if not number.isdigit():
            return None
        return f"{sign}{number}Hz"

    async def _resolve_tts(self, guild_id: int, user_id: int) -> dict[str, Any]:
        db = self._get_db()
        if db is None:
            raise RuntimeError("settings_db indisponível")
        return await self._maybe_await(db.resolve_tts(guild_id, user_id))

    async def _get_user_tts(self, guild_id: int, user_id: int) -> dict[str, Any]:
        db = self._get_db()
        if db is None:
            raise RuntimeError("settings_db indisponível")
        return await self._maybe_await(db.get_user_tts(guild_id, user_id))

    async def _get_guild_defaults(self, guild_id: int) -> dict[str, Any]:
        db = self._get_db()
        if db is None:
            raise RuntimeError("settings_db indisponível")
        return await self._maybe_await(db.get_guild_tts_defaults(guild_id))

    async def _block_voice_bot_enabled(self, guild_id: int) -> bool:
        db = self._get_db()
        if db is None:
            return False
        if hasattr(db, "block_voice_bot_enabled"):
            return bool(await self._maybe_await(db.block_voice_bot_enabled(guild_id)))
        defaults = await self._get_guild_defaults(guild_id)
        return bool((defaults or {}).get("block_voice_bot", False))

    def _target_voice_bot_in_channel(self, voice_channel) -> bool:
        target_bot_id = BLOCK_VOICE_BOT_ID
        if not target_bot_id or voice_channel is None:
            return False
        return any(member.id == target_bot_id for member in getattr(voice_channel, "members", []))

    async def _should_block_for_voice_bot(self, guild: discord.Guild, voice_channel) -> bool:
        if not await self._block_voice_bot_enabled(guild.id):
            return False
        return self._target_voice_bot_in_channel(voice_channel)

    async def _disconnect_and_clear(self, guild: discord.Guild):
        state = self._get_state(guild.id)
        try:
            while not state.queue.empty():
                state.queue.get_nowait()
                state.queue.task_done()
        except Exception:
            pass

        vc = guild.voice_client
        if vc and vc.is_connected():
            try:
                await vc.disconnect(force=False)
            except Exception as e:
                print(f"[tts_voice] erro ao desconectar guild={guild.id}: {e}")

    async def _disconnect_if_blocked(self, guild: discord.Guild):
        vc = guild.voice_client
        if vc is None or not vc.is_connected() or vc.channel is None:
            return
        if await self._should_block_for_voice_bot(guild, vc.channel):
            print(
                f"[tts_voice] desconectando por outro bot de voz | guild={guild.id} channel={vc.channel.id} target_bot_id={BLOCK_VOICE_BOT_ID}"
            )
            await self._disconnect_and_clear(guild)

    async def _ensure_connected(self, guild: discord.Guild, voice_channel):
        if voice_channel is None:
            print(f"[tts_voice] _ensure_connected falhou | canal inválido | guild={guild.id}")
            return None

        vc = guild.voice_client
        try:
            if vc and vc.is_connected():
                if vc.channel and vc.channel.id == voice_channel.id:
                    return vc
                await vc.move_to(voice_channel)
                print(f"[tts_voice] Movido para canal {voice_channel.id} na guild {guild.id}")
                return vc

            new_vc = await voice_channel.connect(self_deaf=True)
            print(f"[tts_voice] Conectado no canal {voice_channel.id} na guild {guild.id}")
            return new_vc
        except Exception as e:
            print(f"[tts_voice] Erro ao conectar na guild {guild.id}: {e}")
            return None

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        print(
            f"[tts_voice] on_message recebido | guild={getattr(message.guild, 'id', None)} "
            f"channel_type={type(message.channel).__name__} user={getattr(message.author, 'id', None)} "
            f"raw={message.content!r}"
        )

        if not getattr(config, "TTS_ENABLED", True):
            print("[tts_voice] ignorado | TTS_ENABLED false")
            return
        if message.author.bot:
            print("[tts_voice] ignorado | autor é bot")
            return
        if not message.guild:
            print("[tts_voice] ignorado | sem guild")
            return
        if not message.content:
            print("[tts_voice] ignorado | sem conteúdo")
            return
        if not message.content.startswith(","):
            print("[tts_voice] ignorado | sem prefixo")
            return

        author_voice = getattr(message.author, "voice", None)
        if author_voice is None or author_voice.channel is None:
            print("[tts_voice] ignorado | autor não está em call")
            return

        voice_channel = author_voice.channel

        blocked = await self._should_block_for_voice_bot(message.guild, voice_channel)
        if blocked:
            print(
                f"[tts_voice] bloqueado | outro bot de voz detectado | guild={message.guild.id} canal_voz={voice_channel.id}"
            )
            await self._disconnect_and_clear(message.guild)
            return

        db = self._get_db()
        if db is None:
            print("[tts_voice] ignorado | settings_db indisponível")
            return

        try:
            resolved = await self._resolve_tts(message.guild.id, message.author.id)
        except Exception as e:
            print(f"[tts_voice] erro em resolve_tts | guild={message.guild.id} user={message.author.id} erro={e}")
            return

        text = message.content[1:].strip()
        if not text:
            print("[tts_voice] ignorado | texto vazio após prefixo")
            return

        state = self._get_state(message.guild.id)
        state.last_text_channel_id = getattr(message.channel, "id", None)

        await state.queue.put(
            QueueItem(
                guild_id=message.guild.id,
                channel_id=voice_channel.id,
                author_id=message.author.id,
                text=text,
                engine=resolved["engine"],
                voice=resolved["voice"],
                language=resolved["language"],
                rate=resolved["rate"],
                pitch=resolved["pitch"],
            )
        )

        print(
            f"[tts_voice] Mensagem enfileirada | guild={message.guild.id} user={message.author.id} "
            f"msg_channel={getattr(message.channel, 'id', None)} canal_voz={voice_channel.id} "
            f"engine={resolved['engine']} texto={text!r}"
        )
        self._ensure_worker(message.guild.id)

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        guild = member.guild
        vc = guild.voice_client
        if vc is None or not vc.is_connected() or vc.channel is None:
            return
        if not await self._block_voice_bot_enabled(guild.id):
            return
        if self._target_voice_bot_in_channel(vc.channel):
            print(
                f"[tts_voice] Bot de voz alvo detectado na call | guild={guild.id} channel={vc.channel.id} target_bot_id={BLOCK_VOICE_BOT_ID}"
            )
            await self._disconnect_and_clear(guild)

    @app_commands.command(name="leave", description="Faz o bot sair da call e limpa a fila de TTS")
    async def leave(self, interaction: discord.Interaction):
        await self._defer_ephemeral(interaction)
        if not interaction.guild:
            await self._respond(interaction, content="Esse comando só pode ser usado em servidor.")
            return

        vc = interaction.guild.voice_client
        if vc is None or not vc.is_connected():
            await self._respond(interaction, embed=self._make_embed("Nada para desconectar", "O bot não está conectado em nenhum canal de voz.", ok=False))
            return

        user_voice = getattr(interaction.user, "voice", None)
        if user_voice is None or user_voice.channel is None:
            await self._respond(interaction, embed=self._make_embed("Entre em uma call", "Você precisa estar em um canal de voz para usar esse comando.", ok=False))
            return

        if vc.channel and user_voice.channel.id != vc.channel.id and not interaction.user.guild_permissions.manage_guild:
            await self._respond(interaction, embed=self._make_embed("Canal diferente", "Você precisa estar na mesma call do bot, ou ter `Gerenciar Servidor`.", ok=False))
            return

        await self._disconnect_and_clear(interaction.guild)
        await self._respond(interaction, embed=self._make_embed("Bot desconectado", "Saí da call e limpei a fila de TTS.", ok=True))

    @app_commands.command(name="tts_status", description="Mostra as configurações atuais de TTS")
    async def tts_status(self, interaction: discord.Interaction):
        await self._defer_ephemeral(interaction)
        if not interaction.guild:
            await self._respond(interaction, content="Esse comando só pode ser usado em servidor.")
            return
        db = self._get_db()
        if db is None:
            await self._respond(interaction, content="Banco de dados indisponível.")
            return

        user_cfg = await self._get_user_tts(interaction.guild.id, interaction.user.id)
        guild_cfg = await self._get_guild_defaults(interaction.guild.id)
        resolved = await self._resolve_tts(interaction.guild.id, interaction.user.id)
        block_enabled = await self._block_voice_bot_enabled(interaction.guild.id)
        block_bot_text = f"ativado ({BLOCK_VOICE_BOT_ID})" if block_enabled and BLOCK_VOICE_BOT_ID else "desativado"

        desc = (
            "**Configuração usada agora**\n"
            f"- Engine: `{resolved['engine']}`\n"
            f"- Voz Edge: `{resolved['voice']}`\n"
            f"- Idioma gTTS: `{resolved['language']}`\n"
            f"- Velocidade: `{resolved['rate']}`\n"
            f"- Tom: `{resolved['pitch']}`\n\n"
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
        await self._defer_ephemeral(interaction)
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
        await self._respond(interaction, embed=self._make_embed("Engine atualizada", f"Sua engine de TTS agora é `{engine}`.\n\n{extra}", ok=True))

    @app_commands.command(name="set_server_tts_engine", description="Define a engine de TTS padrão do servidor")
    @app_commands.describe(engine="Escolha entre `gtts` e `edge`")
    @app_commands.default_permissions(manage_guild=True)
    async def set_server_tts_engine(self, interaction: discord.Interaction, engine: str):
        await self._defer_ephemeral(interaction)
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
        await self._respond(interaction, embed=self._make_embed("Engine padrão atualizada", f"A engine padrão do servidor agora é `{engine}`.\n\nEssa configuração será usada por padrão para membros sem configuração própria.", ok=True))

    @app_commands.command(name="set_voice", description="Define sua voz do Edge TTS")
    @app_commands.describe(voice="Exemplo: pt-BR-FranciscaNeural")
    async def set_voice(self, interaction: discord.Interaction, voice: str):
        await self._defer_ephemeral(interaction)
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
            await self._respond(interaction, embed=self._make_embed("Voz inválida", "Essa voz não existe na lista do Edge TTS.\n\nUse `/voices_edge` para ver as opções disponíveis.", ok=False))
            return
        await self._maybe_await(db.set_user_tts(interaction.guild.id, interaction.user.id, voice=voice))
        await self._respond(interaction, embed=self._make_embed("Voz atualizada", f"Sua voz do Edge foi definida para `{voice}`.", ok=True))

    @app_commands.command(name="set_server_voice", description="Define a voz padrão do Edge TTS no servidor")
    @app_commands.describe(voice="Exemplo: pt-BR-FranciscaNeural")
    @app_commands.default_permissions(manage_guild=True)
    async def set_server_voice(self, interaction: discord.Interaction, voice: str):
        await self._defer_ephemeral(interaction)
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
            await self._respond(interaction, embed=self._make_embed("Voz inválida", "Essa voz não existe na lista do Edge TTS.\n\nUse `/voices_edge` para ver as opções disponíveis.", ok=False))
            return
        await self._maybe_await(db.set_guild_tts_defaults(interaction.guild.id, voice=voice))
        await self._respond(interaction, embed=self._make_embed("Voz padrão atualizada", f"A voz padrão do servidor foi definida para `{voice}`.", ok=True))

    @app_commands.command(name="set_language", description="Define seu idioma do gTTS")
    @app_commands.describe(language="Exemplo: pt-br, en, es, fr")
    async def set_language(self, interaction: discord.Interaction, language: str):
        await self._defer_ephemeral(interaction)
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
            await self._respond(interaction, embed=self._make_embed("Idioma inválido", "Esse idioma não existe na lista do gTTS.\n\nUse `/voices_gtts` para ver os idiomas disponíveis.", ok=False))
            return
        await self._maybe_await(db.set_user_tts(interaction.guild.id, interaction.user.id, language=language))
        await self._respond(interaction, embed=self._make_embed("Idioma atualizado", f"Seu idioma do gTTS foi definido para `{language}` — {self.gtts_languages[language]}.", ok=True))

    @app_commands.command(name="set_server_language", description="Define o idioma padrão do gTTS no servidor")
    @app_commands.describe(language="Exemplo: pt-br, en, es, fr")
    @app_commands.default_permissions(manage_guild=True)
    async def set_server_language(self, interaction: discord.Interaction, language: str):
        await self._defer_ephemeral(interaction)
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
            await self._respond(interaction, embed=self._make_embed("Idioma inválido", "Esse idioma não existe na lista do gTTS.\n\nUse `/voices_gtts` para ver os idiomas disponíveis.", ok=False))
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
        await self._defer_ephemeral(interaction)
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
        value = self._normalize_rate_value(rate)
        if value is None:
            await self._respond(interaction, embed=self._make_embed("Velocidade inválida", "Use um valor como `10%`, `+10%` ou `-10%`.", ok=False))
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
        await self._defer_ephemeral(interaction)
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
        value = self._normalize_pitch_value(pitch)
        if value is None:
            await self._respond(interaction, embed=self._make_embed("Tom inválido", "Use um valor como `10Hz`, `+10Hz` ou `-10Hz`.", ok=False))
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
        await self._defer_ephemeral(interaction)
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
        if hasattr(db, "set_block_voice_bot_enabled"):
            await self._maybe_await(db.set_block_voice_bot_enabled(interaction.guild.id, enabled))
        else:
            await self._maybe_await(db.set_guild_tts_defaults(interaction.guild.id, block_voice_bot=enabled))
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
