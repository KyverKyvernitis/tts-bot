from __future__ import annotations

import contextlib
import io
import logging
from datetime import datetime, timezone
from typing import Any, Literal

import discord
from discord import app_commands
from discord.ext import commands

from music_system import AudioRouter
from music_system.diagnostics import (
    DiagnosticsOptions,
    build_full_vps_diagnostics_report,
    build_git_tracked_base_archive,
    build_music_diagnostics_report,
    build_music_diagnostics_archive,
    build_vps_snapshot_archive,
)

logger = logging.getLogger(__name__)

VPS_COMMAND_GUILD_ID = 927002914449424404
VPS_COMMAND_GUILD = discord.Object(id=VPS_COMMAND_GUILD_ID)

VpsAction = Literal["base_git", "music_diag", "full_diag"]


def _get_audio_router(bot: commands.Bot) -> AudioRouter:
    router = getattr(bot, "audio_router", None)
    if router is None:
        router = AudioRouter(bot)
        setattr(bot, "audio_router", router)
    return router


def _safe_get_value(item: Any, *, default: Any = None) -> Any:
    with contextlib.suppress(Exception):
        value = getattr(item, "value")
        if value not in (None, ""):
            return value
    with contextlib.suppress(Exception):
        values = list(getattr(item, "values") or [])
        if values:
            return values[0]
    return default


class VpsModal(discord.ui.Modal, title="Painel da VPS"):
    """Modal simples do /vps.

    Usa RadioGroup e Checkbox quando disponíveis no discord.py 2.7. Se o runtime
    não tiver esses componentes por algum motivo, cai para TextInput para não
    quebrar o comando.
    """

    def __init__(self, cog: "VpsCommandMixin"):
        super().__init__(timeout=180)
        self.cog = cog
        self._ui_mode = "fallback_text"

        radio_cls = getattr(discord.ui, "RadioGroup", None)
        checkbox_cls = getattr(discord.ui, "Checkbox", None)
        label_cls = getattr(discord.ui, "Label", None)
        radio_opt_cls = getattr(discord, "RadioGroupOption", None)

        if radio_cls is not None and checkbox_cls is not None and label_cls is not None:
            try:
                if radio_opt_cls is not None:
                    options = [
                        radio_opt_cls(label="Base Git", value="base_git", description="Envia só os arquivos atuais rastreados pelo Git", default=True),
                        radio_opt_cls(label="Diagnóstico musical", value="music_diag", description="Lavalink, LavaSrc, yt-dlp, Spotify e SoundCloud"),
                        radio_opt_cls(label="Diagnóstico completo", value="full_diag", description="Logs completas do bot, serviços e resumo geral"),
                    ]
                    self.action = radio_cls(custom_id="vps_action", required=True, options=options)
                else:
                    self.action = radio_cls(custom_id="vps_action", required=True)
                    self.action.add_option(label="Base Git", value="base_git", description="Envia só os arquivos atuais rastreados pelo Git", default=True)
                    self.action.add_option(label="Diagnóstico musical", value="music_diag", description="Lavalink, LavaSrc, yt-dlp, Spotify e SoundCloud")
                    self.action.add_option(label="Diagnóstico completo", value="full_diag", description="Logs completas do bot, serviços e resumo geral")

                self.attach_snapshot = checkbox_cls(custom_id="vps_snapshot", default=False)

                self.add_item(label_cls(
                    text="O que enviar?",
                    description="Escolha uma opção principal.",
                    component=self.action,
                ))
                self.add_item(label_cls(
                    text="Anexar snapshot da VPS",
                    description="Inclui configs sanitizadas, services, DB musicnode e logs filtradas em .zip.",
                    component=self.attach_snapshot,
                ))
                self._ui_mode = "radio_checkbox"
                return
            except Exception:
                logger.exception("[utility/vps] falha ao montar modal com RadioGroup/Checkbox; usando fallback")

        self.action_input = discord.ui.TextInput(
            label="Ação",
            placeholder="base, musica ou completo",
            required=True,
            default="base",
            max_length=20,
        )
        self.snapshot_input = discord.ui.TextInput(
            label="Anexar snapshot da VPS?",
            placeholder="sim ou não",
            required=False,
            default="não",
            max_length=10,
        )
        self.add_item(self.action_input)
        self.add_item(self.snapshot_input)

    def _selected_action(self) -> VpsAction:
        if self._ui_mode == "radio_checkbox":
            raw = str(_safe_get_value(getattr(self, "action", None), default="base_git") or "base_git").strip().lower()
        else:
            raw = str(getattr(getattr(self, "action_input", None), "value", "base") or "base").strip().lower()

        aliases = {
            "base": "base_git",
            "base_git": "base_git",
            "git": "base_git",
            "musica": "music_diag",
            "música": "music_diag",
            "music": "music_diag",
            "music_diag": "music_diag",
            "diagnóstico musical": "music_diag",
            "diagnostico musical": "music_diag",
            "completo": "full_diag",
            "full": "full_diag",
            "full_diag": "full_diag",
            "diagnóstico completo": "full_diag",
            "diagnostico completo": "full_diag",
        }
        return aliases.get(raw, "base_git")  # type: ignore[return-value]

    def _include_snapshot(self) -> bool:
        if self._ui_mode == "radio_checkbox":
            return bool(_safe_get_value(getattr(self, "attach_snapshot", None), default=False))
        raw = str(getattr(getattr(self, "snapshot_input", None), "value", "") or "").strip().lower()
        return raw in {"s", "sim", "y", "yes", "1", "true", "on"}

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog._run_vps_action(
            interaction,
            action=self._selected_action(),
            include_snapshot=self._include_snapshot(),
        )


