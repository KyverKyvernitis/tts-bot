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

from utility.commands.workers_registry import get_core_workers_registry


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
    re.compile(r"(CORE_WORKER_TOKEN\s*=\s*)[^\s]+", re.IGNORECASE),
    re.compile(r"(X-Phone-Worker-Token:\s*)[^\s]+", re.IGNORECASE),
    re.compile(r"(X-Core-Worker-Token:\s*)[^\s]+", re.IGNORECASE),
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
    value = re.sub(r"\s+", " ", value)
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


def _format_age(value: object) -> str:
    if value is None:
        return "nunca"
    try:
        seconds = max(0.0, float(value))
    except Exception:
        return "desconhecido"
    if seconds < 2:
        return "agora"
    return f"há {_format_seconds(seconds)}"


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
    # Tailscale costuma usar 100.x.y.z. Não é segredo, mas mascarar reduz
    # vazamento acidental em print do painel.
    if re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){3}", host):
        parts = host.split(".")
        return f"{parts[0]}.{parts[1]}.x.x"
    if len(host) > 28:
        return host[:14] + "…" + host[-8:]
    return host


def _repo_root() -> Path:
    # utility/commands/workers.py -> repo root
    return Path(__file__).resolve().parents[2]


def _public_base_url() -> str:
    return str(os.getenv("CORE_WORKER_PUBLIC_BASE_URL") or os.getenv("VPS_PUBLIC_BASE_URL") or "").strip().rstrip("/")


def _battery_text(worker: dict[str, Any]) -> str:
    battery = worker.get("battery") if isinstance(worker.get("battery"), dict) else {}
    if not battery:
        return "bateria n/a"
    level = battery.get("level") or battery.get("percent") or battery.get("percentage")
    charging = battery.get("charging")
    parts: list[str] = []
    try:
        if level is not None:
            parts.append(f"{int(float(level))}%")
    except Exception:
        pass
    if isinstance(charging, bool):
        parts.append("carregando" if charging else "sem carga")
    elif charging is not None:
        parts.append(_shorten(charging, limit=18))
    return " ".join(parts) if parts else "bateria n/a"


def _network_text(worker: dict[str, Any]) -> str:
    network = worker.get("network") if isinstance(worker.get("network"), dict) else {}
    if not network:
        return "rede n/a"
    kind = network.get("type") or network.get("kind") or network.get("transport") or network.get("name")
    tailscale = network.get("tailscale")
    parts: list[str] = []
    if kind:
        parts.append(_shorten(kind, limit=18))
    if isinstance(tailscale, bool):
        parts.append("tailscale ok" if tailscale else "tailscale off")
    return " · ".join(parts) if parts else "rede n/a"


