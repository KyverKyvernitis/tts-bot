from __future__ import annotations

import asyncio
import contextlib
import io
import logging
import re
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
    build_quick_vps_status_report,
    build_music_diagnostics_archive,
    build_music_diagnostics_emergency_report,
    build_vps_snapshot_archive,
)

logger = logging.getLogger(__name__)

VPS_COMMAND_GUILD_ID = 927002914449424404
VPS_COMMAND_GUILD = discord.Object(id=VPS_COMMAND_GUILD_ID)

VpsItem = Literal["quick_status", "base_git", "music_diag", "full_diag", "snapshot"]

VPS_QUICK_STATUS_TIMEOUT_SECONDS = 22.0
VPS_BASE_TIMEOUT_SECONDS = 70.0
VPS_MUSIC_DIAG_TIMEOUT_SECONDS = 115.0
VPS_FULL_DIAG_TIMEOUT_SECONDS = 150.0
VPS_SNAPSHOT_TIMEOUT_SECONDS = 75.0


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


def _format_attachment_size(size_bytes: int | None) -> str:
    try:
        size = max(0, int(size_bytes or 0))
    except Exception:
        size = 0
    if size < 1_000:
        return f"{size} B"
    if size < 1_000_000:
        return f"{round(size / 1_000)} kB"
    return f"{size / 1_000_000:.2f} MB"


