#!/usr/bin/env python3
"""Run the licensed Linux CLI through a configured Termux PRoot runtime."""
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


def _guest_path(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.startswith("/") or ":" in value or ".." in PurePosixPath(value).parts or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ConfigurationError(f"{field} deve ser um caminho absoluto Linux")
    return value


def load_config() -> dict[str, str]:
    path = Path(os.environ.get("VOICEPEAK_TERMUX_CONFIG", str(Path.home() / ".voicepeak-termux/config.json")))
    try:
        if path.stat().st_size > 16384:
            raise ConfigurationError("configuração excede 16 KiB")
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ConfigurationError("configuração ausente ou inválida; execute setup.sh") from exc
    if not isinstance(value, dict) or set(value) - {"container", "guest_executable", "display", "engine_directory", "backend", "guest_box64", "box64_library_path"}:
        raise ConfigurationError("campos de configuração inválidos")
    container = value.get("container", "")
    executable = value.get("guest_executable", "")
    display = value.get("display", "")
    if display == "":
        display = os.environ.get("DISPLAY", "")
    engine_directory = value.get("engine_directory", "")
    backend = value.get("backend", "qemu")
    guest_box64 = value.get("guest_box64", "/opt/voicepeak-box64/bin/box64")
    library_path = value.get("box64_library_path", "")
    if not isinstance(container, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", container):
        raise ConfigurationError("nome de container inválido")
    if not isinstance(executable, str) or not executable.startswith("/") or ".." in PurePosixPath(executable).parts or any(ord(c) < 32 for c in executable):
        raise ConfigurationError("guest_executable deve ser um caminho absoluto Linux")
    if not isinstance(display, str) or (display and not re.fullmatch(r":[0-9]{1,3}(?:\.[0-9]{1,2})?", display)):
        raise ConfigurationError("display deve estar vazio ou usar :1, por exemplo")
    if not isinstance(engine_directory, str) or (engine_directory and (not Path(engine_directory).is_absolute() or ":" in engine_directory or any(ord(c) < 32 for c in engine_directory))):
        raise ConfigurationError("engine_directory deve estar vazio ou ser um caminho absoluto sem dois-pontos")
    if backend not in ("qemu", "box64"):
        raise ConfigurationError("backend deve ser qemu ou box64")
    _guest_path(guest_box64, "guest_box64")
    if not isinstance(library_path, str) or len(library_path) > 4096:
        raise ConfigurationError("box64_library_path deve conter caminhos Linux separados por dois-pontos")
    if library_path:
        for entry in library_path.split(":"):
            _guest_path(entry, "box64_library_path")
    result = {"container": container, "guest_executable": executable, "display": display, "engine_directory": engine_directory}
    # Keep the original QEMU configuration shape when no backend was selected.
    if "backend" in value or backend == "box64":
        result["backend"] = backend
    if backend == "box64" or "guest_box64" in value:
        result["guest_box64"] = guest_box64
    if backend == "box64" or "box64_library_path" in value:
        result["box64_library_path"] = library_path
    return result


def voicepeak_command(config: dict[str, str], arguments: list[str]) -> list[str]:
    """Wrap the x86_64 engine only; guest utilities retain their architecture."""
    command = [config["guest_executable"], *arguments]
    if config.get("backend", "qemu") == "box64":
        command.insert(0, config.get("guest_box64", "/opt/voicepeak-box64/bin/box64"))
    return command


def login_command(config: dict[str, str], command: list[str], *, gui: bool = True) -> list[str]:
    proot = shutil.which("proot-distro")
    backend = config.get("backend", "qemu")
    if backend not in ("qemu", "box64"):
        raise ConfigurationError("backend deve ser qemu ou box64")
    if backend == "box64" and not proot:
        raise ConfigurationError("instale proot-distro no Termux")
    if backend == "qemu" and (not proot or not shutil.which("qemu-x86_64")):
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
    if backend == "box64":
        result.extend(["BOX64_LOG=0", "BOX64_NOBANNER=1"])
        if config.get("box64_library_path"):
            result.append(f"BOX64_LD_LIBRARY_PATH={config['box64_library_path']}")
    if gui and config["display"]:
        result.append(f"DISPLAY={config['display']}")
    return result + command


def main() -> int:
    try:
        config = load_config()
        command = login_command(config, voicepeak_command(config, sys.argv[1:]))
        os.execv(command[0], command)
    except (ConfigurationError, OSError) as exc:
        print(f"VOICEPEAK Termux: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
