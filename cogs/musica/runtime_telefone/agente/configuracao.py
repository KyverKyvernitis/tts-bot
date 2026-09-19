"""Configuração e bootstrap de ambiente do Music Agent do Phone Worker."""
from __future__ import annotations

import contextlib
import os
import re
import secrets
from pathlib import Path


def load_env_file(path: Path, *, override: bool = False) -> None:
    try:
        lines = path.expanduser().read_text("utf-8", errors="replace").splitlines()
    except Exception:
        return
    for line in lines:
        raw = line.strip()
        if not raw or raw.startswith("#") or "=" not in raw:
            continue
        key, value = raw.split("=", 1)
        key = key.strip()
        if not key or not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", key):
            continue
        if override or key not in os.environ:
            os.environ[key] = value.strip().strip('"').strip("'")


def bootstrap_env() -> None:
    worker_dir = Path(os.getenv("PHONE_WORKER_DIR") or Path.home() / "phone-worker").expanduser()
    load_env_file(Path(os.getenv("PHONE_WORKER_ENV") or Path.home() / ".phone-worker.env"), override=False)
    env_file = Path(os.getenv("MUSIC_AGENT_ENV") or worker_dir / "secrets" / "music-agent.env").expanduser()
    load_env_file(env_file, override=False)
    if not str(os.getenv("MUSIC_AGENT_TOKEN") or "").strip():
        token = secrets.token_urlsafe(32)
        os.environ["MUSIC_AGENT_TOKEN"] = token
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


def truthy(value: object, default: bool = False) -> bool:
    if value is None:
        return default
    text = str(value).strip().lower().strip('"\'')
    if not text:
        return default
    if text in {"0", "false", "no", "n", "off", "nao", "não"}:
        return False
    return text in {"1", "true", "yes", "y", "on", "sim"}


def env_int(name: str, default: int) -> int:
    try:
        return int(float(os.getenv(name, str(default))))
    except Exception:
        return default


def env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except Exception:
        return default
