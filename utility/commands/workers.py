from __future__ import annotations

import asyncio
import base64
import hashlib
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

WORKER_ACTION_SPECS: tuple[dict[str, Any], ...] = (
    {"label": "Testar worker", "value": "ping", "job_type": "ping", "payload": {}, "summary": "teste manual pelo painel workers", "description": "Ping seguro no worker selecionado", "emoji": "🧪"},
    {"label": "Saúde", "value": "worker_self_check", "job_type": "worker_self_check", "payload": {}, "summary": "saúde completa pelo painel workers", "description": "Bateria, rede, Tailscale e sistema", "emoji": "🩺"},
    {"label": "Atualizar agent", "value": "worker_update", "job_type": "worker_update", "payload": {}, "summary": "atualizar arquivos do phone-worker", "description": "Aplica a versão atual e reinicia", "emoji": "⬆️", "requires_declared": True},
    {"label": "Reparar scripts", "value": "worker_repair_scripts", "job_type": "worker_update", "payload": {"scripts_only": True}, "summary": "reinstalar scripts auxiliares do worker", "description": "Reinstala start/watch/pair", "emoji": "🛠️", "requires_declared": True},
    {"label": "Logs", "value": "worker_logs", "job_type": "worker_logs", "payload": {"lines": 140}, "summary": "logs recentes do phone-worker", "description": "Busca logs recentes", "emoji": "📜"},
    {"label": "Tailscale", "value": "tailscale_status", "job_type": "tailscale_status", "payload": {}, "summary": "status Tailscale e alcance da VPS", "description": "Verifica Tailscale e VPS", "emoji": "🌐"},
    {"label": "Status serviços", "value": "service_status", "job_type": "service_status", "payload": {"service": "phone-worker"}, "summary": "status de serviços do celular", "description": "Mostra serviços permitidos", "emoji": "🧰"},
    {"label": "Iniciar watchdog", "value": "service_start_watch", "job_type": "service_start", "payload": {"service": "phone-worker-watch"}, "summary": "iniciar watchdog do phone-worker", "description": "Inicia phone-worker-watch", "emoji": "▶️"},
    {"label": "Parar watchdog", "value": "service_stop_watch", "job_type": "service_stop", "payload": {"service": "phone-worker-watch"}, "summary": "parar watchdog do phone-worker", "description": "Para phone-worker-watch", "emoji": "⏹️"},
    {"label": "Reiniciar worker", "value": "service_restart_worker", "job_type": "service_restart", "payload": {"service": "phone-worker"}, "summary": "reiniciar phone-worker no celular", "description": "Reinicia após responder", "emoji": "🔁"},
    {"label": "Parar worker", "value": "service_stop_worker", "job_type": "service_stop", "payload": {"service": "phone-worker"}, "summary": "parar phone-worker no celular", "description": "Para após responder", "emoji": "🛑"},
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
        return "bat n/a"
    level = battery.get("level") or battery.get("percent") or battery.get("percentage")
    charging = battery.get("charging")
    parts: list[str] = []
    try:
        if level is not None:
            parts.append(f"{int(float(level))}%")
    except Exception:
        pass
    if isinstance(charging, bool):
        parts.append("⚡" if charging else "🔋")
    elif charging is not None:
        parts.append(_shorten(charging, limit=12))
    try:
        temp = battery.get("temperature_c") or battery.get("temperature")
        if temp is not None:
            temp_f = float(temp)
            if 0 < temp_f < 90:
                parts.append(f"{temp_f:.0f}°C")
    except Exception:
        pass
    return " ".join(parts) if parts else "bat n/a"


def _network_text(worker: dict[str, Any]) -> str:
    network = worker.get("network") if isinstance(worker.get("network"), dict) else {}
    if not network:
        return "rede n/a"
    kind = network.get("type") or network.get("kind") or network.get("transport") or network.get("name")
    tailscale = network.get("tailscale")
    tailscale_state = str(network.get("tailscale_state") or "").strip().lower()
    tailscale_cli = bool(network.get("tailscale_cli"))
    via_vps = bool(network.get("tailscale_via_vps_url"))
    parts: list[str] = []
    if kind and str(kind).lower() not in {"unknown", ""}:
        label = "rede ok" if str(kind).lower() == "connected" else _shorten(kind, limit=16)
        parts.append(label)
    if isinstance(tailscale, bool):
        if tailscale:
            if tailscale_cli:
                label = "ts cli ok"
            elif via_vps or tailscale_state in {"app/vpn", "vpn", "app"}:
                label = "ts app ok"
            else:
                label = "ts ok"
        else:
            label = "ts n/a" if not tailscale_cli else "ts off"
        if tailscale_state and tailscale_state not in {"unknown", "no-cli", "app/vpn"}:
            label += f"/{_shorten(tailscale_state, limit=12)}"
        parts.append(label)
    elif tailscale_state and tailscale_state not in {"unknown", ""}:
        parts.append(f"ts {_shorten(tailscale_state, limit=14)}")
    if network.get("tailscale_ip_masked"):
        parts.append(str(network.get("tailscale_ip_masked")))
    return " · ".join(parts) if parts else "rede n/a"


def _script_health_label(worker: dict[str, Any]) -> str:
    status = worker.get("status") if isinstance(worker.get("status"), dict) else {}
    health = worker.get("health") if isinstance(worker.get("health"), dict) else {}
    scripts = status.get("scripts") if isinstance(status.get("scripts"), dict) else {}
    if not scripts and isinstance(health.get("scripts"), dict):
        scripts = health.get("scripts")
    if not scripts:
        scripts_ok = health.get("scripts_ok")
        if scripts_ok is True:
            return "scripts ok"
        if scripts_ok is False:
            return "scripts incompletos"
        return "scripts n/a"
    complete = scripts.get("complete")
    mirrored = scripts.get("mirrored")
    if complete and mirrored:
        return "scripts ok"
    if complete:
        return "scripts ok parcial"
    return "scripts incompletos"




def _queue_status_text(worker: dict[str, Any]) -> str:
    status = worker.get("status") if isinstance(worker.get("status"), dict) else {}
    queue = status.get("core_worker_jobs") if isinstance(status.get("core_worker_jobs"), dict) else {}
    if not queue:
        return ""

    state = str(queue.get("last_poll_state") or "").strip().lower()
    if state == "no_compatible_job":
        reason = _shorten(queue.get("last_poll_reason"), limit=60)
        return f"sem job compatível{f' · {reason}' if reason else ''}"
    # Estados normais como "poll agora · sem job" poluem o card principal.
    # Detalhes completos ficam em "Ver último resultado"/logs.
    return ""

def _role_text(roles: list[str], *, limit: int = 8) -> str:
    selected = [str(role) for role in roles[:limit] if role]
    if not selected:
        return "`nenhuma`"
    return " ".join(f"`{_shorten(role, limit=24)}`" for role in selected)


def _task_name(value: object) -> str:
    return re.sub(r"[^a-z0-9_]+", "_", str(value or "").strip().lower().replace("-", "_")).strip("_")


def _task_set(value: object) -> set[str]:
    if isinstance(value, (list, tuple, set)):
        raw_items = value
    elif isinstance(value, str):
        raw_items = value.replace(";", ",").split(",")
    else:
        raw_items = []
    result: set[str] = set()
    for item in raw_items:
        clean = _task_name(item)
        if clean:
            result.add(clean)
    return result


def _compact_failure(exc: BaseException) -> str:
    text = _redact(str(exc) or type(exc).__name__)
    match = re.search(r"HTTP\s+(\d+)\s*:\s*(\{.*\})", text, flags=re.IGNORECASE | re.DOTALL)
    if match:
        code = match.group(1)
        try:
            data = json.loads(match.group(2))
        except Exception:
            data = {}
        error = str((data or {}).get("error") or "").strip()
        if error:
            lowered = error.lower()
            if "task não suportada" in lowered or "task nao suportada" in lowered:
                return f"worker desatualizado/sem suporte para essa ação (HTTP {code})"
            return f"HTTP {code}: {_shorten(error, limit=90)}"
    lowered = text.lower()
    if "task não suportada" in lowered or "task nao suportada" in lowered:
        return "worker desatualizado/sem suporte para essa ação"
    return _shorten(text, limit=110)


def _job_result_note(job_type: object, job: dict[str, Any] | None) -> str:
    kind = _task_name(job_type)
    if not isinstance(job, dict) or not job:
        return f"`{kind}` enviado; aguardando resultado"
    status = str(job.get("status") or "queued").strip().lower()
    result = job.get("result") if isinstance(job.get("result"), dict) else {}
    raw_summary = result.get("summary") or job.get("summary") or job.get("error") or status
    summary = _shorten(raw_summary, limit=72)
    if summary.strip().lower() in {"succeeded", "success", "ok", "true"}:
        summary = ""
    suffix = f": {summary}" if summary else ""
    if status == "succeeded":
        return f"✅ `{kind}` concluído{suffix}"
    if status == "failed":
        return f"❌ `{kind}` falhou{suffix}"
    if status == "running":
        return f"⏳ `{kind}` em execução"
    if status == "queued":
        return f"⏳ `{kind}` aguardando worker"
    return f"`{kind}`{suffix or f': {status}'}"


def _job_detail_text(job: dict[str, Any] | None) -> str:
    if not isinstance(job, dict) or not job:
        return "Nenhum resultado recente encontrado para este worker."
    status = str(job.get("status") or "desconhecido")
    kind = str(job.get("type") or "job")
    result = job.get("result") if isinstance(job.get("result"), dict) else {}
    lines = [
        f"## Resultado do worker",
        f"Tipo: `{_shorten(kind, limit=48)}`",
        f"Status: `{_shorten(status, limit=32)}`",
    ]
    summary = result.get("summary") or job.get("summary") or job.get("error")
    if summary:
        lines.append(f"Resumo: {_shorten(_redact(summary), limit=240)}")
    if job.get("error"):
        lines.append(f"Erro: `{_shorten(_redact(job.get('error')), limit=220)}`")
    interesting_keys = [
        "version", "target_version", "scripts", "battery", "network", "tailscale",
        "services", "ffmpeg", "ffprobe", "lines", "error_lines", "path",
    ]
    shown = 0
    for key in interesting_keys:
        if key not in result:
            continue
        value = result.get(key)
        if isinstance(value, (dict, list)):
            text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        else:
            text = str(value)
        lines.append(f"`{key}`: {_shorten(_redact(text), limit=420)}")
        shown += 1
        if shown >= 6:
            break
    tail = result.get("tail")
    if isinstance(tail, str) and tail.strip():
        tail = _redact(tail.strip())
        lines.append("```txt\n" + tail[-1400:] + "\n```")
    return "\n".join(lines)[:1900]


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
        # O phone-worker direto é só fallback. Quando há worker registrado online,
        # o painel deve focar no registry para evitar duplicidade visual.
        return bool(self.snapshot.configured and self.snapshot.online and not _has_online_registry_worker(self.snapshot))

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

    def _selected_supported_tasks(self) -> set[str] | None:
        if self._selected_is_legacy():
            status = self.snapshot.status if isinstance(self.snapshot.status, dict) else {}
            supported = _task_set(status.get("supported_tasks"))
            if supported:
                return supported
            # Worker legado antigo: liberar só ações que não dependem do /task novo.
            return {"ping", "status", "worker_self_check", "diagnostic_basic"}
        worker = self._selected_worker()
        if not worker:
            return set()
        supported = _task_set(worker.get("supported_tasks"))
        if not supported:
            health = worker.get("health") if isinstance(worker.get("health"), dict) else {}
            status = worker.get("status") if isinstance(worker.get("status"), dict) else {}
            supported = _task_set(health.get("supported_tasks")) or _task_set(status.get("supported_tasks"))
        # None = worker registrado antigo/externo sem declarar suporte; manter ações visíveis.
        return supported or None

    def _action_specs_for_selected(self) -> list[dict[str, Any]]:
        supported = self._selected_supported_tasks()
        specs: list[dict[str, Any]] = []
        # Ações do próprio painel ficam no select para manter o card compacto.
        specs.append({"label": "Parear novo worker", "value": "_create_pairing", "description": "Gera código temporário", "emoji": "🔐", "panel_action": "pair"})
        specs.append({"label": "Limpar jobs", "value": "_cleanup_jobs", "description": "Remove jobs travados/antigos", "emoji": "🧹", "panel_action": "cleanup"})
        if self._selected_worker() is not None:
            specs.append({"label": "Ver último resultado", "value": "_show_last_result", "description": "Mostra detalhes completos", "emoji": "📄", "panel_action": "last_result"})
            specs.append({"label": "Renomear worker", "value": "_rename_worker", "description": "Troca o nome exibido", "emoji": "✏️", "panel_action": "rename"})
        for spec in WORKER_ACTION_SPECS:
            job_type = _task_name(spec.get("job_type"))
            requires_declared = bool(spec.get("requires_declared"))
            if requires_declared and (supported is None or job_type not in supported):
                continue
            if supported is not None and job_type not in supported:
                continue
            specs.append(dict(spec))
        return specs

    def _action_select_options(self) -> list[discord.SelectOption]:
        specs = self._action_specs_for_selected()
        if not specs:
            return [discord.SelectOption(
                label="Worker sem ações compatíveis",
                value="_unsupported",
                description="Atualize/reinicie o phone-worker para liberar ações",
                emoji="⚠️",
            )]
        return [
            discord.SelectOption(
                label=str(spec["label"])[:100],
                value=str(spec["value"])[:100],
                description=_shorten(spec.get("description"), limit=100),
                emoji=str(spec.get("emoji") or "⚙️"),
            )
            for spec in specs[:25]
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
        registry_label = "nenhum worker pareado" if registered <= 0 else f"`{registered}` reg · `{online}` online"
        if pairings:
            registry_label += f" · `{pairings}` pair"
        jobs_label = f"`{queued}` pend · `{running}` rod"
        if queued and not online and not self._has_legacy_worker():
            jobs_label += " · sem worker"
        header = discord.ui.TextDisplay(
            "# 📱 Core Workers\n"
            f"-# privado · `workers` / `worker` / `w` · guild `{WORKERS_COMMAND_GUILD_ID}`\n"
            f"**Estado:** {snapshot.state_label}\n"
            f"**Registry:** {registry_label}\n"
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

            action_options = self._action_select_options()
            action_disabled = bool(action_options and str(action_options[0].value) == "_unsupported")
            action_select = self._new_select(
                placeholder="Escolha uma ação segura" if not action_disabled else "Worker desatualizado/sem ações compatíveis",
                min_values=1,
                max_values=1,
                options=action_options,
                disabled=action_disabled,
            )
            action_select.callback = self._select_action

        refresh = discord.ui.Button(label="Atualizar", emoji="🔄", style=discord.ButtonStyle.primary)
        refresh.callback = self._refresh

        pairing = discord.ui.Button(label="Parear worker", emoji="🔐", style=discord.ButtonStyle.success)
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
            components.append(discord.ui.ActionRow(refresh))
        else:
            components.append(discord.ui.ActionRow(refresh, pairing, cleanup_jobs))
        if not _has_online_registry_worker(snapshot):
            components.append(discord.ui.ActionRow(wake))
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
            version = _shorten(worker.get("version") or "sem versão", limit=24)
            lines.append(f"{icon} **{name}** · `{worker_id}`")
            lines.append(f"-# visto {seen} · v `{version}` · {_battery_text(worker)} · {_network_text(worker)} · {_script_health_label(worker)}")
            lines.append(f"Roles: {roles}")
            queue_text = _queue_status_text(worker)
            if queue_text:
                lines.append(f"-# Fila: {queue_text}")
        elif self._selected_is_legacy():
            roles = _role_text(snapshot.roles, limit=6)
            version = _shorten((snapshot.status or {}).get("version") or "sem versão", limit=24)
            lines.append(f"🟢 **{_shorten(snapshot.name or 'phone-worker direto', limit=36)}** · `direto`")
            status = snapshot.status if isinstance(snapshot.status, dict) else {}
            lines.append(f"-# endpoint local/Tailscale · v `{version}` · {_script_health_label({'status': status})}")
            lines.append(f"Roles: {roles}")
        elif workers:
            lines.append("Selecione um worker.")
        elif snapshot.configured:
            legacy_state = "🟢 direto online" if snapshot.online else "🔴 direto offline"
            lines.append(f"{legacy_state}. Nenhum worker pareado ainda.")
        else:
            lines.append("Nenhum worker configurado ou pareado.")

        supported = self._selected_supported_tasks()
        if self._selected_is_legacy() and supported is not None and "service_status" not in supported:
            lines.append("-# Ações avançadas ocultas: phone-worker legado/desatualizado.")
        elif supported == set():
            lines.append("-# Nenhuma ação compatível declarada por este worker.")

        if snapshot.action_note:
            lines.append(f"-# Última ação: {_shorten(_redact(snapshot.action_note), limit=80)}")
        # Saídas completas de watchdog/sync ficam fora do painel principal para manter o card compacto.
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
        if action == "_unsupported":
            await interaction.response.send_message("Esse worker está desatualizado ou não declarou suporte para ações seguras.", ephemeral=True)
            return
        if action == "_create_pairing":
            await self._create_pairing(interaction)
            return
        if action == "_cleanup_jobs":
            await self._cleanup_jobs(interaction)
            return
        if action == "_show_last_result":
            await self._show_last_result(interaction)
            return
        if action == "_rename_worker":
            await self._open_rename_modal(interaction)
            return
        specs = {str(spec.get("value")): spec for spec in self._action_specs_for_selected()}
        spec = specs.get(action)
        if spec is None:
            await interaction.response.send_message("Ação indisponível para esse worker.", ephemeral=True)
            return
        await self._queue_named_job(
            interaction,
            job_type=str(spec.get("job_type") or action),
            payload=dict(spec.get("payload") or {}),
            summary=str(spec.get("summary") or spec.get("label") or action),
        )

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

    async def _sync_worker(self, interaction: discord.Interaction):
        await interaction.response.defer(thinking=False)
        snapshot = await self.cog._sync_phone_worker()
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
            base_url = _public_base_url() or "http://IP_TAILSCALE_DA_VPS:10000"
            msg = (
                "## 🔐 Pareamento Core Worker\n"
                f"Código temporário: `{code}`\n"
                f"Validade: `{ttl}` · expira em `{expires}`\n\n"
                "No phone-worker/Termux atualizado, rode:\n"
                f"`~/phone-worker/pair-phone-worker.sh {code} {base_url}`\n\n"
                "Ou pelo Python:\n"
                f"`cd ~/phone-worker && python phone_worker.py --pair {code} --vps-url {base_url}`\n\n"
                "O token é salvo automaticamente em `~/.phone-worker.env` e não aparece no GitHub."
            )
            self.snapshot = await self.cog._collect_workers_snapshot(action_note=f"código de pareamento gerado: {code}")
            self._ensure_selected_worker()
            self._rebuild_layout()
            await interaction.edit_original_response(view=self, allowed_mentions=discord.AllowedMentions.none())
            await interaction.followup.send(msg, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
        except Exception as exc:
            self.snapshot = await self.cog._collect_workers_snapshot(action_note=f"falha ao gerar pareamento: {_compact_failure(exc)}")
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
            if _task_name(job_type) == "worker_update":
                payload = await self.cog._build_worker_update_payload(scripts_only=bool((payload or {}).get("scripts_only")))
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
                job_id = str((job or {}).get("job_id") or "")
                target_label = _shorten(target_worker_id or "worker online", limit=32)
                self.snapshot = await self.cog._collect_workers_snapshot(action_note=f"⏳ `{job_type}` enviado para {target_label}; aguardando resultado")
                self._ensure_selected_worker()
                self._rebuild_layout()
                await interaction.edit_original_response(view=self, allowed_mentions=discord.AllowedMentions.none())
                final_job = await self.cog._wait_core_worker_job(job_id, timeout=_env_float("WORKERS_PANEL_ACTION_WAIT_SECONDS", 12.0)) if job_id else None
                self.snapshot = await self.cog._collect_workers_snapshot(action_note=_job_result_note(job_type, final_job))
        except Exception as exc:
            self.snapshot = await self.cog._collect_workers_snapshot(action_note=f"falha em `{job_type}`: {_compact_failure(exc)}")
        self._ensure_selected_worker()
        self._rebuild_layout()
        await interaction.edit_original_response(view=self, allowed_mentions=discord.AllowedMentions.none())

    async def _show_last_result(self, interaction: discord.Interaction):
        worker_id = self._job_target_worker_id()
        if not worker_id:
            await interaction.response.send_message("Selecione um worker registrado para ver resultados.", ephemeral=True)
            return
        try:
            job = await self.cog._latest_core_worker_job(worker_id)
            await interaction.response.send_message(_job_detail_text(job), ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
        except Exception as exc:
            await interaction.response.send_message(f"Não consegui buscar o resultado: {_compact_failure(exc)}", ephemeral=True)

    async def _open_rename_modal(self, interaction: discord.Interaction):
        worker = self._selected_worker()
        if not worker:
            await interaction.response.send_message("Selecione um worker registrado para renomear.", ephemeral=True)
            return
        view = self
        current_name = _shorten(worker.get("name") or worker.get("worker_id") or "Core Worker", limit=64)
        worker_id = str(worker.get("worker_id") or "")

        class RenameWorkerModal(discord.ui.Modal, title="Renomear Core Worker"):
            new_name = discord.ui.TextInput(
                label="Novo nome",
                placeholder="Ex.: Redmi Worker",
                default=current_name[:100],
                min_length=2,
                max_length=64,
                required=True,
            )

            async def on_submit(self, modal_interaction: discord.Interaction) -> None:
                await modal_interaction.response.defer(thinking=False)
                try:
                    await view.cog._rename_core_worker(worker_id, str(self.new_name.value or ""))
                    view.snapshot = await view.cog._collect_workers_snapshot(action_note=f"worker renomeado para {_shorten(self.new_name.value, limit=40)}")
                except Exception as exc:
                    view.snapshot = await view.cog._collect_workers_snapshot(action_note=f"falha ao renomear: {_compact_failure(exc)}")
                view._ensure_selected_worker()
                view._rebuild_layout()
                if view.message is not None:
                    await view.message.edit(view=view, allowed_mentions=discord.AllowedMentions.none())
                with contextlib.suppress(Exception):
                    await modal_interaction.followup.send("Nome do worker atualizado.", ephemeral=True)

        await interaction.response.send_modal(RenameWorkerModal())

    async def _cleanup_jobs(self, interaction: discord.Interaction):
        await interaction.response.defer(thinking=False)
        try:
            result = await self.cog._cleanup_core_worker_jobs()
            self.snapshot = await self.cog._collect_workers_snapshot(action_note=f"limpeza de jobs: {result}")
        except Exception as exc:
            self.snapshot = await self.cog._collect_workers_snapshot(action_note=f"falha limpando jobs: {_compact_failure(exc)}")
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
            body_text = exc.read().decode("utf-8", errors="replace")[:1024]
            try:
                parsed_error = json.loads(body_text or "{}")
            except Exception:
                parsed_error = {}
            error_text = str((parsed_error or {}).get("error") or body_text or "erro HTTP").strip()
            if "task não suportada" in error_text.lower() or "task nao suportada" in error_text.lower():
                raise RuntimeError(f"HTTP {exc.code}: worker legado/desatualizado não suporta `{task}`") from exc
            raise RuntimeError(f"HTTP {exc.code}: {_shorten(_redact(error_text), limit=120)}") from exc
        parsed = json.loads(raw.decode("utf-8", errors="replace"))
        if not isinstance(parsed, dict):
            raise RuntimeError("resposta não é JSON object")
        if parsed.get("ok") is False:
            raise RuntimeError(_shorten(_redact(parsed.get("error") or "worker retornou erro"), limit=120))
        return parsed

    def _build_worker_update_payload_sync(self, *, scripts_only: bool = False) -> dict[str, Any]:
        src_dir = _repo_root() / "deploy" / "termux" / "phone-worker"
        files: list[dict[str, Any]] = []
        all_targets = (
            ("phone_worker.py", 0o755),
            ("start-phone-worker.sh", 0o755),
            ("watch-phone-worker.sh", 0o755),
            ("pair-phone-worker.sh", 0o755),
            ("install.sh", 0o755),
            ("README.md", 0o644),
            ("phone-worker.env.example", 0o600),
        )
        script_targets = (
            ("start-phone-worker.sh", 0o755),
            ("watch-phone-worker.sh", 0o755),
            ("pair-phone-worker.sh", 0o755),
            ("install.sh", 0o755),
        )
        targets = script_targets if scripts_only else all_targets
        version = ""
        version_re = re.compile(r'^PHONE_WORKER_VERSION\s*=\s*["\\\']([^"\\\']+)["\\\']', re.MULTILINE)
        for name, mode in targets:
            path = src_dir / name
            if not path.is_file():
                continue
            data = path.read_bytes()
            if name == "phone_worker.py":
                with contextlib.suppress(Exception):
                    match = version_re.search(data.decode("utf-8", errors="ignore"))
                    if match:
                        version = match.group(1)
            files.append({
                "target": name,
                "mode": mode,
                "sha256": hashlib.sha256(data).hexdigest(),
                "data_b64": base64.b64encode(data).decode("ascii"),
            })
        if not files:
            raise RuntimeError("arquivos do phone-worker não encontrados em deploy/termux/phone-worker")
        return {"version": version or "desconhecida", "restart": not scripts_only, "scripts_only": scripts_only, "files": files}

    async def _build_worker_update_payload(self, *, scripts_only: bool = False) -> dict[str, Any]:
        return await asyncio.to_thread(self._build_worker_update_payload_sync, scripts_only=scripts_only)

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
            "worker_update",
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

    async def _get_core_worker_job(self, job_id: str) -> dict[str, Any] | None:
        if not job_id:
            return None
        registry = get_core_workers_registry()
        try:
            result = await asyncio.to_thread(registry.get_job, job_id)
        except Exception:
            return None
        job = result.get("job") if isinstance(result, dict) else None
        return job if isinstance(job, dict) else None

    async def _wait_core_worker_job(self, job_id: str, *, timeout: float = 7.0) -> dict[str, Any] | None:
        deadline = time.monotonic() + max(0.5, float(timeout or 0))
        last: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            job = await self._get_core_worker_job(job_id)
            if isinstance(job, dict):
                last = job
                if str(job.get("status") or "").lower() in {"succeeded", "failed", "expired"}:
                    return job
            await asyncio.sleep(0.8)
        return last

    async def _latest_core_worker_job(self, worker_id: str) -> dict[str, Any] | None:
        registry = get_core_workers_registry()
        result = await asyncio.to_thread(registry.latest_job_for_worker, worker_id)
        job = result.get("job") if isinstance(result, dict) else None
        return job if isinstance(job, dict) else None

    async def _rename_core_worker(self, worker_id: str, name: str) -> dict[str, Any]:
        registry = get_core_workers_registry()
        return await asyncio.to_thread(registry.rename_worker, worker_id, name)

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

    async def _sync_phone_worker(self) -> WorkerSnapshot:
        script = _repo_root() / "scripts" / "sync-phone-worker.sh"
        if not script.exists():
            return await self._collect_workers_snapshot(action_note="sync não encontrado em scripts/sync-phone-worker.sh")

        timeout = max(15.0, _env_float("WORKERS_PANEL_SYNC_TIMEOUT_SECONDS", 75.0))
        started = time.perf_counter()
        note = "sync executado"
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
            out = (stdout or b"").decode("utf-8", errors="replace").strip()
            err = (stderr or b"").decode("utf-8", errors="replace").strip()
            merged = " | ".join(part for part in [out, err] if part)
            output = merged or "sem saída"
            if proc.returncode == 0:
                note = f"phone-worker atualizado em {elapsed:.1f}s"
            else:
                note = f"sync retornou código {proc.returncode} em {elapsed:.1f}s"
        except asyncio.TimeoutError:
            note = f"sync excedeu {timeout:.0f}s"
            if proc is not None:
                with contextlib.suppress(Exception):
                    proc.kill()
        except Exception as exc:
            note = f"sync falhou: {_compact_failure(exc)}"
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
