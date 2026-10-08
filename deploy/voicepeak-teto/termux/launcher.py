#!/usr/bin/env python3
"""Run the licensed Linux CLI through Termux's x86_64 PRoot container."""
from __future__ import annotations

import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sys
import tempfile


class ConfigurationError(ValueError):
    pass


def load_config() -> dict[str, str]:
    path = Path(os.environ.get("VOICEPEAK_TERMUX_CONFIG", str(Path.home() / ".voicepeak-termux/config.json")))
    try:
        if path.stat().st_size > 16384:
            raise ConfigurationError("configuração excede 16 KiB")
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ConfigurationError("configuração ausente ou inválida; execute setup.sh") from exc
    if not isinstance(value, dict) or set(value) - {"container", "guest_executable", "display", "engine_directory"}:
        raise ConfigurationError("campos de configuração inválidos")
    container = value.get("container", "")
    executable = value.get("guest_executable", "")
    display = value.get("display", "")
    if display == "":
        display = os.environ.get("DISPLAY", "")
    engine_directory = value.get("engine_directory", "")
    if not isinstance(container, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", container):
        raise ConfigurationError("nome de container inválido")
    if not isinstance(executable, str) or not executable.startswith("/") or ".." in PurePosixPath(executable).parts or any(ord(c) < 32 for c in executable):
        raise ConfigurationError("guest_executable deve ser um caminho absoluto Linux")
    if not isinstance(display, str) or (display and not re.fullmatch(r":[0-9]{1,3}(?:\.[0-9]{1,2})?", display)):
        raise ConfigurationError("display deve estar vazio ou usar :1, por exemplo")
    if not isinstance(engine_directory, str) or (engine_directory and (not Path(engine_directory).is_absolute() or ":" in engine_directory or any(ord(c) < 32 for c in engine_directory))):
        raise ConfigurationError("engine_directory deve estar vazio ou ser um caminho absoluto sem dois-pontos")
    return {"container": container, "guest_executable": executable, "display": display, "engine_directory": engine_directory}


def login_command(config: dict[str, str], command: list[str], *, gui: bool = True) -> list[str]:
    proot = shutil.which("proot-distro")
    if not proot or not shutil.which("qemu-x86_64"):
        raise ConfigurationError("instale proot-distro e qemu-user-x86-64 no Termux")
    temporary = Path(os.environ.get("TMPDIR") or tempfile.gettempdir()).resolve()
    if not temporary.is_dir() or ":" in str(temporary) or any(ord(c) < 32 for c in str(temporary)):
        raise ConfigurationError("TMPDIR precisa apontar para uma pasta existente sem dois-pontos")
    result = [proot, "login", config["container"], "--bind", f"{temporary}:{temporary}"]
    engine_directory = config.get("engine_directory", "")
    if engine_directory and Path(engine_directory).is_dir():
        result.extend(["--bind", f"{Path(engine_directory).resolve()}:/opt/Voicepeak"])
    if gui and config["display"]:
        result.append("--shared-tmp")
    result.extend(["--", "/usr/bin/env", "LANG=C.UTF-8", "LC_ALL=C.UTF-8"])
    if gui and config["display"]:
        result.append(f"DISPLAY={config['display']}")
    return result + command


def main() -> int:
    try:
        config = load_config()
        command = login_command(config, [config["guest_executable"], *sys.argv[1:]])
        os.execv(command[0], command)
    except (ConfigurationError, OSError) as exc:
        print(f"VOICEPEAK Termux: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
