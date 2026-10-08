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


def bounded(command: list[str], timeout: float, *, include_stderr: bool = False, environment: dict[str, str] | None = None) -> dict:
    # VOICEPEAK writes successful --help to stderr on Linux. Combined output is
    # consumed internally; report() never publishes vendor help or inventory.
    try:
        with tempfile.TemporaryFile() as stdout:
            process = subprocess.Popen(command, stdout=stdout, stderr=subprocess.STDOUT if include_stderr else subprocess.DEVNULL,
                                       start_new_session=True, env=environment)
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


def outcome(value: dict) -> dict:
    """Publish only bounded status metadata, never guest command output."""
    return {key: value[key] for key in ("ok", "code", "error") if key in value}


def libc_state(value: dict) -> str | None:
    if not value.get("ok"):
        return None
    text = value.get("output", "").strip()
    # ${Status} consists of requested action, error state and package state.
    match = re.fullmatch(r"(?:unknown|install|hold|deinstall|purge) (?:ok|reinstreq) "
                         r"(not-installed|config-files|half-installed|unpacked|half-configured|triggers-awaited|triggers-pending|installed)", text)
    return match.group(1) if match else None


def report(*, probe_runtime: bool = False, probe_system: bool = False, timeout: float = 20.0) -> dict:
    deadline = time.monotonic() + timeout if probe_runtime or probe_system else None
    def host_probe(command: list[str], *, limit: float | None = None, **kwargs) -> dict:
        remaining = deadline - time.monotonic() if deadline is not None else timeout
        if remaining <= 0:
            return {"ok": False, "error": "tempo total esgotado"}
        return bounded(command, min(remaining, limit) if limit is not None else remaining, **kwargs)

    memory = {}
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            name, value = line.split(":", 1)
            if name in {"MemTotal", "MemAvailable"}:
                memory[name] = int(value.strip().split()[0]) * 1024
    except (OSError, ValueError):
        pass
    result = {"native_architecture": platform.machine(), "python_platform": sys.platform, "python_version": platform.python_version(),
              "page_size_bytes": os.sysconf("SC_PAGE_SIZE"), "memory_bytes": memory,
              "free_disk_bytes": shutil.disk_usage(Path.home()).free, "engine_verified": False,
              "note": "ambiente preparado não comprova VOICEPEAK instalado, ativado ou capaz de sintetizar"}
    qemu = shutil.which("qemu-x86_64")
    result["qemu_installed"] = bool(qemu)
    if qemu:
        version = host_probe([qemu, "--version"], limit=3.0)
        result["qemu_version"] = [line[:160] for line in version.get("output", "").splitlines()[:1]]
        result["qemu_version_probe"] = outcome(version)
    config = None
    try:
        config = load_config()
        result["configuration"] = config
        result["engine_directory_present"] = bool(config["engine_directory"] and Path(config["engine_directory"]).is_dir())
    except ConfigurationError as exc:
        result["configuration_error"] = str(exc)

    if probe_system:
        cpu = os.environ.get("QEMU_CPU", "")
        result["qemu_cpu"] = cpu if re.fullmatch(r"[A-Za-z0-9_.=,+-]{1,256}", cpu) else "valor omitido" if cpu else "padrão do QEMU"
        result["proot_no_seccomp_configured"] = os.environ.get("PROOT_NO_SECCOMP") == "1"
        for name in ("proot", "proot-distro"):
            binary = shutil.which(name)
            version = host_probe([binary, "--version"], limit=3.0) if binary else {"ok": False, "error": "comando indisponível"}
            entry = outcome(version)
            first = version.get("output", "").splitlines()[:1]
            if first:
                entry["version"] = first[0][:160]
            result[name.replace("-", "_") + "_version"] = entry
        system_config = config or {"container": "voicepeak-x64", "guest_executable": "/opt/Voicepeak/voicepeak", "display": "", "engine_directory": ""}
        system = {"container": system_config["container"], "configuration_source": "config.json" if config else "padrão somente para diagnóstico",
                  "read_only": True, "system_healthy": False,
                  "note": "ldconfig -p lê o cache; nenhum comando configura pacotes ou ativa o VOICEPEAK"}
        result["system_probe"] = system
        def system_probe(command: list[str], *, no_seccomp: bool = False) -> dict:
            try:
                arguments = login_command(system_config, command, gui=False)
            except ConfigurationError as exc:
                return {"ok": False, "error": str(exc)}
            options = {}
            if no_seccomp:
                options["environment"] = dict(os.environ, PROOT_NO_SECCOMP="1")
            return host_probe(arguments, **options)
        architecture = system_probe(["/usr/bin/dpkg", "--print-architecture"])
        system["architecture_probe"] = outcome(architecture)
        architecture_text = architecture.get("output", "").strip()
        system["guest_architecture"] = architecture_text if architecture.get("ok") and architecture_text in {"amd64", "arm64", "i386", "armhf"} else None
        package = system_probe(["/usr/bin/dpkg-query", "-W", "-f=${Status}\\n", "libc-bin"])
        system["libc_bin_query"] = outcome(package)
        system["libc_bin_state"] = libc_state(package)
        system["libc_bin_pending"] = system["libc_bin_state"] in {"half-installed", "unpacked", "half-configured", "triggers-awaited", "triggers-pending"} if system["libc_bin_state"] else None
        system["libc_bin_reinstall_required"] = package.get("output", "").strip().split()[1] == "reinstreq" if system["libc_bin_state"] else None
        system["libc_bin_status_ok"] = bool(package.get("ok") and re.fullmatch(r"(?:install|hold) ok installed", package.get("output", "").strip()))
        audit = system_probe(["/usr/bin/dpkg", "--audit"])
        system["dpkg_audit"] = outcome(audit)
        system["audit_has_findings"] = bool(audit.get("output", "").strip()) if audit.get("ok") else None
        cache = system_probe(["/sbin/ldconfig", "-p"])
        system["ldconfig_cache"] = outcome(cache)
        system["ldconfig_cache_readable"] = bool(cache.get("ok") and re.search(r"(?m)^[1-9][0-9]* libs found in cache ", cache.get("output", "")))
        alternate = system_probe(["/sbin/ldconfig", "-p"], no_seccomp=True)
        system["ldconfig_no_seccomp"] = outcome(alternate)
        system["no_seccomp_test_note"] = "teste somente de leitura; sucesso não comprova correção do pós-instalação nem do motor"
        # https://github.com/bminor/glibc/blob/glibc-2.35/elf/ldconfig.c
        # glibc 2.35: -N skips save_cache/save_aux_cache, -X disables link
        # changes. -N alone still loads the auxiliary cache; -i skips that
        # load. The comparison diagnoses scanning and never repairs a cache.
        scan = system_probe(["/sbin/ldconfig", "-N", "-X"])
        system["ldconfig_scan"] = outcome(scan)
        scan_without_aux = system_probe(["/sbin/ldconfig", "-N", "-X", "-i"])
        system["ldconfig_scan_without_aux_cache"] = outcome(scan_without_aux)
        scan_without_aux_no_seccomp = system_probe(["/sbin/ldconfig", "-N", "-X", "-i"], no_seccomp=True)
        system["ldconfig_scan_without_aux_cache_no_seccomp"] = outcome(scan_without_aux_no_seccomp)
        system["scan_test_note"] = "-N -X não grava caches nem altera links; diferença com -i não comprova uma correção"
        system["system_healthy"] = bool(system["guest_architecture"] == "amd64" and system["libc_bin_status_ok"] and audit.get("ok") and not system["audit_has_findings"] and system["ldconfig_cache_readable"] and scan.get("ok"))

    try:
        # These commands use positional arguments; executable paths never become shell source.
        if probe_runtime:
            if config is None:
                result["runtime_error"] = "configuração do motor ausente ou inválida; diagnóstico do sistema disponível com --probe-system"
                return result
            if probe_system and not result["system_probe"]["system_healthy"]:
                result["runtime_error"] = "sistema guest não confirmou libc-bin instalado e cache legível; motor não executado"
                return result
            def probe(command: list[str], *, include_stderr: bool = False) -> dict:
                return host_probe(login_command(config, command, gui=False), include_stderr=include_stderr)
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
    parser.add_argument("--probe-system", action="store_true", help="inspeciona dpkg e lê o cache ldconfig, sem configurar pacotes ou iniciar o motor")
    parser.add_argument("--probe-runtime", action="store_true", help="testa guest, ELF e inventário com prazo total")
    parser.add_argument("--timeout", type=float, default=20.0, help="prazo total dos testes, de 1 a 120 segundos")
    arguments = parser.parse_args()
    if not 1 <= arguments.timeout <= 120:
        parser.error("timeout deve estar entre 1 e 120 segundos")
    result = report(probe_runtime=arguments.probe_runtime, probe_system=arguments.probe_system, timeout=arguments.timeout)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if arguments.probe_runtime:
        return 0 if result["engine_verified"] else 1
    if arguments.probe_system:
        return 0 if result["system_probe"]["system_healthy"] else 1
    return 0 if "configuration_error" not in result else 1


if __name__ == "__main__":
    raise SystemExit(main())
