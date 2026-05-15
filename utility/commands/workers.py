from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands


WORKERS_COMMAND_GUILD_ID = 927002914449424404
WORKERS_COMMAND_GUILD = discord.Object(id=WORKERS_COMMAND_GUILD_ID)
WORKERS_PANEL_TIMEOUT_SECONDS = 180.0
WORKERS_DEFAULT_ROLES = (
    "vps",
    "diagnostics",
    "log-summary",
    "maintenance-plan",
    "zip-validate",
    "ffmpeg",
    "ffprobe",
    "tts-convert",
)

_SECRET_PATTERNS = (
    re.compile(r"(Authorization:\s*Bearer\s+)[^\s]+", re.IGNORECASE),
    re.compile(r"(PHONE_WORKER_TOKEN\s*=\s*)[^\s]+", re.IGNORECASE),
    re.compile(r"(X-Phone-Worker-Token:\s*)[^\s]+", re.IGNORECASE),
)


def _env_bool(name: str, default: bool = False) -> bool:
    raw = str(os.getenv(name, "") or "").strip().lower()
    if raw in {"1", "true", "yes", "y", "on", "sim"}:
        return True
    if raw in {"0", "false", "no", "n", "off", "nao", "não"}:
        return False
    return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(str(os.getenv(name, default)).strip().replace(",", "."))
    except Exception:
        return default


def _shorten(text: object, *, limit: int = 80) -> str:
    value = str(text or "").replace("\n", " ").strip()
    if len(value) > limit:
        return value[: max(1, limit - 1)].rstrip() + "…"
    return value


def _redact(text: object) -> str:
    value = str(text or "")
    for pattern in _SECRET_PATTERNS:
        value = pattern.sub(r"\1[redacted]", value)
    return value


def _format_seconds(value: object) -> str:
    try:
        seconds = max(0, int(float(value or 0)))
    except Exception:
        return "desconhecido"
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if secs or not parts:
        parts.append(f"{secs}s")
    return " ".join(parts[:3])


def _format_bytes(value: object) -> str:
    try:
        size = max(0, float(value or 0))
    except Exception:
        return "0 B"
    units = ["B", "KB", "MB", "GB", "TB"]
    index = 0
    while size >= 1024 and index < len(units) - 1:
        size /= 1024
        index += 1
    if index == 0:
        return f"{int(size)} {units[index]}"
    return f"{size:.1f} {units[index]}"


def _parse_roles(raw: str | None, *, status: dict[str, Any] | None = None) -> list[str]:
    roles: list[str] = []
    for item in str(raw or "").replace(";", ",").split(","):
        clean = item.strip().lower().replace("_", "-")
        if clean and clean not in roles:
            roles.append(clean)
    if not roles:
        roles.extend(WORKERS_DEFAULT_ROLES)
    status = status or {}
    if status.get("ffmpeg") and "ffmpeg" not in roles:
        roles.append("ffmpeg")
    if status.get("ffprobe") and "ffprobe" not in roles:
        roles.append("ffprobe")
    return roles[:16]


def _host_label(host: str) -> str:
    host = str(host or "").strip()
    if not host:
        return "não configurado"
    # Tailscale costuma usar 100.x.y.z. Não é segredo, mas evitar mostrar tudo
    # em painel técnico reduz vazamento acidental em prints.
    if re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){3}", host):
        parts = host.split(".")
        return f"{parts[0]}.{parts[1]}.x.x"
    if len(host) > 28:
        return host[:14] + "…" + host[-8:]
    return host


def _repo_root() -> Path:
    # utility/commands/workers.py -> repo root
    return Path(__file__).resolve().parents[2]


@dataclass(slots=True)
class WorkerSnapshot:
    enabled: bool
    configured: bool
    online: bool
    host: str = ""
    port: str = "8766"
    scheme: str = "http"
    name: str = "Core Phone Worker"
    roles: list[str] = field(default_factory=list)
    status: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    checked_at: float = field(default_factory=time.time)
    action_note: str = ""
    watch_output: str = ""

    @property
    def base_url_label(self) -> str:
        if not self.configured:
            return "não configurado"
        return f"{self.scheme}://{_host_label(self.host)}:{self.port}"

    @property
    def state_label(self) -> str:
        if not self.enabled:
            return "⚫ desativado"
        if not self.configured:
            return "🟠 incompleto"
        if self.online:
            return "🟢 online"
        return "🔴 offline"

    @property
    def accent(self) -> discord.Color:
        if self.online:
            return discord.Color.green()
        if self.configured and self.enabled:
            return discord.Color.orange()
        return discord.Color.dark_grey()


