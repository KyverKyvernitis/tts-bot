#!/usr/bin/env python3
"""Read-only environment report; optional bounded probes never synthesize audio."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import platform
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time

TOOLKIT_ROOT = Path(__file__).resolve().parent
if str(TOOLKIT_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOLKIT_ROOT))
from launcher import ConfigurationError, load_config, login_command


def bounded(command: list[str], timeout: float, *, include_stderr: bool = False) -> dict:
    # VOICEPEAK writes successful --help to stderr on Linux. Combined output is
    # consumed internally; report() never publishes vendor help or inventory.
    try:
        with tempfile.TemporaryFile() as stdout:
            process = subprocess.Popen(command, stdout=stdout, stderr=subprocess.STDOUT if include_stderr else subprocess.DEVNULL, start_new_session=True)
            try:
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
                return {"ok": False, "error": "tempo esgotado"}
            stdout.seek(0)
            output = stdout.read(65537)
        if len(output) > 65536:
            return {"ok": False, "error": "saída excede limite"}
        if process.returncode:
            return {"ok": False, "code": process.returncode}
        return {"ok": True, "code": 0, "output": output.decode("utf-8-sig", errors="replace")}
    except OSError:
        return {"ok": False, "error": "comando indisponível"}


def report(*, probe_runtime: bool = False, timeout: float = 20.0) -> dict:
    memory = {}
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            name, value = line.split(":", 1)
            if name in {"MemTotal", "MemAvailable"}:
                memory[name] = int(value.strip().split()[0]) * 1024
    except (OSError, ValueError):
        pass
    result = {"native_architecture": platform.machine(), "page_size_bytes": os.sysconf("SC_PAGE_SIZE"), "memory_bytes": memory,
              "free_disk_bytes": shutil.disk_usage(Path.home()).free, "engine_verified": False,
              "note": "ambiente preparado não comprova VOICEPEAK instalado, ativado ou capaz de sintetizar"}
    qemu = shutil.which("qemu-x86_64")
    result["qemu_installed"] = bool(qemu)
    if qemu:
        version = bounded([qemu, "--version"], 3.0)
        result["qemu_version"] = version.get("output", "").splitlines()[:1]
    try:
        config = load_config()
        result["configuration"] = config
        result["engine_directory_present"] = bool(config["engine_directory"] and Path(config["engine_directory"]).is_dir())
        # These commands use positional arguments; executable paths never become shell source.
        if probe_runtime:
            deadline = time.monotonic() + timeout
            def probe(command: list[str], *, include_stderr: bool = False) -> dict:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return {"ok": False, "error": "tempo total esgotado"}
                return bounded(login_command(config, command, gui=False), remaining, include_stderr=include_stderr)
            architecture = probe(["/usr/bin/dpkg", "--print-architecture"])
            result["guest_architecture"] = architecture.get("output", "").strip() if architecture["ok"] else None
            if result["guest_architecture"] != "amd64":
                result["runtime_error"] = "guest indisponível ou arquitetura diferente de amd64"
                return result
            elf = probe(["/bin/sh", "-c", 'test -f "$1" && od -An -tx1 -N20 "$1"', "voicepeak-diagnostic", config["guest_executable"]])
            header = elf.get("output", "").split()
            result["engine_elf_x86_64"] = elf["ok"] and len(header) == 20 and header[:5] == ["7f", "45", "4c", "46", "02"] and header[18:20] == ["3e", "00"]
            if not result["engine_elf_x86_64"]:
                result["runtime_error"] = "executável VOICEPEAK ausente ou ELF x86_64 inválido; instale o arquivo oficial"
                return result
            help_result = probe([config["guest_executable"], "--help"], include_stderr=True)
            narrators = probe([config["guest_executable"], "--list-narrator"], include_stderr=True)
            result["cli_help_ok"] = help_result["ok"]
            result["teto_inventory_ok"] = narrators["ok"] and any(re.sub(r"^\s*(?:[-*]\s+|\d+[.)]\s+)", "", line).strip().strip("\"'") in {"重音テト", "Kasane Teto", "Teto"} for line in narrators.get("output", "").splitlines())
            result["engine_verified"] = bool(result["cli_help_ok"] and result["teto_inventory_ok"])
            result["note"] = "inventário da Teto confirmado; ainda falta o teste WAV real" if result["engine_verified"] else "CLI não confirmou a Teto; instalação/ativação pode exigir a GUI oficial"
    except ConfigurationError as exc:
        result["configuration_error"] = str(exc)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--probe-runtime", action="store_true", help="testa guest, ELF e inventário com prazo total")
    parser.add_argument("--timeout", type=float, default=20.0, help="prazo total dos testes, de 1 a 120 segundos")
    arguments = parser.parse_args()
    if not 1 <= arguments.timeout <= 120:
        parser.error("timeout deve estar entre 1 e 120 segundos")
    result = report(probe_runtime=arguments.probe_runtime, timeout=arguments.timeout)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if (not arguments.probe_runtime and "configuration_error" not in result) or result["engine_verified"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