class VpsCommandMixin:
    """Comando /vps da cog Utility."""

    async def _can_use_vps(self, interaction: discord.Interaction) -> bool:
        with contextlib.suppress(Exception):
            return bool(await self.bot.is_owner(interaction.user))
        return False

    async def _vps_context_options(self, interaction: discord.Interaction) -> DiagnosticsOptions:
        guild = interaction.guild
        user = interaction.user
        return DiagnosticsOptions(
            guild_id=int(getattr(guild, "id", 0) or 0),
            guild_name=str(getattr(guild, "name", "guilda de teste") or "guilda de teste"),
            requester_id=int(getattr(user, "id", 0) or 0),
            requester_name=str(getattr(user, "display_name", None) or getattr(user, "name", "usuário")),
            include_journalctl=True,
            include_local_logs=True,
        )

    async def _run_vps_action(self, interaction: discord.Interaction, *, action: VpsAction, include_snapshot: bool) -> None:
        if interaction.guild is None or int(getattr(interaction.guild, "id", 0) or 0) != VPS_COMMAND_GUILD_ID:
            await interaction.response.send_message("Esse painel só funciona na guilda de teste configurada.")
            return

        if not await self._can_use_vps(interaction):
            await interaction.response.send_message("Esse painel técnico da VPS é exclusivo do dono do bot.")
            return

        await interaction.response.defer(thinking=True, ephemeral=False)

        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        files: list[discord.File] = []
        lines: list[str] = ["`🖥️` Painel da VPS concluído."]

        if action == "base_git":
            try:
                payload, filename, summary, manifest = await build_git_tracked_base_archive()
                if payload and filename:
                    files.append(discord.File(io.BytesIO(payload), filename=filename))
                    lines.append("`📦` Base Git rastreada anexada.")
                    if summary:
                        lines.append(f"`ℹ️` {summary}")
                else:
                    lines.append(f"`⚠️` {summary or 'Não consegui gerar a base Git.'}")
                if manifest:
                    manifest_payload = manifest.encode("utf-8", "replace")
                    files.append(discord.File(io.BytesIO(manifest_payload), filename=f"vps-base-manifest-{stamp}.txt"))
            except Exception as exc:
                logger.exception("[utility/vps] falha ao gerar base git")
                lines.append(f"`⚠️` Base Git falhou: {type(exc).__name__}: {str(exc)[:300]}")

        elif action == "music_diag":
            router = _get_audio_router(self.bot)
            try:
                payload, filename, summary, fallback_report = await build_music_diagnostics_archive(router, await self._vps_context_options(interaction))
                if payload and filename:
                    files.append(discord.File(io.BytesIO(payload), filename=filename))
                    lines.append("`🎵` Diagnóstico musical modular anexado em .zip.")
                    if summary:
                        lines.append(f"`ℹ️` {summary}")
                    if fallback_report:
                        files.append(discord.File(io.BytesIO(fallback_report.encode("utf-8", "replace")), filename=f"vps-music-diagnostics-summary-{stamp}.txt"))
                else:
                    lines.append(f"`⚠️` Diagnóstico modular não foi anexado: {summary or 'falha sem detalhes'}")
                    report = fallback_report or await build_music_diagnostics_report(router, await self._vps_context_options(interaction))
                    files.append(discord.File(io.BytesIO(report.encode("utf-8", "replace")), filename=f"vps-music-diagnostics-{stamp}.txt"))
            except Exception as exc:
                logger.exception("[utility/vps] falha ao gerar diagnóstico musical")
                report = f"# Diagnóstico musical falhou\nTipo: {type(exc).__name__}\nErro: {str(exc)[:500]}\n"
                files.append(discord.File(io.BytesIO(report.encode("utf-8", "replace")), filename=f"vps-music-diagnostics-{stamp}.txt"))
                lines.append("`⚠️` Diagnóstico musical falhou; anexei relatório mínimo.")

        else:
            router = _get_audio_router(self.bot)
            try:
                report = await build_full_vps_diagnostics_report(router, await self._vps_context_options(interaction))
            except Exception as exc:
                logger.exception("[utility/vps] falha ao gerar diagnóstico completo")
                report = f"# Diagnóstico completo falhou\nTipo: {type(exc).__name__}\nErro: {str(exc)[:500]}\n"
            files.append(discord.File(io.BytesIO(report.encode("utf-8", "replace")), filename=f"vps-full-diagnostics-{stamp}.txt"))
            lines.append("`🧾` Diagnóstico completo anexado.")

        if include_snapshot:
            try:
                payload, filename, summary = await build_vps_snapshot_archive()
                if payload and filename:
                    files.append(discord.File(io.BytesIO(payload), filename=filename))
                    lines.append("`🧰` Snapshot sanitizado da VPS anexado.")
                    if summary:
                        lines.append(f"`ℹ️` {summary}")
                else:
                    lines.append(f"`⚠️` Snapshot da VPS não foi anexado: {summary or 'falha sem detalhes'}")
            except Exception as exc:
                logger.exception("[utility/vps] falha ao gerar snapshot da VPS")
                lines.append(f"`⚠️` Snapshot da VPS falhou: {type(exc).__name__}: {str(exc)[:300]}")

        if not files:
            lines.append("`⚠️` Nenhum arquivo foi gerado.")

        await interaction.followup.send("\n".join(lines), files=files[:10])

    @app_commands.command(name="vps", description="Abre o painel de diagnóstico/anexos da VPS")
    @app_commands.guilds(VPS_COMMAND_GUILD)
    async def vps(self, interaction: discord.Interaction):
        if interaction.guild is None or int(getattr(interaction.guild, "id", 0) or 0) != VPS_COMMAND_GUILD_ID:
            await interaction.response.send_message("Esse painel só funciona na guilda de teste configurada.")
            return
        if not await self._can_use_vps(interaction):
            await interaction.response.send_message("Esse painel técnico da VPS é exclusivo do dono do bot.")
            return
        await interaction.response.send_modal(VpsModal(self))