class WorkersPanelView(discord.ui.LayoutView):
    def __init__(self, cog: "WorkersCommandMixin", *, owner_id: int, snapshot: WorkerSnapshot):
        super().__init__(timeout=WORKERS_PANEL_TIMEOUT_SECONDS)
        self.cog = cog
        self.owner_id = int(owner_id)
        self.snapshot = snapshot
        self.message: discord.Message | None = None
        self._rebuild_layout()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if int(getattr(interaction.user, "id", 0) or 0) == self.owner_id:
            return True
        message = "Só quem abriu o painel de workers pode usar esses controles."
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
        return False

    async def on_timeout(self) -> None:
        self._rebuild_layout(expired=True)
        with contextlib.suppress(Exception):
            if self.message is not None:
                await self.message.edit(view=self, allowed_mentions=discord.AllowedMentions.none())

    def _clear_items(self) -> None:
        for item in list(self.children):
            self.remove_item(item)

    def _rebuild_layout(self, *, expired: bool = False) -> None:
        self._clear_items()
        snapshot = self.snapshot
        if expired:
            self.add_item(discord.ui.Container(
                discord.ui.TextDisplay(
                    "# 📱 Core Workers\n"
                    "Esse painel expirou. Use `/workers` de novo para atualizar o controle."
                ),
                accent_color=discord.Color.dark_grey(),
            ))
            return

        header = discord.ui.TextDisplay(
            "# 📱 Core Workers\n"
            f"-# Painel privado do orquestrador · guild `{WORKERS_COMMAND_GUILD_ID}`\n"
            f"**Estado:** {snapshot.state_label} · **Worker:** `{_shorten(snapshot.name, limit=40)}`"
        )

        status_lines = self._status_lines(snapshot)
        role_lines = self._role_lines(snapshot)
        action_lines = self._action_lines(snapshot)

        refresh = discord.ui.Button(label="Atualizar", emoji="🔄", style=discord.ButtonStyle.primary)
        refresh.callback = self._refresh

        wake = discord.ui.Button(
            label="Acordar worker",
            emoji="📡",
            style=discord.ButtonStyle.success,
            disabled=not snapshot.configured,
        )
        wake.callback = self._wake_worker

        pairing = discord.ui.Button(label="Pareamento APK", emoji="🔐", style=discord.ButtonStyle.secondary)
        pairing.callback = self._pairing_info

        close = discord.ui.Button(label="Fechar", emoji="✖️", style=discord.ButtonStyle.danger)
        close.callback = self._close

        container = discord.ui.Container(
            header,
            discord.ui.Separator(),
            discord.ui.TextDisplay("\n".join(status_lines)),
            discord.ui.Separator(),
            discord.ui.TextDisplay("\n".join(role_lines)),
            discord.ui.Separator(),
            discord.ui.TextDisplay("\n".join(action_lines)),
            discord.ui.ActionRow(refresh, wake, pairing, close),
            accent_color=snapshot.accent,
        )
        self.add_item(container)

    def _status_lines(self, snapshot: WorkerSnapshot) -> list[str]:
        status = snapshot.status or {}
        disk = status.get("disk_home") if isinstance(status.get("disk_home"), dict) else {}
        free = _format_bytes(disk.get("free")) if isinstance(disk, dict) else "desconhecido"
        used = _format_bytes(disk.get("used")) if isinstance(disk, dict) else "desconhecido"
        load = status.get("loadavg")
        if isinstance(load, list) and load:
            load_text = " / ".join(str(round(float(item), 2)) for item in load[:3] if isinstance(item, (int, float))) or "desconhecido"
        else:
            load_text = "indisponível"

        lines = [
            "## 🩺 Saúde",
            f"Endpoint: `{snapshot.base_url_label}`",
            f"Configurado no `.env`: **{'sim' if snapshot.configured else 'não'}** · habilitado: **{'sim' if snapshot.enabled else 'não'}**",
        ]
        if snapshot.online:
            lines.extend([
                f"Uptime: `{_format_seconds(status.get('uptime_seconds'))}` · PID: `{status.get('pid', '?')}`",
                f"Jobs: `{status.get('jobs_started', 0)}` iniciados · `{status.get('jobs_failed', 0)}` falhas",
                f"Disco home: `{used}` usado · `{free}` livre · load: `{load_text}`",
                f"Python: `{_shorten(status.get('python'), limit=22)}` · máquina: `{_shorten(status.get('machine'), limit=18)}`",
            ])
        elif snapshot.error:
            lines.append(f"Erro: `{_shorten(_redact(snapshot.error), limit=180)}`")
        else:
            lines.append("Ainda não há resposta do worker atual.")
        return lines

    def _role_lines(self, snapshot: WorkerSnapshot) -> list[str]:
        status = snapshot.status or {}
        roles = snapshot.roles or []
        role_text = " · ".join(f"`{role}`" for role in roles[:12]) or "`nenhuma`"
        capability_bits = [
            f"ffmpeg {'✅' if status.get('ffmpeg') else '❌'}",
            f"ffprobe {'✅' if status.get('ffprobe') else '❌'}",
        ]
        return [
            "## 🧩 Roles e capacidades",
            role_text,
            "-# " + " · ".join(capability_bits),
            "-# Esta versão ainda lê o phone-worker atual. O registro multi-celular entra na próxima etapa do Core Worker.",
        ]

    def _action_lines(self, snapshot: WorkerSnapshot) -> list[str]:
        lines = [
            "## 🕹️ Controle",
            "`Atualizar` consulta `/status` sem expor token.",
            "`Acordar worker` chama o watchdog seguro da VPS (`scripts/phone-worker-watch.sh`).",
            "`Pareamento APK` mostra o fluxo planejado para o Core Worker privado.",
        ]
        if snapshot.action_note:
            lines.append(f"\n**Última ação:** {_shorten(_redact(snapshot.action_note), limit=220)}")
        if snapshot.watch_output:
            lines.append(f"-# `{_shorten(_redact(snapshot.watch_output), limit=260)}`")
        return lines

    async def _refresh(self, interaction: discord.Interaction):
        await interaction.response.defer(thinking=False)
        self.snapshot = await self.cog._collect_workers_snapshot()
        self._rebuild_layout()
        await interaction.edit_original_response(view=self, allowed_mentions=discord.AllowedMentions.none())

    async def _wake_worker(self, interaction: discord.Interaction):
        await interaction.response.defer(thinking=False)
        snapshot = await self.cog._wake_phone_worker()
        self.snapshot = snapshot
        self._rebuild_layout()
        await interaction.edit_original_response(view=self, allowed_mentions=discord.AllowedMentions.none())

    async def _pairing_info(self, interaction: discord.Interaction):
        message = (
            "## 🔐 Pareamento planejado do Core Worker APK\n"
            "1. `/workers` gera um código temporário.\n"
            "2. O APK lê o código/QR e pede um token limitado para aquele celular.\n"
            "3. A VPS salva só o hash do token e as roles do aparelho.\n"
            "4. O celular aparece no painel com bateria, rede, roles, logs e saúde.\n\n"
            "Nada de token do Discord, GitHub token ou segredo global dentro do APK/GitHub."
        )
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)

    async def _close(self, interaction: discord.Interaction):
        await interaction.response.defer(thinking=False)
        self._rebuild_layout(expired=True)
        with contextlib.suppress(Exception):
            await interaction.edit_original_response(view=self, allowed_mentions=discord.AllowedMentions.none())
        self.stop()


