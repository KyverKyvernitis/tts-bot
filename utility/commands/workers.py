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
LEGACY_WORKER_ID = "__legacy_phone_worker__"


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
    tailscale_state = network.get("tailscale_state")
    parts: list[str] = []
    if kind:
        parts.append(_shorten(kind, limit=18))
    if isinstance(tailscale, bool):
        label = "tailscale ok" if tailscale else "tailscale off"
        if tailscale_state and str(tailscale_state).lower() not in {"unknown", ""}:
            label += f"/{_shorten(tailscale_state, limit=18)}"
        parts.append(label)
    elif tailscale_state:
        parts.append(f"tailscale {_shorten(tailscale_state, limit=18)}")
    if network.get("tailscale_ip_masked"):
        parts.append(str(network.get("tailscale_ip_masked")))
    return " · ".join(parts) if parts else "rede n/a"


def _role_text(roles: list[str], *, limit: int = 8) -> str:
    selected = [str(role) for role in roles[:limit] if role]
    if not selected:
        return "`nenhuma`"
    return " ".join(f"`{_shorten(role, limit=24)}`" for role in selected)


def _has_online_registry_worker(snapshot: "WorkerSnapshot") -> bool:
    summary = (snapshot.registry_snapshot or {}).get("summary") if isinstance(snapshot.registry_snapshot, dict) else {}
    try:
        return int((summary or {}).get("online") or 0) > 0
    except Exception:
        return False


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
    def __init__(
        self,
        cog: "WorkersCommandMixin",
        *,
        owner_id: int,
        snapshot: WorkerSnapshot,
        selected_worker_id: str = "",
    ):
        super().__init__(timeout=WORKERS_PANEL_TIMEOUT_SECONDS)
        self.cog = cog
        self.owner_id = int(owner_id)
        self.snapshot = snapshot
        self.selected_worker_id = str(selected_worker_id or "")
        self.message: discord.Message | None = None
        self._ensure_selected_worker()
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

    def _registry_workers(self) -> list[dict[str, Any]]:
        registry = self.snapshot.registry_snapshot or {}
        workers = registry.get("workers") if isinstance(registry.get("workers"), list) else []
        return [worker for worker in workers if isinstance(worker, dict)]

    def _has_legacy_worker(self) -> bool:
        return bool(self.snapshot.configured and self.snapshot.online)

    def _worker_choices_exist(self) -> bool:
        return bool(self._registry_workers() or self._has_legacy_worker())

    def _ensure_selected_worker(self) -> None:
        workers = self._registry_workers()
        worker_ids = [str(worker.get("worker_id") or "") for worker in workers if worker.get("worker_id")]
        if self.selected_worker_id and (self.selected_worker_id in worker_ids or (self.selected_worker_id == LEGACY_WORKER_ID and self._has_legacy_worker())):
            return
        online = next((worker for worker in workers if worker.get("online") and worker.get("worker_id")), None)
        if isinstance(online, dict):
            self.selected_worker_id = str(online.get("worker_id") or "")
            return
        first = next((worker for worker in workers if worker.get("worker_id")), None)
        if isinstance(first, dict):
            self.selected_worker_id = str(first.get("worker_id") or "")
            return
        self.selected_worker_id = LEGACY_WORKER_ID if self._has_legacy_worker() else ""

    def _selected_worker(self) -> dict[str, Any] | None:
        wanted = str(self.selected_worker_id or "")
        for worker in self._registry_workers():
            if str(worker.get("worker_id") or "") == wanted:
                return worker
        return None

    def _selected_is_legacy(self) -> bool:
        return self.selected_worker_id == LEGACY_WORKER_ID and self._has_legacy_worker()

    def _job_target_worker_id(self) -> str:
        if self._selected_is_legacy():
            return ""
        worker = self._selected_worker()
        if not worker:
            return ""
        return str(worker.get("worker_id") or "")

    def _worker_select_options(self) -> list[discord.SelectOption]:
        options: list[discord.SelectOption] = []
        for worker in self._registry_workers()[:24]:
            worker_id = str(worker.get("worker_id") or "").strip()
            if not worker_id:
                continue
            name = _shorten(worker.get("name") or worker_id, limit=80)
            seen = _format_age(worker.get("last_seen_age_seconds"))
            roles = ", ".join(str(role) for role in (worker.get("roles") or [])[:3]) or "sem roles"
            options.append(discord.SelectOption(
                label=name[:100],
                value=worker_id[:100],
                description=_shorten(f"{('online' if worker.get('online') else 'offline')} · {seen} · {roles}", limit=100),
                emoji="🟢" if worker.get("online") else "🔴",
                default=(worker_id == self.selected_worker_id),
            ))
        if self._has_legacy_worker():
            roles = ", ".join(self.snapshot.roles[:3]) or "phone-worker"
            options.append(discord.SelectOption(
                label=_shorten(self.snapshot.name or "phone-worker direto", limit=100),
                value=LEGACY_WORKER_ID,
                description=_shorten(f"direto online · {roles}", limit=100),
                emoji="🟢",
                default=(self.selected_worker_id == LEGACY_WORKER_ID),
            ))
        return options

    def _action_select_options(self) -> list[discord.SelectOption]:
        return [
            discord.SelectOption(label="Testar worker", value="ping", description="Cria um ping seguro no worker selecionado", emoji="🧪"),
            discord.SelectOption(label="Saúde", value="worker_self_check", description="Coleta bateria, rede, Tailscale, serviços e sistema", emoji="🩺"),
            discord.SelectOption(label="Logs", value="worker_logs", description="Busca logs recentes do phone-worker", emoji="📜"),
            discord.SelectOption(label="Tailscale", value="tailscale_status", description="Verifica Tailscale e alcance da VPS", emoji="🌐"),
            discord.SelectOption(label="Status serviços", value="service_status", description="Mostra status dos serviços permitidos", emoji="🧰"),
            discord.SelectOption(label="Iniciar watchdog", value="service_start_watch", description="Inicia phone-worker-watch", emoji="▶️"),
            discord.SelectOption(label="Parar watchdog", value="service_stop_watch", description="Para phone-worker-watch", emoji="⏹️"),
            discord.SelectOption(label="Reiniciar worker", value="service_restart_worker", description="Reinicia phone-worker depois de responder o job", emoji="🔁"),
            discord.SelectOption(label="Parar worker", value="service_stop_worker", description="Para phone-worker depois de responder o job", emoji="🛑"),
        ]

    def _new_select(self, **kwargs: Any):
        select_cls = getattr(discord.ui, "StringSelect", None) or getattr(discord.ui, "Select")
        return select_cls(**kwargs)

    def _rebuild_layout(self, *, expired: bool = False) -> None:
        self._clear_items()
        snapshot = self.snapshot
        if expired:
            self.add_item(discord.ui.Container(
                discord.ui.TextDisplay(
                    "# 📱 Core Workers\n"
                    "Painel expirado. Use `workers`, `worker` ou `w` para abrir novamente."
                ),
                accent_color=discord.Color.dark_grey(),
            ))
            return

        self._ensure_selected_worker()
        summary = (snapshot.registry_snapshot or {}).get("summary") if isinstance(snapshot.registry_snapshot, dict) else {}
        queued = int((summary or {}).get('jobs_queued') or 0)
        running = int((summary or {}).get('jobs_running') or 0)
        registered = int((summary or {}).get('registered') or 0)
        online = int((summary or {}).get('online') or 0)
        pairings = int((summary or {}).get('pairings_active') or 0)
        jobs_label = f"`{queued}` pend · `{running}` rod"
        if queued and not online and not self._has_legacy_worker():
            jobs_label += " · sem worker"
        header = discord.ui.TextDisplay(
            "# 📱 Core Workers\n"
            f"-# privado · `workers` / `worker` / `w` · guild `{WORKERS_COMMAND_GUILD_ID}`\n"
            f"**Estado:** {snapshot.state_label}\n"
            f"**Registry:** `{registered}` reg · `{online}` online · `{pairings}` pair\n"
            f"**Jobs:** {jobs_label}"
        )

        worker_options = self._worker_select_options()
        worker_select = None
        action_select = None
        if worker_options:
            worker_select = self._new_select(
                placeholder="Escolha um worker",
                min_values=1,
                max_values=1,
                options=worker_options,
                disabled=False,
            )
            worker_select.callback = self._select_worker

            action_select = self._new_select(
                placeholder="Escolha uma ação segura",
                min_values=1,
                max_values=1,
                options=self._action_select_options(),
                disabled=False,
            )
            action_select.callback = self._select_action

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

        cleanup_jobs = discord.ui.Button(label="Limpar jobs", emoji="🧹", style=discord.ButtonStyle.secondary)
        cleanup_jobs.callback = self._cleanup_jobs

        components: list[Any] = [
            header,
            discord.ui.Separator(),
            discord.ui.TextDisplay("\n".join(self._selected_worker_lines(snapshot))),
        ]
        if worker_select is not None and action_select is not None:
            components.extend([
                discord.ui.Separator(),
                discord.ui.ActionRow(worker_select),
                discord.ui.ActionRow(action_select),
            ])
        components.append(discord.ui.ActionRow(refresh, pairing, wake, cleanup_jobs))
        container = discord.ui.Container(*components, accent_color=snapshot.accent)
        self.add_item(container)

    def _selected_worker_lines(self, snapshot: WorkerSnapshot) -> list[str]:
        if snapshot.registry_error:
            return [
                "## Worker",
                f"Registry indisponível: `{_shorten(_redact(snapshot.registry_error), limit=120)}`",
            ]

        workers = self._registry_workers()
        worker = self._selected_worker()
        lines = ["## Worker"]
        if worker:
            icon = "🟢" if worker.get("online") else "🔴"
            name = _shorten(worker.get("name") or worker.get("worker_id") or "Core Worker", limit=36)
            worker_id = _shorten(worker.get("worker_id"), limit=24)
            seen = _format_age(worker.get("last_seen_age_seconds"))
            roles = _role_text([str(r) for r in (worker.get("roles") or [])], limit=5)
            lines.append(f"{icon} **{name}** · `{worker_id}`")
            lines.append(f"-# visto {seen} · {_battery_text(worker)} · {_network_text(worker)}")
            lines.append(f"Roles: {roles}")
        elif self._selected_is_legacy():
            roles = _role_text(snapshot.roles, limit=6)
            lines.append(f"🟢 **{_shorten(snapshot.name or 'phone-worker direto', limit=36)}** · `direto`")
            lines.append(f"-# phone-worker antigo online via endpoint local/Tailscale")
            lines.append(f"Roles: {roles}")
        elif workers:
            lines.append("Selecione um worker.")
        elif snapshot.configured:
            legacy_state = "🟢 direto online" if snapshot.online else "🔴 direto offline"
            lines.append(f"{legacy_state}. Nenhum APK pareado ainda.")
        else:
            lines.append("Nenhum worker configurado ou pareado.")

        if snapshot.action_note:
            lines.append(f"-# Última ação: {_shorten(_redact(snapshot.action_note), limit=110)}")
        if snapshot.watch_output:
            lines.append(f"-# Watchdog: `{_shorten(_redact(snapshot.watch_output), limit=90)}`")
        return lines

    async def _select_worker(self, interaction: discord.Interaction):
        values = list(getattr(getattr(interaction, "data", None), "get", lambda _k, _d=None: _d)("values", []) or [])
        if not values:
            with contextlib.suppress(Exception):
                values = list(getattr(interaction, "values", []) or [])
        if values:
            self.selected_worker_id = str(values[0] or "")
        self._ensure_selected_worker()
        self._rebuild_layout()
        await interaction.response.edit_message(view=self)

    async def _select_action(self, interaction: discord.Interaction):
        values = list(getattr(getattr(interaction, "data", None), "get", lambda _k, _d=None: _d)("values", []) or [])
        if not values:
            with contextlib.suppress(Exception):
                values = list(getattr(interaction, "values", []) or [])
        action = str((values or [""])[0] or "")
        mapping: dict[str, tuple[str, dict[str, Any], str]] = {
            "ping": ("ping", {}, "teste manual pelo painel workers"),
            "worker_self_check": ("worker_self_check", {}, "saúde completa pelo painel workers"),
            "worker_logs": ("worker_logs", {"lines": 140}, "logs recentes do phone-worker"),
            "tailscale_status": ("tailscale_status", {}, "status Tailscale e alcance da VPS"),
            "service_status": ("service_status", {"service": "phone-worker"}, "status de serviços do celular"),
            "service_start_watch": ("service_start", {"service": "phone-worker-watch"}, "iniciar watchdog do phone-worker"),
            "service_stop_watch": ("service_stop", {"service": "phone-worker-watch"}, "parar watchdog do phone-worker"),
            "service_restart_worker": ("service_restart", {"service": "phone-worker"}, "reiniciar phone-worker no celular"),
            "service_stop_worker": ("service_stop", {"service": "phone-worker"}, "parar phone-worker no celular"),
        }
        job = mapping.get(action)
        if job is None:
            await interaction.response.send_message("Ação inválida.", ephemeral=True)
            return
        job_type, payload, summary = job
        await self._queue_named_job(interaction, job_type=job_type, payload=payload, summary=summary)

    async def _refresh(self, interaction: discord.Interaction):
        await interaction.response.defer(thinking=False)
        self.snapshot = await self.cog._collect_workers_snapshot()
        self._ensure_selected_worker()
        self._rebuild_layout()
        await interaction.edit_original_response(view=self, allowed_mentions=discord.AllowedMentions.none())

    async def _wake_worker(self, interaction: discord.Interaction):
        await interaction.response.defer(thinking=False)
        snapshot = await self.cog._wake_phone_worker()
        self.snapshot = snapshot
        self._ensure_selected_worker()
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
                "No APK/agent, use esse código na rota:\n"
                f"`POST {base_url}/core-worker/pair`\n\n"
                "O token do worker é entregue uma única vez e deve ficar só no celular."
            )
            self.snapshot = await self.cog._collect_workers_snapshot(action_note=f"código de pareamento gerado: {code}")
            self._ensure_selected_worker()
            self._rebuild_layout()
            await interaction.edit_original_response(view=self, allowed_mentions=discord.AllowedMentions.none())
            await interaction.followup.send(msg, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
        except Exception as exc:
            self.snapshot = await self.cog._collect_workers_snapshot(action_note=f"falha ao gerar pareamento: {type(exc).__name__}: {exc}")
            self._ensure_selected_worker()
            self._rebuild_layout()
            await interaction.edit_original_response(view=self, allowed_mentions=discord.AllowedMentions.none())
            await interaction.followup.send("Não consegui gerar o pareamento agora.", ephemeral=True)

    async def _queue_named_job(
        self,
        interaction: discord.Interaction,
        *,
        job_type: str,
        payload: dict[str, Any] | None = None,
        summary: str = "",
    ):
        await interaction.response.defer(thinking=False)
        target_worker_id = self._job_target_worker_id()
        try:
            if self._selected_is_legacy():
                result = await self.cog._run_legacy_worker_action(job_type=job_type, payload=payload or {})
                note = _shorten((result or {}).get("summary") or "ação direta concluída", limit=90)
                self.snapshot = await self.cog._collect_workers_snapshot(action_note=f"direto `{job_type}`: {note}")
            else:
                result = await self.cog._queue_core_worker_job(
                    interaction.user,
                    job_type=job_type,
                    payload=payload or {},
                    summary=summary or job_type,
                    target_worker_id=target_worker_id,
                )
                job = result.get("job") if isinstance(result, dict) else {}
                job_id = _shorten((job or {}).get("job_id"), limit=24)
                target_label = _shorten(target_worker_id or "qualquer worker online", limit=32)
                self.snapshot = await self.cog._collect_workers_snapshot(action_note=f"job `{job_type}` criado para {target_label}: {job_id}")
        except Exception as exc:
            self.snapshot = await self.cog._collect_workers_snapshot(action_note=f"falha em `{job_type}`: {type(exc).__name__}: {exc}")
        self._ensure_selected_worker()
        self._rebuild_layout()
        await interaction.edit_original_response(view=self, allowed_mentions=discord.AllowedMentions.none())

    async def _cleanup_jobs(self, interaction: discord.Interaction):
        await interaction.response.defer(thinking=False)
        try:
            result = await self.cog._cleanup_core_worker_jobs()
            self.snapshot = await self.cog._collect_workers_snapshot(action_note=f"limpeza de jobs: {result}")
        except Exception as exc:
            self.snapshot = await self.cog._collect_workers_snapshot(action_note=f"falha limpando jobs: {type(exc).__name__}: {exc}")
        self._ensure_selected_worker()
        self._rebuild_layout()
        await interaction.edit_original_response(view=self, allowed_mentions=discord.AllowedMentions.none())


class WorkersCommandMixin:
    """Comando prefixado `workers`/`worker`/`w` da cog Utility.

    Painel único em Components V2 para o Core Worker. O comando é privado do
    dono do bot e limitado à guild configurada; não é mais slash command.
    """

    async def _can_use_workers_author(self, user: discord.abc.User) -> bool:
        with contextlib.suppress(Exception):
            return bool(await self.bot.is_owner(user))
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

    def _request_worker_task_sync(self, task: str, payload: dict[str, Any] | None = None, *, timeout: float) -> dict[str, Any]:
        enabled, configured, host, port, scheme, _name = self._worker_base_config()
        if not enabled:
            raise RuntimeError("PHONE_WORKER_ENABLED=false")
        if not configured:
            raise RuntimeError("PHONE_WORKER_HOST não configurado")
        token = str(os.getenv("PHONE_WORKER_TOKEN") or "").strip()
        body = dict(payload or {})
        body["task"] = str(task or "").strip()
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(f"{scheme}://{host}:{port}/task", data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=max(0.8, timeout)) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            body_text = exc.read().decode("utf-8", errors="replace")[:240]
            raise RuntimeError(f"HTTP {exc.code}: {_redact(body_text)}") from exc
        parsed = json.loads(raw.decode("utf-8", errors="replace"))
        if not isinstance(parsed, dict):
            raise RuntimeError("resposta não é JSON object")
        return parsed

    async def _run_legacy_worker_action(self, *, job_type: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        kind = str(job_type or "").strip().lower().replace("-", "_")
        if kind in {"ping", "status", "diagnostic_basic", "worker_self_check"}:
            timeout = max(1.0, _env_float("WORKERS_PANEL_STATUS_TIMEOUT_SECONDS", 3.0))
            result = await asyncio.to_thread(self._request_worker_status_sync, timeout=timeout)
            result.setdefault("summary", "status direto coletado")
            return result
        timeout = max(3.0, _env_float("WORKERS_PANEL_DIRECT_TASK_TIMEOUT_SECONDS", 18.0))
        direct_tasks = {
            "network_probe",
            "tailscale_status",
            "worker_logs",
            "service_status",
            "service_start",
            "service_stop",
            "service_restart",
            "ffmpeg_check",
            "ffprobe_check",
        }
        if kind not in direct_tasks:
            raise RuntimeError("ação direta não suportada pelo phone-worker legado")
        return await asyncio.to_thread(self._request_worker_task_sync, kind, payload or {}, timeout=timeout)

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

    async def _queue_core_worker_job(
        self,
        owner: discord.abc.User,
        *,
        job_type: str,
        payload: dict[str, Any] | None = None,
        summary: str = "",
        target_worker_id: str = "",
    ) -> dict[str, Any]:
        registry = get_core_workers_registry()
        if not target_worker_id:
            snapshot = await asyncio.to_thread(registry.snapshot)
            workers = snapshot.get("workers") if isinstance(snapshot.get("workers"), list) else []
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
        return await asyncio.to_thread(registry.cleanup_jobs, clear_active=True)

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

    async def _send_workers_panel_from_context(self, ctx: commands.Context) -> None:
        if ctx.guild is None or int(getattr(ctx.guild, "id", 0) or 0) != WORKERS_COMMAND_GUILD_ID:
            with contextlib.suppress(Exception):
                await ctx.message.delete()
            return
        if not await self._can_use_workers_author(ctx.author):
            with contextlib.suppress(Exception):
                await ctx.message.delete()
            return

        with contextlib.suppress(Exception):
            await ctx.message.delete()

        snapshot = await self._collect_workers_snapshot()
        view = WorkersPanelView(self, owner_id=int(ctx.author.id), snapshot=snapshot)
        message = await ctx.send(view=view, allowed_mentions=discord.AllowedMentions.none())
        view.message = message

    @commands.command(name="workers", aliases=["worker", "w"], hidden=True)
    async def workers(self, ctx: commands.Context):
        await self._send_workers_panel_from_context(ctx)
