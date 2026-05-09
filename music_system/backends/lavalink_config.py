from __future__ import annotations

import contextlib
import json
import logging
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import config

from .lavalink import LavalinkConfig, _as_bool, _normalize_mode, _safe_int

logger = logging.getLogger(__name__)


BASE_DIR = Path(getattr(config, "BASE_DIR", ".")).resolve()
DEFAULT_MUSICNODE_DB_PATH = BASE_DIR / "data" / "musicnode" / "musicnode.db"
LEGACY_LAVALINK_JSON_PATH = BASE_DIR / "data" / "music" / "lavalink_config.json"
_ALLOWED_MODES = {"off", "shadow", "lavalink", "auto"}
_DEFAULT_OPTIONS = {"hide_host_in_panel": True, "test_after_save": False}
_SCHEMA_VERSION = 1


def _safe_float(value: object, default: float = 8.0) -> float:
    try:
        return float(str(value).strip().replace(",", "."))
    except Exception:
        return default


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _env_config() -> LavalinkConfig:
    return LavalinkConfig(
        enabled=_as_bool(getattr(config, "LAVALINK_ENABLED", False), False),
        mode=_normalize_mode(getattr(config, "LAVALINK_MODE", "off")),
        host=str(getattr(config, "LAVALINK_HOST", "") or "").strip(),
        port=max(1, _safe_int(getattr(config, "LAVALINK_PORT", 2333), 2333)),
        password=str(getattr(config, "LAVALINK_PASSWORD", "") or "").strip(),
        secure=_as_bool(getattr(config, "LAVALINK_SECURE", False), False),
        node_name=str(getattr(config, "LAVALINK_NODE_NAME", "main") or "main").strip() or "main",
        timeout_seconds=max(2.0, _safe_float(getattr(config, "LAVALINK_TIMEOUT_SECONDS", 8.0), 8.0)),
    )


def _options_from_mapping(raw: dict[str, Any] | None, *, fallback: dict[str, Any] | None = None) -> dict[str, bool]:
    base = dict(_DEFAULT_OPTIONS)
    if fallback:
        for key in _DEFAULT_OPTIONS:
            if key in fallback:
                base[key] = _as_bool(fallback.get(key), base[key])
    if isinstance(raw, dict):
        for key in _DEFAULT_OPTIONS:
            if key in raw:
                base[key] = _as_bool(raw.get(key), base[key])
    return base


def _legacy_config_from_mapping(raw: dict[str, Any], *, fallback: LavalinkConfig | None = None) -> LavalinkConfig:
    fallback = fallback or _env_config()
    node = raw.get("node") if isinstance(raw.get("node"), dict) else raw
    if not isinstance(node, dict):
        node = {}
    mode = _normalize_mode(raw.get("mode", getattr(fallback, "mode", "off")))
    if mode not in _ALLOWED_MODES:
        mode = "off"
    enabled = _as_bool(raw.get("enabled"), mode != "off")
    return LavalinkConfig(
        enabled=enabled,
        mode=mode,
        host=str(node.get("host", fallback.host) or "").strip(),
        port=max(1, _safe_int(node.get("port", fallback.port), fallback.port or 2333)),
        password=str(node.get("password", fallback.password) or "").strip(),
        secure=_as_bool(node.get("secure"), fallback.secure),
        node_name=str(node.get("name", fallback.node_name) or "main").strip() or "main",
        timeout_seconds=max(2.0, _safe_float(raw.get("timeout_seconds", fallback.timeout_seconds), fallback.timeout_seconds or 8.0)),
    )




def _enable_lavalink_for_all_guilds() -> bool:
    """Migração atual: servidores sem override herdam Lavalink real por padrão.

    O dono ainda pode desligar definindo MUSIC_LAVALINK_ENABLE_ALL_GUILDS=false
    ou colocando um override off no `_musicnode`.
    """
    return _as_bool(getattr(config, "MUSIC_LAVALINK_ENABLE_ALL_GUILDS", True), True)