class WorkersCommandMixin:
    """Comando `/workers` da cog Utility.

    É um painel único em Components V2 para controlar a camada Core Worker.
    Nesta primeira etapa ele monitora o phone-worker atual do Termux e deixa a
    base pronta para evoluir para registry multi-celular sem expor segredos.
    """

    async def _can_use_workers(self, interaction: discord.Interaction) -> bool:
        with contextlib.suppress(Exception):
            return bool(await self.bot.is_owner(interaction.user))
        return False

    def _worker_base_config(self) -> tuple[bool, bool, str, str, str, str]:
        enabled = _env_bool("PHONE_WORKER_ENABLED", False)
        host = str(os.getenv("PHONE_WORKER_HOST") or os.getenv("AUX_LAVALINK_HOST") or os.getenv("PHONE_LAVALINK_HOST") or "").strip()
        port = str(os.getenv("PHONE_WORKER_PORT") or "8766").strip() or "8766"
        scheme = str(os.getenv("PHONE_WORKER_SCHEME") or "http").strip() or "http"
        name = str(os.getenv("PHONE_WORKER_NAME") or os.getenv("CORE_WORKER_NAME") or "Core Phone Worker").strip() or "Core Phone Worker"
        configured = bool(host)
        return enabled, configured, host, port, scheme, name

    def _request_worker_status_sync(self, *, timeout: float) -> dict[str, Any]:
        enabled, configured, host, port, scheme, _name = self._worker_base_config()
        if not enabled:
            raise RuntimeError("PHONE_WORKER_ENABLED=false")
        if not configured:
            raise RuntimeError("PHONE_WORKER_HOST não configurado")
        token = str(os.getenv("PHONE_WORKER_TOKEN") or "").strip()
        headers = {"Accept": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(f"{scheme}://{host}:{port}/status", headers=headers, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=max(0.5, timeout)) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")[:240]
            raise RuntimeError(f"HTTP {exc.code}: {_redact(body)}") from exc
        parsed = json.loads(raw.decode("utf-8", errors="replace"))
        if not isinstance(parsed, dict):
            raise RuntimeError("resposta não é JSON object")
        return parsed

    async def _collect_workers_snapshot(self, *, action_note: str = "", watch_output: str = "") -> WorkerSnapshot:
        enabled, configured, host, port, scheme, name = self._worker_base_config()
        timeout = max(0.8, _env_float("WORKERS_PANEL_STATUS_TIMEOUT_SECONDS", _env_float("PHONE_WORKER_QUICK_STATUS_TIMEOUT_SECONDS", 1.6)))
        status: dict[str, Any] = {}
        error = ""
        online = False
        if enabled and configured:
            try:
                status = await asyncio.to_thread(self._request_worker_status_sync, timeout=timeout)
                online = bool(status.get("ok", True))
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
        roles = _parse_roles(os.getenv("PHONE_WORKER_ROLES") or os.getenv("CORE_WORKER_ROLES"), status=status)
        return WorkerSnapshot(
            enabled=enabled,
            configured=configured,
            online=online,
            host=host,
            port=port,
            scheme=scheme,
            name=name,
            roles=roles,
            status=status,
            error=error,
            action_note=action_note,
            watch_output=watch_output,
        )

    async def _wake_phone_worker(self) -> WorkerSnapshot:
        script = _repo_root() / "scripts" / "phone-worker-watch.sh"
        if not script.exists():
            return await self._collect_workers_snapshot(action_note="watchdog não encontrado em scripts/phone-worker-watch.sh")

        timeout = max(5.0, _env_float("WORKERS_PANEL_WAKE_TIMEOUT_SECONDS", 28.0))
        started = time.perf_counter()
        note = "watchdog executado"
        output = ""
        try:
            proc = await asyncio.create_subprocess_exec(
                "bash",
                str(script),
                cwd=str(_repo_root()),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            elapsed = time.perf_counter() - started
            output_text = (stdout or b"").decode("utf-8", errors="replace").strip()
            error_text = (stderr or b"").decode("utf-8", errors="replace").strip()
            merged = " | ".join(part for part in [output_text, error_text] if part)
            output = merged or "sem saída"
            note = f"watchdog finalizou com código {proc.returncode} em {elapsed:.1f}s"
        except asyncio.TimeoutError:
            note = f"watchdog excedeu {timeout:.0f}s"
            with contextlib.suppress(Exception):
                proc.kill()
            with contextlib.suppress(Exception):
                await proc.communicate()
        except Exception as exc:
            note = f"watchdog falhou: {type(exc).__name__}: {exc}"
        return await self._collect_workers_snapshot(action_note=note, watch_output=output)

    async def _send_workers_panel(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or int(getattr(interaction.guild, "id", 0) or 0) != WORKERS_COMMAND_GUILD_ID:
            await interaction.response.send_message("Esse painel só funciona na guilda configurada para workers.", ephemeral=True)
            return
        await interaction.response.defer(thinking=True, ephemeral=True)
        if not await self._can_use_workers(interaction):
            await interaction.followup.send("Esse painel de workers é exclusivo do dono do bot.", ephemeral=True)
            return

        snapshot = await self._collect_workers_snapshot()
        view = WorkersPanelView(self, owner_id=int(interaction.user.id), snapshot=snapshot)
        message = await interaction.followup.send(view=view, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
        view.message = message

    @app_commands.command(name="workers", description="Abre o painel Core Worker privado da VPS")
    @app_commands.guilds(WORKERS_COMMAND_GUILD)
    async def workers(self, interaction: discord.Interaction):
        await self._send_workers_panel(interaction)