class VpsModal(discord.ui.Modal, title="Painel da VPS"):
    """Modal simples do /vps.

    Usa Checkboxes quando disponíveis no discord.py 2.7 para permitir múltipla
    escolha. Se o runtime não tiver esses componentes por algum motivo, cai para
    TextInput para não quebrar o comando.
    """

    def __init__(self, cog: "VpsCommandMixin", *, force_text_fallback: bool = False):
        super().__init__(timeout=180)
        self.cog = cog
        self._ui_mode = "fallback_text"

        checkbox_cls = None if force_text_fallback else getattr(discord.ui, "Checkbox", None)
        label_cls = None if force_text_fallback else getattr(discord.ui, "Label", None)

        if checkbox_cls is not None and label_cls is not None:
            try:
                self.base_git = checkbox_cls(custom_id="vps_base_git", default=True)
                self.music_diag = checkbox_cls(custom_id="vps_music_diag", default=False)
                self.full_diag = checkbox_cls(custom_id="vps_full_diag", default=False)
                self.snapshot = checkbox_cls(custom_id="vps_snapshot", default=False)
                self.quick_status = checkbox_cls(custom_id="vps_quick_status", default=False)

                self.add_item(label_cls(
                    text="Base Git leve",
                    description="Código rastreado pelo Git, sem assets e sem manifestos.",
                    component=self.base_git,
                ))
                self.add_item(label_cls(
                    text="Diagnóstico musical",
                    description="Lavalink, LavaSrc, yt-dlp, players, filas e logs musicais.",
                    component=self.music_diag,
                ))
                self.add_item(label_cls(
                    text="Diagnóstico completo",
                    description="Logs gerais do bot, serviços, memória, disco e resumo da VPS.",
                    component=self.full_diag,
                ))
                self.add_item(label_cls(
                    text="Snapshot da VPS",
                    description="Configs sanitizadas, services, DB musicnode e logs filtradas em .zip.",
                    component=self.snapshot,
                ))
                self.add_item(label_cls(
                    text="Status rápido",
                    description="Resumo em texto: RAM, disco, serviços, Git status e erros recentes.",
                    component=self.quick_status,
                ))
                self._ui_mode = "checkboxes"
                return
            except Exception:
                logger.exception("[utility/vps] falha ao montar modal com Checkboxes; usando fallback")

        self.items_input = discord.ui.TextInput(
            label="O que enviar?",
            placeholder="base, musica, completo, snapshot, status",
            required=True,
            default="base",
            max_length=120,
        )
        self.add_item(self.items_input)

    def _selected_items(self) -> list[VpsItem]:
        if self._ui_mode == "checkboxes":
            selected: list[VpsItem] = []
            mapping: list[tuple[str, VpsItem]] = [
                ("quick_status", "quick_status"),
                ("base_git", "base_git"),
                ("music_diag", "music_diag"),
                ("full_diag", "full_diag"),
                ("snapshot", "snapshot"),
            ]
            for attr, item in mapping:
                if bool(_safe_get_value(getattr(self, attr, None), default=False)):
                    selected.append(item)
            return selected

        raw = str(getattr(getattr(self, "items_input", None), "value", "base") or "base").strip().lower()
        tokens = [token.strip() for token in re.split(r"[,;\n]+", raw) if token.strip()]
        selected: list[VpsItem] = []
        aliases: dict[str, VpsItem] = {
            "base": "base_git",
            "base git": "base_git",
            "base_git": "base_git",
            "git": "base_git",
            "codigo": "base_git",
            "código": "base_git",
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
            "snapshot": "snapshot",
            "vps": "snapshot",
            "status": "quick_status",
            "status rápido": "quick_status",
            "status rapido": "quick_status",
            "quick": "quick_status",
            "quick_status": "quick_status",
        }
        for token in tokens:
            item = aliases.get(token)
            if item is not None and item not in selected:
                selected.append(item)
        return selected

    async def on_submit(self, interaction: discord.Interaction):
        try:
            await self.cog._run_vps_action(
                interaction,
                selected_items=self._selected_items(),
            )
        except Exception as exc:
            logger.exception("[utility/vps] erro fatal no submit do modal")
            message = f"`⚠️` O painel da VPS falhou antes de concluir: {type(exc).__name__}: {str(exc)[:300]}"
            with contextlib.suppress(Exception):
                if not interaction.response.is_done():
                    await interaction.response.send_message(message)
                    return
            with contextlib.suppress(Exception):
                await interaction.followup.send(message)


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

    async def _defer_vps_interaction(self, interaction: discord.Interaction) -> bool:
        try:
            if not interaction.response.is_done():
                await interaction.response.defer(thinking=True, ephemeral=False)
            return True
        except discord.InteractionResponded:
            return True
        except Exception:
            logger.exception("[utility/vps] não consegui deferir interação /vps")
            return False

    async def _with_vps_timeout(self, label: str, coro, *, timeout: float):
        try:
            return await asyncio.wait_for(coro, timeout=timeout)
        except asyncio.TimeoutError as exc:
            logger.warning("[utility/vps] %s excedeu timeout de %.1fs", label, timeout)
            raise TimeoutError(f"{label} excedeu {timeout:.0f}s") from exc

    async def _run_vps_action(self, interaction: discord.Interaction, *, selected_items: list[VpsItem]) -> None:
        if interaction.guild is None or int(getattr(interaction.guild, "id", 0) or 0) != VPS_COMMAND_GUILD_ID:
            await interaction.response.send_message("Esse painel só funciona na guilda de teste configurada.")
            return

        # Modal submit precisa ser reconhecido em até poucos segundos.
        # Fazemos o defer antes de qualquer coleta, consulta de owner ou I/O pesado.
        if not await self._defer_vps_interaction(interaction):
            return

        if not await self._can_use_vps(interaction):
            await interaction.followup.send("Esse painel técnico da VPS é exclusivo do dono do bot.")
            return

        if not selected_items:
            await interaction.followup.send("`⚠️` Selecione pelo menos uma opção no painel da VPS.")
            return

        ordered_items: list[VpsItem] = [
            item for item in ["quick_status", "base_git", "music_diag", "full_diag", "snapshot"] if item in selected_items
        ]

        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        files: list[discord.File] = []
        lines: list[str] = ["`🖥️` Painel da VPS concluído."]
        generated_any = False

        for item in ordered_items:
            if item == "quick_status":
                try:
                    report = await self._with_vps_timeout("status rápido", build_quick_vps_status_report(), timeout=VPS_QUICK_STATUS_TIMEOUT_SECONDS)
                    report = (report or "Status rápido vazio.").strip()
                    if len(report) > 1350:
                        files.append(discord.File(io.BytesIO(report.encode("utf-8", "replace")), filename=f"status-{stamp}.txt"))
                        lines.append("`⚡` Status rápido anexado.")
                    else:
                        lines.append("`⚡` Status rápido:")
                        lines.append(f"```txt\n{report}\n```")
                    generated_any = True
                except Exception as exc:
                    logger.exception("[utility/vps] falha ao gerar status rápido")
                    lines.append(f"`⚠️` Status rápido falhou: {type(exc).__name__}: {str(exc)[:300]}")
                continue

            if item == "base_git":
                try:
                    payload, filename, summary, _manifest = await self._with_vps_timeout("base Git", build_git_tracked_base_archive(), timeout=VPS_BASE_TIMEOUT_SECONDS)
                    if payload and filename:
                        files.append(discord.File(io.BytesIO(payload), filename=filename))
                        lines.append(f"`📦` Repositório anexado ({_format_attachment_size(len(payload))}).")
                        generated_any = True
                    else:
                        lines.append(f"`⚠️` {summary or 'Não consegui gerar a base Git.'}")
                except Exception as exc:
                    logger.exception("[utility/vps] falha ao gerar base git")
                    lines.append(f"`⚠️` Base Git falhou: {type(exc).__name__}: {str(exc)[:300]}")
                continue

            if item == "music_diag":
                router = _get_audio_router(self.bot)
                try:
                    payload, filename, summary, fallback_report = await self._with_vps_timeout("diagnóstico musical", build_music_diagnostics_archive(router, await self._vps_context_options(interaction)), timeout=VPS_MUSIC_DIAG_TIMEOUT_SECONDS)
                    if payload and filename:
                        files.append(discord.File(io.BytesIO(payload), filename=filename))
                        lines.append(f"`🎵` Diagnóstico musical anexado ({_format_attachment_size(len(payload))}).")
                        generated_any = True
                        # O diagnóstico musical modular deve ser um único anexo.
                        # O resumo completo fica dentro do zip como 00-resumo-curto.txt/summary.txt.
                    else:
                        lines.append(f"`⚠️` Diagnóstico modular não foi anexado: {summary or 'falha sem detalhes'}")
                        report = fallback_report or await self._with_vps_timeout("diagnóstico musical texto", build_music_diagnostics_report(router, await self._vps_context_options(interaction)), timeout=VPS_MUSIC_DIAG_TIMEOUT_SECONDS)
                        report_bytes = report.encode("utf-8", "replace")
                        files.append(discord.File(io.BytesIO(report_bytes), filename=f"music-diag-{stamp}.txt"))
                        lines.append(f"`🎵` Diagnóstico musical anexado ({_format_attachment_size(len(report_bytes))}).")
                        generated_any = True
                except Exception as exc:
                    logger.exception("[utility/vps] falha ao gerar diagnóstico musical")
                    try:
                        report = await self._with_vps_timeout(
                            "diagnóstico musical emergencial",
                            build_music_diagnostics_emergency_report(router, await self._vps_context_options(interaction), reason=f"{type(exc).__name__}: {str(exc)[:500]}"),
                            timeout=18.0,
                        )
                    except Exception as emergency_exc:
                        report = (
                            "# Diagnóstico musical falhou\n"
                            f"Tipo: {type(exc).__name__}\n"
                            f"Erro: {str(exc)[:500]}\n\n"
                            "# Diagnóstico emergencial também falhou\n"
                            f"Tipo: {type(emergency_exc).__name__}\n"
                            f"Erro: {str(emergency_exc)[:500]}\n"
                        )
                    report_bytes = report.encode("utf-8", "replace")
                    files.append(discord.File(io.BytesIO(report_bytes), filename=f"music-diag-emergency-{stamp}.txt"))
                    lines.append(f"`⚠️` Diagnóstico musical emergencial anexado ({_format_attachment_size(len(report_bytes))}).")
                    generated_any = True
                continue

            if item == "full_diag":
                router = _get_audio_router(self.bot)
                try:
                    report = await self._with_vps_timeout("diagnóstico completo", build_full_vps_diagnostics_report(router, await self._vps_context_options(interaction)), timeout=VPS_FULL_DIAG_TIMEOUT_SECONDS)
                except Exception as exc:
                    logger.exception("[utility/vps] falha ao gerar diagnóstico completo")
                    report = f"# Diagnóstico completo falhou\nTipo: {type(exc).__name__}\nErro: {str(exc)[:500]}\n"
                report_bytes = report.encode("utf-8", "replace")
                files.append(discord.File(io.BytesIO(report_bytes), filename=f"full-diag-{stamp}.txt"))
                lines.append(f"`🧾` Diagnóstico completo anexado ({_format_attachment_size(len(report_bytes))}).")
                generated_any = True
                continue

            if item == "snapshot":
                try:
                    payload, filename, summary = await self._with_vps_timeout("snapshot da VPS", build_vps_snapshot_archive(), timeout=VPS_SNAPSHOT_TIMEOUT_SECONDS)
                    if payload and filename:
                        files.append(discord.File(io.BytesIO(payload), filename=filename))
                        lines.append(f"`🧰` Snapshot da VPS anexado ({_format_attachment_size(len(payload))}).")
                        generated_any = True
                    else:
                        lines.append(f"`⚠️` Snapshot da VPS não foi anexado: {summary or 'falha sem detalhes'}")
                except Exception as exc:
                    logger.exception("[utility/vps] falha ao gerar snapshot da VPS")
                    lines.append(f"`⚠️` Snapshot da VPS falhou: {type(exc).__name__}: {str(exc)[:300]}")
                continue

        if not generated_any:
            lines.append("`⚠️` Nenhum arquivo ou status foi gerado.")

        try:
            content = "\n".join(lines)
            if len(content) > 1900:
                overflow = content
                files.append(discord.File(io.BytesIO(overflow.encode("utf-8", "replace")), filename=f"vps-resumo-{stamp}.txt"))
                content = "`🖥️` Painel da VPS concluído.\n`ℹ️` O resumo ficou grande e foi anexado em .txt."
            await interaction.followup.send(content, files=files[:10])
        except Exception as exc:
            logger.exception("[utility/vps] falha ao enviar resposta final")
            fallback = "\n".join(lines + [f"`⚠️` Falhei ao anexar/enviar arquivos: {type(exc).__name__}: {str(exc)[:300]}"])
            with contextlib.suppress(Exception):
                await interaction.followup.send(fallback[:1900])

    async def _send_vps_modal(self, interaction: discord.Interaction) -> None:
        """Abre o modal do /vps sem defer prévio.

        Discord não permite ``send_modal`` depois de ``defer``. Além disso, em
        alguns runtimes/mobile os componentes novos de Modal (RadioGroup/Checkbox
        dentro de Label) podem falhar antes de abrir. Nesse caso, o comando cai
        imediatamente para um modal clássico de TextInput em vez de deixar
        “O aplicativo não respondeu”.
        """
        try:
            await interaction.response.send_modal(VpsModal(self))
            return
        except Exception:
            logger.exception("[utility/vps] falha ao abrir modal avançado; tentando fallback TextInput")

        if interaction.response.is_done():
            return
        try:
            await interaction.response.send_modal(VpsModal(self, force_text_fallback=True))
        except Exception:
            logger.exception("[utility/vps] fallback TextInput também falhou")
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "`⚠️` Não consegui abrir o painel da VPS. Tente novamente em alguns segundos.",
                    ephemeral=True,
                )

    @app_commands.command(name="vps", description="Abre o painel de diagnóstico/anexos da VPS")
    @app_commands.guilds(VPS_COMMAND_GUILD)
    async def vps(self, interaction: discord.Interaction):
        if interaction.guild is None or int(getattr(interaction.guild, "id", 0) or 0) != VPS_COMMAND_GUILD_ID:
            await interaction.response.send_message("Esse painel só funciona na guilda de teste configurada.", ephemeral=True)
            return
        # Não faça await pesado antes de send_modal: se o owner check/rede travar,
        # o Discord mostra “O aplicativo não respondeu” e o modal nem abre.
        # A validação de dono continua no submit, logo após o defer correto.
        await self._send_vps_modal(interaction)
