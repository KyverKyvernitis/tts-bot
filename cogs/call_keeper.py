from __future__ import annotations

import contextlib
import logging
import re
from typing import Optional

import discord
from discord.ext import commands

from callkeeper_runtime import CALLKEEPER_OWNER_USER_ID, CallKeeperStateStore, load_settings

log = logging.getLogger(__name__)


class CallKeeper(commands.Cog):
    """Comando de controle do CallKeeper standalone.

    A lógica de voz dos 3 bots auxiliares fica no processo separado
    `callkeeper_service.py`. Esta cog só escreve o estado compartilhado no Mongo:
    ligado/desligado e canal alvo. Assim uma falha fatal em outra cog do bot
    principal não derruba os CallKeepers.
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.settings = load_settings()

    def _db_store(self) -> Optional[CallKeeperStateStore]:
        db = getattr(self.bot, "settings_db", None)
        if db is None:
            return None
        return CallKeeperStateStore(db, default_channel_id=self.settings.default_channel_id)

    def _is_authorized_prefix_context(self, ctx: commands.Context) -> bool:
        guild = getattr(ctx, "guild", None)
        author = getattr(ctx, "author", None)
        if self.settings.guild_id <= 0:
            return False
        if guild is None or int(getattr(guild, "id", 0) or 0) != int(self.settings.guild_id):
            return False
        if int(getattr(author, "id", 0) or 0) != int(CALLKEEPER_OWNER_USER_ID):
            return False
        return True

    def _is_voice_target(self, channel: object) -> bool:
        return isinstance(channel, (discord.VoiceChannel, discord.StageChannel))

    def _channel_in_callkeeper_guild(self, channel: object) -> bool:
        guild = getattr(channel, "guild", None)
        return bool(self._is_voice_target(channel) and guild and int(guild.id) == int(self.settings.guild_id))

    async def _resolve_channel_by_id(self, ctx: commands.Context, channel_id: int):
        if channel_id <= 0:
            return None
        guild = getattr(ctx, "guild", None)
        if guild is not None:
            channel = guild.get_channel(channel_id)
            if self._channel_in_callkeeper_guild(channel):
                return channel
        channel = self.bot.get_channel(channel_id)
        if self._channel_in_callkeeper_guild(channel):
            return channel
        try:
            fetched = await self.bot.fetch_channel(channel_id)
        except Exception:
            return None
        if self._channel_in_callkeeper_guild(fetched):
            return fetched
        return None

    async def _resolve_channel_argument(self, ctx: commands.Context, raw: str | None):
        if not raw:
            return None
        text = str(raw).strip()
        if not text:
            return None

        mention = re.fullmatch(r"<#(\d{15,25})>", text)
        if mention:
            return await self._resolve_channel_by_id(ctx, int(mention.group(1)))

        cleaned = text.strip().strip('"').strip("'").strip()
        if cleaned.isdigit():
            return await self._resolve_channel_by_id(ctx, int(cleaned))

        guild = getattr(ctx, "guild", None)
        if guild is None:
            return None

        lowered = cleaned.casefold()
        voice_channels = [channel for channel in getattr(guild, "channels", []) if self._is_voice_target(channel)]

        for channel in voice_channels:
            if str(getattr(channel, "name", "")).casefold() == lowered:
                return channel

        for channel in voice_channels:
            name = str(getattr(channel, "name", ""))
            if lowered in name.casefold():
                return channel
        return None

    async def _resolve_default_target_channel(self, ctx: commands.Context, store: CallKeeperStateStore):
        configured_id = int(self.settings.default_channel_id or 0)
        if configured_id > 0:
            channel = await self._resolve_channel_by_id(ctx, configured_id)
            if channel is not None:
                return channel

        user_voice = getattr(getattr(ctx, "author", None), "voice", None)
        user_channel = getattr(user_voice, "channel", None)
        if self._channel_in_callkeeper_guild(user_channel):
            return user_channel

        saved_id = store.get_channel_id(self.settings.guild_id)
        if saved_id > 0:
            channel = await self._resolve_channel_by_id(ctx, saved_id)
            if channel is not None:
                return channel
        return None

    async def _missing_target_permission_text(self, target) -> str:
        guild = getattr(target, "guild", None)
        if guild is None:
            return "Canal inválido para o CallKeeper."

        main_me = getattr(guild, "me", None)
        if main_me is not None:
            perms = target.permissions_for(main_me)
            if not bool(getattr(perms, "view_channel", False) and getattr(perms, "connect", False)):
                return "O bot principal precisa de permissão para ver e conectar nesse canal."
        return ""

    @commands.command(name="callkeeper", hidden=True)
    async def callkeeper_toggle(self, ctx: commands.Context, *, canal: str | None = None):
        # Fora da guild alvo ou usado por outro usuário: ignora 100%, sem resposta.
        if not self._is_authorized_prefix_context(ctx):
            return

        store = self._db_store()
        if store is None:
            with contextlib.suppress(discord.HTTPException):
                await ctx.reply("Banco de dados ainda não está pronto para controlar o CallKeeper.", mention_author=False)
            return

        if len(self.settings.bot_tokens) < 3:
            with contextlib.suppress(discord.HTTPException):
                await ctx.reply(
                    "Configure os 3 tokens na `.env`: `CALLKEEPER_BOT_1_TOKEN`, `CALLKEEPER_BOT_2_TOKEN` e `CALLKEEPER_BOT_3_TOKEN`.",
                    mention_author=False,
                )
            return

        # _callkeeper <canal> muda o foco. Não funciona como off.
        if canal:
            target = await self._resolve_channel_argument(ctx, canal)
            if target is None:
                with contextlib.suppress(discord.HTTPException):
                    await ctx.reply("Não encontrei esse canal de voz/stage no servidor dos CallKeepers.", mention_author=False)
                return

            permission_error = await self._missing_target_permission_text(target)
            if permission_error:
                with contextlib.suppress(discord.HTTPException):
                    await ctx.reply(permission_error, mention_author=False)
                return

            await store.set_channel_id(self.settings.guild_id, int(target.id))
            if store.is_enabled(self.settings.guild_id):
                message = f"Foco do CallKeeper alterado para {target.mention}."
            else:
                message = f"Foco do CallKeeper salvo em {target.mention}. Use `_callkeeper` para ligar."
            with contextlib.suppress(discord.HTTPException):
                await ctx.reply(message, mention_author=False)
            return

        if store.is_enabled(self.settings.guild_id):
            await store.set_enabled(self.settings.guild_id, False)
            with contextlib.suppress(discord.HTTPException):
                await ctx.reply("CallKeeper desligado. O serviço separado vai remover os auxiliares da call.", mention_author=False)
            return

        target = await self._resolve_default_target_channel(ctx, store)
        if target is None:
            with contextlib.suppress(discord.HTTPException):
                await ctx.reply(
                    "Configure `CALLKEEPER_CHANNEL_ID`, use `_callkeeper <canal>` ou entre na call que o CallKeeper deve proteger antes de ligar.",
                    mention_author=False,
                )
            return

        permission_error = await self._missing_target_permission_text(target)
        if permission_error:
            with contextlib.suppress(discord.HTTPException):
                await ctx.reply(permission_error, mention_author=False)
            return

        await store.set_channel_id(self.settings.guild_id, int(target.id))
        await store.set_enabled(self.settings.guild_id, True)
        with contextlib.suppress(discord.HTTPException):
            await ctx.reply(f"CallKeeper ligado em {target.mention}. O serviço separado vai aplicar a regra.", mention_author=False)


async def setup(bot: commands.Bot):
    settings = load_settings()
    if settings.guild_id <= 0:
        log.warning("[callkeeper] CALLKEEPER_GUILD_ID ausente; cog não registrada")
        return
    await bot.add_cog(CallKeeper(bot))
