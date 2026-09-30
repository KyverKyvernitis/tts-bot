from __future__ import annotations

import asyncio

import discord
from discord.ext import commands

from ..agente_telefone.comandos import music_agent_status
from ..arquivo_coordenador import _archive_agent_ready
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
            guild_id, channel_id = await asyncio.to_thread(arquivo.channel)
            kind = await asyncio.to_thread(arquivo.channel_type)
            counts = await asyncio.to_thread(arquivo.counts)
            channel_text = f"<#{channel_id}>" if channel_id else "desativado"
            mode = "fórum" if kind == "forum" else "canal antigo"
            agent_label = ""
            if guild_id and kind == "forum":
                try:
                    agent = await music_agent_status(guild_id=guild_id, timeout_seconds=3.0)
                    version = str(agent.get("version") or "?")
                    agent_label = f" · agente: {version}" + ("" if _archive_agent_ready(agent) else " (aguardando 0.3.78)")
                except Exception:
                    agent_label = " · agente: indisponível"
            await ctx.reply(f"Arquivo ({mode}): {channel_text} · escolhas na memória: {counts.get('choices', 0)} · músicas indexadas: {counts.get('learned', 0)} · aguardando envio: {counts.get('unposted', 0)} · enviadas: {counts.get('done', 0)} · atualizando posts: {counts.get('refresh', 0)} · falhas: {counts.get('failed', 0)} · acima do limite: {counts.get('too_large', 0)}{agent_label}.",
                            mention_author=False, allowed_mentions=discord.AllowedMentions.none())
            return
        try:
            channel_id = int(value.removeprefix("<#").removesuffix(">"))
            channel = self.bot.get_channel(channel_id) or await self.bot.fetch_channel(channel_id)
        except (ValueError, discord.DiscordException):
            channel = None
        if not isinstance(channel, discord.ForumChannel) or channel.is_media():
            await ctx.reply("Use `_musicarquivo #fórum`, `status` ou `off`.", mention_author=False)
            return
        bot_member = channel.guild.me
        permissions = channel.permissions_for(bot_member) if bot_member else None
        if permissions is None or not all((permissions.view_channel, permissions.send_messages,
                                            permissions.attach_files, permissions.embed_links,
                                            permissions.read_message_history)):
            await ctx.reply("Preciso criar posts e ler mensagens, anexos e embeds nesse fórum.", mention_author=False)
            return
        if channel.flags.require_tag and not channel.available_tags:
            await ctx.reply("Esse fórum exige tags, mas não tem nenhuma disponível.", mention_author=False)
            return
        await asyncio.to_thread(arquivo.set_channel, channel.guild.id, channel.id, kind="forum")
        await ctx.reply(f"Arquivo de músicas configurado no fórum {channel.mention}.", mention_author=False,
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
