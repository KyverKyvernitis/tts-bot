from __future__ import annotations

import asyncio
import contextlib
import io
import logging
import re
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
    diagnostics_file_stamp,
)

logger = logging.getLogger(__name__)

VPS_COMMAND_GUILD_ID = 927002914449424404
VPS_COMMAND_GUILD = discord.Object(id=VPS_COMMAND_GUILD_ID)

VpsItem = Literal["quick_status", "base_git", "music_diag", "full_diag", "snapshot", "servers"]

VPS_QUICK_STATUS_TIMEOUT_SECONDS = 22.0
VPS_BASE_TIMEOUT_SECONDS = 70.0
VPS_MUSIC_DIAG_TIMEOUT_SECONDS = 115.0
VPS_FULL_DIAG_TIMEOUT_SECONDS = 150.0
VPS_SNAPSHOT_TIMEOUT_SECONDS = 75.0
VPS_SERVERS_TIMEOUT_SECONDS = 25.0


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

    Usa select multi-escolha em Components V2 para comportar mais de 5 opções.
    Se o runtime do Discord recusar o componente, cai para TextInput para não
    quebrar o comando.
    """

    def __init__(self, cog: "VpsCommandMixin", *, force_text_fallback: bool = False):
        super().__init__(timeout=180)
        self.cog = cog
        self._ui_mode = "fallback_text"

        label_cls = None if force_text_fallback else getattr(discord.ui, "Label", None)
        select_cls = None if force_text_fallback else (getattr(discord.ui, "StringSelect", None) or getattr(discord.ui, "Select", None))

        # Com mais de 5 opções, checkboxes individuais estouram o limite prático
        # de componentes do modal. Um select multi-escolha mantém o modal leve e
        # deixa espaço para futuras opções sem voltar para escolha única.
        if select_cls is not None and label_cls is not None:
            try:
                self.items_select = select_cls(
                    custom_id="vps_items",
                    placeholder="Escolha uma ou mais opções",
                    min_values=1,
                    max_values=6,
                    options=[
                        discord.SelectOption(
                            label="Base Git leve",
                            description="Código rastreado pelo Git, sem assets e sem manifestos.",
                            value="base_git",
                            emoji="📦",
                            default=True,
                        ),
                        discord.SelectOption(
                            label="Diagnóstico musical",
                            description="Lavalink, LavaSrc, yt-dlp, players, filas e logs musicais.",
                            value="music_diag",
                            emoji="🎵",
                        ),
                        discord.SelectOption(
                            label="Diagnóstico completo",
                            description="Logs gerais, serviços, memória, disco e resumo da VPS.",
                            value="full_diag",
                            emoji="🧾",
                        ),
                        discord.SelectOption(
                            label="Snapshot da VPS",
                            description="Configs sanitizadas, services, DB musicnode e logs filtradas.",
                            value="snapshot",
                            emoji="🧰",
                        ),
                        discord.SelectOption(
                            label="Status rápido",
                            description="RAM, disco, serviços, Git e 3 erros recentes.",
                            value="quick_status",
                            emoji="⚡",
                        ),
                        discord.SelectOption(
                            label="Servidores",
                            description="Guilds, membros e TTS sintetizados por engine.",
                            value="servers",
                            emoji="🌐",
                        ),
                    ],
                )
                self.add_item(label_cls(
                    text="O que enviar?",
                    description="Marque uma ou mais opções no seletor.",
                    component=self.items_select,
                ))
                self._ui_mode = "select"
                return
            except Exception:
                logger.exception("[utility/vps] falha ao montar modal com select multi-escolha; usando fallback")

        self.items_input = discord.ui.TextInput(
            label="O que enviar?",
            placeholder="base, musica, completo, snapshot, status, servidores",
            required=True,
            default="base",
            max_length=160,
        )
        self.add_item(self.items_input)

    def _selected_items(self) -> list[VpsItem]:
        if self._ui_mode == "select":
            values = []
            with contextlib.suppress(Exception):
                values = list(getattr(self.items_select, "values") or [])
            selected: list[VpsItem] = []
            valid: set[str] = {"quick_status", "base_git", "music_diag", "full_diag", "snapshot", "servers"}
            for value in values:
                value = str(value or "").strip()
                if value in valid and value not in selected:
                    selected.append(value)  # type: ignore[arg-type]
            return selected

        if self._ui_mode == "checkboxes":
            selected: list[VpsItem] = []
            mapping: list[tuple[str, VpsItem]] = [
                ("quick_status", "quick_status"),
                ("base_git", "base_git"),
                ("music_diag", "music_diag"),
                ("full_diag", "full_diag"),
                ("snapshot", "snapshot"),
                ("servers", "servers"),
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
            "servidores": "servers",
            "servers": "servers",
            "guilds": "servers",
            "guildas": "servers",
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



class VpsResultView(discord.ui.LayoutView):
    """Resposta final do /vps em Components V2."""

    def __init__(self, *, status_report: str | None, servers_report: str | None, attachment_lines: list[str], error_lines: list[str]):
        super().__init__(timeout=None)

        # Não usa card de título separado: ele ocupava espaço no mobile e não
        # trazia informação útil. O primeiro card já identifica a resposta.
        if status_report:
            self.add_item(
                discord.ui.Container(
                    discord.ui.TextDisplay(status_report.strip()),
                    accent_color=discord.Color.green(),
                )
            )

        if servers_report:
            self.add_item(
                discord.ui.Container(
                    discord.ui.TextDisplay(servers_report.strip()),
                    accent_color=discord.Color.blurple(),
                )
            )

        if attachment_lines:
            self.add_item(
                discord.ui.Container(
                    discord.ui.TextDisplay("## 📎 Anexos\n" + "\n".join(attachment_lines)),
                    accent_color=discord.Color.dark_teal(),
                )
            )

        if error_lines:
            self.add_item(
                discord.ui.Container(
                    discord.ui.TextDisplay("## ⚠️ Avisos\n" + "\n".join(error_lines)),
                    accent_color=discord.Color.orange(),
                )
            )

        if not status_report and not servers_report and not attachment_lines and not error_lines:
            self.add_item(
                discord.ui.Container(
                    discord.ui.TextDisplay("⚠️ Nenhum arquivo ou status foi gerado."),
                    accent_color=discord.Color.orange(),
                )
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

    @staticmethod
    def _format_vps_int(value: int | None) -> str:
        try:
            number = max(0, int(value or 0))
        except Exception:
            number = 0
        return f"{number:,}".replace(",", ".")

    @staticmethod
    def _shorten_vps_name(value: object, *, limit: int = 42) -> str:
        text = str(value or "Servidor sem nome").replace("\n", " ").strip() or "Servidor sem nome"
        if len(text) > limit:
            text = text[: max(1, limit - 1)].rstrip() + "…"
        with contextlib.suppress(Exception):
            text = discord.utils.escape_markdown(text)
        return text

    async def _collect_tts_synt_stats(self) -> dict[int, dict[str, int]]:
        db = getattr(self.bot, "settings_db", None)
        getter = getattr(db, "get_all_tts_synt_stats", None)
        if not callable(getter):
            return {}
        try:
            raw = getter()
            if asyncio.iscoroutine(raw):
                raw = await raw
        except Exception:
            logger.exception("[utility/vps] falha ao ler estatísticas persistentes de synts")
            return {}

        result: dict[int, dict[str, int]] = {}
        for guild_id, stats in dict(raw or {}).items():
            try:
                gid = int(guild_id)
            except Exception:
                continue
            if not isinstance(stats, dict):
                continue
            normalized: dict[str, int] = {}
            for engine in ("edge", "google", "gtts"):
                try:
                    normalized[engine] = max(0, int(stats.get(engine, 0) or 0))
                except Exception:
                    normalized[engine] = 0
            result[gid] = normalized
        return result

    async def _build_vps_servers_report(self) -> str:
        synt_stats = await self._collect_tts_synt_stats()
        guilds = list(getattr(self.bot, "guilds", []) or [])
        guilds.sort(key=lambda guild: (-(int(getattr(guild, "member_count", 0) or 0)), str(getattr(guild, "name", "")).casefold()))

        total_members = 0
        total_synts = 0
        rows: list[str] = []
        for index, guild in enumerate(guilds[:18], start=1):
            guild_id = int(getattr(guild, "id", 0) or 0)
            member_count = int(getattr(guild, "member_count", 0) or 0)
            if member_count <= 0:
                with contextlib.suppress(Exception):
                    member_count = len(getattr(guild, "members", []) or [])
            total_members += member_count

            stats = synt_stats.get(guild_id, {})
            edge = int(stats.get("edge", 0) or 0)
            google = int(stats.get("google", 0) or 0)
            gtts = int(stats.get("gtts", 0) or 0)
            guild_synts = edge + google + gtts
            total_synts += guild_synts

            engine_parts: list[str] = []
            if edge:
                engine_parts.append(f"Edge: {self._format_vps_int(edge)}")
            if google:
                engine_parts.append(f"Google: {self._format_vps_int(google)}")
            if gtts:
                engine_parts.append(f"gTTS: {self._format_vps_int(gtts)}")

            synt_line = f"Synts: {self._format_vps_int(guild_synts)}"
            if engine_parts:
                synt_line += " · " + " · ".join(engine_parts)

            rows.append(
                f"**{index}. {self._shorten_vps_name(getattr(guild, 'name', None))}**\n"
                f"Membros: {self._format_vps_int(member_count)} · {synt_line}"
            )

        # Soma membros/synts de guilds que não couberam no painel também.
        for guild in guilds[18:]:
            guild_id = int(getattr(guild, "id", 0) or 0)
            member_count = int(getattr(guild, "member_count", 0) or 0)
            if member_count <= 0:
                with contextlib.suppress(Exception):
                    member_count = len(getattr(guild, "members", []) or [])
            total_members += member_count
            stats = synt_stats.get(guild_id, {})
            total_synts += int(stats.get("edge", 0) or 0) + int(stats.get("google", 0) or 0) + int(stats.get("gtts", 0) or 0)

        lines = [
            "## 🌐 Servidores",
            f"Total: {self._format_vps_int(len(guilds))} servidor(es)",
            f"Membros somados: {self._format_vps_int(total_members)}",
            f"Synts totais: {self._format_vps_int(total_synts)}",
        ]
        if rows:
            lines.extend(["", *rows])
        else:
            lines.append("Nenhum servidor encontrado no cache do bot.")
        if len(guilds) > 18:
            lines.append(f"… +{self._format_vps_int(len(guilds) - 18)} servidor(es) oculto(s) para manter o painel compacto.")

        report = "\n".join(lines).strip()
        if len(report) > 3500:
            report = report[:3500].rstrip() + "\n[cortado por tamanho]"
        return report

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
            item for item in ["quick_status", "servers", "base_git", "music_diag", "full_diag", "snapshot"] if item in selected_items
        ]

        stamp = diagnostics_file_stamp()
        files: list[discord.File] = []
        attachment_lines: list[str] = []
        error_lines: list[str] = []
        status_report: str | None = None
        servers_report: str | None = None
        generated_any = False

        for item in ordered_items:
            if item == "quick_status":
                try:
                    report = await self._with_vps_timeout("status rápido", build_quick_vps_status_report(), timeout=VPS_QUICK_STATUS_TIMEOUT_SECONDS)
                    report = (report or "Status rápido vazio.").strip()
                    if len(report) > 3400:
                        files.append(discord.File(io.BytesIO(report.encode("utf-8", "replace")), filename=f"status-{stamp}.txt"))
                        attachment_lines.append("⚡ Status rápido anexado.")
                    else:
                        status_report = report
                    generated_any = True
                except Exception as exc:
                    logger.exception("[utility/vps] falha ao gerar status rápido")
                    error_lines.append(f"Status rápido falhou: {type(exc).__name__}: {str(exc)[:300]}")
                continue

            if item == "servers":
                try:
                    servers_report = await self._with_vps_timeout("servidores", self._build_vps_servers_report(), timeout=VPS_SERVERS_TIMEOUT_SECONDS)
                    generated_any = True
                except Exception as exc:
                    logger.exception("[utility/vps] falha ao gerar lista de servidores")
                    error_lines.append(f"Servidores falhou: {type(exc).__name__}: {str(exc)[:300]}")
                continue

            if item == "base_git":
                try:
                    payload, filename, summary, _manifest = await self._with_vps_timeout("base Git", build_git_tracked_base_archive(), timeout=VPS_BASE_TIMEOUT_SECONDS)
                    if payload and filename:
                        files.append(discord.File(io.BytesIO(payload), filename=filename))
                        attachment_lines.append(f"📦 Repositório anexado ({_format_attachment_size(len(payload))}).")
                        generated_any = True
                    else:
                        error_lines.append(summary or "Não consegui gerar a base Git.")
                except Exception as exc:
                    logger.exception("[utility/vps] falha ao gerar base git")
                    error_lines.append(f"Base Git falhou: {type(exc).__name__}: {str(exc)[:300]}")
                continue

            if item == "music_diag":
                router = _get_audio_router(self.bot)
                try:
                    payload, filename, summary, fallback_report = await self._with_vps_timeout("diagnóstico musical", build_music_diagnostics_archive(router, await self._vps_context_options(interaction)), timeout=VPS_MUSIC_DIAG_TIMEOUT_SECONDS)
                    if payload and filename:
                        files.append(discord.File(io.BytesIO(payload), filename=filename))
                        attachment_lines.append(f"🎵 Diagnóstico musical anexado ({_format_attachment_size(len(payload))}).")
                        generated_any = True
                        # O diagnóstico musical modular deve ser um único anexo.
                        # O resumo completo fica dentro do zip como 00-resumo-curto.txt/summary.txt.
                    else:
                        error_lines.append(f"Diagnóstico modular não foi anexado: {summary or 'falha sem detalhes'}")
                        report = fallback_report or await self._with_vps_timeout("diagnóstico musical texto", build_music_diagnostics_report(router, await self._vps_context_options(interaction)), timeout=VPS_MUSIC_DIAG_TIMEOUT_SECONDS)
                        report_bytes = report.encode("utf-8", "replace")
                        files.append(discord.File(io.BytesIO(report_bytes), filename=f"music-diag-{stamp}.txt"))
                        attachment_lines.append(f"🎵 Diagnóstico musical anexado ({_format_attachment_size(len(report_bytes))}).")
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
                    attachment_lines.append(f"⚠️ Diagnóstico musical emergencial anexado ({_format_attachment_size(len(report_bytes))}).")
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
                attachment_lines.append(f"🧾 Diagnóstico completo anexado ({_format_attachment_size(len(report_bytes))}).")
                generated_any = True
                continue

            if item == "snapshot":
                try:
                    payload, filename, summary = await self._with_vps_timeout("snapshot da VPS", build_vps_snapshot_archive(), timeout=VPS_SNAPSHOT_TIMEOUT_SECONDS)
                    if payload and filename:
                        files.append(discord.File(io.BytesIO(payload), filename=filename))
                        attachment_lines.append(f"🧰 Snapshot da VPS anexado ({_format_attachment_size(len(payload))}).")
                        generated_any = True
                    else:
                        error_lines.append(f"Snapshot da VPS não foi anexado: {summary or 'falha sem detalhes'}")
                except Exception as exc:
                    logger.exception("[utility/vps] falha ao gerar snapshot da VPS")
                    error_lines.append(f"Snapshot da VPS falhou: {type(exc).__name__}: {str(exc)[:300]}")
                continue

        if not generated_any:
            error_lines.append("Nenhum arquivo ou status foi gerado.")

        try:
            view = VpsResultView(status_report=status_report, servers_report=servers_report, attachment_lines=attachment_lines, error_lines=error_lines)
            # Components V2 e anexos no mesmo followup podem não renderizar os
            # arquivos em alguns runtimes/clientes. Envia o painel bonito em uma
            # mensagem e os arquivos em outra para garantir que os anexos apareçam.
            await interaction.followup.send(view=view)
            if files:
                try:
                    await interaction.followup.send(files=files[:10])
                except Exception as file_exc:
                    logger.exception("[utility/vps] falha ao enviar anexos do painel da VPS")
                    await interaction.followup.send(
                        f"`⚠️` O painel foi gerado, mas os anexos falharam: {type(file_exc).__name__}: {str(file_exc)[:300]}"
                    )
        except Exception as exc:
            logger.exception("[utility/vps] falha ao enviar resposta final em Components V2; usando fallback texto")
            fallback_lines: list[str] = []
            if status_report:
                fallback_lines.append(status_report.strip())
            if servers_report:
                fallback_lines.append(servers_report.strip())
            fallback_lines.extend(attachment_lines)
            if error_lines:
                fallback_lines.append("`⚠️` Avisos:")
                fallback_lines.extend(error_lines)
            fallback_lines.append(f"`⚠️` Falhei ao enviar Components V2: {type(exc).__name__}: {str(exc)[:300]}")
            fallback = "\n".join(line for line in fallback_lines if line).strip()
            if len(fallback) > 1900:
                files.append(discord.File(io.BytesIO(fallback.encode("utf-8", "replace")), filename=f"vps-resumo-{stamp}.txt"))
                fallback = "`ℹ️` O resumo ficou grande e foi anexado em .txt."
            with contextlib.suppress(Exception):
                await interaction.followup.send(fallback[:1900] or "`⚠️` Nenhum resultado gerado.", files=files[:10])

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
