import asyncio
import atexit
from collections import deque
import hashlib
import json
import logging
import logging.handlers
import os
import queue
import re
import threading
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

from cogs.musica.integracoes.logs import eh_cancelamento_esperado

# -----------------------------------------------------------------------------
# Logging — precisa vir ANTES de qualquer import do discord para capturar os
# logs de inicialização da biblioteca (gateway/voice/cogs).
# Nível geral INFO; libs barulhentas (discord.gateway / discord.voice_client)
# são rebaixadas para WARNING para não poluir.
# -----------------------------------------------------------------------------
_LOG_DIR = Path(__file__).resolve().parent / "logs"
_LOG_DIR.mkdir(parents=True, exist_ok=True)

class _LowValueNoiseFilter(logging.Filter):
    """Remove ruído conhecido sem esconder erro real do bot.

    O bot roda em VPS pequena. Logs de heartbeat do discord.py podem trazer stack
    inteira e repetir várias vezes durante a mesma travada; isso aumenta I/O no
    journald justamente quando o event loop já está atrasado. Mantemos o primeiro
    aviso e aplicamos cooldown nos repetidos.
    """

    _VOICE_LOGGERS = ("discord.voice", "discord.gateway", "discord.player")
    def __init__(self) -> None:
        super().__init__()
        self._last_by_key: dict[str, float] = {}
        self._lock = threading.Lock()

    def _allow_every(self, key: str, seconds: float) -> bool:
        now = time.monotonic()
        with self._lock:
            last = self._last_by_key.get(key, 0.0)
            if now - last < seconds:
                return False
            self._last_by_key[key] = now
            return True

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            logger_name = str(record.name or "")
            message = record.getMessage()
            lowered = message.lower()

            if logger_name.startswith("discord.gateway") and (
                "heartbeat blocked" in lowered or "loop thread traceback" in lowered
            ):
                cooldown = max(15.0, float(os.getenv("DISCORD_GATEWAY_HEARTBEAT_LOG_COOLDOWN_SECONDS", "60") or 60))
                if not self._allow_every("discord.gateway.heartbeat_blocked", cooldown):
                    return False

            if logger_name.startswith(self._VOICE_LOGGERS):
                exc_text = ""
                if record.exc_info:
                    exc_text = "".join(traceback.format_exception_only(record.exc_info[0], record.exc_info[1]))
                combined = f"{message}\n{exc_text}".lower()
                if "1006" in combined and ("websocket" in combined or "voice" in combined or "closed" in combined):
                    cooldown = max(5.0, float(os.getenv("DISCORD_VOICE_1006_LOG_COOLDOWN_SECONDS", "60") or 60))
                    return self._allow_every("discord.voice.close_1006", cooldown)

            if logger_name == "asyncio" and "exception was never retrieved" in lowered:
                exc_text = ""
                if record.exc_info:
                    exc_text = "".join(traceback.format_exception_only(record.exc_info[0], record.exc_info[1]))
                if eh_cancelamento_esperado(exc_text):
                    return False
        except Exception:
            return True
        return True


class _FastQueueHandler(logging.handlers.QueueHandler):
    def prepare(self, record: logging.LogRecord) -> logging.LogRecord:
        # QueueHandler padrão formata exc_info/stack antes de enfileirar. Como o
        # listener roda no mesmo processo, podemos atrasar essa formatação para a
        # thread de logging e manter o event loop leve.
        return record


def _configure_async_logging() -> logging.handlers.QueueListener:
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    file_handler = logging.handlers.RotatingFileHandler(
        _LOG_DIR / "bot.log",
        maxBytes=2_000_000,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)

    log_queue: queue.SimpleQueue[logging.LogRecord] = queue.SimpleQueue()
    queue_handler = _FastQueueHandler(log_queue)
    queue_handler.addFilter(_LowValueNoiseFilter())

    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(logging.INFO)
    root.addHandler(queue_handler)

    listener = logging.handlers.QueueListener(
        log_queue,
        stream_handler,
        file_handler,
        respect_handler_level=True,
    )
    listener.start()
    atexit.register(listener.stop)
    return listener


_LOG_LISTENER = _configure_async_logging()
logging.getLogger("discord.gateway").setLevel(logging.WARNING)
logging.getLogger("discord.voice_client").setLevel(logging.WARNING)
logging.getLogger("discord.player").setLevel(logging.WARNING)

import discord
from discord.ext import commands

import config
from db import SettingsDB
from webserver import run_webserver, set_health_provider, set_update_action_provider
from cogs.musica.integracoes.bot import IntegracaoMusicaBot
from utility.interaction_safety import is_unknown_interaction, safe_send_interaction_message
from utility.application_bio import ApplicationBioService
from utility.application_presence import ApplicationPresenceService
from updater.discord import IntegracaoDiscordUpdaterMixin, inicializar_integracao_updater
# Integração do updater vive em updater/discord; esta importação também força reload seguro em migrações.


BOOT_LOG = logging.getLogger("bot.boot")
COG_LOG = logging.getLogger("bot.cogs")
ASYNCIO_LOG = logging.getLogger("bot.asyncio")


print("BOT.PY INICIOU")



REMOVED_SLASH_COMMANDS = {
    "form_config",
    "form_customizar",
    "form_repostar",
    "form_reset",
    "form_status",
    # O painel técnico de TTS foi movido para /vps > TTS.
    "health",
    # Core Workers agora é comando privado de prefixo: workers/worker/w.
    "workers",
}

TRUTHY_VALUES = {"1", "true", "yes", "y", "on", "sim", "s"}

