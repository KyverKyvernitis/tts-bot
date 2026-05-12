from __future__ import annotations

import contextlib
import io
import logging
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

from music_system import AudioRouter
from music_system.diagnostics import (
    DiagnosticsOptions,
    build_git_tracked_base_archive,
    build_music_diagnostics_report,
)

logger = logging.getLogger(__name__)

MUSIC_DIAGNOSTICS_GUILD_ID = 927002914449424404
MUSIC_DIAGNOSTICS_GUILD = discord.Object(id=MUSIC_DIAGNOSTICS_GUILD_ID)


def _get_audio_router(bot: commands.Bot) -> AudioRouter:
    router = getattr(bot, "audio_router", None)
    if router is None:
        router = AudioRouter(bot)
        setattr(bot, "audio_router", router)
    return router


class MusicDiagnosticsCommandMixin:
    """Comando de diagnóstico de música registrado na cog Utility.

    A coleta pesada continua em music_system.diagnostics; este módulo cuida só
    da camada Discord/app command para manter a cog utility modularizada.
    """

    async def _can_use_music_diagnostics(self, interaction: discord.Interaction) -> bool:
        # O relatório contém caminhos, nomes de serviços e logs sanitizados.
        # Mantém restrito ao dono do bot para evitar vazamento operacional.
        with contextlib.suppress(Exception):
            return bool(await self.bot.is_owner(interaction.user))
        return False

    @app_commands.command(
        name="diagnostico_musica",
        description="Gera um relatório técnico de música/Lavalink/yt-dlp em anexo",
    )
    @app_commands.guilds(MUSIC_DIAGNOSTICS_GUILD)
    @app_commands.describe(
        incluir_journalctl="Inclui logs recentes dos serviços tts-bot/lavalink/nodelink quando o usuário do bot puder ler",
        incluir_logs_locais="Inclui o final dos arquivos em logs/*.log do projeto",
        anexar_base="Anexa também um .zip com os arquivos atuais rastreados pelo Git",
    )
    async def diagnostico_musica(
        self,
        interaction: discord.Interaction,
        incluir_journalctl: bool = True,
        incluir_logs_locais: bool = True,
        anexar_base: bool = True,
    ):
        if interaction.guild is None or int(getattr(interaction.guild, "id", 0) or 0) != MUSIC_DIAGNOSTICS_GUILD_ID:
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "Esse diagnóstico só funciona na guilda de teste configurada."
                )
            return

        if not await self._can_use_music_diagnostics(interaction):
            await interaction.response.send_message(
                "Esse diagnóstico técnico de música é exclusivo do dono do bot."
            )
            return

        await interaction.response.defer(thinking=True)

        router = _get_audio_router(self.bot)
        try:
            report = await build_music_diagnostics_report(
                router,
                DiagnosticsOptions(
                    guild_id=int(interaction.guild.id),
                    guild_name=str(getattr(interaction.guild, "name", "guilda de teste") or "guilda de teste"),
                    requester_id=int(getattr(interaction.user, "id", 0) or 0),
                    requester_name=str(getattr(interaction.user, "display_name", None) or getattr(interaction.user, "name", "usuário")),
                    include_journalctl=bool(incluir_journalctl),
                    include_local_logs=bool(incluir_logs_locais),
                ),
            )
        except Exception as exc:
            logger.exception("[utility/diagnostico_musica] falha ao gerar relatório")
            report = (
                "# Diagnóstico de música falhou\n"
                f"Tipo: {type(exc).__name__}\n"
                f"Erro: {str(exc)[:500]}\n"
            )

        base_summary = ""
        base_payload: bytes | None = None
        base_filename = ""
        base_manifest = ""
        if anexar_base:
            try:
                base_payload, base_filename, base_summary, base_manifest = await build_git_tracked_base_archive()
            except Exception as exc:
                logger.exception("[utility/diagnostico_musica] falha ao gerar base git-tracked")
                base_summary = f"Base git-tracked não foi anexada: {type(exc).__name__}: {str(exc)[:300]}"

        if base_manifest:
            report += (
                "\n\n"
                "============================================================\n"
                "BASE GIT-TRACKED ANEXADA\n"
                "============================================================\n"
                f"{base_manifest}"
            )

        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        payload = report.encode("utf-8", "replace")
        files: list[discord.File] = [
            discord.File(
                io.BytesIO(payload),
                filename=f"music-diagnostics-{stamp}.txt",
            )
        ]

        if base_payload and base_filename:
            files.append(discord.File(io.BytesIO(base_payload), filename=base_filename))

        message = "`🧪` Diagnóstico de música concluído. O relatório foi anexado em `.txt` com segredos mascarados."
        if anexar_base:
            message += "\n`📦` Também anexei a base atual rastreada pelo Git em `.zip`." if len(files) > 1 else f"\n`⚠️` {base_summary or 'Não consegui anexar a base git-tracked.'}"
        if base_summary and len(files) > 1:
            message += f"\n`ℹ️` {base_summary}"

        await interaction.followup.send(
            message,
            files=files,
        )
