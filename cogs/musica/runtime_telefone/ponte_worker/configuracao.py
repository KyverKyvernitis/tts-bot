"""Configuração precoce do Music Agent usada pela ponte do Phone Worker."""
from __future__ import annotations

import contextlib
import os
import re
import secrets
from pathlib import Path
from typing import Any


def truthy(value: object, default: bool = False) -> bool:
    if value is None:
        return default
    text = str(value).strip().lower().strip('"\'')
    if not text:
        return default
    if text in {"0", "false", "no", "n", "off", "nao", "não"}:
        return False
    if text in {"1", "true", "yes", "y", "on", "sim"}:
        return True
    return default


def _decode_env_value(raw: str) -> str:
    value = str(raw or "").strip()
    if len(value) >= 2 and value[:1] == value[-1:] and value[0] in {'"', "'"}:
        value = value[1:-1]
    return value


def load_env_file(path: Path, *, override: bool = False) -> dict[str, str]:
    loaded: dict[str, str] = {}
    try:
        lines = path.expanduser().read_text("utf-8", errors="replace").splitlines()
    except Exception:
        return loaded
    for line in lines:
        raw = line.strip()
        if not raw or raw.startswith("#") or "=" not in raw:
            continue
        key, value = raw.split("=", 1)
        key = key.strip()
        if not key or not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", key):
            continue
        value = _decode_env_value(value)
        loaded[key] = value
        if override or key not in os.environ:
            os.environ[key] = value
    return loaded


def music_agent_env_file() -> Path:
    worker_dir = Path(os.getenv("PHONE_WORKER_DIR") or Path.home() / "phone-worker").expanduser()
    return Path(os.getenv("MUSIC_AGENT_ENV") or worker_dir / "secrets" / "music-agent.env").expanduser()


def ensure_music_agent_token(*, persist: bool = True) -> str:
    env_file = music_agent_env_file()
    load_env_file(env_file, override=False)
    token = str(os.getenv("MUSIC_AGENT_TOKEN") or "").strip()
    if token:
        return token
    token = secrets.token_urlsafe(32)
    os.environ["MUSIC_AGENT_TOKEN"] = token
    if persist:
        try:
            env_file.parent.mkdir(parents=True, exist_ok=True)
            old = env_file.read_text("utf-8", errors="replace") if env_file.exists() else ""
            lines: list[str] = []
            replaced = False
            for line in old.splitlines():
                if re.match(r"^\s*MUSIC_AGENT_TOKEN\s*=", line):
                    if not replaced:
                        lines.append("MUSIC_AGENT_TOKEN=" + token)
                        replaced = True
                    continue
                lines.append(line)
            if not replaced:
                lines.append("MUSIC_AGENT_TOKEN=" + token)
            env_file.write_text("\n".join(lines).rstrip() + "\n", "utf-8")
            with contextlib.suppress(Exception):
                os.chmod(env_file, 0o600)
        except Exception:
            pass
    return token


def load_runtime_env(*, phone_worker_env: Path | None = None) -> None:
    env_path = phone_worker_env or Path(os.getenv("PHONE_WORKER_ENV") or Path.home() / ".phone-worker.env")
    load_env_file(env_path, override=False)
    load_env_file(music_agent_env_file(), override=False)
    if truthy(os.getenv("MUSIC_AGENT_AUTO_TOKEN"), True):
        ensure_music_agent_token(persist=True)
