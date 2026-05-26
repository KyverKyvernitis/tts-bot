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
from utility.interaction_safety import (
    is_unknown_interaction,
    safe_defer_interaction,
    safe_send_interaction_message,
)
from music_system.diagnostics import (
    DiagnosticsOptions,
    build_full_vps_diagnostics_report,
    build_git_tracked_base_archive,
    build_music_diagnostics_report,
    build_quick_vps_status_report,
    build_music_diagnostics_archive,
    build_music_diagnostics_emergency_report,
    build_vps_snapshot_archive,
    build_core_worker_apk_diagnostics_report,
    diagnostics_file_stamp,
)

logger = logging.getLogger(__name__)

VPS_COMMAND_GUILD_ID = 927002914449424404
VPS_COMMAND_GUILD = discord.Object(id=VPS_COMMAND_GUILD_ID)

VpsItem = Literal["quick_status", "base_git", "music_diag", "full_diag", "snapshot", "servers", "tts", "apk_diag"]

VPS_QUICK_STATUS_TIMEOUT_SECONDS = 22.0
VPS_BASE_TIMEOUT_SECONDS = 70.0
VPS_MUSIC_DIAG_TIMEOUT_SECONDS = 115.0
VPS_FULL_DIAG_TIMEOUT_SECONDS = 150.0
VPS_SNAPSHOT_TIMEOUT_SECONDS = 75.0
VPS_SERVERS_TIMEOUT_SECONDS = 25.0
VPS_TTS_TIMEOUT_SECONDS = 18.0
VPS_APK_DIAG_TIMEOUT_SECONDS = 25.0


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
                    max_values=8,
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
                            description="RAM, disco, serviços, Git, APK e 3 erros recentes.",
                            value="quick_status",
                            emoji="⚡",
                        ),
                        discord.SelectOption(
                            label="Diagnóstico APK",
                            description="Bateria, rede/VPN, push, runtime, cache e jobs internos.",
                            value="apk_diag",
                            emoji="📲",
                        ),
                        discord.SelectOption(
                            label="Servidores",
                            description="Guilds, membros e TTS sintetizados por engine.",
                            value="servers",
                            emoji="🌐",
                        ),
                        discord.SelectOption(
                            label="TTS",
                            description="Engines, fila, cache e synts desde o último restart.",
                            value="tts",
                            emoji="🔊",
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
            except Exception as exc:
                logger.exception("[utility/vps] falha ao montar modal com select multi-escolha")
                if not force_text_fallback:
                    raise RuntimeError("modal avançado da VPS indisponível") from exc

        if not force_text_fallback:
            raise RuntimeError("discord.py sem suporte a Label/StringSelect em modal para /vps")

        self.items_input = discord.ui.TextInput(
            label="O que enviar?",
            placeholder="base, apk, musica, completo, snapshot, status, servidores, tts",
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
            valid: set[str] = {"quick_status", "base_git", "music_diag", "full_diag", "snapshot", "servers", "tts", "apk_diag"}
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
                ("apk_diag", "apk_diag"),
                ("servers", "servers"),
                ("tts", "tts"),
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
            "apk": "apk_diag",
            "apk_diag": "apk_diag",
            "diagnóstico apk": "apk_diag",
            "diagnostico apk": "apk_diag",
            "core worker apk": "apk_diag",
            "core-worker apk": "apk_diag",
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
            "tts": "tts",
            "voz": "tts",
            "voice": "tts",
            "engines": "tts",
            "engine": "tts",
            "fila": "tts",
            "cache": "tts",
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
            await safe_send_interaction_message(
                interaction,
                message,
                log=logger,
                label="utility/vps.on_submit",
            )



class VpsResultView(discord.ui.LayoutView):
    """Resposta final do /vps em Components V2."""

    def __init__(self, *, status_report: str | None, servers_report: str | None, tts_report: str | None, attachment_lines: list[str], error_lines: list[str]):
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

        if tts_report:
            self.add_item(
                discord.ui.Container(
                    discord.ui.TextDisplay(tts_report.strip()),
                    accent_color=discord.Color.dark_teal(),
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

        if not status_report and not servers_report and not tts_report and not attachment_lines and not error_lines:
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
        return await safe_defer_interaction(
            interaction,
            thinking=True,
            ephemeral=False,
            log=logger,
            label="utility/vps",
        )

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


    @staticmethod
    def _vps_engine_label(engine: object) -> str:
        key = str(engine or "gtts").strip().lower().replace("-", "_").replace(" ", "_")
        if key in {"edge", "edge_tts", "microsoft_edge"}:
            return "Edge"
        if key in {"google", "gcloud", "google_cloud", "googlecloud"}:
            return "Google"
        if key in {"gtts", "google_translate", "google_translate_tts"}:
            return "gTTS"
        if key in {"piper", "piper_tts"}:
            return "Piper"
        if key.startswith("tts_agent:"):
            requested = key.split(":", 1)[1] or "auto"
            return f"TTS Agent → {VpsCommandMixin._vps_engine_label(requested)}"
        return str(engine or "TTS").strip() or "TTS"

    @staticmethod
    def _vps_engine_key(engine: object) -> str:
        label = VpsCommandMixin._vps_engine_label(engine)
        return {"Edge": "edge", "Google": "google", "gTTS": "gtts"}.get(label, label.casefold())

    def _vps_format_ms(self, value: Any) -> str:
        formatter = getattr(self, "_format_ms", None)
        if callable(formatter):
            with contextlib.suppress(Exception):
                return str(formatter(value))
        try:
            number = float(value or 0)
        except Exception:
            return "n/a"
        if number <= 0:
            return "0 ms"
        return f"{number:.0f} ms" if number >= 10 else f"{number:.2f} ms"

    def _vps_format_bytes_human(self, value: Any) -> str:
        formatter = getattr(self, "_format_bytes_human", None)
        if callable(formatter):
            with contextlib.suppress(Exception):
                return str(formatter(value))
        try:
            size = float(value or 0)
        except Exception:
            return "0 B"
        units = ["B", "KB", "MB", "GB", "TB"]
        idx = 0
        while size >= 1024.0 and idx < len(units) - 1:
            size /= 1024.0
            idx += 1
        return f"{int(size)} {units[idx]}" if idx == 0 else f"{size:.2f} {units[idx]}"

    def _collect_vps_tts_snapshot(self) -> dict[str, Any]:
        collector = getattr(self, "_collect_health_snapshot", None)
        if callable(collector):
            with contextlib.suppress(Exception):
                return dict(collector() or {})

        snapshot: dict[str, Any] = {}
        get_snapshot = getattr(getattr(self, "bot", None), "get_health_snapshot", None)
        if callable(get_snapshot):
            with contextlib.suppress(Exception):
                snapshot = dict(get_snapshot() or {})

        tmp_root = "tmp_audio"
        runtime_dir = f"{tmp_root}/runtime"
        cache_dir = f"{tmp_root}/cache"
        credentials_dir = f"{tmp_root}/credentials"

        def _dir_stats(path: str) -> tuple[int, int]:
            total_bytes = 0
            total_files = 0
            with contextlib.suppress(Exception):
                import os
                for entry in os.scandir(path):
                    if not entry.is_file():
                        continue
                    total_files += 1
                    with contextlib.suppress(Exception):
                        total_bytes += int(entry.stat().st_size)
            return total_files, total_bytes

        runtime_files, runtime_bytes = _dir_stats(runtime_dir)
        cache_files, cache_bytes = _dir_stats(cache_dir)
        cred_files, cred_bytes = _dir_stats(credentials_dir)
        tts_metrics = dict(snapshot.get("tts_metrics") or {})
        snapshot.update({
            "tts_metrics": tts_metrics,
            "engine_metrics": dict(tts_metrics.get("engines") or {}),
            "runtime_files": runtime_files,
            "cache_files": cache_files,
            "cred_files": cred_files,
            "total_tmp_bytes": runtime_bytes + cache_bytes + cred_bytes,
        })
        return snapshot

    async def _build_vps_tts_report(self) -> str:
        snapshot = self._collect_vps_tts_snapshot()
        tts_metrics = dict(snapshot.get("tts_metrics") or {})
        tts_agent = dict(tts_metrics.get("tts_agent") or {})
        voice_agent = dict(tts_agent.get("voice_agent") or tts_metrics.get("worker_voice_agent") or {})
        raw_engine_metrics = dict(snapshot.get("engine_metrics") or tts_metrics.get("engines") or {})

        def _engine_label(value: Any) -> str:
            text = str(value or "").strip()
            if not text:
                return "—"
            return self._vps_engine_label(text)

        def _split_agent_engine(raw_engine: object) -> tuple[bool, str]:
            key = str(raw_engine or "").strip().lower().replace("-", "_").replace(" ", "_")
            if key.startswith("tts_agent:"):
                return True, key.split(":", 1)[1] or "auto"
            if key in {"tts_agent", "worker_tts_agent"}:
                return True, "auto"
            return False, str(raw_engine or "gtts").strip()

        def _combine_engine_lines(entries: list[tuple[str, dict[str, Any]]], *, worker: bool) -> tuple[list[str], int]:
            combined: dict[str, dict[str, Any]] = {}
            for raw_engine, raw_data in entries:
                key = self._vps_engine_key(raw_engine)
                label = _engine_label(raw_engine)
                if worker:
                    label = f"Pedido {_engine_label(raw_engine)}"
                data = dict(raw_data or {})
                target = combined.setdefault(key, {
                    "label": label,
                    "synth_count": 0,
                    "synth_failures": 0,
                    "consecutive_failures": 0,
                    "synth_total_ms": 0.0,
                    "samples": 0,
                    "avg_synth_ms": 0.0,
                })
                synth_count = max(0, int(data.get("synth_count", 0) or 0))
                avg_ms = float(data.get("avg_synth_ms", 0.0) or 0.0)
                target["synth_count"] = int(target.get("synth_count", 0) or 0) + synth_count
                target["synth_failures"] = int(target.get("synth_failures", 0) or 0) + max(0, int(data.get("synth_failures", 0) or 0))
                target["consecutive_failures"] = max(
                    int(target.get("consecutive_failures", 0) or 0),
                    max(0, int(data.get("consecutive_failures", 0) or 0)),
                )
                if synth_count > 0 and avg_ms > 0:
                    target["synth_total_ms"] = float(target.get("synth_total_ms", 0.0) or 0.0) + (avg_ms * synth_count)
                    target["samples"] = int(target.get("samples", 0) or 0) + synth_count
                elif avg_ms > 0:
                    target["avg_synth_ms"] = max(float(target.get("avg_synth_ms", 0.0) or 0.0), avg_ms)

            engine_order = {"edge": 0, "google": 1, "gtts": 2, "piper": 3, "auto": 4}
            lines: list[str] = []
            total = 0
            for key, data in sorted(combined.items(), key=lambda item: (engine_order.get(item[0], 99), item[0])):
                synth_count = int(data.get("synth_count", 0) or 0)
                failures = int(data.get("synth_failures", 0) or 0)
                consecutive = int(data.get("consecutive_failures", 0) or 0)
                total += synth_count
                samples = int(data.get("samples", 0) or 0)
                if samples > 0:
                    avg_value = float(data.get("synth_total_ms", 0.0) or 0.0) / samples
                else:
                    avg_value = float(data.get("avg_synth_ms", 0.0) or 0.0)
                dot = "🟢" if failures == 0 and consecutive == 0 else ("🟡" if consecutive == 0 else "🔴")
                lines.append(
                    f"{dot} **{data.get('label', key)}** · synts {self._format_vps_int(synth_count)}"
                    f" · falhas {self._format_vps_int(failures)}"
                    f" · seguidas {self._format_vps_int(consecutive)}"
                    f" · média {self._vps_format_ms(avg_value)}"
                )
            return lines, total

        local_entries: list[tuple[str, dict[str, Any]]] = []
        worker_entries: list[tuple[str, dict[str, Any]]] = []
        for raw_engine, raw_data in raw_engine_metrics.items():
            is_worker, clean_engine = _split_agent_engine(raw_engine)
            if is_worker:
                worker_entries.append((clean_engine, dict(raw_data or {})))
            else:
                local_entries.append((str(raw_engine), dict(raw_data or {})))

        local_engine_lines, local_total_synts = _combine_engine_lines(local_entries, worker=False)
        worker_engine_lines, worker_total_synts = _combine_engine_lines(worker_entries, worker=True)

        if not local_engine_lines:
            local_engine_lines.append("Ainda não há sínteses da VPS/fallback local desde o último restart.")
        if not worker_engine_lines:
            worker_engine_lines.append("Ainda não há sínteses pelo TTS Agent desde o último restart.")

        cache_hits = max(0, int(tts_metrics.get("cache_hits", 0) or 0))
        cache_misses = max(0, int(tts_metrics.get("cache_misses", 0) or 0))
        cache_stores = max(0, int(tts_metrics.get("cache_stores", 0) or 0))
        total_cache_lookups = cache_hits + cache_misses
        cache_hit_rate = (cache_hits / total_cache_lookups * 100.0) if total_cache_lookups else 0.0

        worker_cache_hits = max(0, int(tts_metrics.get("worker_cache_lookup_hits", 0) or 0))
        worker_cache_misses = max(0, int(tts_metrics.get("worker_cache_lookup_misses", 0) or 0))
        worker_cache_skipped = max(0, int(tts_metrics.get("worker_cache_lookup_skipped", 0) or 0))
        worker_cache_errors = max(0, int(tts_metrics.get("worker_cache_lookup_errors", 0) or 0))

        route = str(tts_agent.get("route") or "vps").strip().lower()
        agent_enabled = bool(tts_agent.get("enabled"))
        agent_ok = bool(tts_agent.get("ok"))
        cooldown = float(tts_agent.get("cooldown_remaining_seconds") or 0.0)
        route_label = "Worker" if route == "worker" and agent_ok else "VPS"
        route_dot = "🟢" if route == "worker" and agent_ok else ("🟡" if agent_enabled and cooldown > 0 else "🔵")
        worker_id = str(tts_agent.get("worker_id") or "nenhum").strip() or "nenhum"
        worker_version = str(tts_agent.get("worker_version") or "").strip()
        worker_suffix = f" · versão {worker_version}" if worker_version else ""
        available_engines = ", ".join(_engine_label(value) for value in list(tts_agent.get("available_engines") or [])[:6]) or "nenhuma"
        last_requested = str(tts_agent.get("last_requested_engine") or tts_metrics.get("tts_agent_last_requested_engine") or "").strip()
        last_selected = str(tts_agent.get("last_selected_engine") or tts_metrics.get("tts_agent_last_selected_engine") or "").strip()
        last_format = str(tts_agent.get("last_audio_format") or tts_metrics.get("tts_agent_last_audio_format") or "").strip()
        last_bytes = int(tts_agent.get("last_audio_bytes") or tts_metrics.get("tts_agent_last_audio_bytes") or 0)
        last_cache_hit = bool(tts_agent.get("last_cache_hit") or tts_metrics.get("tts_agent_last_cache_hit"))
        last_synth_ms = float(tts_agent.get("last_synth_ms") or tts_metrics.get("tts_agent_last_synth_ms") or tts_metrics.get("avg_tts_agent_synth_ms") or 0.0)
        raw_voice_state = str(voice_agent.get("state") or "sem health").strip() or "sem health"
        voice_state_labels = {
            "voice_handoff_received_waiting_transfer": "handoff recebido · aguardando posse da voz",
            "voice_handoff_registered_dry_run": "handoff recebido · conexão direta não iniciada",
            "shared_voice_session_registered": "sessão compartilhada registrada",
            "voice_connection_dry_run_ready": "conexão dry-run pronta",
            "not_ready": "preparando",
        }
        voice_state = voice_state_labels.get(raw_voice_state, raw_voice_state)
        voice_dot = "🟢" if voice_agent.get("direct_tts_ready") else ("🟡" if voice_agent.get("shared_session_ready") or voice_agent.get("ok") or voice_agent.get("available") else "🔵")
        voice_missing = ", ".join(str(item) for item in list(voice_agent.get("missing") or [])[:3]) or "nenhum"
        voice_tts_direct = "pronto" if voice_agent.get("direct_tts_ready") else ("preparação" if voice_agent.get("available") or voice_agent.get("ok") else "não pronto")
        voice_music_ready = "sim" if voice_agent.get("music_ready") else "não"
        voice_tts_ready = "sim" if voice_agent.get("tts_ready") else "não"
        voice_session_count = int(voice_agent.get("session_count") or 0)
        voice_handoff_count = int(voice_agent.get("handoff_count") or 0)
        voice_connection_count = int(voice_agent.get("connection_count") or 0)
        voice_shared_ready = "sim" if voice_agent.get("shared_session_ready") else "não"
        voice_handoff_ready = "sim" if voice_agent.get("handoff_ready") else "não"
        voice_connection_ready = "sim" if voice_agent.get("connection_ready") else "não"
        last_voice_session = voice_agent.get("last_session") if isinstance(voice_agent.get("last_session"), dict) else {}
        last_handoff = voice_agent.get("last_handoff") if isinstance(voice_agent.get("last_handoff"), dict) else {}
        last_connection = voice_agent.get("last_connection") if isinstance(voice_agent.get("last_connection"), dict) else {}
        if last_voice_session:
            voice_session_line = (
                f"Sessão lógica: `{self._format_vps_int(voice_session_count)}` · compartilhada `{voice_shared_ready}` · "
                f"guild `{str(last_voice_session.get('guild_id') or '?')}` canal `{str(last_voice_session.get('channel_id') or '?')}` "
                f"· TTL `{str(last_voice_session.get('ttl_seconds') or 0)}s`"
            )
        else:
            voice_session_line = f"Sessão lógica: `{self._format_vps_int(voice_session_count)}` · compartilhada `{voice_shared_ready}` · aguardando primeira sessão da VPS"
        if last_handoff:
            handoff_owner = str(last_handoff.get('voice_owner') or last_handoff.get('transport_owner') or 'vps')[:30]
            voice_handoff_line = (
                f"Handoff voz: `{voice_handoff_ready}` · dono atual `{handoff_owner}` · registros `{self._format_vps_int(voice_handoff_count)}` · "
                f"session `{ 'sim' if last_handoff.get('session_id_present') else 'não' }` · endpoint `{ 'sim' if last_handoff.get('endpoint_present') else 'não' }` · "
                f"token temp `{ 'sim' if last_handoff.get('voice_token_present') else 'não' }` · TTL `{str(last_handoff.get('ttl_seconds') or 0)}s`"
            )
        else:
            voice_handoff_line = f"Handoff voz: `{voice_handoff_ready}` · registros `{self._format_vps_int(voice_handoff_count)}` · aguardando session_id/endpoint/token temporário"
        if last_connection:
            conn_state = str(last_connection.get('state') or '—')[:60]
            conn_stage = str(last_connection.get('stage') or '—')[:60]
            conn_latency = last_connection.get('latency_ms')
            conn_error = str(last_connection.get('error') or '')[:100]
            voice_connection_line = (
                f"Conexão direta: `{voice_connection_ready}` · registros `{self._format_vps_int(voice_connection_count)}` · "
                f"estado `{conn_state}` · etapa `{conn_stage}` · WS ready `{ 'sim' if last_connection.get('ready_received') else 'não' }` · "
                f"UDP `{ 'sim' if last_connection.get('udp_probe_ok') else 'não' }` · {self._vps_format_ms(conn_latency or 0)}"
            )
            if conn_error:
                voice_connection_line += f" · erro `{conn_error}`"
        else:
            voice_connection_line = f"Conexão direta: `{voice_connection_ready}` · registros `{self._format_vps_int(voice_connection_count)}` · aguardando transferência de posse da voz"
        voice_report_ok = int(tts_metrics.get("worker_voice_session_reports_ok", 0) or 0)
        voice_report_failed = int(tts_metrics.get("worker_voice_session_reports_failed", 0) or 0)
        voice_report_skipped = int(tts_metrics.get("worker_voice_session_skipped", 0) or 0)
        voice_handoff_ok = int(tts_metrics.get("worker_voice_session_handoff_ok", 0) or 0)
        voice_handoff_failed = int(tts_metrics.get("worker_voice_session_handoff_failed", 0) or 0)
        voice_handoff_skipped = int(tts_metrics.get("worker_voice_session_handoff_skipped", 0) or 0)
        voice_connection_probe_ok = int(tts_metrics.get("worker_voice_session_connection_probe_ok", 0) or 0)
        voice_connection_probe_failed = int(tts_metrics.get("worker_voice_session_connection_probe_failed", 0) or 0)
        voice_connection_probe_skipped = int(tts_metrics.get("worker_voice_session_connection_probe_skipped", 0) or 0)

        if last_requested or last_selected:
            last_worker_line = (
                f"Última síntese worker: {_engine_label(last_requested)} pedido → {_engine_label(last_selected)} usado"
                f" · {last_format or 'formato ?'} · {self._vps_format_bytes_human(last_bytes)}"
                f" · cache {'hit' if last_cache_hit else 'miss'} · {self._vps_format_ms(last_synth_ms)}"
            )
        else:
            last_worker_line = "Última síntese worker: ainda sem registro."

        lines = [
            "## 🔊 TTS",
            "-# Métricas desde o último restart do bot. Agora separa rota worker e fallback/VPS.",
            "",
            "### 🧭 Rota atual",
            f"{route_dot} **Modo em uso:** {route_label}",
            f"Worker selecionado: `{worker_id}`{worker_suffix}",
            f"Motivo/estado: `{str(tts_agent.get('reason') or '—')[:80]}` · cooldown: {self._vps_format_ms(cooldown * 1000.0)}",
            f"Último erro do Agent: `{str(tts_agent.get('last_error') or 'nenhum')[:160]}`",
            "",
            "### 🎛️ Worker Voice Agent",
            f"{voice_dot} Estado: `{voice_state}` · direto worker→Discord: `{voice_tts_direct}` · handoff: `{voice_handoff_ready}`",
            f"Plano: VPS controla comandos/UI; worker controla só áudio/voz quando pronto · sessão compartilhada: `{'sim' if voice_agent.get('shared_session_enabled') else 'não'}`",
            f"Music `{voice_music_ready}` · TTS `{voice_tts_ready}` · ducking `{'sim' if voice_agent.get('ducking_ready') else 'não'}`",
            voice_session_line,
            voice_handoff_line,
            voice_connection_line,
            f"Registro sessão: `{self._format_vps_int(voice_report_ok)}` ok · `{self._format_vps_int(voice_report_failed)}` falhas · `{self._format_vps_int(voice_report_skipped)}` pulados",
            f"Registro handoff: `{self._format_vps_int(voice_handoff_ok)}` ok · `{self._format_vps_int(voice_handoff_failed)}` falhas · `{self._format_vps_int(voice_handoff_skipped)}` pulados",
            f"Probe conexão: `{self._format_vps_int(voice_connection_probe_ok)}` start ok · `{self._format_vps_int(voice_connection_probe_failed)}` falhas · `{self._format_vps_int(voice_connection_probe_skipped)}` aguardando posse",
            f"Pendências: `{voice_missing[:140]}`",
            "",
            "### 📱 TTS do Worker / TTS Agent",
            f"Estado: `{'pronto' if agent_ok and route == 'worker' else 'fallback/VPS' if agent_enabled else 'desativado'}` · engines disponíveis: {available_engines}",
            f"Fila worker: {self._format_vps_int(int(tts_agent.get('queue_active', 0) or 0))}/{self._format_vps_int(int(tts_agent.get('queue_limit', 0) or 0))}",
            f"Health ok/falha: {self._format_vps_int(int(tts_metrics.get('tts_agent_health_ok', 0) or 0))}/{self._format_vps_int(int(tts_metrics.get('tts_agent_health_fail', 0) or 0))}",
            f"Synth tentadas/ok/falhas: {self._format_vps_int(int(tts_metrics.get('tts_agent_synth_attempts', 0) or 0))}/"
            f"{self._format_vps_int(int(tts_metrics.get('tts_agent_synth_ok', 0) or 0))}/"
            f"{self._format_vps_int(int(tts_metrics.get('tts_agent_synth_failed', 0) or 0))}",
            f"Média Agent: {self._vps_format_ms(tts_metrics.get('avg_tts_agent_synth_ms'))} · rota worker/VPS: "
            f"{self._format_vps_int(int(tts_metrics.get('tts_agent_route_worker_samples', 0) or 0))}/"
            f"{self._format_vps_int(int(tts_metrics.get('tts_agent_route_vps_samples', 0) or 0))}",
            last_worker_line,
            *worker_engine_lines,
            "",
            "### 🖥️ TTS da VPS / fallback local",
            *local_engine_lines,
            f"Fila agora: {self._format_vps_int(int(tts_metrics.get('queued_items_current', 0) or 0))} · guild states: {self._format_vps_int(int(tts_metrics.get('guild_states_current', 0) or 0))}",
            "Enfileiradas / deduplicadas / descartadas: "
            f"{self._format_vps_int(int(tts_metrics.get('queue_enqueued', 0) or 0))} / "
            f"{self._format_vps_int(int(tts_metrics.get('queue_deduplicated', 0) or 0))} / "
            f"{self._format_vps_int(int(tts_metrics.get('queue_dropped', 0) or 0))}",
            f"Espera média: {self._vps_format_ms(tts_metrics.get('avg_queue_wait_ms'))} · despacho médio: {self._vps_format_ms(tts_metrics.get('avg_dispatch_ms'))}",
            "",
            "### 📦 Cache e armazenamento",
            f"Cache VPS: {self._format_vps_int(cache_hits)} hits · {self._format_vps_int(cache_misses)} misses · {self._format_vps_int(cache_stores)} stores · {cache_hit_rate:.1f}% hit rate",
            f"Cache worker lookup: {self._format_vps_int(worker_cache_hits)} hits · {self._format_vps_int(worker_cache_misses)} misses · {self._format_vps_int(worker_cache_skipped)} skips · {self._format_vps_int(worker_cache_errors)} erros",
            f"tmp_audio: {self._vps_format_bytes_human(snapshot.get('total_tmp_bytes'))} · runtime/cache/cred {snapshot.get('runtime_files', 0)}/{snapshot.get('cache_files', 0)}/{snapshot.get('cred_files', 0)}",
            "",
            "### 🧮 Total de sínteses",
            f"Worker Agent: {self._format_vps_int(worker_total_synts)} · VPS/fallback: {self._format_vps_int(local_total_synts)}",
        ]
        report = "\n".join(lines).strip()
        if len(report) > 3800:
            report = report[:3800].rstrip() + "\n[cortado por tamanho]"
        return report

    async def _run_vps_action(self, interaction: discord.Interaction, *, selected_items: list[VpsItem]) -> None:
        if interaction.guild is None or int(getattr(interaction.guild, "id", 0) or 0) != VPS_COMMAND_GUILD_ID:
            await safe_send_interaction_message(
                interaction,
                "Esse painel só funciona na guilda de teste configurada.",
                ephemeral=True,
                log=logger,
                label="utility/vps.guild_guard",
            )
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
            item for item in ["quick_status", "apk_diag", "tts", "servers", "base_git", "music_diag", "full_diag", "snapshot"] if item in selected_items
        ]

        stamp = diagnostics_file_stamp()
        files: list[discord.File] = []
        attachment_lines: list[str] = []
        error_lines: list[str] = []
        status_report: str | None = None
        servers_report: str | None = None
        tts_report: str | None = None
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

            if item == "apk_diag":
                try:
                    report = await self._with_vps_timeout("diagnóstico APK", build_core_worker_apk_diagnostics_report(), timeout=VPS_APK_DIAG_TIMEOUT_SECONDS)
                    report_bytes = (report or "Diagnóstico APK vazio.\n").encode("utf-8", "replace")
                    files.append(discord.File(io.BytesIO(report_bytes), filename=f"core-worker-apk-diag-{stamp}.txt"))
                    attachment_lines.append(f"📲 Diagnóstico APK anexado ({_format_attachment_size(len(report_bytes))}).")
                    generated_any = True
                except Exception as exc:
                    logger.exception("[utility/vps] falha ao gerar diagnóstico APK")
                    error_lines.append(f"Diagnóstico APK falhou: {type(exc).__name__}: {str(exc)[:300]}")
                continue

            if item == "tts":
                try:
                    tts_report = await self._with_vps_timeout("TTS", self._build_vps_tts_report(), timeout=VPS_TTS_TIMEOUT_SECONDS)
                    generated_any = True
                except Exception as exc:
                    logger.exception("[utility/vps] falha ao gerar status TTS")
                    error_lines.append(f"TTS falhou: {type(exc).__name__}: {str(exc)[:300]}")
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
            view = VpsResultView(status_report=status_report, servers_report=servers_report, tts_report=tts_report, attachment_lines=attachment_lines, error_lines=error_lines)
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
            if tts_report:
                fallback_lines.append(tts_report.strip())
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
        """Abre o modal avançado do /vps sem fazer await pesado antes.

        Discord invalida a interação se a primeira resposta não acontecer em
        poucos segundos; por isso este método só monta e envia o modal. O select
        dentro do modal é o fluxo principal. O TextInput simples não deve virar
        fallback silencioso, porque isso esconde regressão de Components V2.
        """
        try:
            await interaction.response.send_modal(VpsModal(self, force_text_fallback=False))
            return
        except Exception as exc:
            if is_unknown_interaction(exc):
                logger.warning("[utility/vps] interação /vps expirou antes do modal abrir: %s", exc)
                return
            logger.exception("[utility/vps] falha ao abrir modal avançado")
            await safe_send_interaction_message(
                interaction,
                "`⚠️` Não consegui abrir o painel avançado da VPS. Tente novamente em alguns segundos.",
                ephemeral=True,
                log=logger,
                label="utility/vps.modal_fallback",
            )

    @app_commands.command(name="vps", description="Abre o painel de diagnóstico/anexos da VPS")
    @app_commands.guilds(VPS_COMMAND_GUILD)
    async def vps(self, interaction: discord.Interaction):
        if interaction.guild is None or int(getattr(interaction.guild, "id", 0) or 0) != VPS_COMMAND_GUILD_ID:
            await safe_send_interaction_message(
                interaction,
                "Esse painel só funciona na guilda de teste configurada.",
                ephemeral=True,
                log=logger,
                label="utility/vps.command_guard",
            )
            return
        # Não faça await pesado antes de send_modal: se o owner check/rede travar,
        # o Discord mostra “O aplicativo não respondeu” e o modal nem abre.
        # A validação de dono continua no submit, logo após o defer correto.
        await self._send_vps_modal(interaction)