# Cogs que não devem ser carregadas automaticamente pelo varredor genérico.
# `cogs.tts` é carregado por módulos explícitos mais abaixo; carregar o pacote
# inteiro também criaria ambiguidade/duplicidade.
SKIPPED_COG_FILES = {"voice_moderation"}
SKIPPED_COG_PACKAGES = {"tts", "gincana"}
COG_EXTENSION_ALIASES = {"cogs.gincana": "cogs.games"}
EXPLICIT_COG_EXTENSIONS = (
    "cogs.tts.cog",
    "cogs.tts.toggle",
)


def _env_truthy(name: str, *, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return str(value).strip().lower() in TRUTHY_VALUES


def _csv_values(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        raw_items = re.split(r"[,;\s]+", value)
    elif isinstance(value, (list, tuple, set, frozenset)):
        raw_items = list(value)
    else:
        raw_items = [value]
    return [str(item).strip() for item in raw_items if str(item).strip()]


def _normalize_extension_name(value: str) -> str:
    value = str(value or "").strip().replace("/", ".").replace("\\", ".")
    if value.endswith(".py"):
        value = value[:-3]
    value = value.strip(".")
    if not value:
        return ""
    if not value.startswith("cogs."):
        value = f"cogs.{value}"
    return COG_EXTENSION_ALIASES.get(value, value)


def _cfg(*names: str, default=None):
    for name in names:
        if hasattr(config, name):
            return getattr(config, name)
    return default


class BotLocal(IntegracaoDiscordUpdaterMixin, commands.Bot):

    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.guilds = True
        intents.members = True
        intents.voice_states = True
        intents.messages = True

        super().__init__(
            command_prefix=commands.when_mentioned_or(
                getattr(config, "BOT_PREFIX", "_"),
                getattr(config, "PREFIX", "_"),
            ),
            intents=intents,
            help_command=None,
        )

        self.started_at = datetime.now(timezone.utc)
        self.settings_db: SettingsDB | None = None
        self.health_state: dict[str, object] = {
            "status": "starting",
            "healthy": True,
            "starting": True,
            "discord_ready": False,
            "discord_closed": False,
            "guild_count": 0,
            "latency_ms": None,
            "mongo_ok": False,
            "mongo_error": None,
            "last_update": None,
        }
        self._health_task: asyncio.Task | None = None
        self._event_loop_watchdog_task: asyncio.Task | None = None
        self._event_loop_last_lag_ms: float = 0.0
        self._event_loop_max_lag_ms: float = 0.0
        self._event_loop_lag_warnings: int = 0
        self._event_loop_last_warning_at: float = 0.0
        self._event_loop_last_diagnostic_at: float = 0.0
        self._event_loop_last_task_summary: str = ""
        self._event_loop_recent_lags_ms: deque[float] = deque(maxlen=12)
        self._repo_root = Path(__file__).resolve().parent
        inicializar_integracao_updater(self)
        self._app_command_manifest_path = self._repo_root / "data" / "app_commands_manifest.json"
        self._app_command_sync_status_path = self._repo_root / "data" / "app_commands_sync_status.json"
        self._removed_slash_cleanup_state_path = self._repo_root / "data" / "removed_slash_cleanup_state.json"
        self.integracao_musica = IntegracaoMusicaBot(self)
        self.integracao_musica.instalar_compatibilidade()
        self.application_bio = ApplicationBioService(self, self._repo_root / "data" / "application_bio.json")
        self.application_presence = ApplicationPresenceService(
            self,
            self._update_staging_root / "candidates" / "runtime-state.json",
            self._repo_root / "data" / "application_presence.json",
        )
        self.loaded_extensions: list[str] = []
        self.failed_extensions: dict[str, dict[str, object]] = {}
        self.skipped_extensions: dict[str, str] = {}
        self.critical_extensions: set[str] = self._read_critical_extensions()
        self.cog_loading_finished = False

        set_health_provider(self.get_health_snapshot)
        set_update_action_provider(self.handle_internal_update_action)

    def _read_critical_extensions(self) -> set[str]:
        """Lê a lista de cogs realmente críticas.

        Por padrão, nenhuma feature cog derruba o bot inteiro. Se algum sistema
        base virar indispensável, ele pode ser listado em `CRITICAL_COGS` no
        config.py ou em `BOT_CRITICAL_COGS` no .env/ambiente.
        """
        values: list[str] = []
        values.extend(_csv_values(getattr(config, "CRITICAL_COGS", None)))
        values.extend(_csv_values(getattr(config, "BOT_CRITICAL_COGS", None)))
        values.extend(_csv_values(os.getenv("BOT_CRITICAL_COGS")))
        normalized = {_normalize_extension_name(item) for item in values}
        normalized.discard("")
        return normalized

    def _is_critical_extension(self, extension: str) -> bool:
        return _normalize_extension_name(extension) in self.critical_extensions

    def _extension_error_summary(self, exc: BaseException) -> str:
        root = getattr(exc, "original", None) or getattr(exc, "__cause__", None) or exc
        root_type = type(root).__name__
        root_message = str(root).strip()
        if not root_message:
            root_message = repr(root)
        return f"{root_type}: {root_message}"

    def _record_failed_extension(self, extension: str, exc: BaseException) -> None:
        summary = self._extension_error_summary(exc)
        root = getattr(exc, "original", None) or getattr(exc, "__cause__", None) or exc
        self.failed_extensions[extension] = {
            "summary": summary,
            "exception_type": type(exc).__name__,
            "root_type": type(root).__name__,
            "critical": self._is_critical_extension(extension),
        }

    def _extension_has_setup_entrypoint(self, init_py: Path) -> bool:
        try:
            source = init_py.read_text(encoding="utf-8", errors="ignore")
        except Exception as exc:
            COG_LOG.warning("[cogs] não consegui ler %s: %r", init_py, exc)
            return False
        return bool(re.search(r"^\s*(async\s+def|def)\s+setup\s*\(", source, flags=re.MULTILINE))

    def _discover_cog_extensions(self) -> list[str]:
        cogs_dir = self._repo_root / "cogs"
        discovered: list[str] = []
        seen: set[str] = set()

        def add_extension(extension: str) -> None:
            extension = _normalize_extension_name(extension)
            if not extension or extension in seen:
                return
            seen.add(extension)
            discovered.append(extension)

        if not cogs_dir.is_dir():
            COG_LOG.warning("[cogs] pasta não encontrada: %s", cogs_dir)
            return []

        for entry in sorted(cogs_dir.iterdir(), key=lambda item: item.name.casefold()):
            name = entry.name
            if name.startswith("_"):
                continue

            if entry.is_file() and entry.suffix == ".py":
                module_name = entry.stem
                if module_name in SKIPPED_COG_FILES:
                    self.skipped_extensions[f"cogs.{module_name}"] = "ignorada pelo loader"
                    continue
                add_extension(f"cogs.{module_name}")
                continue

            if entry.is_dir():
                if name in SKIPPED_COG_PACKAGES:
                    self.skipped_extensions[f"cogs.{name}"] = "carregada por módulos explícitos"
                    continue
                init_py = entry / "__init__.py"
                if not init_py.is_file():
                    continue
                if self._extension_has_setup_entrypoint(init_py):
                    add_extension(f"cogs.{name}")

        for extension in EXPLICIT_COG_EXTENSIONS:
            add_extension(extension)

        return discovered

    async def _load_extension_safely(self, extension: str) -> bool:
        extension = _normalize_extension_name(extension)
        try:
            await self.load_extension(extension)
        except commands.ExtensionAlreadyLoaded:
            COG_LOG.info("[cogs] já estava carregada: %s", extension)
            if extension not in self.loaded_extensions:
                self.loaded_extensions.append(extension)
            return True
        except Exception as exc:
            self._record_failed_extension(extension, exc)
            summary = self._extension_error_summary(exc)
            if self._is_critical_extension(extension):
                COG_LOG.exception(
                    "[cogs] cog crítica falhou ao carregar: %s | %s",
                    extension,
                    summary,
                )
                raise
            COG_LOG.error(
                "[cogs] %s falhou ao carregar, mas o bot continuará online. Erro: %s",
                extension,
                summary,
            )
            return False

        self.loaded_extensions.append(extension)
        COG_LOG.info("[cogs] carregada: %s", extension)
        return True

    async def _load_cogs_safely(self) -> None:
        extensions = self._discover_cog_extensions()
        COG_LOG.info("[cogs] preparando %s extensão(ões)", len(extensions))
        for extension in extensions:
            await self._load_extension_safely(extension)
        self.cog_loading_finished = True

        failed = len(self.failed_extensions)
        loaded = len(self.loaded_extensions)
        if failed:
            COG_LOG.warning(
                "[cogs] boot continuou com aviso: %s carregada(s), %s com falha",
                loaded,
                failed,
            )
        else:
            COG_LOG.info("[cogs] todas carregadas: %s", loaded)

    async def _cleanup_removed_slash_commands(self, guild_ids: set[int]) -> None:
        """Remove comandos slash antigos sem tocar em comandos de outras cogs."""
        names = REMOVED_SLASH_COMMANDS

        async def delete_matching(scope: str, *, guild: discord.Object | None = None) -> None:
            try:
                commands_found = await self.tree.fetch_commands(guild=guild)
            except Exception as e:
                print(f"[SYNC][{scope}] não consegui buscar comandos pra limpar slash antigos: {e}")
                return

            for cmd in commands_found:
                name = str(getattr(cmd, "name", "") or "")
                if name not in names:
                    continue
                try:
                    await cmd.delete()
                    print(f"[SYNC][{scope}] removido comando antigo: /{name}")
                except Exception as e:
                    print(f"[SYNC][{scope}] falha ao remover /{name}: {e}")

        await delete_matching("GLOBAL")
        for guild_id in sorted(guild_ids):
            await delete_matching(
                f"GUILD {guild_id}",
                guild=discord.Object(id=int(guild_id)),
            )

    def _json_safe_value(self, value):
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        if isinstance(value, dict):
            return {str(k): self._json_safe_value(v) for k, v in sorted(value.items(), key=lambda item: str(item[0]))}
        if isinstance(value, (list, tuple, set)):
            return [self._json_safe_value(v) for v in value]
        enum_value = getattr(value, "value", None)
        if isinstance(enum_value, (str, int, float, bool)):
            return enum_value
        enum_name = getattr(value, "name", None)
        if isinstance(enum_name, str):
            return enum_name
        return str(value)

    def _app_command_to_dict(self, command) -> dict[str, object]:
        data = None
        to_dict = getattr(command, "to_dict", None)
        if callable(to_dict):
            for args in ((self.tree,), tuple()):
                try:
                    data = to_dict(*args)
                    break
                except TypeError:
                    continue
                except Exception:
                    data = None
                    break
        if not isinstance(data, dict):
            data = {
                "name": getattr(command, "name", ""),
                "description": getattr(command, "description", ""),
                "type": self._json_safe_value(getattr(command, "type", None)),
            }
            children = list(getattr(command, "commands", []) or [])
            if children:
                data["options"] = [self._app_command_to_dict(child) for child in children]
            params = list(getattr(command, "parameters", []) or [])
            if params:
                data["parameters"] = [
                    {
                        "name": getattr(param, "name", ""),
                        "description": getattr(param, "description", ""),
                        "required": getattr(param, "required", None),
                        "type": self._json_safe_value(getattr(param, "type", None)),
                        "choices": self._json_safe_value(getattr(param, "choices", None)),
                        "autocomplete": getattr(param, "autocomplete", None),
                    }
                    for param in params
                ]
        return self._json_safe_value(data)

    def _app_command_labels_from_dict(self, command_data: dict[str, object], prefix: str = "") -> list[str]:
        name = str(command_data.get("name") or "").strip()
        current = f"{prefix} {name}".strip()
        labels = [f"/{current}"] if current else []
        options = command_data.get("options") or command_data.get("parameters") or []
        if isinstance(options, list):
            for option in options:
                if not isinstance(option, dict):
                    continue
                # Discord slash subcommand/subcommand group option types are 1/2.
                option_type = option.get("type")
                if str(option_type) in {"1", "2", "subcommand", "sub_command", "subcommand_group", "sub_command_group"}:
                    labels.extend(self._app_command_labels_from_dict(option, current))
        return labels

    def _build_app_commands_manifest(self, guild_ids: set[int]) -> dict[str, object]:
        scopes: list[dict[str, object]] = []
        all_labels: set[str] = set()

        def add_scope(scope: str, commands_list) -> None:
            entries = [self._app_command_to_dict(cmd) for cmd in commands_list]
            entries.sort(key=lambda item: (str(item.get("type", "")), str(item.get("name", ""))))
            for entry in entries:
                all_labels.update(self._app_command_labels_from_dict(entry))
            scopes.append({"scope": scope, "commands": entries})

        try:
            add_scope("global", list(self.tree.get_commands()))
        except Exception as exc:
            scopes.append({"scope": "global", "error": f"{type(exc).__name__}: {exc}", "commands": []})

        for guild_id in sorted(guild_ids):
            guild_obj = discord.Object(id=int(guild_id))
            try:
                add_scope(f"guild:{guild_id}", list(self.tree.get_commands(guild=guild_obj)))
            except Exception as exc:
                scopes.append({"scope": f"guild:{guild_id}", "error": f"{type(exc).__name__}: {exc}", "commands": []})

        body = {
            "version": 1,
            "scopes": scopes,
            "labels": sorted(all_labels),
        }
        canonical = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return {
            **body,
            "hash": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }

    def _load_previous_app_commands_manifest(self) -> dict[str, object] | None:
        try:
            return json.loads(self._app_command_manifest_path.read_text(encoding="utf-8"))
        except Exception:
            return None

    def _app_command_manifest_diff(self, previous: dict[str, object] | None, current: dict[str, object]) -> dict[str, object]:
        current_labels = set(current.get("labels") or [])
        if not isinstance(previous, dict):
            # Primeiro boot com o manifest novo: sincroniza e cria baseline, mas
            # não trata todos os comandos existentes como “adicionados” no log.
            return {
                "added": [],
                "removed": [],
                "previous_hash": "",
                "current_hash": str(current.get("hash") or ""),
            }
        previous_labels = set(previous.get("labels") or [])
        return {
            "added": sorted(current_labels - previous_labels),
            "removed": sorted(previous_labels - current_labels),
            "previous_hash": str(previous.get("hash") or ""),
            "current_hash": str(current.get("hash") or ""),
        }

    def _write_app_command_sync_status(self, status: dict[str, object]) -> None:
        try:
            self._app_command_sync_status_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._app_command_sync_status_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(status, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
            tmp.replace(self._app_command_sync_status_path)
        except Exception:
            BOOT_LOG.warning("[SYNC] falha ao salvar status de comandos", exc_info=True)

    def _save_app_commands_manifest(self, manifest: dict[str, object]) -> None:
        self._app_command_manifest_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._app_command_manifest_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(self._app_command_manifest_path)

    def _removed_slash_cleanup_signature(self, guild_ids: set[int]) -> str:
        payload = {
            "names": sorted(REMOVED_SLASH_COMMANDS),
            "guild_ids": sorted(int(gid) for gid in guild_ids),
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()

    async def _cleanup_removed_slash_commands_if_needed(self, guild_ids: set[int]) -> None:
        signature = self._removed_slash_cleanup_signature(guild_ids)
        try:
            state = json.loads(self._removed_slash_cleanup_state_path.read_text(encoding="utf-8"))
        except Exception:
            state = {}
        if isinstance(state, dict) and state.get("signature") == signature:
            print("[SYNC] limpeza de slash antigos já conferida; pulando.")
            return
        await self._cleanup_removed_slash_commands(guild_ids)
        try:
            self._removed_slash_cleanup_state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._removed_slash_cleanup_state_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps({"signature": signature, "updated_at": datetime.now(timezone.utc).isoformat()}, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self._removed_slash_cleanup_state_path)
        except Exception:
            BOOT_LOG.warning("[SYNC] falha ao salvar marcador de limpeza slash", exc_info=True)

    def _resolve_app_command_sync_guild_ids(self) -> set[int]:
        health_guild_id = 927002914449424404
        guild_ids = {int(gid) for gid in (getattr(config, "GUILD_IDS", []) or []) if gid}
        guild_ids.add(health_guild_id)

        return guild_ids

    def _app_command_sync_enabled(self) -> bool:
        if os.getenv("APP_COMMAND_SYNC_ENABLED") is not None:
            return _env_truthy("APP_COMMAND_SYNC_ENABLED")
        return _env_truthy("SYNC_SLASH_COMMANDS")

    def _app_command_global_sync_allowed(self) -> bool:
        scope = str(os.getenv("APP_COMMAND_SYNC_SCOPE", "") or "").strip().lower()
        if scope in {"global", "globals", "public", "production"}:
            return True
        if scope in {"guild", "guilds", "server", "servers"}:
            return False
        return _env_truthy("SYNC_GLOBAL_SLASH_COMMANDS")

    async def _smart_sync_app_commands(self, guild_ids: set[int], *, should_sync: bool, allow_global_sync: bool, clear_globals_allowed: bool, trigger: str = "boot") -> dict[str, object]:
        current_manifest = self._build_app_commands_manifest(guild_ids)
        previous_manifest = self._load_previous_app_commands_manifest()
        diff = self._app_command_manifest_diff(previous_manifest, current_manifest)
        manifest_changed = previous_manifest is None or diff["previous_hash"] != diff["current_hash"]
        status: dict[str, object] = {
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "manifest_changed": manifest_changed,
            "sync_enabled": bool(should_sync),
            "global_sync_allowed": bool(allow_global_sync),
            "clear_global_allowed": bool(clear_globals_allowed),
            "sync_performed": False,
            "clear_performed": False,
            "added": diff["added"],
            "removed": diff["removed"],
            "previous_hash": diff["previous_hash"],
            "current_hash": diff["current_hash"],
            "reason": "manifest_changed" if manifest_changed else "unchanged",
            "trigger": trigger,
        }

        if not should_sync:
            status["reason"] = "disabled_by_env"
            self._write_app_command_sync_status(status)
            print(f"[SYNC] Pulado ({trigger}): sync de slash commands desativado")
            return status

        if not manifest_changed:
            status["reason"] = "unchanged"
            self._write_app_command_sync_status(status)
            print(f"[SYNC] Manifest de slash commands sem mudanças ({trigger}); sync/clear global pulados.")
            return status

        try:
            if allow_global_sync:
                synced_global = await self.tree.sync()
                status["sync_performed"] = True
                status["global_count"] = len(synced_global)
                print(f"[SYNC] Slash commands sincronizados globalmente: {len(synced_global)}")
                for cmd in synced_global:
                    name = getattr(cmd, "name", None) or str(cmd)
                    print(f"[SYNC][GLOBAL] /{name}")
                for guild_id in sorted(guild_ids):
                    guild_obj = discord.Object(id=guild_id)
                    synced_guild = await self.tree.sync(guild=guild_obj)
                    print(f"[SYNC] Comandos guild-specific sincronizados na guild {guild_id}: {len(synced_guild)}")
                    for cmd in synced_guild:
                        name = getattr(cmd, "name", None) or str(cmd)
                        print(f"[SYNC][GUILD {guild_id}] /{name}")
            else:
                if clear_globals_allowed and diff["removed"]:
                    try:
                        existing_globals = await self.tree.fetch_commands()
                    except Exception as e:
                        print(f"[SYNC] Não consegui buscar comandos globais: {e}")
                        existing_globals = []
                    removed_names = {label.lstrip('/').split()[0] for label in diff["removed"]}
                    deleted = 0
                    preserved = 0
                    for cmd in existing_globals:
                        cmd_type = getattr(cmd, "type", None)
                        type_value = getattr(cmd_type, "value", cmd_type)
                        if type_value == 4:
                            preserved += 1
                            print(f"[SYNC][GLOBAL] preservado Entry Point: /{cmd.name}")
                            continue
                        if getattr(cmd, "name", "") not in removed_names:
                            continue
                        try:
                            await cmd.delete()
                            deleted += 1
                            status["clear_performed"] = True
                            print(f"[SYNC][GLOBAL] deletado após remoção local: /{cmd.name}")
                        except Exception as e:
                            print(f"[SYNC][GLOBAL] falha ao deletar /{cmd.name}: {e}")
                    print(f"[SYNC] Limpeza global controlada: {deleted} deletados, {preserved} preservados (Entry Point)")
                else:
                    print("[SYNC] Limpeza global pulada; sem remoção que exija clear controlado.")

                for guild_id in sorted(guild_ids):
                    guild_obj = discord.Object(id=guild_id)
                    self.tree.copy_global_to(guild=guild_obj)
                    synced_guild = await self.tree.sync(guild=guild_obj)
                    status["sync_performed"] = True
                    print(f"[SYNC] Slash commands sincronizados na guild {guild_id}: {len(synced_guild)}")
                    for cmd in synced_guild:
                        name = getattr(cmd, "name", None) or str(cmd)
                        print(f"[SYNC][GUILD {guild_id}] /{name}")
            self._save_app_commands_manifest(current_manifest)
            status["reason"] = "synced"
        except Exception as exc:
            status["reason"] = "failed"
            status["error"] = f"{type(exc).__name__}: {exc}"
            self._write_app_command_sync_status(status)
            raise
        self._write_app_command_sync_status(status)
        return status


    async def setup_hook(self):
        print("SETUP_HOOK INICIOU")
        try:
            print(f"[DIAGNOSTICS] {self.integracao_musica.limpar_temporarios_diagnostico()}")
        except Exception as exc:
            print(f"[DIAGNOSTICS] cleanup temporário falhou: {type(exc).__name__}: {exc}")

        mongo_uri = _cfg("MONGODB_URI", "MONGO_URI")
        mongo_db_name = _cfg("MONGODB_DB", "MONGO_DB_NAME", "MONGODB_DB_NAME", default="chat_revive")
        mongo_collection_name = _cfg("MONGODB_COLLECTION", "MONGO_COLLECTION_NAME", "MONGODB_COLLECTION_NAME", default="settings")

        if not mongo_uri:
            raise RuntimeError("Nenhuma URI do MongoDB encontrada no config.py (MONGODB_URI/MONGO_URI).")

        self.settings_db = SettingsDB(
            mongo_uri,
            mongo_db_name,
            mongo_collection_name,
        )
        await self.settings_db.init()
        self.health_state.update({"mongo_ok": True, "mongo_error": None})

        print("Carregando cogs...")
        await self._load_cogs_safely()

        should_sync = self._app_command_sync_enabled()
        allow_global_sync = self._app_command_global_sync_allowed()
        clear_globals_allowed = _env_truthy("CLEAR_GLOBAL_COMMANDS")
        guild_ids = self._resolve_app_command_sync_guild_ids()

        # Limpa comandos antigos só quando a lista/guilds mudarem. Isso evita
        # fetch/delete remoto em todo boot e mantém a proteção contra slash
        # commands que foram migrados para prefixo.
        await self._cleanup_removed_slash_commands_if_needed(guild_ids)

        # SYNC_SLASH_COMMANDS e CLEAR_GLOBAL_COMMANDS continuam existindo, mas
        # agora são permissões: sync/clear só roda quando o manifest local de
        # comandos realmente muda. Restart comum sem mudança de comando fica
        # muito mais rápido.
        await self._smart_sync_app_commands(
            guild_ids,
            should_sync=should_sync,
            allow_global_sync=allow_global_sync,
            clear_globals_allowed=clear_globals_allowed,
            trigger="boot",
        )

    def get_health_snapshot(self) -> dict[str, object]:
        snapshot = dict(self.health_state)
        uptime_seconds = (datetime.now(timezone.utc) - self.started_at).total_seconds()
        snapshot["uptime_seconds"] = round(uptime_seconds, 2)

        # Não espere o monitor periódico de 15s para refletir que o bot já
        # voltou. O updater consulta /health logo após restart/reload; usar o
        # estado vivo aqui reduz atraso sem afrouxar a validação real.
        try:
            snapshot["discord_ready"] = self.is_ready()
            snapshot["discord_closed"] = self.is_closed()
            snapshot["guild_count"] = len(self.guilds)
            snapshot["latency_ms"] = round(float(self.latency) * 1000, 2)
        except Exception:
            pass

        ready = bool(snapshot.get("discord_ready"))
        closed = bool(snapshot.get("discord_closed"))
        mongo_ok = bool(snapshot.get("mongo_ok"))
        failed_extensions = dict(getattr(self, "failed_extensions", {}) or {})
        critical_failed = [
            name for name, data in failed_extensions.items()
            if isinstance(data, dict) and bool(data.get("critical"))
        ]

        starting = (not ready) and uptime_seconds < 120
        healthy = (ready and not closed and mongo_ok and not critical_failed) or starting
        warnings = []
        if failed_extensions:
            warnings.append(f"{len(failed_extensions)} cog(s) não carregaram")

        snapshot["starting"] = starting
        snapshot["healthy"] = healthy
        snapshot["status"] = "starting" if starting else ("ok" if healthy else "error")
        snapshot["warnings"] = warnings
        snapshot["loaded_cogs_count"] = len(getattr(self, "loaded_extensions", []) or [])
        snapshot["failed_cogs_count"] = len(failed_extensions)
        snapshot["failed_cogs"] = failed_extensions
        snapshot["critical_failed_cogs"] = critical_failed
        snapshot["cog_loading_finished"] = bool(getattr(self, "cog_loading_finished", False))
        snapshot["event_loop_last_lag_ms"] = round(float(getattr(self, "_event_loop_last_lag_ms", 0.0) or 0.0), 1)
        snapshot["event_loop_max_lag_ms"] = round(float(getattr(self, "_event_loop_max_lag_ms", 0.0) or 0.0), 1)
        snapshot["event_loop_lag_warnings"] = int(getattr(self, "_event_loop_lag_warnings", 0) or 0)
        snapshot["event_loop_recent_lags_ms"] = [round(float(v), 1) for v in list(getattr(self, "_event_loop_recent_lags_ms", []) or [])[-12:]]
        snapshot["event_loop_last_task_summary"] = str(getattr(self, "_event_loop_last_task_summary", "") or "")[:500]

        tts_cog = self.get_cog("TTSVoice")
        if tts_cog is not None and hasattr(tts_cog, "get_tts_metrics_snapshot"):
            try:
                snapshot["tts_metrics"] = tts_cog.get_tts_metrics_snapshot()
            except Exception as e:
                snapshot["tts_metrics_error"] = str(e)
        return snapshot

    async def _health_monitor_loop(self):
        while not self.is_closed():
            mongo_ok = False
            mongo_error = None
            try:
                if self.settings_db is not None:
                    await self.settings_db.client.admin.command("ping")
                    mongo_ok = True
                else:
                    mongo_error = "settings_db not initialized"
            except Exception as e:
                mongo_error = str(e)

            latency_ms = None
            try:
                latency_ms = round(float(self.latency) * 1000, 2)
            except Exception:
                pass

            self.health_state.update({
                "discord_ready": self.is_ready(),
                "discord_closed": self.is_closed(),
                "guild_count": len(self.guilds),
                "latency_ms": latency_ms,
                "mongo_ok": mongo_ok,
                "mongo_error": mongo_error,
                "last_update": datetime.now(timezone.utc).isoformat(),
            })
            await asyncio.sleep(15)

    def _event_loop_task_summary(self, *, limit: int = 8) -> str:
        try:
            current = asyncio.current_task()
            counts: dict[str, int] = {}
            for task in asyncio.all_tasks():
                if task is current or task.done():
                    continue
                coro = task.get_coro()
                qualname = getattr(coro, "__qualname__", "") or getattr(coro, "__name__", "")
                label = str(qualname or type(coro).__name__).replace("<", "").replace(">", "")
                if "await" in label and hasattr(coro, "cr_code"):
                    label = getattr(coro.cr_code, "co_name", label)
                counts[label[:90]] = counts.get(label[:90], 0) + 1
            if not counts:
                return "sem tasks pendentes do bot"
            rows = sorted(counts.items(), key=lambda item: (-item[1], item[0]))[: max(1, limit)]
            return ", ".join(f"{name}={count}" for name, count in rows)[:500]
        except Exception as exc:
            return f"indisponível: {type(exc).__name__}"

    async def _event_loop_watchdog_loop(self):
        interval = max(0.5, float(getattr(config, "BOT_EVENT_LOOP_WATCHDOG_INTERVAL_SECONDS", 1.0) or 1.0))
        warn_after = max(0.25, float(getattr(config, "BOT_EVENT_LOOP_LAG_WARNING_SECONDS", 1.5) or 1.5))
        warning_cooldown = max(10.0, float(getattr(config, "BOT_EVENT_LOOP_LAG_WARNING_COOLDOWN_SECONDS", 30.0) or 30.0))
        severe_after = max(warn_after * 2.0, float(getattr(config, "BOT_EVENT_LOOP_LAG_SEVERE_SECONDS", 8.0) or 8.0))
        diagnostic_cooldown = max(warning_cooldown, float(getattr(config, "BOT_EVENT_LOOP_LAG_DIAGNOSTIC_COOLDOWN_SECONDS", 90.0) or 90.0))
        loop = asyncio.get_running_loop()
        expected = loop.time() + interval
        while not self.is_closed():
            await asyncio.sleep(interval)
            now = loop.time()
            lag = max(0.0, now - expected)
            expected = now + interval
            lag_ms = lag * 1000.0
            self._event_loop_last_lag_ms = lag_ms
            if lag_ms > self._event_loop_max_lag_ms:
                self._event_loop_max_lag_ms = lag_ms
            if lag >= warn_after:
                self._event_loop_lag_warnings += 1
                self._event_loop_recent_lags_ms.append(round(lag_ms, 1))
                # Evita logar a cada segundo durante uma trava longa; o contador no
                # health snapshot continua registrando todos os atrasos detectados.
                now_mono = time.monotonic()
                last = float(getattr(self, "_event_loop_last_warning_at", 0.0) or 0.0)
                if now_mono - last >= warning_cooldown:
                    self._event_loop_last_warning_at = now_mono
                    task_summary = ""
                    last_diag = float(getattr(self, "_event_loop_last_diagnostic_at", 0.0) or 0.0)
                    if lag >= severe_after and now_mono - last_diag >= diagnostic_cooldown:
                        self._event_loop_last_diagnostic_at = now_mono
                        task_summary = self._event_loop_task_summary()
                        self._event_loop_last_task_summary = task_summary
                    if task_summary:
                        ASYNCIO_LOG.warning(
                            "event loop atrasado %.0f ms; diagnóstico leve: %s",
                            lag_ms,
                            task_summary,
                        )
                    else:
                        ASYNCIO_LOG.warning(
                            "event loop atrasado %.0f ms; possível I/O síncrono ou CPU em callback async",
                            lag_ms,
                        )


    async def close(self):
        if self._zip_update_reconcile_task is not None:
            self._zip_update_reconcile_task.cancel()
        if self._event_loop_watchdog_task is not None:
            self._event_loop_watchdog_task.cancel()
        if self._health_task is not None:
            self._health_task.cancel()
        application_presence = getattr(self, "application_presence", None)
        if application_presence is not None:
            try:
                await application_presence.close()
            except Exception as e:
                print(f"[bot] falha ao fechar application_presence: {e!r}")
        application_bio = getattr(self, "application_bio", None)
        if application_bio is not None:
            try:
                await application_bio.close()
            except Exception as e:
                print(f"[bot] falha ao fechar application_bio: {e!r}")
        integracao_musica = getattr(self, "integracao_musica", None)
        if integracao_musica is not None:
            await integracao_musica.fechar()
        await super().close()


    async def on_ready(self):
        print(f"Logado como {self.user} (id: {self.user.id})")
        print(f"Em {len(self.guilds)} servidor(es)")
        application_presence = getattr(self, "application_presence", None)
        if application_presence is not None:
            application_presence.start()
        self.integracao_musica.agendar_reconciliacao_inicial()
        if self._health_task is None or self._health_task.done():
            self._health_task = asyncio.create_task(self._health_monitor_loop())
        if self._event_loop_watchdog_task is None or self._event_loop_watchdog_task.done():
            self._event_loop_watchdog_task = asyncio.create_task(self._event_loop_watchdog_loop())
        if self._zip_update_reconcile_task is None or self._zip_update_reconcile_task.done():
            self._zip_update_reconcile_task = asyncio.create_task(self._zip_update_reconcile_loop())
        application_bio = getattr(self, "application_bio", None)
        if application_bio is not None:
            application_bio.start()

    async def on_guild_join(self, guild: discord.Guild):
        application_presence = getattr(self, "application_presence", None)
        if application_presence is not None:
            application_presence.schedule_refresh()
        application_bio = getattr(self, "application_bio", None)
        if application_bio is not None:
            application_bio.schedule_sync("guild_join")

    async def on_guild_remove(self, guild: discord.Guild):
        application_presence = getattr(self, "application_presence", None)
        if application_presence is not None:
            application_presence.schedule_refresh()
        application_bio = getattr(self, "application_bio", None)
        if application_bio is not None:
            application_bio.schedule_sync("guild_remove")

    async def _dispatch_tts_message_bridge(self, message: discord.Message) -> None:
        # Regressão defensiva: o TTS depende de on_message para transformar
        # mensagens prefixadas em fila de síntese. Como bot.py também define
        # on_message para o updater/comandos, chamamos o cog explicitamente aqui
        # para não depender só do fan-out interno de listeners. O cog deduplica
        # pelo ID da mensagem, então se o listener normal também rodar não há
        # enqueue duplicado.
        tts_cog = self.get_cog("TTSVoice")
        handler = getattr(tts_cog, "handle_message_from_bot_on_message", None) if tts_cog is not None else None
        if not callable(handler):
            return
        try:
            await handler(message)
        except Exception as exc:
            logging.getLogger("tts.bridge").exception(
                "[tts_bridge] falha ao encaminhar mensagem para TTS | guild=%s channel=%s user=%s erro=%r",
                getattr(getattr(message, "guild", None), "id", None),
                getattr(getattr(message, "channel", None), "id", None),
                getattr(getattr(message, "author", None), "id", None),
                exc,
            )

    async def _dispatch_antibot_message_bridge(self, message: discord.Message) -> bool:
        """Entrega a mensagem à armadilha antes de TTS e comandos.

        A cog retorna ``True`` somente para mensagens que precisam morrer no
        pipeline. O guard síncrono em ``on_message`` filtra as mensagens comuns
        antes desta coroutine.
        """
        antibot_cog = self.get_cog("AntibotCog")
        handler = (
            getattr(antibot_cog, "handle_message_from_bot_on_message", None)
            if antibot_cog is not None
            else None
        )
        if not callable(handler):
            return False
        try:
            return bool(await handler(message))
        except Exception as exc:
            logging.getLogger("antibot.bridge").exception(
                "[antibot_bridge] falha ao processar mensagem | guild=%s channel=%s user=%s erro=%r",
                getattr(getattr(message, "guild", None), "id", None),
                getattr(getattr(message, "channel", None), "id", None),
                getattr(getattr(message, "author", None), "id", None),
                exc,
            )
            # O guard já confirmou que a mensagem pertence à armadilha. Em uma
            # falha interna, mantemos o pipeline fechado para ela não alcançar
            # TTS, comandos ou o updater.
            return True

    async def on_message(self, message: discord.Message):
        try:
            # Não criamos nem aguardamos coroutine da armadilha para mensagens
            # comuns. O único custo fixo é um guard síncrono O(1), em memória.
            antibot_guard = getattr(self, "antibot_should_block_message", None)
            if callable(antibot_guard) and bool(antibot_guard(message)):
                if await self._dispatch_antibot_message_bridge(message):
                    return
            if getattr(message.author, "bot", False):
                return
            if int(getattr(message.channel, "id", 0)) == self.ZIP_UPDATE_CHANNEL_ID:
                await self._handle_zip_update_message(message)
                return
            await self._dispatch_tts_message_bridge(message)
            await self.process_commands(message)
        except Exception as e:
            print(f"[bot] falha ao processar comandos: {e!r}")

    async def on_command_error(self, ctx: commands.Context, error: commands.CommandError):
        if isinstance(error, commands.CommandNotFound):
            return

        if hasattr(ctx.command, "on_error"):
            return

        cog = ctx.cog
        if cog is not None:
            overridden = cog._get_overridden_method(cog.cog_command_error)
            if overridden is not None:
                return

        logger = logging.getLogger("discord.ext.commands.bot")
        logger.error("Ignoring exception in command %s", ctx.command, exc_info=error)

    async def on_app_command_error(
        self,
        interaction: discord.Interaction,
        error: discord.app_commands.AppCommandError,
    ):
        root = getattr(error, "original", None) or getattr(error, "__cause__", None) or error
        if is_unknown_interaction(root):
            logging.getLogger("discord.app_commands.tree").warning(
                "Comando %s expirou antes da primeira resposta: %s",
                getattr(getattr(interaction, "command", None), "name", "?"),
                root,
            )
            return
        print(f"[APP_COMMAND_ERROR] {error!r}")
        ok = await safe_send_interaction_message(
            interaction,
            f"Erro ao executar o comando: {error}",
            ephemeral=True,
            log=logging.getLogger("discord.app_commands.tree"),
            label="bot.app_command_error",
        )
        if not ok:
            print("[APP_COMMAND_ERROR] Falha ao responder ao usuário: token/interaction indisponível")


async def main():
    print("MAIN INICIOU")

    web_thread = threading.Thread(target=run_webserver, daemon=True)
    web_thread.start()

    bot = BotLocal()
    try:
        await bot.start(config.TOKEN)
    finally:
        if not bot.is_closed():
            await bot.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        BOOT_LOG.info("Encerrado manualmente")
