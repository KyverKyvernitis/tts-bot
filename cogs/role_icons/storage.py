from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from .models import sanitize_config


class RoleIconStorage:
    def __init__(self, bot):
        self.bot = bot
        self.root = Path(__file__).resolve().parents[2] / "data" / "role_icons"

    @property
    def db(self):
        return getattr(self.bot, "settings_db", None)

    def get_config(self, guild_id: int) -> dict[str, Any]:
        db = self.db
        if db is None or not hasattr(db, "get_role_icons_config"):
            return sanitize_config({})
        return sanitize_config(db.get_role_icons_config(int(guild_id)))

    async def save_config(self, guild_id: int, config: dict[str, Any]) -> dict[str, Any]:
        cfg = sanitize_config(config)
        cfg["revision"] = int(cfg.get("revision") or 0) + 1
        db = self.db
        if db is not None and hasattr(db, "set_role_icons_config"):
            await db.set_role_icons_config(int(guild_id), cfg)
        return cfg

    def icon_dir(self, guild_id: int, role_id: int) -> Path:
        return self.root / str(int(guild_id)) / str(int(role_id))

    def original_icon_path(self, guild_id: int, role_id: int) -> Path:
        return self.icon_dir(guild_id, role_id) / "original.png"

    def rendered_icon_path(self, guild_id: int, role_id: int) -> Path:
        return self.icon_dir(guild_id, role_id) / "rendered-current.png"

    def write_original_icon(self, guild_id: int, role_id: int, data: bytes) -> tuple[str, str]:
        path = self.original_icon_path(guild_id, role_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return str(path.relative_to(Path(__file__).resolve().parents[2])), hashlib.sha256(data).hexdigest()

    def write_rendered_icon(self, guild_id: int, role_id: int, data: bytes) -> tuple[str, str]:
        path = self.rendered_icon_path(guild_id, role_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return str(path.relative_to(Path(__file__).resolve().parents[2])), hashlib.sha256(data).hexdigest()

    def resolve_project_path(self, value: str) -> Path:
        path = Path(str(value or ""))
        if path.is_absolute():
            return path
        return Path(__file__).resolve().parents[2] / path

    def read_original_icon(self, connection: dict[str, Any], guild_id: int | None = None) -> bytes:
        raw_path = str(connection.get("original_icon_path") or "")
        path = self.resolve_project_path(raw_path) if raw_path else None
        if path is None or not path.is_file():
            if guild_id is None:
                raise FileNotFoundError("ícone base ausente")
            path = self.original_icon_path(int(guild_id), int(connection.get("role_id") or 0))
        return path.read_bytes()
