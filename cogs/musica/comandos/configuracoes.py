from __future__ import annotations

import asyncio

import discord
from discord.ext import commands

from ..busca import arquivo
from ..interface.componentes import VoiceStatusSettingsView


class FluxoConfiguracoes:
    """Configurações expostas por comandos do domínio de música."""

    async def _run_musicarquivo(self, ctx: commands.Context, option: str = "") -> None:
        if not await self.bot.is_owner(ctx.author):
            return
        value = option.strip().lower()
        if value == "off":
            await asyncio.to_thread(arquivo.set_channel, 0, 0)
            await ctx.reply("Arquivo de músicas desativado.", mention_author=False)
            return
        if value in {"", "status"}:
            _guild_id, channel_id = await asyncio.to_thread(arquivo.channel)
            counts = await asyncio.to_thread(arquivo.counts)
            channel_text = f"<#{channel_id}>" if channel_id else "desativado"
            await ctx.reply(f"Arquivo: {channel_text} · enviadas: {counts.get('done', 0)} · pendentes: {counts.get('waiting', 0)} · acima do limite: {counts.get('too_large', 0)}.",
                            mention_author=False, allowed_mentions=discord.AllowedMentions.none())
            return
        try:
            channel_id = int(value.removeprefix("<#").removesuffix(">"))
            channel = self.bot.get_channel(channel_id) or await self.bot.fetch_channel(channel_id)
        except (ValueError, discord.DiscordException):
            channel = None
        if not isinstance(channel, discord.TextChannel):
            await ctx.reply("Use `_musicarquivo #canal`, `status` ou `off`.", mention_author=False)
            return
        bot_member = channel.guild.me
        permissions = channel.permissions_for(bot_member) if bot_member else None
        if permissions is None or not all((permissions.view_channel, permissions.send_messages,
                                            permissions.attach_files, permissions.embed_links,
                                            permissions.read_message_history)):
            await ctx.reply("Preciso ler o histórico e enviar mensagens, anexos e embeds nesse canal.", mention_author=False)
            return
        await asyncio.to_thread(arquivo.set_channel, channel.guild.id, channel.id)
        await ctx.reply(f"Arquivo de músicas configurado em {channel.mention}.", mention_author=False,
                        allowed_mentions=discord.AllowedMentions.none())

    async def _run_voicestatus(self, ctx: commands.Context, action: str = "", *, value: str = "") -> None:
        if not self.router.is_music_staff(ctx.author):
            await self._reply(ctx, "Apenas staff pode configurar o status do canal de voz.")
            return

        action_norm = (action or "").strip().lower()
        if action_norm in {"on", "ativar", "ligar", "enable", "enabled"}:
            await self.router.set_voice_status_enabled(ctx.guild.id, True)
        elif action_norm in {"off", "desativar", "desligar", "disable", "disabled"}:
            await self.router.set_voice_status_enabled(ctx.guild.id, False)
        elif action_norm in {"template", "modelo", "status", "tocando"}:
            if value.strip():
                await self.router.set_voice_status_template(ctx.guild.id, value)
        elif action_norm in {"idle", "parado", "vazio"}:
            idle = value.strip()
            if idle in {"-", "clear", "limpar", "reset", "vazio"}:
                idle = ""
            await self.router.set_voice_status_idle(ctx.guild.id, idle)
        elif action_norm in {"reset", "padrao", "padrão", "default"}:
            await self.router.reset_voice_status_settings(ctx.guild.id)
        elif action_norm and action_norm not in {"painel", "panel", "config", "configurar"}:
            await self._reply(ctx, "Use `_voicestatus` para abrir o painel, ou `_voicestatus template <modelo>` para alterar direto.")
            return

        await self._reply(ctx, view=VoiceStatusSettingsView(self.router, ctx.guild.id, owner_id=ctx.author.id))