def _row_has_usable_node(row: sqlite3.Row | dict[str, Any] | None) -> bool:
    if row is None:
        return False
    try:
        return bool(_as_bool(row["enabled"], False) and str(row["host"] or "").strip() and str(row["password"] or "").strip())
    except Exception:
        return False

def _normalize_options_from_row(row: sqlite3.Row | dict[str, Any] | None) -> dict[str, bool]:
    if row is None:
        return dict(_DEFAULT_OPTIONS)
    return {
        "hide_host_in_panel": _as_bool(row["hide_host"] if "hide_host" in row.keys() else None, True),
        "test_after_save": _as_bool(row["test_on_save"] if "test_on_save" in row.keys() else None, False),
    }


class LavalinkConfigStore:
    """Configuração persistida do painel `_musicnode` em SQLite separado.

    O DB fica fora do banco principal do bot para isolar senha/config técnica do
    node Lavalink. Ele deve permanecer em `.gitignore` e nunca ser exibido em
    mensagens/logs. O JSON usado nos patches anteriores é lido apenas uma vez
    como migração automática, se existir na VPS.
    """

    def __init__(
        self,
        db_path: str | os.PathLike[str] | None = None,
        *,
        legacy_json_path: str | os.PathLike[str] | None = None,
    ) -> None:
        self.db_path = Path(db_path or DEFAULT_MUSICNODE_DB_PATH)
        self.legacy_json_path = Path(legacy_json_path or LEGACY_LAVALINK_JSON_PATH)
        self._lock = threading.RLock()
        self._initialized = False

    @property
    def path(self) -> Path:
        # Compatibilidade com o código/painel antigo que lia `store.path`.
        return self.db_path

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path), timeout=8.0)
        conn.row_factory = sqlite3.Row
        with contextlib.suppress(Exception):
            conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    @contextlib.contextmanager
    def _connection(self):
        conn = self._connect()
        try:
            yield conn
        finally:
            conn.close()

    def _ensure_ready(self) -> None:
        with self._lock:
            if self._initialized:
                return
            with self._connection() as conn:
                self._create_schema(conn)
                self._ensure_default_node(conn)
                self._migrate_legacy_json_once(conn)
                conn.commit()
            self._initialized = True

    def _create_schema(self, conn: sqlite3.Connection) -> None:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS lavalink_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS lavalink_nodes (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                name TEXT NOT NULL DEFAULT 'main',
                host TEXT NOT NULL DEFAULT '',
                port INTEGER NOT NULL DEFAULT 2333,
                password TEXT NOT NULL DEFAULT '',
                secure INTEGER NOT NULL DEFAULT 0,
                enabled INTEGER NOT NULL DEFAULT 0,
                timeout_seconds REAL NOT NULL DEFAULT 8.0,
                hide_host INTEGER NOT NULL DEFAULT 1,
                test_on_save INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS lavalink_guild_settings (
                guild_id INTEGER PRIMARY KEY,
                mode TEXT NOT NULL DEFAULT 'off',
                fallback_enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )
        conn.execute(
            "INSERT OR REPLACE INTO lavalink_meta(key, value) VALUES('schema_version', ?)",
            (str(_SCHEMA_VERSION),),
        )

    def _ensure_default_node(self, conn: sqlite3.Connection) -> None:
        row = conn.execute("SELECT id FROM lavalink_nodes WHERE id = 1").fetchone()
        if row is not None:
            return
        now = _utc_now_iso()
        env_cfg = _env_config()
        options = dict(_DEFAULT_OPTIONS)
        conn.execute(
            """
            INSERT INTO lavalink_nodes(
                id, name, host, port, password, secure, enabled, timeout_seconds,
                hide_host, test_on_save, created_at, updated_at
            ) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                env_cfg.node_name or "main",
                env_cfg.host or "",
                int(env_cfg.port or 2333),
                env_cfg.password or "",
                1 if env_cfg.secure else 0,
                1 if env_cfg.enabled else 0,
                float(env_cfg.timeout_seconds or 8.0),
                1 if options["hide_host_in_panel"] else 0,
                1 if options["test_after_save"] else 0,
                now,
                now,
            ),
        )
        # Modo global herdado do env apenas como fallback inicial.
        if env_cfg.mode and env_cfg.mode != "off":
            conn.execute(
                "INSERT OR REPLACE INTO lavalink_meta(key, value) VALUES('default_mode', ?)",
                (env_cfg.mode,),
            )

    def _read_legacy_payload(self) -> dict[str, Any] | None:
        if not self.legacy_json_path.exists():
            return None
        try:
            raw = json.loads(self.legacy_json_path.read_text(encoding="utf-8"))
            return raw if isinstance(raw, dict) else None
        except Exception:
            logger.warning("[music/lavalink] falha ao ler JSON legado do Lavalink para migração", exc_info=True)
            return None

    def _migrate_legacy_json_once(self, conn: sqlite3.Connection) -> None:
        imported = conn.execute(
            "SELECT value FROM lavalink_meta WHERE key = 'legacy_json_imported'"
        ).fetchone()
        if imported is not None:
            return
        payload = self._read_legacy_payload()
        now = _utc_now_iso()
        if payload is None:
            conn.execute(
                "INSERT OR REPLACE INTO lavalink_meta(key, value) VALUES('legacy_json_imported', ?)",
                (now,),
            )
            conn.execute(
                "INSERT OR REPLACE INTO lavalink_meta(key, value) VALUES('legacy_json_migrated', '0')"
            )
            return

        cfg = _legacy_config_from_mapping(payload, fallback=_env_config())
        options = _options_from_mapping(payload.get("options") if isinstance(payload, dict) else None)
        conn.execute(
            """
            UPDATE lavalink_nodes
            SET name = ?, host = ?, port = ?, password = ?, secure = ?, enabled = ?,
                timeout_seconds = ?, hide_host = ?, test_on_save = ?, updated_at = ?
            WHERE id = 1
            """,
            (
                cfg.node_name or "main",
                cfg.host or "",
                int(cfg.port or 2333),
                cfg.password or "",
                1 if cfg.secure else 0,
                1 if cfg.enabled else 0,
                float(cfg.timeout_seconds or 8.0),
                1 if options["hide_host_in_panel"] else 0,
                1 if options["test_after_save"] else 0,
                now,
            ),
        )
        conn.execute(
            "INSERT OR REPLACE INTO lavalink_meta(key, value) VALUES('default_mode', ?)",
            (cfg.mode if cfg.mode in _ALLOWED_MODES else "off",),
        )
        conn.execute(
            "INSERT OR REPLACE INTO lavalink_meta(key, value) VALUES('legacy_json_imported', ?)",
            (now,),
        )
        conn.execute(
            "INSERT OR REPLACE INTO lavalink_meta(key, value) VALUES('legacy_json_migrated', '1')"
        )
        logger.info("[music/lavalink] JSON legado migrado para DB separado: %s", self.db_path)

    def _node_row(self, conn: sqlite3.Connection) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM lavalink_nodes WHERE id = 1").fetchone()
        if row is None:
            self._ensure_default_node(conn)
            row = conn.execute("SELECT * FROM lavalink_nodes WHERE id = 1").fetchone()
        if row is None:
            raise RuntimeError("falha ao inicializar DB separado do MusicNode")
        return row

    def _default_mode(self, conn: sqlite3.Connection) -> str:
        row = conn.execute("SELECT value FROM lavalink_meta WHERE key = 'default_mode'").fetchone()
        mode = _normalize_mode(row["value"] if row is not None else "off")
        return mode if mode in _ALLOWED_MODES else "off"

    def _guild_mode(self, conn: sqlite3.Connection, guild_id: int | None) -> str:
        if guild_id is None:
            return self._default_mode(conn)
        row = conn.execute(
            "SELECT mode FROM lavalink_guild_settings WHERE guild_id = ?",
            (int(guild_id),),
        ).fetchone()
        if row is None:
            return self._default_mode(conn)
        mode = _normalize_mode(row["mode"])
        return mode if mode in _ALLOWED_MODES else "off"

    def _guild_has_override(self, conn: sqlite3.Connection, guild_id: int | None) -> bool:
        if guild_id is None:
            return False
        return conn.execute(
            "SELECT 1 FROM lavalink_guild_settings WHERE guild_id = ?",
            (int(guild_id),),
        ).fetchone() is not None

    def _effective_mode_for_row(self, conn: sqlite3.Connection, row: sqlite3.Row, guild_id: int | None) -> str:
        mode = self._guild_mode(conn, guild_id)
        if (
            guild_id is not None
            and mode == "off"
            and not self._guild_has_override(conn, guild_id)
            and _enable_lavalink_for_all_guilds()
            and _row_has_usable_node(row)
        ):
            # Após a migração, outras guilds sem configuração própria passam a
            # herdar Lavalink real automaticamente. Guilds com override off não
            # são forçadas.
            return "lavalink"
        return mode

    def _config_from_row(self, row: sqlite3.Row, *, mode: str) -> LavalinkConfig:
        mode = _normalize_mode(mode)
        if mode not in _ALLOWED_MODES:
            mode = "off"
        enabled = bool(_as_bool(row["enabled"], False) and mode != "off")
        return LavalinkConfig(
            enabled=enabled,
            mode=mode,
            host=str(row["host"] or "").strip(),
            port=max(1, _safe_int(row["port"], 2333)),
            password=str(row["password"] or "").strip(),
            secure=_as_bool(row["secure"], False),
            node_name=str(row["name"] or "main").strip() or "main",
            timeout_seconds=max(2.0, _safe_float(row["timeout_seconds"], 8.0)),
        )

    def load(self, guild_id: int | None = None) -> LavalinkConfig:
        self._ensure_ready()
        with self._lock, self._connection() as conn:
            row = self._node_row(conn)
            mode = self._effective_mode_for_row(conn, row, guild_id)
            return self._config_from_row(row, mode=mode)

    def source_label(self) -> str:
        self._ensure_ready()
        return "DB separado"

    def update_node(
        self,
        *,
        node_name: str,
        host: str,
        port: int,
        password: str | None,
        secure: bool,
        guild_id: int | None = None,
    ) -> LavalinkConfig:
        self._ensure_ready()
        with self._lock, self._connection() as conn:
            current = self._node_row(conn)
            kept_password = str(current["password"] or "") if password is None else str(password or "").strip()
            now = _utc_now_iso()
            conn.execute(
                """
                UPDATE lavalink_nodes
                SET name = ?, host = ?, port = ?, password = ?, secure = ?, enabled = 1, updated_at = ?
                WHERE id = 1
                """,
                (
                    str(node_name or "main").strip() or "main",
                    str(host or "").strip(),
                    max(1, int(port or 2333)),
                    kept_password,
                    1 if secure else 0,
                    now,
                ),
            )
            current_mode = self._guild_mode(conn, guild_id)
            if current_mode == "off":
                current_mode = "lavalink" if _enable_lavalink_for_all_guilds() else "shadow"
                self._set_mode_locked(conn, current_mode, guild_id=guild_id)
            conn.commit()
            row = self._node_row(conn)
            return self._config_from_row(row, mode=current_mode)

    def _set_mode_locked(self, conn: sqlite3.Connection, mode: str, *, guild_id: int | None = None) -> None:
        normalized = _normalize_mode(mode)
        if normalized not in _ALLOWED_MODES:
            normalized = "off"
        now = _utc_now_iso()
        if guild_id is None:
            conn.execute(
                "INSERT OR REPLACE INTO lavalink_meta(key, value) VALUES('default_mode', ?)",
                (normalized,),
            )
            return
        conn.execute(
            """
            INSERT INTO lavalink_guild_settings(guild_id, mode, fallback_enabled, created_at, updated_at)
            VALUES (?, ?, 1, ?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET mode = excluded.mode, updated_at = excluded.updated_at
            """,
            (int(guild_id), normalized, now, now),
        )

    def set_mode(self, mode: str, *, guild_id: int | None = None) -> LavalinkConfig:
        self._ensure_ready()
        normalized = _normalize_mode(mode)
        if normalized not in _ALLOWED_MODES:
            normalized = "off"
        with self._lock, self._connection() as conn:
            self._set_mode_locked(conn, normalized, guild_id=guild_id)
            conn.commit()
            row = self._node_row(conn)
            return self._config_from_row(row, mode=normalized)

    def update_options(self, **options: Any) -> dict[str, bool]:
        self._ensure_ready()
        with self._lock, self._connection() as conn:
            row = self._node_row(conn)
            merged = _normalize_options_from_row(row)
            for key in _DEFAULT_OPTIONS:
                if key in options:
                    merged[key] = _as_bool(options.get(key), merged[key])
            conn.execute(
                """
                UPDATE lavalink_nodes
                SET hide_host = ?, test_on_save = ?, updated_at = ?
                WHERE id = 1
                """,
                (
                    1 if merged["hide_host_in_panel"] else 0,
                    1 if merged["test_after_save"] else 0,
                    _utc_now_iso(),
                ),
            )
            conn.commit()
            return merged

    def clear(self, *, guild_id: int | None = None) -> LavalinkConfig:
        self._ensure_ready()
        with self._lock, self._connection() as conn:
            now = _utc_now_iso()
            conn.execute(
                """
                UPDATE lavalink_nodes
                SET name = 'main', host = '', port = 2333, password = '', secure = 0,
                    enabled = 0, timeout_seconds = ?, hide_host = 1, test_on_save = 0, updated_at = ?
                WHERE id = 1
                """,
                (
                    max(2.0, _safe_float(getattr(config, "LAVALINK_TIMEOUT_SECONDS", 8.0), 8.0)),
                    now,
                ),
            )
            if guild_id is None:
                conn.execute("INSERT OR REPLACE INTO lavalink_meta(key, value) VALUES('default_mode', 'off')")
                conn.execute("DELETE FROM lavalink_guild_settings")
                mode = "off"
            else:
                self._set_mode_locked(conn, "off", guild_id=guild_id)
                mode = "off"
            conn.commit()
            row = self._node_row(conn)
            return self._config_from_row(row, mode=mode)

    def summary(self, guild_id: int | None = None) -> dict[str, Any]:
        self._ensure_ready()
        with self._lock, self._connection() as conn:
            row = self._node_row(conn)
            mode = self._effective_mode_for_row(conn, row, guild_id)
            cfg = self._config_from_row(row, mode=mode)
            migrated = conn.execute(
                "SELECT value FROM lavalink_meta WHERE key = 'legacy_json_migrated'"
            ).fetchone()
            has_guild_override = False
            if guild_id is not None:
                has_guild_override = conn.execute(
                    "SELECT 1 FROM lavalink_guild_settings WHERE guild_id = ?",
                    (int(guild_id),),
                ).fetchone() is not None
            return {
                "source": self.source_label(),
                "enabled": bool(cfg.enabled),
                "mode": cfg.mode,
                "configured": bool(cfg.configured),
                "host_defined": bool(cfg.host),
                "host_label": cfg.safe_host_label,
                "port": int(cfg.port or 2333),
                "password_defined": bool(cfg.password),
                "secure": bool(cfg.secure),
                "node_name": cfg.node_name or "main",
                "config_path": str(self.db_path),
                "legacy_json_path": str(self.legacy_json_path),
                "legacy_json_migrated": bool(migrated and str(migrated["value"]) == "1"),
                "guild_id": int(guild_id) if guild_id is not None else None,
                "guild_override": has_guild_override,
                "global_lavalink_default": bool(_enable_lavalink_for_all_guilds()),
                "options": _normalize_options_from_row(row),
            }
