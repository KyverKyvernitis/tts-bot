import asyncio
import base64
import contextlib
import hashlib
import json
import logging
import logging.handlers
import os
import re
import shutil
import stat
import subprocess
import tempfile
import threading
import time
import traceback
import uuid
import zipfile
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

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

    - voice WebSocket 1006 do discord.py costuma ser reconexão rotineira;
    - cancelamentos esperados do player não devem virar traceback do asyncio.
    """

    _VOICE_LOGGERS = ("discord.voice", "discord.gateway", "discord.player")
    _EXPECTED_MUSIC_CANCELS = (
        "MusicPlaybackError: Música pulada antes de iniciar o áudio.",
        "MusicPlaybackError: Playback cancelado.",
    )

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            logger_name = str(record.name or "")
            message = record.getMessage()
            lowered = message.lower()

            if logger_name.startswith(self._VOICE_LOGGERS):
                exc_text = ""
                if record.exc_info:
                    exc_text = "".join(traceback.format_exception_only(record.exc_info[0], record.exc_info[1]))
                combined = f"{message}\n{exc_text}".lower()
                if "1006" in combined and ("websocket" in combined or "voice" in combined or "closed" in combined):
                    return False

            if logger_name == "asyncio" and "exception was never retrieved" in lowered:
                exc_text = ""
                if record.exc_info:
                    exc_text = "".join(traceback.format_exception_only(record.exc_info[0], record.exc_info[1]))
                if any(marker in exc_text for marker in self._EXPECTED_MUSIC_CANCELS):
                    return False
        except Exception:
            return True
        return True


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(),
        logging.handlers.RotatingFileHandler(
            _LOG_DIR / "bot.log",
            maxBytes=2_000_000,
            backupCount=3,
            encoding="utf-8",
        ),
    ],
)
_noise_filter = _LowValueNoiseFilter()
for _handler in logging.getLogger().handlers:
    _handler.addFilter(_noise_filter)
logging.getLogger("discord.gateway").setLevel(logging.WARNING)
logging.getLogger("discord.voice_client").setLevel(logging.WARNING)
logging.getLogger("discord.player").setLevel(logging.WARNING)

import discord
from discord.ext import commands

import config
from db import SettingsDB
from webserver import run_webserver, set_health_provider, set_update_action_provider
from music_system import AudioRouter
from utility.interaction_safety import is_unknown_interaction, safe_send_interaction_message


BOOT_LOG = logging.getLogger("bot.boot")
COG_LOG = logging.getLogger("bot.cogs")
UPDATE_LOG = logging.getLogger("zip_update")
ASYNCIO_LOG = logging.getLogger("bot.asyncio")

print("BOT.PY INICIOU")



REMOVED_SLASH_COMMANDS = {
    "form_config",
    "form_customizar",
    "form_repostar",
    "form_reset",
    "form_status",
    # O CallKeeper agora é apenas comando de prefixo; remove o grupo slash antigo.
    "callkeeper",
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
SKIPPED_COG_PACKAGES = {"tts"}
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
    if value.startswith("cogs."):
        return value
    return f"cogs.{value}"


def _cfg(*names: str, default=None):
    for name in names:
        if hasattr(config, name):
            return getattr(config, name)
    return default


class BotLocal(commands.Bot):
    ZIP_UPDATE_CHANNEL_ID = 1490093068706386131

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
        self._zip_update_lock = asyncio.Lock()
        self._repo_root = Path(__file__).resolve().parent
        self._update_temp_root = Path("/tmp/discord-auto-update")
        self._update_staging_root = Path(os.getenv("DISCORD_AUTO_UPDATE_STAGING_DIR", str(self._repo_root.parent / "bot-update-staging")))
        self._zip_rollback_state_path = self._update_staging_root / "zip_update_rollback_state.json"
        self._app_command_manifest_path = self._repo_root / "data" / "app_commands_manifest.json"
        self._app_command_sync_status_path = self._repo_root / "data" / "app_commands_sync_status.json"
        self._removed_slash_cleanup_state_path = self._repo_root / "data" / "removed_slash_cleanup_state.json"
        self.audio_router = AudioRouter(self)
        self._music_bitrate_reconciled = False
        self._music_voice_status_reconciled = False

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

    async def _smart_sync_app_commands(self, guild_ids: set[int], *, should_sync: bool, allow_global_sync: bool, clear_globals_allowed: bool) -> None:
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
        }

        if not should_sync:
            status["reason"] = "disabled_by_env"
            self._write_app_command_sync_status(status)
            print("[SYNC] Pulado no boot: SYNC_SLASH_COMMANDS=false")
            return

        if not manifest_changed:
            status["reason"] = "unchanged"
            self._write_app_command_sync_status(status)
            print("[SYNC] Manifest de slash commands sem mudanças; sync/clear global pulados.")
            return

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


    async def setup_hook(self):
        print("SETUP_HOOK INICIOU")
        try:
            from music_system.diagnostics import cleanup_music_diagnostics_temp_artifacts

            print(f"[DIAGNOSTICS] {cleanup_music_diagnostics_temp_artifacts()}")
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

        should_sync = _env_truthy("SYNC_SLASH_COMMANDS")
        allow_global_sync = _env_truthy("SYNC_GLOBAL_SLASH_COMMANDS")
        clear_globals_allowed = _env_truthy("CLEAR_GLOBAL_COMMANDS")

        health_guild_id = 927002914449424404
        guild_ids = {int(gid) for gid in (getattr(config, "GUILD_IDS", []) or []) if gid}
        guild_ids.add(health_guild_id)

        try:
            callkeeper_guild_id = int(getattr(config, "CALLKEEPER_GUILD_ID", 0) or 0)
        except Exception:
            callkeeper_guild_id = 0
        if callkeeper_guild_id > 0:
            guild_ids.add(callkeeper_guild_id)

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

    async def _event_loop_watchdog_loop(self):
        interval = max(0.5, float(getattr(config, "BOT_EVENT_LOOP_WATCHDOG_INTERVAL_SECONDS", 1.0) or 1.0))
        warn_after = max(0.25, float(getattr(config, "BOT_EVENT_LOOP_LAG_WARNING_SECONDS", 1.5) or 1.5))
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
                # Evita logar a cada segundo durante uma trava longa; o contador no
                # health snapshot continua registrando todos os atrasos detectados.
                last = float(getattr(self, "_event_loop_last_warning_at", 0.0) or 0.0)
                if time.monotonic() - last >= 10.0:
                    self._event_loop_last_warning_at = time.monotonic()
                    ASYNCIO_LOG.warning(
                        "event loop atrasado %.0f ms; possível I/O síncrono ou CPU em callback async",
                        lag_ms,
                    )

    def _make_zip_update_view(
        self,
        title: str,
        description: str,
        color: discord.Color,
        control: dict[str, object] | None = None,
    ) -> discord.ui.LayoutView:
        view = discord.ui.LayoutView(timeout=None)
        title = self._zip_update_normalize_title(title, "")
        text = f"# {title}\n{description.strip()}".strip()
        if len(text) > 3900:
            text = text[:3897].rstrip() + "..."
        children: list[discord.ui.Item] = [discord.ui.TextDisplay(text)]
        if isinstance(control, dict) and control.get("enabled"):
            emoji = str(control.get("emoji") or "◀️")
            custom_id = str(control.get("custom_id") or "")[:100]
            disabled = bool(control.get("disabled"))
            if custom_id:
                button = discord.ui.Button(
                    emoji=emoji,
                    style=discord.ButtonStyle.secondary,
                    custom_id=custom_id,
                    disabled=disabled,
                )
                children.append(discord.ui.Separator())
                children.append(discord.ui.ActionRow(button))
        view.add_item(discord.ui.Container(*children, accent_color=color))
        return view

    def _zip_update_state_load(self) -> dict[str, object]:
        try:
            if self._zip_rollback_state_path.is_file():
                data = json.loads(self._zip_rollback_state_path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return data
        except Exception:
            logging.getLogger("zip_update").warning("falha ao ler estado de rollback do updater", exc_info=True)
        return {}

    def _zip_update_state_save(self, data: dict[str, object]) -> None:
        try:
            self._zip_rollback_state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._zip_rollback_state_path.with_name(f".{self._zip_rollback_state_path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
            os.replace(tmp, self._zip_rollback_state_path)
        except Exception:
            logging.getLogger("zip_update").warning("falha ao salvar estado de rollback do updater", exc_info=True)

    def _zip_update_rollback_request_roots(self) -> list[Path]:
        roots: list[Path] = []
        raw_env = os.getenv("DISCORD_AUTO_UPDATE_ROLLBACK_REQUEST_DIRS") or os.getenv("DISCORD_AUTO_UPDATE_ROLLBACK_REQUEST_DIR") or ""
        for raw in re.split(r"[:;,]", raw_env):
            raw = raw.strip()
            if raw:
                roots.append(Path(raw))
        roots.extend([
            self._update_staging_root / "candidates" / "rollback",
            self._repo_root / "data" / "runtime" / "update-rollback",
            Path(tempfile.gettempdir()) / "tts-bot-update-rollback",
        ])
        unique: list[Path] = []
        seen: set[str] = set()
        for root in roots:
            try:
                key = str(root.expanduser().resolve(strict=False))
            except Exception:
                key = str(root)
            if key in seen:
                continue
            seen.add(key)
            unique.append(root)
        return unique

    def _zip_update_find_writable_rollback_request_root(self) -> tuple[Path | None, list[str]]:
        details: list[str] = []
        for root in self._zip_update_rollback_request_roots():
            try:
                root.mkdir(parents=True, exist_ok=True)
                with contextlib.suppress(Exception):
                    root.chmod(0o775)
                probe = root / f".write-test.{os.getpid()}.{uuid.uuid4().hex}.tmp"
                probe.write_text("ok", encoding="utf-8")
                probe.unlink(missing_ok=True)
                return root, details
            except Exception as exc:
                details.append(f"{root}: {exc.__class__.__name__}: {exc}")
        return None, details

    def _zip_update_has_pending_rollback_request(self) -> bool:
        for root in self._zip_update_rollback_request_roots():
            try:
                if (root / "pending.json").exists() or (root / "active.json").exists():
                    return True
            except Exception:
                continue
        return False

    def _zip_update_control_for_record(self, record: dict[str, object], *, disabled: bool = False) -> dict[str, object] | None:
        token = str(record.get("token") or "").strip()
        mode = str(record.get("mode") or "rollback").strip().lower()
        if not token or mode not in {"rollback", "redo"}:
            return None
        return {
            "enabled": True,
            "emoji": "▶️" if mode == "redo" else "◀️",
            "custom_id": f"zip_update:{mode}:{token}"[:100],
            "disabled": disabled,
        }

    def _zip_update_authorized_user_ids(self, record: dict[str, object] | None = None) -> set[int]:
        ids: set[int] = set()
        raw_values = [os.getenv("DISCORD_AUTO_UPDATE_USER_IDS", ""), os.getenv("BOT_OWNER_IDS", "")]
        if isinstance(record, dict):
            raw_values.extend([
                str(record.get("source_author_id") or ""),
                str(record.get("requested_by") or ""),
            ])
        for raw in raw_values:
            for piece in re.split(r"[,;\s]+", str(raw or "")):
                piece = piece.strip()
                if piece.isdigit():
                    ids.add(int(piece))
        return ids

    def _zip_update_can_control(self, interaction: discord.Interaction, record: dict[str, object]) -> bool:
        user_id = int(getattr(interaction.user, "id", 0) or 0)
        if user_id in self._zip_update_authorized_user_ids(record):
            return True
        perms = getattr(getattr(interaction, "user", None), "guild_permissions", None)
        if perms and (getattr(perms, "administrator", False) or getattr(perms, "manage_guild", False)):
            return True
        return False

    async def _send_zip_update_message(
        self,
        message: discord.Message,
        title: str,
        description: str,
        color: discord.Color,
        control: dict[str, object] | None = None,
    ) -> discord.Message:
        view = self._make_zip_update_view(title, description, color, control=control)
        return await message.reply(view=view, mention_author=False, allowed_mentions=discord.AllowedMentions.none())

    async def _edit_zip_update_message(
        self,
        source_message: discord.Message,
        status_message: discord.Message | None,
        title: str,
        description: str,
        color: discord.Color,
        control: dict[str, object] | None = None,
    ) -> discord.Message:
        view = self._make_zip_update_view(title, description, color, control=control)
        if status_message is not None:
            try:
                await status_message.edit(view=view, allowed_mentions=discord.AllowedMentions.none())
                return status_message
            except discord.HTTPException:
                logging.getLogger("zip_update").warning(
                    "não consegui editar a mensagem de status do ZIP; enviando resultado final",
                    exc_info=True,
                )
        return await source_message.reply(view=view, mention_author=False, allowed_mentions=discord.AllowedMentions.none())

    async def _zip_update_fetch_message(self, channel_id: int, message_id: int) -> discord.Message | None:
        try:
            channel = self.get_channel(int(channel_id))
            if channel is None:
                channel = await self.fetch_channel(int(channel_id))
            if not hasattr(channel, "fetch_message"):
                return None
            return await channel.fetch_message(int(message_id))
        except Exception:
            logging.getLogger("zip_update").warning("falha ao buscar mensagem de update", exc_info=True)
            return None

    async def _zip_update_clear_previous_control(self, previous: dict[str, object] | None, *, keep_message_id: int = 0) -> None:
        if not isinstance(previous, dict):
            return
        try:
            channel_id = int(str(previous.get("channel_id") or "0"))
            message_id = int(str(previous.get("message_id") or "0"))
        except Exception:
            return
        if not channel_id or not message_id or message_id == keep_message_id:
            return
        msg = await self._zip_update_fetch_message(channel_id, message_id)
        if msg is None:
            return
        title = str(previous.get("title") or "Update aplicado")
        description = str(previous.get("description") or "")
        status = str(previous.get("status") or "success")
        color = self._zip_update_status_color(status)
        try:
            await msg.edit(view=self._make_zip_update_view(title, description, color), allowed_mentions=discord.AllowedMentions.none())
        except Exception:
            logging.getLogger("zip_update").warning("falha ao remover controle de update antigo", exc_info=True)

    def _zip_update_status_color(self, status: str) -> discord.Color:
        status = str(status or "info").lower().strip()
        if status in {"success", "ok", "done"}:
            return discord.Color.green()
        if status in {"warn", "warning", "progress"}:
            return discord.Color.gold()
        if status in {"error", "failed", "failure"}:
            return discord.Color.red()
        if status in {"applying", "pending"}:
            return discord.Color.blurple()
        return discord.Color.blurple()

    def _zip_update_normalize_title(self, title: str, status: str = "") -> str:
        title = str(title or "").strip() or "Update"
        normalized = re.sub(r"\s+", " ", title).strip()
        lowered = normalized.casefold()
        status = str(status or "").lower().strip()
        if re.fullmatch(r"(?:✅\s*)?update aplicado", lowered):
            return "✅ Update aplicado"
        if status in {"success", "ok", "done"} and lowered == "update":
            return "✅ Update aplicado"
        return normalized

    def _zip_update_latest_message_ref(self, interaction: discord.Interaction, record: dict[str, object]) -> tuple[str, str]:
        channel_id = str(record.get("channel_id") or "").strip()
        message_id = str(record.get("message_id") or "").strip()
        msg = getattr(interaction, "message", None)
        if msg is not None:
            if not channel_id:
                channel_id = str(getattr(getattr(msg, "channel", None), "id", "") or "")
            if not message_id:
                message_id = str(getattr(msg, "id", "") or "")
        return channel_id, message_id

    def _zip_update_restore_record_message_view(self, record: dict[str, object]) -> discord.ui.LayoutView:
        title = self._zip_update_normalize_title(str(record.get("title") or "Update aplicado"), str(record.get("status") or "success"))
        description = str(record.get("description") or "")
        status = str(record.get("status") or "success")
        return self._make_zip_update_view(
            title,
            description,
            self._zip_update_status_color(status),
            control=self._zip_update_control_for_record(record),
        )

    def _git_env(self) -> dict[str, str]:
        env = os.environ.copy()
        env.setdefault("HOME", "/home/ubuntu")
        key_path = Path("/home/ubuntu/.ssh/id_ed25519")
        if key_path.is_file():
            env.setdefault("GIT_SSH_COMMAND", f"ssh -i {key_path} -o IdentitiesOnly=yes")
        return env

    def _run_cmd(self, args: list[str], cwd: Path, *, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            args,
            cwd=str(cwd),
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

    def _normalize_zip_member_parts(self, raw_name: str) -> tuple[str, ...]:
        posix = PurePosixPath(raw_name.replace("\\", "/"))
        return tuple(part for part in posix.parts if part not in ("", "."))

    def _zip_update_should_ignore_generated_file(self, rel_path: Path) -> bool:
        """Ignora lixo de build que não deve virar commit pelo auto updater."""
        parts = tuple(str(part) for part in rel_path.parts)
        name = parts[-1] if parts else ""
        if not parts:
            return True
        if any(part in {"__pycache__", ".gradle", ".idea"} for part in parts):
            return True
        if name.endswith((".pyc", ".pyo", ".tmp")):
            return True
        if name.startswith("build.gradle.bak") or name.endswith(".bak-sdk35"):
            return True
        core_worker_prefix = ("android", "core-worker-app")
        if parts[:2] == core_worker_prefix:
            if len(parts) >= 4 and parts[2] == "app" and parts[3] == "build":
                return True
            if len(parts) >= 3 and parts[2] == "releases":
                return True
        return False

    def _zip_update_is_callkeeper_protected_path(self, rel_path: Path | str) -> bool:
        parts = tuple(Path(str(rel_path)).parts)
        if not parts:
            return False
        first = parts[0]
        if first == "callkeeper_runtime":
            return True
        if first in {"callkeeper_service.py"}:
            return True
        if len(parts) >= 2 and parts[0] == "cogs" and parts[1] == "call_keeper.py":
            return True
        if len(parts) >= 3 and parts[0] == "deploy" and parts[1] == "systemd" and parts[-1] == "callkeeper.service":
            return True
        return False

    def _prepare_update_staging_clone(self, origin_url: str, branch_name: str, env: dict[str, str]) -> Path:
        """Mantém um clone staging reutilizável para não clonar o repositório inteiro a cada ZIP."""
        self._update_staging_root.mkdir(parents=True, exist_ok=True)
        clone_dir = self._update_staging_root / "repo"

        def clone_fresh() -> Path:
            shutil.rmtree(clone_dir, ignore_errors=True)
            clone_result = self._run_cmd(["git", "clone", "--branch", branch_name, "--single-branch", origin_url, str(clone_dir)], self._update_staging_root, env=env)
            if clone_result.returncode != 0:
                err = (clone_result.stderr or clone_result.stdout or "").strip()
                raise RuntimeError(f"Falha ao preparar staging persistente do update. {err}")
            return clone_dir

        if not (clone_dir / ".git").exists():
            return clone_fresh()

        # Se o staging antigo ficou corrompido, recria do zero. Caso contrário,
        # reset/clean é muito mais rápido do que um clone completo.
        for args in (
            ["git", "remote", "set-url", "origin", origin_url],
            ["git", "fetch", "--prune", "origin", branch_name],
            ["git", "checkout", "-B", branch_name, f"origin/{branch_name}"],
            ["git", "reset", "--hard", f"origin/{branch_name}"],
            ["git", "clean", "-fdx"],
        ):
            result = self._run_cmd(args, clone_dir, env=env)
            if result.returncode != 0:
                logging.getLogger("zip_update").warning("staging persistente falhou em %s; recriando clone", " ".join(args))
                return clone_fresh()
        return clone_dir

    def _git_diff_numstat_sync(self, repo_dir: Path, env: dict[str, str]) -> dict[str, object]:
        result = self._run_cmd(["git", "diff", "--cached", "--numstat"], repo_dir, env=env)
        if result.returncode != 0:
            err = (result.stderr or result.stdout or "").strip()
            raise RuntimeError(f"Falha ao calcular diff de linhas. {err}")

        entries: list[dict[str, object]] = []
        total_added = 0
        total_removed = 0
        binary_files = 0
        for raw in (result.stdout or "").splitlines():
            parts = raw.split("\t")
            if len(parts) < 3:
                continue
            added_raw, removed_raw, path_raw = parts[0], parts[1], parts[-1]
            is_binary = added_raw == "-" or removed_raw == "-"
            added = 0 if is_binary else int(added_raw or 0)
            removed = 0 if is_binary else int(removed_raw or 0)
            if is_binary:
                binary_files += 1
            else:
                total_added += added
                total_removed += removed
            entries.append({"path": path_raw, "added": added, "removed": removed, "binary": is_binary})

        summary = f"+{total_added} -{total_removed}"
        if binary_files:
            summary += f" · {binary_files} binário(s)"
        return {"entries": entries, "summary": summary, "total_added": total_added, "total_removed": total_removed, "binary_files": binary_files}

    def _write_local_update_candidate_sync(
        self,
        *,
        branch_name: str,
        base_commit: str | None,
        changed_files: list[str],
        diff_stats: dict[str, object],
        extracted_files: list[tuple[Path, Path]],
        zip_name: str,
        status_context: dict[str, object] | None = None,
    ) -> dict[str, object]:
        """Grava um candidato local para o updater testar na VPS antes do push.

        O GitHub só recebe commit depois que scripts/tts-bot-update.sh aplica o
        candidato, valida reload/restart/health e confirma que o estado ficou bom.
        """
        candidate_root = self._update_staging_root / "candidates"
        candidate_root.mkdir(parents=True, exist_ok=True)
        candidate_id = f"zip-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:8]}"
        candidate_dir = candidate_root / candidate_id
        files_dir = candidate_dir / "files"
        files_dir.mkdir(parents=True, exist_ok=False)

        written_files: list[str] = []
        for extracted_path, rel_path in extracted_files:
            rel_posix = rel_path.as_posix()
            if rel_posix not in changed_files:
                continue
            if self._zip_update_is_callkeeper_protected_path(rel_path):
                raise RuntimeError(f"Arquivo protegido do CallKeeper não pode ser alterado pelo updater comum: {rel_posix}")
            target = files_dir / rel_path
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(extracted_path, target)
            written_files.append(rel_posix)

        if not written_files:
            shutil.rmtree(candidate_dir, ignore_errors=True)
            raise RuntimeError("O candidato local não trouxe nenhum arquivo alterado.")

        manifest = {
            "id": candidate_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "branch": branch_name,
            "base_commit": base_commit,
            "source": "discord_zip",
            "zip_name": zip_name,
            "commit_message": f"auto update from discord zip ({len(changed_files)} arquivo(s))",
            "changed_files": changed_files,
            "diff_stats": diff_stats,
        }
        if isinstance(status_context, dict) and status_context.get("message_id") and status_context.get("channel_id"):
            manifest["discord_status"] = {
                "guild_id": str(status_context.get("guild_id") or ""),
                "channel_id": str(status_context.get("channel_id") or ""),
                "message_id": str(status_context.get("message_id") or ""),
                "source_message_id": str(status_context.get("source_message_id") or ""),
                "source_author_id": str(status_context.get("source_author_id") or ""),
            }
        (candidate_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

        pending_path = candidate_root / "pending.json"
        tmp_pending = candidate_root / f"pending.{candidate_id}.tmp"
        tmp_pending.write_text(json.dumps({"candidate_dir": str(candidate_dir), "id": candidate_id}, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp_pending, pending_path)
        return {"candidate_id": candidate_id, "candidate_dir": str(candidate_dir), "pending_path": str(pending_path)}

    def _trigger_updater_service_sync(self) -> tuple[bool, str]:
        service = Path("/etc/systemd/system/tts-bot-updater.service")
        if not service.exists():
            return False, "updater via systemd não encontrado"
        for args in (["sudo", "-n", "systemctl", "start", "--no-block", "tts-bot-updater.service"], ["systemctl", "start", "--no-block", "tts-bot-updater.service"]):
            result = self._run_cmd(args, self._repo_root, env=self._git_env())
            if result.returncode == 0:
                return True, "updater disparado agora"
        detail = (result.stderr or result.stdout or "").strip() if 'result' in locals() else ""
        if detail:
            detail = detail.splitlines()[-1][:180]
            return False, f"commit enviado; timer aplicará depois ({detail})"
        return False, "commit enviado; timer aplicará depois"

    def _guess_repo_name(self, origin_url: str) -> str:
        cleaned = (origin_url or "").strip().rstrip("/")
        if cleaned.endswith(".git"):
            cleaned = cleaned[:-4]
        if "/" in cleaned:
            cleaned = cleaned.rsplit("/", 1)[-1]
        if ":" in cleaned:
            cleaned = cleaned.rsplit(":", 1)[-1]
        return cleaned.strip()

    def _pick_zip_strip_count(self, file_members: list[tuple[str, ...]], repo_name_hint: str, branch_name: str) -> int:
        if not file_members:
            return 0

        repo_root = self._repo_root.resolve()
        repo_top_names = {child.name for child in repo_root.iterdir()}
        max_strip = min(max(len(parts) - 1, 0) for parts in file_members)
        best_strip = 0
        best_score = (-1, -1, 0)

        for strip_count in range(max_strip + 1):
            mapped_members = [parts[strip_count:] for parts in file_members]
            if any(not parts for parts in mapped_members):
                continue

            exact_exists = 0
            top_level_exists = 0
            for mapped_parts in mapped_members:
                rel_path = Path(*mapped_parts)
                if (repo_root / rel_path).exists():
                    exact_exists += 1
                if mapped_parts[0] in repo_top_names:
                    top_level_exists += 1

            score = (exact_exists, top_level_exists, -strip_count)
            if score > best_score:
                best_score = score
                best_strip = strip_count

        if best_score[:2] != (0, 0):
            return best_strip

        common_first = file_members[0][0] if file_members[0] else ""
        if common_first and all(parts and parts[0] == common_first for parts in file_members):
            wrapper_names = {
                self._repo_root.name,
                repo_name_hint,
                f"{repo_name_hint}-main",
                f"{repo_name_hint}-master",
                f"{repo_name_hint}-{branch_name}",
            }
            if common_first in wrapper_names and common_first not in repo_top_names:
                return 1

        return best_strip

    def _phone_worker_base_url(self) -> str | None:
        if not bool(_cfg("PHONE_WORKER_ENABLED", default=False)):
            return None
        host = str(_cfg("PHONE_WORKER_HOST", default="") or "").strip()
        if not host:
            return None
        scheme = str(_cfg("PHONE_WORKER_SCHEME", default="http") or "http").strip() or "http"
        port = int(_cfg("PHONE_WORKER_PORT", default=8766) or 8766)
        return f"{scheme}://{host}:{port}"

    def _phone_worker_request_sync(self, task: str, payload: dict[str, object], *, timeout: float = 5.0) -> dict[str, object] | None:
        base_url = self._phone_worker_base_url()
        if not base_url:
            return None
        token = str(_cfg("PHONE_WORKER_TOKEN", default="") or "").strip()
        payload = dict(payload)
        payload["task"] = task
        data = json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(f"{base_url}/task", data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
            parsed = json.loads(raw.decode("utf-8"))
            return parsed if isinstance(parsed, dict) else None
        except Exception as exc:
            logging.getLogger("zip_update").info("phone-worker indisponível para %s: %r", task, exc)
            return None

    def _phone_worker_validate_zip_sync(self, zip_path: Path) -> dict[str, object] | None:
        if not bool(_cfg("PHONE_WORKER_ZIP_VALIDATE_ENABLED", default=True)):
            return None
        try:
            max_mb = int(_cfg("PHONE_WORKER_ZIP_VALIDATE_MAX_MB", default=24) or 24)
            if zip_path.stat().st_size > max_mb * 1024 * 1024:
                logging.getLogger("zip_update").info("zip grande demais para validação phone-worker: %.2f MB", zip_path.stat().st_size / 1048576)
                return None
            timeout = float(_cfg("PHONE_WORKER_ZIP_VALIDATE_TIMEOUT_SECONDS", default=5.0) or 5.0)
            result = self._phone_worker_request_sync(
                "zip_validate",
                {
                    "filename": zip_path.name,
                    "data_b64": base64.b64encode(zip_path.read_bytes()).decode("ascii"),
                    "max_entries": 800,
                    "max_preview": 40,
                },
                timeout=timeout,
            )
            if result:
                logging.getLogger("zip_update").info(
                    "phone-worker validou ZIP: ok=%s risk=%s files=%s size=%s",
                    result.get("ok"), result.get("risk"), result.get("files"), result.get("size"),
                )
            return result
        except Exception as exc:
            logging.getLogger("zip_update").info("falha ao preparar validação phone-worker do ZIP: %r", exc)
            return None

    def _safe_extract_patch(self, zip_path: Path, extract_dir: Path, repo_name_hint: str, branch_name: str) -> list[tuple[Path, Path]]:
        accepted: list[tuple[Path, Path]] = []
        with zipfile.ZipFile(zip_path) as zf:
            file_members: list[tuple[str, ...]] = []
            prepared_infos: list[tuple[zipfile.ZipInfo, tuple[str, ...]]] = []

            for info in zf.infolist():
                raw_parts = self._normalize_zip_member_parts(info.filename)
                if not raw_parts:
                    continue
                if raw_parts[0] == "__MACOSX":
                    continue
                if raw_parts[-1] == ".DS_Store":
                    continue
                if any(part == ".." for part in raw_parts):
                    raise RuntimeError(f"Caminho inválido no ZIP: {info.filename}")

                prepared_infos.append((info, raw_parts))
                if not info.is_dir():
                    file_members.append(raw_parts)

            strip_count = self._pick_zip_strip_count(file_members, repo_name_hint, branch_name)

            for info, raw_parts in prepared_infos:
                normalized_parts = raw_parts[strip_count:]
                if not normalized_parts:
                    continue

                normalized = PurePosixPath(*normalized_parts)
                if normalized.is_absolute() or any(part == ".." for part in normalized.parts):
                    raise RuntimeError(f"Caminho inválido no ZIP: {info.filename}")

                mode = (info.external_attr >> 16) & 0o170000
                if mode == stat.S_IFLNK:
                    raise RuntimeError(f"Symlink não é permitido no ZIP: {info.filename}")

                target_rel = Path(*normalized.parts)
                if self._zip_update_should_ignore_generated_file(target_rel):
                    continue
                if info.is_dir():
                    (extract_dir / target_rel).mkdir(parents=True, exist_ok=True)
                    continue

                extract_path = extract_dir / target_rel
                extract_path.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info, "r") as src, open(extract_path, "wb") as dst:
                    shutil.copyfileobj(src, dst)

                accepted.append((extract_path, target_rel))

        if not accepted:
            raise RuntimeError("O ZIP não trouxe nenhum arquivo aplicável.")
        return accepted

    def _apply_patch_to_clone(self, extracted_files: list[tuple[Path, Path]], clone_dir: Path) -> list[str]:
        changed_files: list[str] = []
        for extracted_path, rel_path in extracted_files:
            destination = (clone_dir / rel_path).resolve()
            clone_root = clone_dir.resolve()
            if clone_root not in destination.parents and destination != clone_root:
                raise RuntimeError(f"Arquivo fora do repositório: {rel_path.as_posix()}")

            destination.parent.mkdir(parents=True, exist_ok=True)
            before = destination.read_bytes() if destination.exists() else None
            data = extracted_path.read_bytes()
            if before == data:
                continue
            if self._zip_update_is_callkeeper_protected_path(rel_path):
                raise RuntimeError(f"Arquivo protegido do CallKeeper não pode ser alterado pelo updater comum: {rel_path.as_posix()}")
            destination.write_bytes(data)
            changed_files.append(rel_path.as_posix())
        return changed_files

    def _process_zip_update_sync(self, zip_path: Path, status_context: dict[str, object] | None = None) -> dict[str, object]:
        self._update_temp_root.mkdir(parents=True, exist_ok=True)
        env = self._git_env()
        origin_result = self._run_cmd(["git", "remote", "get-url", "origin"], self._repo_root, env=env)
        if origin_result.returncode != 0:
            raise RuntimeError(f"Não foi possível descobrir o origin do git. {origin_result.stderr.strip() or origin_result.stdout.strip()}")
        origin_url = (origin_result.stdout or "").strip()
        if not origin_url:
            raise RuntimeError("O repositório local não tem origin configurado.")

        branch_result = self._run_cmd(["git", "rev-parse", "--abbrev-ref", "HEAD"], self._repo_root, env=env)
        branch_name = (branch_result.stdout or "main").strip() or "main"
        if branch_result.returncode != 0 or branch_name == "HEAD":
            branch_name = "main"

        work_dir = Path(tempfile.mkdtemp(prefix="discord-auto-update-", dir=str(self._update_temp_root)))
        extract_dir = work_dir / "extracted"
        extract_dir.mkdir(parents=True, exist_ok=True)
        timings: dict[str, int] = {}
        t0 = time.monotonic()
        last = t0

        def mark(label: str) -> None:
            nonlocal last
            now = time.monotonic()
            timings[label] = int((now - last) * 1000)
            last = now

        try:
            repo_name_hint = self._guess_repo_name(origin_url)
            worker_zip_validation = self._phone_worker_validate_zip_sync(zip_path)
            mark("phone_worker_zip_validate_ms")
            if worker_zip_validation and not bool(worker_zip_validation.get("ok", True)):
                errors = worker_zip_validation.get("errors") or []
                if isinstance(errors, list) and errors:
                    raise RuntimeError("ZIP bloqueado pelo phone-worker: " + "; ".join(str(item) for item in errors[:3]))
                raise RuntimeError("ZIP bloqueado pelo phone-worker")

            extracted_files = self._safe_extract_patch(zip_path, extract_dir, repo_name_hint, branch_name)
            mark("extract_ms")

            clone_dir = self._prepare_update_staging_clone(origin_url, branch_name, env)
            mark("staging_prepare_ms")

            changed_files = self._apply_patch_to_clone(extracted_files, clone_dir)
            mark("apply_patch_ms")
            if not changed_files:
                timings["total_ms"] = int((time.monotonic() - t0) * 1000)
                return {
                    "changed_files": [],
                    "commit_hash": None,
                    "triggered_update": False,
                    "trigger_detail": "sem mudanças",
                    "branch": branch_name,
                    "timings": timings,
                }

            self._run_cmd(["git", "config", "user.name", "Discord Auto Update"], clone_dir, env=env)
            self._run_cmd(["git", "config", "user.email", "discord-auto-update@local"], clone_dir, env=env)

            add_result = self._run_cmd(["git", "add", "--", *changed_files], clone_dir, env=env)
            if add_result.returncode != 0:
                err = (add_result.stderr or add_result.stdout or "").strip()
                raise RuntimeError(f"Falha ao preparar arquivos para commit. {err}")

            status_result = self._run_cmd(["git", "status", "--porcelain"], clone_dir, env=env)
            if status_result.returncode != 0:
                err = (status_result.stderr or status_result.stdout or "").strip()
                raise RuntimeError(f"Falha ao verificar alterações do staging persistente. {err}")
            if not (status_result.stdout or "").strip():
                timings["total_ms"] = int((time.monotonic() - t0) * 1000)
                return {
                    "changed_files": [],
                    "commit_hash": None,
                    "triggered_update": False,
                    "trigger_detail": "sem mudanças",
                    "branch": branch_name,
                    "timings": timings,
                }

            diff_stats = self._git_diff_numstat_sync(clone_dir, env)
            changed_files = [str(item.get("path")) for item in diff_stats.get("entries", []) if item.get("path")] or changed_files
            protected_changed = [path for path in changed_files if self._zip_update_is_callkeeper_protected_path(path)]
            if protected_changed:
                raise RuntimeError("Patch bloqueado: arquivos do CallKeeper são protegidos pelo updater comum: " + ", ".join(protected_changed[:5]))
            mark("diff_ms")

            base_result = self._run_cmd(["git", "rev-parse", f"origin/{branch_name}"], clone_dir, env=env)
            base_commit = (base_result.stdout or "").strip() if base_result.returncode == 0 else None

            candidate = self._write_local_update_candidate_sync(
                branch_name=branch_name,
                base_commit=base_commit,
                changed_files=changed_files,
                diff_stats=diff_stats,
                extracted_files=extracted_files,
                zip_name=zip_path.name,
                status_context=status_context,
            )
            mark("candidate_write_ms")

            triggered_update, trigger_detail = self._trigger_updater_service_sync()
            mark("trigger_updater_ms")
            timings["total_ms"] = int((time.monotonic() - t0) * 1000)

            return {
                "changed_files": changed_files,
                "diff_stats": diff_stats,
                "commit_hash": None,
                "candidate_id": candidate.get("candidate_id"),
                "candidate_dir": candidate.get("candidate_dir"),
                "triggered_update": triggered_update,
                "trigger_detail": trigger_detail,
                "branch": branch_name,
                "phone_worker_zip_validation": worker_zip_validation,
                "timings": timings,
                "staging_dir": str(clone_dir),
            }
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)


    def handle_internal_update_action(self, action: str, payload: dict[str, object]) -> dict[str, object]:
        if action == "zip_status":
            future = asyncio.run_coroutine_threadsafe(self._edit_zip_status_from_update(payload), self.loop)
            return future.result(timeout=15)
        if action == "create_zip_status":
            future = asyncio.run_coroutine_threadsafe(self._create_zip_status_from_update(payload), self.loop)
            return future.result(timeout=15)
        if action != "reload_cogs":
            return {"ok": False, "error": "ação interna desconhecida"}
        modules_raw = payload.get("modules") if isinstance(payload, dict) else None
        if not isinstance(modules_raw, list):
            return {"ok": False, "error": "lista de módulos ausente"}
        modules = [str(item).strip() for item in modules_raw if str(item).strip()]
        if not modules:
            return {"ok": False, "error": "nenhuma cog para recarregar"}
        if any(module == "cogs.call_keeper" for module in modules):
            return {"ok": False, "error": "CallKeeper é protegido e não pode ser recarregado pelo updater comum"}

        future = asyncio.run_coroutine_threadsafe(self._reload_cogs_for_update(modules), self.loop)
        return future.result(timeout=25)

    async def _create_zip_status_from_update(self, payload: dict[str, object]) -> dict[str, object]:
        title = str(payload.get("title") or "Update").strip() or "Update"
        description = str(payload.get("description") or "").strip()
        status = str(payload.get("status") or "info").lower().strip()
        title = self._zip_update_normalize_title(title, status)
        color = self._zip_update_status_color(status)
        try:
            channel = self.get_channel(self.ZIP_UPDATE_CHANNEL_ID)
            if channel is None:
                channel = await self.fetch_channel(self.ZIP_UPDATE_CHANNEL_ID)
            if channel is None or not hasattr(channel, "send"):
                return {"ok": False, "error": "canal de update indisponível"}
            view = self._make_zip_update_view(title, description, color)
            msg = await channel.send(view=view, allowed_mentions=discord.AllowedMentions.none())
            return {"ok": True, "channel_id": str(getattr(channel, "id", self.ZIP_UPDATE_CHANNEL_ID)), "message_id": str(msg.id)}
        except Exception as exc:
            logging.getLogger("zip_update").warning("falha ao criar mensagem de update pelo updater", exc_info=True)
            return {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:200]}"}

    async def _edit_zip_status_from_update(self, payload: dict[str, object]) -> dict[str, object]:
        try:
            channel_id = int(str(payload.get("channel_id") or "0"))
            message_id = int(str(payload.get("message_id") or "0"))
        except Exception:
            return {"ok": False, "error": "channel_id/message_id inválido"}
        if not channel_id or not message_id:
            return {"ok": False, "error": "channel_id/message_id ausente"}

        title = str(payload.get("title") or "Update").strip() or "Update"
        description = str(payload.get("description") or "").strip()
        status = str(payload.get("status") or "info").lower().strip()
        title = self._zip_update_normalize_title(title, status)
        color = self._zip_update_status_color(status)
        control_raw = payload.get("control") if isinstance(payload, dict) else None
        control: dict[str, object] | None = None
        state_record: dict[str, object] | None = None

        if isinstance(control_raw, dict) and control_raw.get("enabled"):
            old_state = self._zip_update_state_load()
            previous = old_state.get("latest") if isinstance(old_state.get("latest"), dict) else None
            token = str(control_raw.get("token") or uuid.uuid4().hex[:16])[:24]
            mode = str(control_raw.get("mode") or "rollback").lower().strip()
            if mode not in {"rollback", "redo"}:
                mode = "rollback"
            state_record = {
                "token": token,
                "mode": mode,
                "channel_id": str(channel_id),
                "message_id": str(message_id),
                "title": title,
                "description": description,
                "status": status,
                "branch": str(control_raw.get("branch") or payload.get("branch") or "main"),
                "expected_head": str(control_raw.get("expected_head") or control_raw.get("head_commit") or ""),
                "revert_commit": str(control_raw.get("revert_commit") or control_raw.get("head_commit") or ""),
                "source_author_id": str(control_raw.get("source_author_id") or ""),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            for key in ("update_from", "update_to", "rollback_commit", "redo_commit"):
                if control_raw.get(key):
                    state_record[key] = str(control_raw.get(key))
            await self._zip_update_clear_previous_control(previous, keep_message_id=message_id)
            self._zip_update_state_save({"latest": state_record})
            control = self._zip_update_control_for_record(state_record)
        elif status in {"success", "ok", "warn", "error"}:
            # Um estado finalizado sem controle explícito deixa controles antigos
            # inativos. Isso evita que rollback/redo indisponível mantenha estado
            # interno de processamento depois que a própria mensagem já ficou sem botão.
            old_state = self._zip_update_state_load()
            previous = old_state.get("latest") if isinstance(old_state.get("latest"), dict) else None
            await self._zip_update_clear_previous_control(previous, keep_message_id=message_id)
            self._zip_update_state_save({})

        try:
            status_message = await self._zip_update_fetch_message(channel_id, message_id)
            if status_message is None:
                return {"ok": False, "error": "mensagem não encontrada"}
            view = self._make_zip_update_view(title, description, color, control=control)
            await status_message.edit(view=view, allowed_mentions=discord.AllowedMentions.none())
            return {"ok": True}
        except Exception:
            logging.getLogger("zip_update").warning("falha ao finalizar mensagem do ZIP pelo updater", exc_info=True)
            return {"ok": False, "error": "falha ao editar mensagem"}

    async def _on_zip_update_control_click(self, interaction: discord.Interaction) -> None:
        custom_id = str(getattr(getattr(interaction, "data", None), "get", lambda *_: "")("custom_id") or "")
        if not custom_id.startswith("zip_update:"):
            return
        parts = custom_id.split(":", 2)
        if len(parts) != 3:
            return
        requested_mode, token = parts[1], parts[2]
        state = self._zip_update_state_load()
        record = state.get("latest") if isinstance(state.get("latest"), dict) else None
        if not isinstance(record, dict) or str(record.get("token") or "") != token:
            if not interaction.response.is_done():
                await interaction.response.send_message("Esse controle não está mais disponível.", ephemeral=True)
            return
        if str(record.get("mode") or "") != requested_mode:
            if not interaction.response.is_done():
                await interaction.response.send_message("Esse controle não está mais disponível.", ephemeral=True)
            return
        if not self._zip_update_can_control(interaction, record):
            if not interaction.response.is_done():
                await interaction.response.send_message("Você não pode controlar este update.", ephemeral=True)
            return
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True, thinking=False)
        await self._start_zip_update_rollback_flow(interaction, record, requested_mode)

    async def _start_zip_update_rollback_flow(self, interaction: discord.Interaction, record: dict[str, object], mode: str) -> None:
        mode = "redo" if str(mode).lower().strip() == "redo" else "rollback"
        branch = str(record.get("branch") or "main")
        channel_id, message_id = self._zip_update_latest_message_ref(interaction, record)
        update_from = str(record.get("update_from") or "").strip()
        update_to = str(record.get("update_to") or "").strip()
        rollback_commit = str(record.get("rollback_commit") or "").strip()
        redo_commit = str(record.get("redo_commit") or "").strip()
        expected_head = str(record.get("expected_head") or "").strip()
        revert_commit = str(record.get("revert_commit") or "").strip()

        # O controle usa `git revert`, então o alvo técnico da ação é o commit
        # que precisa ser revertido, não o commit para onde queremos "voltar".
        # Para ◀️, o HEAD esperado é o update aplicado e o commit revertido é
        # esse próprio update. Para ▶️, o HEAD esperado é o commit de rollback e
        # o commit revertido é o rollback. Mantemos fallback explícito para
        # estados antigos/legados já salvos em disco.
        if mode == "rollback":
            # O commit técnico a reverter é sempre o HEAD atual salvo no controle.
            # `update_to` é mantido como metadado do update original e pode ficar
            # diferente depois de uma reaplicação; por isso ele só serve como
            # fallback para estados antigos que ainda não gravavam expected_head.
            expected_head = expected_head or revert_commit or update_to
            revert_commit = revert_commit or expected_head or update_to
        else:
            # Para refazer, revertemos o commit de rollback. Estados novos salvam
            # `rollback_commit`; em estados legados, `revert_commit`/expected_head
            # apontam para o commit atual que precisa ser revertido.
            expected_head = rollback_commit or expected_head or revert_commit or update_to
            revert_commit = rollback_commit or revert_commit or redo_commit or expected_head

        commit_re = re.compile(r"^[0-9a-fA-F]{7,40}$")
        if expected_head and not commit_re.fullmatch(expected_head):
            UPDATE_LOG.warning("rollback/redo com expected_head inválido: %r", expected_head)
            expected_head = ""
        if revert_commit and not commit_re.fullmatch(revert_commit):
            UPDATE_LOG.warning("rollback/redo com revert_commit inválido: %r", revert_commit)
            revert_commit = ""

        if not expected_head or not revert_commit or not channel_id or not message_id:
            try:
                await interaction.followup.send("Não encontrei os dados necessários para essa ação.", ephemeral=True)
            except Exception:
                pass
            return

        if self._zip_update_has_pending_rollback_request():
            try:
                await interaction.followup.send("Já existe uma ação de update em andamento.", ephemeral=True)
            except Exception:
                pass
            return

        request_root, permission_details = self._zip_update_find_writable_rollback_request_root()
        if request_root is None:
            detail = "; ".join(permission_details[-3:]) or "nenhuma pasta gravável encontrada"
            UPDATE_LOG.warning("falha ao preparar pasta de pedido de rollback/redo: %s", detail)
            action_name = "reaplicação" if mode == "redo" else "reversão"
            try:
                await interaction.followup.send(f"Não consegui iniciar a {action_name}: pasta de controle sem permissão de escrita.", ephemeral=True)
            except Exception:
                pass
            return

        pending = request_root / "pending.json"
        active = request_root / "active.json"

        request_id = f"{mode}-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:8]}"
        payload = {
            "id": request_id,
            "mode": mode,
            "branch": branch,
            "expected_head": expected_head,
            "revert_commit": revert_commit,
            "update_from": update_from,
            "update_to": update_to,
            "rollback_commit": rollback_commit,
            "redo_commit": redo_commit,
            "message": {"channel_id": channel_id, "message_id": message_id},
            "source_author_id": str(record.get("source_author_id") or ""),
            "requested_by": str(getattr(interaction.user, "id", "") or ""),
            "previous_record": record,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }

        tmp = pending.with_name(f"pending.{request_id}.tmp")
        try:
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
            os.replace(tmp, pending)
            # Confirma que o pedido existe antes de mexer visualmente na mensagem.
            saved = json.loads(pending.read_text(encoding="utf-8"))
            if str(saved.get("id") or "") != request_id:
                raise RuntimeError("pedido de rollback não foi salvo corretamente")
        except Exception:
            with contextlib.suppress(Exception):
                tmp.unlink()
            UPDATE_LOG.warning("falha ao salvar pedido de rollback/redo em %s", request_root, exc_info=True)
            action_name = "reaplicação" if mode == "redo" else "reversão"
            try:
                await interaction.followup.send(f"Não consegui iniciar a {action_name}: não foi possível gravar o pedido de controle.", ephemeral=True)
            except Exception:
                pass
            return

        title = "<a:areia:1496606578395189473> Reaplicando update..." if mode == "redo" else "<a:areia:1496606578395189473> Revertendo update..."
        description = "<a:loading:1510065277868445796> **Preparando validação local**"
        processing_record = dict(record)
        processing_record["status"] = "processing"
        processing_record["processing_mode"] = mode
        processing_record["processing_request_id"] = request_id
        processing_record["updated_at"] = datetime.now(timezone.utc).isoformat()
        state = self._zip_update_state_load()
        state["latest"] = processing_record
        state["active_request"] = payload
        self._zip_update_state_save(state)

        try:
            msg = interaction.message
            if msg is not None:
                view = self._make_zip_update_view(
                    title,
                    description,
                    discord.Color.gold(),
                    control=self._zip_update_control_for_record(record, disabled=True),
                )
                await msg.edit(view=view, allowed_mentions=discord.AllowedMentions.none())
        except Exception:
            UPDATE_LOG.warning("falha ao colocar update em estado de processamento", exc_info=True)

        triggered, detail = await asyncio.to_thread(self._trigger_updater_service_sync)
        if not triggered:
            UPDATE_LOG.warning("rollback/redo aguardando timer do updater: %s", detail)

        async def _rollback_start_watchdog() -> None:
            await asyncio.sleep(90)
            try:
                # Se o pedido ainda existir igual após um tempo razoável, o updater não consumiu.
                for path in (pending, active):
                    if not path.exists():
                        continue
                    data = json.loads(path.read_text(encoding="utf-8"))
                    if str(data.get("id") or "") != request_id:
                        continue
                    msg = await self._zip_update_fetch_message(int(channel_id), int(message_id))
                    if msg is not None:
                        fail_title = "Falha ao reaplicar" if mode == "redo" else "Falha ao reverter"
                        fail_desc = "A ação não avançou. O estado atual foi mantido."
                        await msg.edit(
                            view=self._make_zip_update_view(
                                fail_title,
                                fail_desc,
                                discord.Color.red(),
                                control=self._zip_update_control_for_record(record),
                            ),
                            allowed_mentions=discord.AllowedMentions.none(),
                        )
                    # Restaura o estado para permitir nova tentativa manual, mas não apaga o arquivo;
                    # o updater ainda poderá arquivar/limpar no próximo ciclo se necessário.
                    state = self._zip_update_state_load()
                    state.pop("active_request", None)
                    state["latest"] = record
                    self._zip_update_state_save(state)
                    return
            except Exception:
                UPDATE_LOG.warning("falha no watchdog de rollback/redo", exc_info=True)

        asyncio.create_task(_rollback_start_watchdog())

    async def on_interaction(self, interaction: discord.Interaction):
        try:
            custom_id = ""
            data = getattr(interaction, "data", None)
            if isinstance(data, dict):
                custom_id = str(data.get("custom_id") or "")
            if custom_id.startswith("zip_update:"):
                await self._on_zip_update_control_click(interaction)
        except Exception:
            UPDATE_LOG.exception("falha ao processar controle de update")
            try:
                if not interaction.response.is_done():
                    await interaction.response.send_message("Falha ao processar esta ação.", ephemeral=True)
            except Exception:
                pass

    async def _reload_cogs_for_update(self, modules: list[str]) -> dict[str, object]:
        results: list[dict[str, str]] = []
        for module in modules:
            if module == "cogs.call_keeper":
                return {"ok": False, "error": "CallKeeper é protegido", "results": results}
            try:
                if module in self.extensions:
                    await self.reload_extension(module)
                    results.append({"module": module, "status": "reloaded"})
                else:
                    await self.load_extension(module)
                    results.append({"module": module, "status": "loaded"})
            except Exception as exc:
                logging.getLogger("zip_update").exception("falha ao recarregar cog %s pelo updater", module)
                return {"ok": False, "error": f"{module}: {type(exc).__name__}: {str(exc)[:300]}", "results": results}
        return {"ok": True, "results": results}


    async def _handle_zip_update_message(self, message: discord.Message):
        zip_attachment = None
        for attachment in message.attachments:
            if attachment.filename.lower().endswith(".zip"):
                zip_attachment = attachment
                break

        if zip_attachment is None:
            await self._send_zip_update_message(
                message,
                "❌ Arquivo inválido",
                "Envie um arquivo **.zip** neste canal.",
                discord.Color.red(),
            )
            return

        status_message: discord.Message | None = None
        if self._zip_update_lock.locked():
            status_message = await self._send_zip_update_message(
                message,
                "📦 ZIP recebido",
                "Na fila. Vou editar esta mensagem quando terminar.",
                discord.Color.orange(),
            )

        async with self._zip_update_lock:
            self._update_temp_root.mkdir(parents=True, exist_ok=True)
            work_dir = Path(tempfile.mkdtemp(prefix="discord-auto-update-msg-", dir=str(self._update_temp_root)))
            zip_path = work_dir / zip_attachment.filename
            try:
                await zip_attachment.save(zip_path)
                if status_message is None:
                    status_message = await self._send_zip_update_message(
                        message,
                        "📦 ZIP recebido",
                        "Processando.",
                        discord.Color.blurple(),
                    )

                status_context = {
                    "guild_id": getattr(message.guild, "id", None),
                    "channel_id": getattr(status_message.channel, "id", None) if status_message else getattr(message.channel, "id", None),
                    "message_id": getattr(status_message, "id", None),
                    "source_message_id": getattr(message, "id", None),
                    "source_author_id": getattr(message.author, "id", None),
                }
                result = await asyncio.to_thread(self._process_zip_update_sync, zip_path, status_context)
                changed_files = list(result.get("changed_files") or [])
                commit_hash = result.get("commit_hash")
                branch = result.get("branch") or "main"
                triggered_update = bool(result.get("triggered_update"))

                if not changed_files:
                    await self._edit_zip_update_message(
                        message,
                        status_message,
                        "ℹ️ Nenhuma alteração",
                        "ZIP válido. Nenhuma mudança no repositório.",
                        discord.Color.gold(),
                    )
                    return

                diff_stats = result.get("diff_stats") if isinstance(result.get("diff_stats"), dict) else {}
                entries = list(diff_stats.get("entries") or []) if isinstance(diff_stats, dict) else []
                diff_summary = str(diff_stats.get("summary") or "").strip() if isinstance(diff_stats, dict) else ""
                if not diff_summary:
                    diff_summary = "diff indisponível"

                def fmt_entry(entry: object) -> str:
                    if isinstance(entry, dict):
                        path = str(entry.get("path") or "")
                        if bool(entry.get("binary")):
                            return f"• `{path}`  binário"
                        return f"• `{path}`  +{int(entry.get('added') or 0)} -{int(entry.get('removed') or 0)}"
                    return f"• `{entry}`"

                if entries:
                    preview_files = "\n".join(fmt_entry(entry) for entry in entries[:20])
                    remaining = max(0, len(entries) - 20)
                else:
                    preview_files = "\n".join(f"• `{path}`" for path in changed_files[:20])
                    remaining = max(0, len(changed_files) - 20)
                if remaining:
                    preview_files += f"\n+{remaining} arquivo(s) restante(s)"

                await self._edit_zip_update_message(
                    message,
                    status_message,
                    "<a:areia:1496606578395189473> Aplicando update...",
                    (
                        "<a:loading:1510065277868445796> **Validando ZIP**\n"
                        "Conferindo base local e arquivos recebidos."
                    ),
                    discord.Color.blurple(),
                )
            except zipfile.BadZipFile:
                await self._edit_zip_update_message(
                    message,
                    status_message,
                    "❌ ZIP inválido",
                    "O arquivo não pôde ser aberto como ZIP válido.",
                    discord.Color.red(),
                )
            except Exception as e:
                logging.getLogger("zip_update").exception(
                    "Falha no auto-update via ZIP do Discord"
                )
                reason = str(e).strip() or type(e).__name__
                if len(reason) > 600:
                    reason = reason[:597].rstrip() + "..."
                await self._edit_zip_update_message(
                    message,
                    status_message,
                    "❌ Falha no update",
                    f"Nada foi aplicado. Motivo: **{reason}**",
                    discord.Color.red(),
                )
            finally:
                shutil.rmtree(work_dir, ignore_errors=True)

    async def close(self):
        if self._event_loop_watchdog_task is not None:
            self._event_loop_watchdog_task.cancel()
        if self._health_task is not None:
            self._health_task.cancel()
        router = getattr(self, "audio_router", None)
        if router is not None:
            try:
                await router.close()
            except Exception as e:
                print(f"[bot] falha ao fechar audio_router: {e!r}")
        await super().close()

    async def on_ready(self):
        print(f"Logado como {self.user} (id: {self.user.id})")
        print(f"Em {len(self.guilds)} servidor(es)")
        try:
            await self.change_presence(
                activity=discord.Activity(
                    type=discord.ActivityType.listening,
                    name="/help | _help",
                )
            )
        except Exception as e:
            print(f"[bot] falha ao aplicar presence: {e!r}")
        if not self._music_bitrate_reconciled:
            self._music_bitrate_reconciled = True
            router = getattr(self, "audio_router", None)
            if router is not None and hasattr(router, "reconcile_auto_bitrate_records"):
                try:
                    await router.reconcile_auto_bitrate_records()
                except Exception as e:
                    logging.getLogger("music").debug("reconciliação de bitrate automático falhou: %r", e, exc_info=True)
        if not self._music_voice_status_reconciled:
            self._music_voice_status_reconciled = True
            router = getattr(self, "audio_router", None)
            if router is not None and hasattr(router, "reconcile_voice_status_records"):
                try:
                    await router.reconcile_voice_status_records()
                except Exception as e:
                    logging.getLogger("music").debug("reconciliação de status de canal falhou: %r", e, exc_info=True)
        if self._health_task is None or self._health_task.done():
            self._health_task = asyncio.create_task(self._health_monitor_loop())
        if self._event_loop_watchdog_task is None or self._event_loop_watchdog_task.done():
            self._event_loop_watchdog_task = asyncio.create_task(self._event_loop_watchdog_loop())

    async def on_message(self, message: discord.Message):
        if getattr(message.author, "bot", False):
            return
        try:
            if int(getattr(message.channel, "id", 0)) == self.ZIP_UPDATE_CHANNEL_ID:
                await self._handle_zip_update_message(message)
                return
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
