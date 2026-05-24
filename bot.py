import asyncio
import base64
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
from webserver import run_webserver, set_health_provider
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
        self.audio_router = AudioRouter(self)
        self._music_bitrate_reconciled = False
        self._music_voice_status_reconciled = False

        self.loaded_extensions: list[str] = []
        self.failed_extensions: dict[str, dict[str, object]] = {}
        self.skipped_extensions: dict[str, str] = {}
        self.critical_extensions: set[str] = self._read_critical_extensions()
        self.cog_loading_finished = False

        set_health_provider(self.get_health_snapshot)

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

        print("Carregando cogs...")
        await self._load_cogs_safely()

        should_sync = _env_truthy("SYNC_SLASH_COMMANDS")
        allow_global_sync = _env_truthy("SYNC_GLOBAL_SLASH_COMMANDS")

        health_guild_id = 927002914449424404
        guild_ids = {int(gid) for gid in (getattr(config, "GUILD_IDS", []) or []) if gid}
        guild_ids.add(health_guild_id)

        try:
            callkeeper_guild_id = int(getattr(config, "CALLKEEPER_GUILD_ID", 0) or 0)
        except Exception:
            callkeeper_guild_id = 0
        if callkeeper_guild_id > 0:
            guild_ids.add(callkeeper_guild_id)

        # Limpa slash antigos que foram substituídos por comandos de prefixo/triggers,
        # sem usar clear_commands e sem afetar comandos de outras cogs.
        await self._cleanup_removed_slash_commands(guild_ids)

        # One-shot flag: limpa comandos globais antes de sync guild. Útil quando
        # o bot antes rodava com sync global e agora tá em modo guild-only —
        # sem isso, os comandos globais fantasmas continuam registrados e o
        # Discord mostra cada comando em duplicata (um global + um guild).
        clear_globals_on_boot = _env_truthy("CLEAR_GLOBAL_COMMANDS")
        if should_sync:
            if allow_global_sync:
                # Modo global: sincroniza global mas TAMBÉM faz sync de cada
                # guild registrada pra propagar os Groups com guild_ids
                # explícitos (que NÃO entram no sync global). Não usamos
                # clear_commands aqui pelas mesmas razões do branch guild-only
                # mais abaixo.
                synced_global = await self.tree.sync()
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
                # Modo guild-only: se vinha de sync global antes, limpa os
                # globais pra evitar duplicação. Só faz isso se
                # CLEAR_GLOBAL_COMMANDS=true no .env.
                #
                # IMPORTANTE: NÃO usar `clear_commands(guild=None) + sync()` pra
                # limpar globais — isso é um bulk update com lista vazia e o
                # Discord rejeita (error 50240) porque apagaria o Entry Point
                # da Activity junto. Em vez disso, busca os comandos globais
                # registrados e deleta um por um, preservando Entry Point
                # (AppCommandType.primary_entry_point, valor 4).
                if clear_globals_on_boot:
                    try:
                        existing_globals = await self.tree.fetch_commands()
                    except Exception as e:
                        print(f"[SYNC] Não consegui buscar comandos globais: {e}")
                        existing_globals = []
                    deleted = 0
                    preserved = 0
                    for cmd in existing_globals:
                        # Entry Point da Activity = type 4. Discord força a
                        # preservação dele; qualquer tentativa de deletar
                        # via bulk com lista vazia falha.
                        cmd_type = getattr(cmd, "type", None)
                        type_value = getattr(cmd_type, "value", cmd_type)
                        if type_value == 4:
                            preserved += 1
                            print(f"[SYNC][GLOBAL] preservado Entry Point: /{cmd.name}")
                            continue
                        try:
                            await cmd.delete()
                            deleted += 1
                            print(f"[SYNC][GLOBAL] deletado: /{cmd.name}")
                        except Exception as e:
                            print(f"[SYNC][GLOBAL] falha ao deletar /{cmd.name}: {e}")
                    print(f"[SYNC] Limpeza global: {deleted} deletados, {preserved} preservados (Entry Point)")
                else:
                    print("[SYNC] Sync global pulado. Se você vê comandos duplicados,")
                    print("[SYNC] rode UMA VEZ com CLEAR_GLOBAL_COMMANDS=true no .env pra limpar.")

                for guild_id in sorted(guild_ids):
                    guild_obj = discord.Object(id=guild_id)
                    # NÃO chamar `clear_commands(guild=guild_obj)` aqui!
                    # Isso apaga da árvore local TODOS os comandos guild-specific,
                    # incluindo os Groups com `guild_ids=[...]` que estão
                    # registrados nativamente pra essa guild (ex: /chatbotadmin
                    # registrado pra MANAGEMENT_GUILD_ID, /vps da Utility).
                    # Depois do clear, copy_global_to só repõe os globais —
                    # os guild-restricted desaparecem do sync e o usuário não
                    # vê os comandos no autocomplete.
                    #
                    # Tradeoff: comandos que foram REMOVIDOS do código continuam
                    # como "fantasmas" no Discord até alguém rodar manualmente
                    # `tree.clear_commands(guild=...)` + sync. Isso é raro e
                    # vale a pena pelo benefício de Groups guild-restricted
                    # funcionarem sem cuidado especial.
                    self.tree.copy_global_to(guild=guild_obj)
                    synced_guild = await self.tree.sync(guild=guild_obj)
                    print(f"[SYNC] Slash commands sincronizados na guild {guild_id}: {len(synced_guild)}")
                    for cmd in synced_guild:
                        name = getattr(cmd, "name", None) or str(cmd)
                        print(f"[SYNC][GUILD {guild_id}] /{name}")
        else:
            print("[SYNC] Pulado no boot (defina SYNC_SLASH_COMMANDS=true para sincronizar no startup)")
            print("[SYNC] Observação: comandos limitados por guild, como /vps, só aparecem após sync da guild correspondente.")

    def get_health_snapshot(self) -> dict[str, object]:
        snapshot = dict(self.health_state)
        uptime_seconds = (datetime.now(timezone.utc) - self.started_at).total_seconds()
        snapshot["uptime_seconds"] = round(uptime_seconds, 2)
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

    def _make_zip_update_embed(self, title: str, description: str, color: discord.Color) -> discord.Embed:
        embed = discord.Embed(title=title, description=description, color=color)
        embed.timestamp = datetime.now(timezone.utc)
        return embed

    async def _send_zip_update_message(self, message: discord.Message, title: str, description: str, color: discord.Color):
        embed = self._make_zip_update_embed(title, description, color)
        await message.reply(embed=embed, mention_author=False)

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
            destination.write_bytes(data)
            changed_files.append(rel_path.as_posix())
        return changed_files

    def _process_zip_update_sync(self, zip_path: Path) -> dict[str, object]:
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
        clone_dir = work_dir / "clone"
        extract_dir.mkdir(parents=True, exist_ok=True)

        try:
            repo_name_hint = self._guess_repo_name(origin_url)
            worker_zip_validation = self._phone_worker_validate_zip_sync(zip_path)
            if worker_zip_validation and not bool(worker_zip_validation.get("ok", True)):
                errors = worker_zip_validation.get("errors") or []
                if isinstance(errors, list) and errors:
                    raise RuntimeError("ZIP bloqueado pelo phone-worker: " + "; ".join(str(item) for item in errors[:3]))
                raise RuntimeError("ZIP bloqueado pelo phone-worker")

            extracted_files = self._safe_extract_patch(zip_path, extract_dir, repo_name_hint, branch_name)

            clone_result = self._run_cmd(["git", "clone", "--branch", branch_name, "--single-branch", origin_url, str(clone_dir)], work_dir, env=env)
            if clone_result.returncode != 0:
                err = (clone_result.stderr or clone_result.stdout or "").strip()
                raise RuntimeError(f"Falha ao clonar o repositório temporário. {err}")

            changed_files = self._apply_patch_to_clone(extracted_files, clone_dir)
            if not changed_files:
                return {
                    "changed_files": [],
                    "commit_hash": None,
                    "triggered_update": False,
                    "branch": branch_name,
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
                raise RuntimeError(f"Falha ao verificar alterações do clone temporário. {err}")
            if not (status_result.stdout or "").strip():
                return {
                    "changed_files": [],
                    "commit_hash": None,
                    "triggered_update": False,
                    "branch": branch_name,
                }

            commit_message = f"auto update from discord zip ({len(changed_files)} arquivo(s))"
            commit_result = self._run_cmd(["git", "commit", "-m", commit_message], clone_dir, env=env)
            if commit_result.returncode != 0:
                err = (commit_result.stderr or commit_result.stdout or "").strip()
                raise RuntimeError(f"Falha ao criar commit do update automático. {err}")

            push_result = self._run_cmd(["git", "push", "origin", branch_name], clone_dir, env=env)
            if push_result.returncode != 0:
                err = (push_result.stderr or push_result.stdout or "").strip()
                raise RuntimeError(f"Falha ao enviar update para o GitHub. {err}")

            hash_result = self._run_cmd(["git", "rev-parse", "HEAD"], clone_dir, env=env)
            commit_hash = (hash_result.stdout or "").strip() if hash_result.returncode == 0 else None

            updater_service = Path("/etc/systemd/system/tts-bot-updater.service")
            updater_timer = Path("/etc/systemd/system/tts-bot-updater.timer")
            triggered_update = updater_service.exists() and updater_timer.exists()

            return {
                "changed_files": changed_files,
                "commit_hash": commit_hash,
                "triggered_update": triggered_update,
                "branch": branch_name,
                "phone_worker_zip_validation": worker_zip_validation,
            }
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)


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
                "Envie um arquivo **.zip** neste canal para iniciar a atualização automática do projeto.",
                discord.Color.red(),
            )
            return

        if self._zip_update_lock.locked():
            await self._send_zip_update_message(
                message,
                "⏳ Atualização em andamento",
                "Já existe outra atualização automática processando um ZIP neste momento. Aguarde ela terminar e envie novamente.",
                discord.Color.orange(),
            )
            return

        async with self._zip_update_lock:
            self._update_temp_root.mkdir(parents=True, exist_ok=True)
            work_dir = Path(tempfile.mkdtemp(prefix="discord-auto-update-msg-", dir=str(self._update_temp_root)))
            zip_path = work_dir / zip_attachment.filename
            try:
                await zip_attachment.save(zip_path)
                await self._send_zip_update_message(
                    message,
                    "📦 ZIP recebido",
                    "Arquivo baixado fora do repositório. Vou validar, ignorar lixo de build, aplicar em clone temporário, enviar para o GitHub e deixar o updater via systemd aplicar automaticamente.",
                    discord.Color.blurple(),
                )

                result = await asyncio.to_thread(self._process_zip_update_sync, zip_path)
                changed_files = list(result.get("changed_files") or [])
                commit_hash = result.get("commit_hash")
                branch = result.get("branch") or "main"
                triggered_update = bool(result.get("triggered_update"))

                if not changed_files:
                    await self._send_zip_update_message(
                        message,
                        "ℹ️ Nenhuma alteração aplicada",
                        "O ZIP foi válido, mas não mudou nenhum arquivo do repositório. Nada foi commitado no GitHub.",
                        discord.Color.gold(),
                    )
                    return

                preview_files = "\n".join(f"• `{path}`" for path in changed_files[:10])
                if len(changed_files) > 10:
                    preview_files += f"\n• ... e mais {len(changed_files) - 10} arquivo(s)"
                short_hash = str(commit_hash)[:7] if commit_hash else "desconhecido"
                update_line = "O commit foi enviado ao GitHub. O updater via systemd verificará e aplicará automaticamente em até 1 minuto." if triggered_update else "O commit foi enviado ao GitHub, mas o updater via systemd não foi encontrado nesta VPS."
                worker_validation = result.get("phone_worker_zip_validation")
                worker_line = ""
                if isinstance(worker_validation, dict):
                    risk = worker_validation.get("risk") or "ok"
                    files = worker_validation.get("files")
                    worker_line = f"\nValidação celular: **{risk}** ({files} arquivo(s))."
                await self._send_zip_update_message(
                    message,
                    "✅ Update enviado para o GitHub",
                    f"Branch: **{branch}**\nCommit: **{short_hash}**\nArquivos alterados: **{len(changed_files)}**{worker_line}\n\n{preview_files}\n\n{update_line}",
                    discord.Color.green(),
                )
            except zipfile.BadZipFile:
                await self._send_zip_update_message(
                    message,
                    "❌ ZIP inválido",
                    "O arquivo enviado não pôde ser aberto como ZIP válido. Nenhuma alteração foi aplicada.",
                    discord.Color.red(),
                )
            except Exception as e:
                logging.getLogger("zip_update").exception(
                    "Falha no auto-update via ZIP do Discord"
                )
                await self._send_zip_update_message(
                    message,
                    "❌ Falha no update automático",
                    f"Nada foi aplicado. Motivo: **{e}**",
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
