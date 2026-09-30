from __future__ import annotations

import asyncio
from datetime import datetime
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import discord
from discord.ext import commands

from ..agente_telefone.comandos import music_agent_status
from ..arquivo_coordenador import _archive_agent_ready
from ..busca import arquivo
from ..interface.componentes import VoiceStatusSettingsView

_BR_TZ = ZoneInfo("America/Sao_Paulo")
_ARCHIVE_REASONS = {
    "no_match": "nenhuma fonte corresponde à música", "source_mismatch": "fonte oficial divergente",
    "metadata_mismatch": "duração do áudio divergente", "source_resolve_error": "fonte indisponível",
    "download_timeout": "tempo esgotado no download", "agent_timeout": "agente não concluiu",
    "media_invalid": "arquivo de áudio inválido", "size_limit": "acima do limite de upload",
    "duration_limit": "acima de 10 minutos", "sem_diagnostico": "falha antiga sem motivo registrado",
    "source_http_403": "acesso recusado à fonte", "download_http_403": "acesso recusado ao áudio",
    "source_download_failed": "download da fonte falhou", "download_failed": "download falhou",
    "discord_http_error": "envio ao Discord falhou", "agent_error": "erro no agente",
    "agent_rejected": "agente rejeitou o envio", "invalid_reference": "anexo não confirmado",
}


def _archive_time(stamp: float) -> str:
    return datetime.fromtimestamp(stamp, _BR_TZ).strftime("%d/%m %H:%M") if stamp else "—"


class FluxoConfiguracoes:
    """Configurações expostas por comandos do domínio de música."""

    async def _run_musicarquivo(self, ctx: commands.Context, option: str = "") -> None:
        if not await self.bot.is_owner(ctx.author):
            return
        raw = option.strip()
        value = raw.lower()
        if value.startswith("fonte "):
            parts = raw.split(maxsplit=2)
            try:
                if len(parts) != 3:
                    raise ValueError("use `_musicarquivo fonte <chave> <link Bandcamp>`")
                await asyncio.to_thread(arquivo.set_source_override, parts[1], parts[2])
            except ValueError as exc:
                await ctx.reply(str(exc), mention_author=False)
                return
            self.archive._wake.set()
            await ctx.reply("Fonte oficial registrada. Arquivamento agendado.", mention_author=False)
            return
        if value.startswith("reavaliar "):
            try:
                await asyncio.to_thread(arquivo.requeue, raw.split(maxsplit=1)[1])
            except ValueError as exc:
                await ctx.reply(str(exc), mention_author=False)
                return
            self.archive._wake.set()
            await ctx.reply("Música colocada novamente na fila de arquivamento.", mention_author=False)
            return
        if value == "falhas" or value.startswith("falhas "):
            try:
                page = int(raw.split(maxsplit=1)[1]) if " " in raw else 1
                if not 1 <= page <= 100:
                    raise ValueError
            except ValueError:
                await ctx.reply("Use `_musicarquivo falhas [página]` (1 a 100).", mention_author=False)
                return
            entries = await asyncio.to_thread(arquivo.failures, 6, (page - 1) * 6)
            if not entries:
                await ctx.reply("Nenhuma música com falha no arquivo.", mention_author=False)
                return
            lines = [f"**Auditoria do arquivo · falhas · página {page}**"]
            for entry in entries:
                reason = _ARCHIVE_REASONS.get(entry["reason"], entry["reason"].replace("_", " "))
                retry = f" · próxima: {_archive_time(entry['retry_at'])}" if entry["retry_at"] else ""
                host = (urlsplit(entry["source"]).hostname or "") if entry["source"] else ""
                lines.append(f"`{entry['key']}` · {entry['title'][:70]}\n"
                             f"{entry['state']} · {reason} · {entry['attempts']} tentativa(s){retry}"
                             f"{' · '+host if host else ''}")
            lines.append("Fonte nova: `_musicarquivo fonte <chave> <link>` · tentar de novo: `_musicarquivo reavaliar <chave>`.")
            await ctx.reply("\n".join(lines)[:1900], mention_author=False,
                            allowed_mentions=discord.AllowedMentions.none())
            return
        if value.startswith("auditoria "):
            parts = raw.split()
            try:
                page = int(parts[2]) if len(parts) == 3 else 1
                if len(parts) > 3 or not 1 <= page <= 100:
                    raise ValueError
            except ValueError:
                await ctx.reply("Use `_musicarquivo auditoria <chave> [página]` (1 a 100).", mention_author=False)
                return
            key = parts[1]
            events = await asyncio.to_thread(arquivo.history, key, 8, (page - 1) * 8)
            if not events:
                await ctx.reply("Nenhum evento encontrado para essa chave.", mention_author=False)
                return
            lines = [f"**Histórico do arquivo · `{key[:32]}` · página {page}**"]
            for event in events:
                reason = _ARCHIVE_REASONS.get(event["reason"], event["reason"].replace("_", " "))
                host = (urlsplit(event["source"]).hostname or "") if event["source"] else ""
                lines.append(f"{_archive_time(event['at'])} · {event['event'].replace('_', ' ')}"
                             f"{' · '+reason if reason else ''}{' · '+host if host else ''}"
                             f"{' · '+event['revision'] if event['revision'] else ''}")
            await ctx.reply("\n".join(lines)[:1900], mention_author=False,
                            allowed_mentions=discord.AllowedMentions.none())
            return
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
                    agent_label = f" · agente: {version}" + ("" if _archive_agent_ready(agent) else " (aguardando 0.3.81)")
                except Exception:
                    agent_label = " · agente: indisponível"
            await ctx.reply(f"Arquivo ({mode}): {channel_text} · escolhas na memória: {counts.get('choices', 0)} · músicas indexadas: {counts.get('learned', 0)} · aguardando envio: {counts.get('unposted', 0)} · enviadas: {counts.get('done', 0)} · atualizando posts: {counts.get('refresh', 0)} · falhas transitórias: {counts.get('failed', 0)} · sem fonte: {counts.get('unavailable', 0)} · pausadas: {counts.get('paused', 0)} · acima do limite: {counts.get('too_large', 0)}{agent_label}.",
                            mention_author=False, allowed_mentions=discord.AllowedMentions.none())
            return
        try:
            channel_id = int(value.removeprefix("<#").removesuffix(">"))
            channel = self.bot.get_channel(channel_id) or await self.bot.fetch_channel(channel_id)
        except (ValueError, discord.DiscordException):
            channel = None
        if not isinstance(channel, discord.ForumChannel) or channel.is_media():
            await ctx.reply("Use `_musicarquivo #fórum`, `status`, `falhas`, `auditoria <chave>`, `reavaliar <chave>` ou `off`.", mention_author=False)
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