def _role_text(roles: list[str], *, limit: int = 8) -> str:
    selected = [str(role) for role in roles[:limit] if role]
    if not selected:
        return "`nenhuma`"
    return " ".join(f"`{_shorten(role, limit=24)}`" for role in selected)


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
    registry_snapshot: dict[str, Any] = field(default_factory=dict)
    registry_error: str = ""

    @property
    def base_url_label(self) -> str:
        if not self.configured:
            return "não configurado"
        return f"{self.scheme}://{_host_label(self.host)}:{self.port}"

    @property
    def state_label(self) -> str:
        registered_online = int(((self.registry_snapshot or {}).get("summary") or {}).get("online") or 0)
        if registered_online > 0:
            return f"🟢 {registered_online} worker(s) online"
        if self.online:
            return "🟢 phone-worker online"
        if not self.enabled:
            return "⚫ phone-worker desativado"
        if not self.configured:
            return "🟠 phone-worker incompleto"
        return "🔴 nenhum worker online"

    @property
    def accent(self) -> discord.Color:
        summary = (self.registry_snapshot or {}).get("summary") if isinstance(self.registry_snapshot, dict) else {}
        if int((summary or {}).get("online") or 0) > 0 or self.online:
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

        summary = (snapshot.registry_snapshot or {}).get("summary") if isinstance(snapshot.registry_snapshot, dict) else {}
        header = discord.ui.TextDisplay(
            "# 📱 Core Workers\n"
            f"-# Painel privado do orquestrador · guild `{WORKERS_COMMAND_GUILD_ID}`\n"
            f"**Estado:** {snapshot.state_label}\n"
            f"**Registry:** `{int((summary or {}).get('registered') or 0)}` registrado(s) · "
            f"`{int((summary or {}).get('online') or 0)}` online · "
            f"`{int((summary or {}).get('pairings_active') or 0)}` pareamento(s) ativo(s) · "
            f"jobs `Q:{int((summary or {}).get('jobs_queued') or 0)}`/`R:{int((summary or {}).get('jobs_running') or 0)}`"
        )

        refresh = discord.ui.Button(label="Atualizar", emoji="🔄", style=discord.ButtonStyle.primary)
        refresh.callback = self._refresh

        pairing = discord.ui.Button(label="Parear APK", emoji="🔐", style=discord.ButtonStyle.success)
        pairing.callback = self._create_pairing

        wake = discord.ui.Button(
            label="Acordar phone-worker",
            emoji="📡",
            style=discord.ButtonStyle.secondary,
            disabled=not snapshot.configured,
        )
        wake.callback = self._wake_worker

        test_job = discord.ui.Button(label="Testar worker", emoji="🧪", style=discord.ButtonStyle.secondary)
        test_job.callback = self._queue_worker_test

        cleanup_jobs = discord.ui.Button(label="Limpar jobs", emoji="🧹", style=discord.ButtonStyle.secondary)
        cleanup_jobs.callback = self._cleanup_jobs

        close = discord.ui.Button(label="Fechar", emoji="✖️", style=discord.ButtonStyle.danger)
        close.callback = self._close

        container = discord.ui.Container(
            header,
            discord.ui.Separator(),
            discord.ui.TextDisplay("\n".join(self._registry_lines(snapshot))),
            discord.ui.Separator(),
            discord.ui.TextDisplay("\n".join(self._legacy_status_lines(snapshot))),
            discord.ui.Separator(),
            discord.ui.TextDisplay("\n".join(self._action_lines(snapshot))),
            discord.ui.ActionRow(refresh, pairing, wake, close),
            accent_color=snapshot.accent,
        )
        self.add_item(container)

    def _registry_lines(self, snapshot: WorkerSnapshot) -> list[str]:
        if snapshot.registry_error:
            return [
                "## 🧬 Registry multi-worker",
                f"Erro lendo registry: `{_shorten(_redact(snapshot.registry_error), limit=180)}`",
            ]
        registry = snapshot.registry_snapshot or {}
        workers = registry.get("workers") if isinstance(registry.get("workers"), list) else []
        pairings = registry.get("pairings") if isinstance(registry.get("pairings"), list) else []
        lines = ["## 🧬 Registry multi-worker"]
        if not workers:
            lines.append("Nenhum Core Worker pareado ainda. Use **Parear APK** para gerar um código temporário.")
        else:
            for worker in workers[:5]:
                if not isinstance(worker, dict):
                    continue
                icon = "🟢" if worker.get("online") else "🔴"
                name = _shorten(worker.get("name") or worker.get("worker_id") or "Core Worker", limit=32)
                worker_id = _shorten(worker.get("worker_id"), limit=22)
                roles = _role_text([str(r) for r in (worker.get("roles") or [])], limit=5)
                seen = _format_age(worker.get("last_seen_age_seconds"))
                battery = _battery_text(worker)
                network = _network_text(worker)
                version = _shorten(worker.get("version"), limit=24) or "sem versão"
                lines.append(f"{icon} **{name}** · `{worker_id}`")
                lines.append(f"-# {roles} · visto {seen} · {battery} · {network} · {version}")
            hidden = max(0, len(workers) - 5)
            if hidden:
                lines.append(f"-# … +{hidden} worker(s) oculto(s) para manter o painel compacto.")
        if pairings:
            active = []
            for pair in pairings[:3]:
                try:
                    ttl = _format_seconds(pair.get("ttl_left_seconds"))
                except Exception:
                    ttl = "?"
                active.append(f"`{ttl}`")
            lines.append("-# Pareamentos ativos expiram em: " + " · ".join(active))
        return lines

    def _job_lines(self, snapshot: WorkerSnapshot) -> list[str]:
        registry = snapshot.registry_snapshot or {}
        jobs = registry.get("jobs") if isinstance(registry.get("jobs"), list) else []
        summary = registry.get("summary") if isinstance(registry.get("summary"), dict) else {}
        lines = [
            "## 🧾 Job Queue segura",
            f"Fila: `{int((summary or {}).get('jobs_queued') or 0)}` aguardando · "
            f"`{int((summary or {}).get('jobs_running') or 0)}` rodando · "
            f"`{int((summary or {}).get('jobs_succeeded') or 0)}` ok · "
            f"`{int((summary or {}).get('jobs_failed') or 0)}` falha",
        ]
        if not jobs:
            lines.append("Nenhum job recente. Use **Testar worker** para criar um `ping` seguro.")
            return lines
        for job in jobs[:5]:
            if not isinstance(job, dict):
                continue
            status = str(job.get("status") or "queued")
            icon = {"queued": "🟡", "running": "🔵", "succeeded": "🟢", "failed": "🔴", "expired": "⚫"}.get(status, "⚪")
            job_id = _shorten(job.get("job_id"), limit=18)
            kind = _shorten(job.get("type"), limit=24)
            worker = _shorten(job.get("worker_id") or job.get("target_worker_id") or "qualquer worker", limit=24)
            age = _format_age(job.get("age_seconds"))
            extra = _shorten(job.get("error") or job.get("summary") or "", limit=80)
            lines.append(f"{icon} `{kind}` · `{job_id}` · `{status}` · {worker} · {age}")
            if extra:
                lines.append(f"-# {extra}")
        return lines

    def _legacy_status_lines(self, snapshot: WorkerSnapshot) -> list[str]:
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
            "## 🩺 Phone-worker atual",
            f"Endpoint: `{snapshot.base_url_label}`",
            f"Configurado no `.env`: **{'sim' if snapshot.configured else 'não'}** · habilitado: **{'sim' if snapshot.enabled else 'não'}**",
        ]
        if snapshot.online:
            lines.extend([
                f"Uptime: `{_format_seconds(status.get('uptime_seconds'))}` · PID: `{status.get('pid', '?')}`",
                f"Jobs: `{status.get('jobs_started', 0)}` iniciados · `{status.get('jobs_failed', 0)}` falhas",
                f"Disco home: `{used}` usado · `{free}` livre · load: `{load_text}`",
                f"Roles: {_role_text(snapshot.roles, limit=8)}",
                f"Capacidades: ffmpeg {'✅' if status.get('ffmpeg') else '❌'} · ffprobe {'✅' if status.get('ffprobe') else '❌'}",
            ])
        elif snapshot.error:
            lines.append(f"Erro: `{_shorten(_redact(snapshot.error), limit=180)}`")
        else:
            lines.append("Ainda não há resposta do phone-worker direto.")
        return lines

    def _action_lines(self, snapshot: WorkerSnapshot) -> list[str]:
        base_url = _public_base_url()
        pair_route = f"{base_url}/core-worker/pair" if base_url else "/core-worker/pair"
        heartbeat_route = f"{base_url}/core-worker/heartbeat" if base_url else "/core-worker/heartbeat"
        poll_route = f"{base_url}/core-worker/jobs/poll" if base_url else "/core-worker/jobs/poll"
        result_route = f"{base_url}/core-worker/jobs/result" if base_url else "/core-worker/jobs/result"
        lines = [
            "## 🕹️ Controle",
            "`Atualizar` consulta o registry e o `/status` do phone-worker sem mostrar token.",
            "`Testar worker` cria um job `ping` na fila; o celular executa por polling autenticado.",
            "`Parear APK` gera código temporário; a VPS salva só hash do código/token.",
            f"Rotas do APK/agent: `POST {pair_route}` · `POST {heartbeat_route}` · `POST {poll_route}` · `POST {result_route}`",
            "`Acordar phone-worker` chama o watchdog seguro da VPS (`scripts/phone-worker-watch.sh`).",
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

    async def _create_pairing(self, interaction: discord.Interaction):
        await interaction.response.defer(thinking=False)
        try:
            pairing = await self.cog._create_core_worker_pairing(interaction.user)
            code = str(pairing.get("code") or "")
            ttl = _format_seconds(pairing.get("ttl_seconds"))
            expires = _format_seconds(max(0, float(pairing.get("expires_at") or 0) - time.time()))
            base_url = _public_base_url() or "URL do webserver da VPS/Tailscale"
            msg = (
                "## 🔐 Pareamento Core Worker\n"
                f"Código temporário: `{code}`\n"
                f"Validade: `{ttl}` · expira em `{expires}`\n\n"
                "No APK/agent, use esse código para chamar:\n"
                f"`POST {base_url}/core-worker/pair`\n\n"
                "Payload mínimo planejado:\n"
                "```json\n"
                f"{{\"code\":\"{code}\",\"name\":\"Meu celular\",\"roles\":[\"tts\",\"ffmpeg\"]}}\n"
                "```\n"
                "A resposta entrega o token do worker **uma única vez**. Salve localmente no APK/agent; não suba isso para GitHub."
            )
            self.snapshot = await self.cog._collect_workers_snapshot(action_note=f"código de pareamento gerado: {code}")
            self._rebuild_layout()
            await interaction.edit_original_response(view=self, allowed_mentions=discord.AllowedMentions.none())
            await interaction.followup.send(msg, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
        except Exception as exc:
            self.snapshot = await self.cog._collect_workers_snapshot(action_note=f"falha ao gerar pareamento: {type(exc).__name__}: {exc}")
            self._rebuild_layout()
            await interaction.edit_original_response(view=self, allowed_mentions=discord.AllowedMentions.none())
            await interaction.followup.send("Não consegui gerar o pareamento agora.", ephemeral=True)

    async def _queue_worker_test(self, interaction: discord.Interaction):
        await interaction.response.defer(thinking=False)
        try:
            result = await self.cog._queue_core_worker_job(interaction.user, job_type="ping", summary="teste manual pelo painel /workers")
            job = result.get("job") if isinstance(result, dict) else {}
            job_id = _shorten((job or {}).get("job_id"), limit=32)
            self.snapshot = await self.cog._collect_workers_snapshot(action_note=f"job de teste criado: {job_id}")
        except Exception as exc:
            self.snapshot = await self.cog._collect_workers_snapshot(action_note=f"falha ao criar job de teste: {type(exc).__name__}: {exc}")
        self._rebuild_layout()
        await interaction.edit_original_response(view=self, allowed_mentions=discord.AllowedMentions.none())

    async def _cleanup_jobs(self, interaction: discord.Interaction):
        await interaction.response.defer(thinking=False)
        try:
            result = await self.cog._cleanup_core_worker_jobs()
            self.snapshot = await self.cog._collect_workers_snapshot(action_note=f"limpeza de jobs: {result}")
        except Exception as exc:
            self.snapshot = await self.cog._collect_workers_snapshot(action_note=f"falha limpando jobs: {type(exc).__name__}: {exc}")
        self._rebuild_layout()
        await interaction.edit_original_response(view=self, allowed_mentions=discord.AllowedMentions.none())

    async def _close(self, interaction: discord.Interaction):
        await interaction.response.defer(thinking=False)
        self._rebuild_layout(expired=True)
        with contextlib.suppress(Exception):
            await interaction.edit_original_response(view=self, allowed_mentions=discord.AllowedMentions.none())
        self.stop()


class WorkersCommandMixin:
    """Comando `/workers` da cog Utility.

    Painel único em Components V2 para o Core Worker: mostra o phone-worker atual,
    o registry multi-celular, gera pareamento temporário e prepara a ponte do APK.
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

    async def _collect_registry_snapshot(self) -> tuple[dict[str, Any], str]:
        try:
            registry = get_core_workers_registry()
            snapshot = await asyncio.to_thread(registry.snapshot)
            return snapshot, ""
        except Exception as exc:
            return {}, f"{type(exc).__name__}: {exc}"

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
        registry_snapshot, registry_error = await self._collect_registry_snapshot()
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
            registry_snapshot=registry_snapshot,
            registry_error=registry_error,
        )

    async def _create_core_worker_pairing(self, owner: discord.abc.User) -> dict[str, Any]:
        registry = get_core_workers_registry()
        return await asyncio.to_thread(
            registry.create_pairing,
            created_by_id=int(getattr(owner, "id", 0) or 0),
            created_by_name=str(getattr(owner, "display_name", None) or getattr(owner, "name", "") or ""),
        )

    async def _queue_core_worker_job(self, owner: discord.abc.User, *, job_type: str, payload: dict[str, Any] | None = None, summary: str = "") -> dict[str, Any]:
        registry = get_core_workers_registry()
        snapshot = await asyncio.to_thread(registry.snapshot)
        workers = snapshot.get("workers") if isinstance(snapshot.get("workers"), list) else []
        target_worker_id = ""
        for worker in workers:
            if isinstance(worker, dict) and worker.get("online"):
                target_worker_id = str(worker.get("worker_id") or "")
                break
        return await asyncio.to_thread(
            registry.create_job,
            job_type=job_type,
            payload=payload or {},
            created_by_id=int(getattr(owner, "id", 0) or 0),
            created_by_name=str(getattr(owner, "display_name", None) or getattr(owner, "name", "") or ""),
            target_worker_id=target_worker_id,
            required_capabilities=["phone-worker"],
            ttl_seconds=900,
            lease_seconds=120,
            max_attempts=2,
            summary=summary or job_type,
        )

    async def _cleanup_core_worker_jobs(self) -> dict[str, Any]:
        registry = get_core_workers_registry()
        return await asyncio.to_thread(registry.cleanup_jobs)

    async def _wake_phone_worker(self) -> WorkerSnapshot:
        script = _repo_root() / "scripts" / "phone-worker-watch.sh"
        if not script.exists():
            return await self._collect_workers_snapshot(action_note="watchdog não encontrado em scripts/phone-worker-watch.sh")

        timeout = max(5.0, _env_float("WORKERS_PANEL_WAKE_TIMEOUT_SECONDS", 28.0))
        started = time.perf_counter()
        note = "watchdog executado"
        output = ""
        proc: asyncio.subprocess.Process | None = None
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
            if proc is not None:
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
