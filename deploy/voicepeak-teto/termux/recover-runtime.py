#!/usr/bin/env python3
"""Optional repair of Ubuntu's ldconfig/dpkg under Termux emulation.

No engine is downloaded, launched or activated. This script never replaces
ldconfig or suppresses dpkg failures; a successful report only concerns Ubuntu.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import platform
import re
import selectors
import signal
import subprocess
import sys
import time

TOOLKIT_ROOT = Path(__file__).resolve().parent
if str(TOOLKIT_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOLKIT_ROOT))
from launcher import ConfigurationError, load_config, login_command

MAX_OUTPUT_BYTES = 65536
AUX_CACHE = "/var/cache/ldconfig/aux-cache"
AUX_BACKUP = "/var/cache/ldconfig/aux-cache.voicepeak-repair.bak"


def _stop(process: subprocess.Popen) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()


def bounded(command: list[str], deadline: float) -> dict:
    """Run with one total deadline and a bounded pipe, hiding failed output."""
    if deadline - time.monotonic() <= 0:
        return {"ok": False, "error": "tempo total esgotado"}
    process = None
    try:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True)
        os.set_blocking(process.stdout.fileno(), False)
        output = bytearray()
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    _stop(process)
                    return {"ok": False, "error": "tempo total esgotado"}
                if not selector.select(min(remaining, 0.25)):
                    continue
                try:
                    chunk = os.read(process.stdout.fileno(), min(4096, MAX_OUTPUT_BYTES - len(output) + 1))
                except BlockingIOError:
                    continue
                if not chunk:
                    break
                output.extend(chunk)
                if len(output) > MAX_OUTPUT_BYTES:
                    _stop(process)
                    return {"ok": False, "error": "saída excede limite"}
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _stop(process)
            return {"ok": False, "error": "tempo total esgotado"}
        try:
            process.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            _stop(process)
            return {"ok": False, "error": "tempo total esgotado"}
        if process.returncode:
            return {"ok": False, "code": process.returncode}
        return {"ok": True, "code": 0, "output": output.decode("utf-8-sig", errors="replace")}
    except OSError:
        if process is not None and process.poll() is None:
            _stop(process)
        return {"ok": False, "error": "comando indisponível"}
    finally:
        if process is not None and process.stdout is not None:
            process.stdout.close()


def outcome(value: dict) -> dict:
    return {key: value[key] for key in ("ok", "code", "error") if key in value}


def recover(*, timeout: float = 120.0) -> dict:
    result = {"experimental": True, "recovered": False, "engine_verified": False,
              "note": "esta recuperação verifica somente Ubuntu; não comprova a voz Teto nem execução do VOICEPEAK",
              "checks": {}, "repair_steps": {}}
    if not 1 <= timeout <= 300:
        result["error"] = "timeout deve estar entre 1 e 300 segundos"
        return result
    if not os.environ.get("TERMUX_VERSION") or not os.environ.get("PREFIX") or platform.machine() != "aarch64":
        result["error"] = "execute com Python no Termux ARM64 nativo, fora do PRoot"
        return result
    try:
        config = load_config()
    except ConfigurationError as exc:
        result["error"] = str(exc)
        return result
    result["container"] = config["container"]
    deadline = time.monotonic() + timeout

    def probe(name: str, command: list[str], *, repair: bool = False) -> dict:
        if deadline - time.monotonic() <= 0:
            value = {"ok": False, "error": "tempo total esgotado"}
        else:
            try:
                value = bounded(login_command(config, command, gui=False), deadline)
            except ConfigurationError as exc:
                value = {"ok": False, "error": str(exc)}
        result["repair_steps" if repair else "checks"][name] = outcome(value)
        return value

    architecture = probe("architecture", ["/usr/bin/dpkg", "--print-architecture"])
    if not architecture.get("ok") or architecture.get("output", "").strip() != "amd64":
        result["error"] = "guest indisponível ou arquitetura diferente de amd64; ambiente preservado"
        return result
    result["guest_architecture"] = "amd64"
    version = probe("ldconfig_version", ["/sbin/ldconfig", "--version"])
    if not version.get("ok"):
        result["error"] = "ldconfig não abriu; nenhum reparo foi executado"
        return result
    scan = probe("ldconfig_scan", ["/sbin/ldconfig", "-N", "-X"])
    ignore_auxiliary = False
    if not scan.get("ok"):
        alternate = probe("ldconfig_ignore_aux_scan", ["/sbin/ldconfig", "-N", "-X", "-i"])
        if not alternate.get("ok"):
            result["error"] = "ambas as leituras ldconfig falharam; nenhum reparo foi executado"
            result["hint"] = "envie o diagnóstico --probe-system; falha de QEMU/PRoot ainda precisa de investigação"
            return result
        ignore_auxiliary = True
    result["strategy"] = "rebuild_ignoring_auxiliary_cache" if ignore_auxiliary else "normal_rebuild"
    if ignore_auxiliary:
        auxiliary = probe("auxiliary_cache_exists", ["/usr/bin/test", "-f", AUX_CACHE])
        if auxiliary.get("ok"):
            backup = probe("auxiliary_cache_backup", ["/bin/cp", "-p", "--no-clobber", "--", AUX_CACHE, AUX_BACKUP], repair=True)
            if not backup.get("ok"):
                result["error"] = "backup auxiliar falhou; cache e pacotes não foram reconstruídos"
                return result
            result["auxiliary_backup"] = "cópia preservada sem sobrescrever backup existente"
        elif auxiliary.get("code") != 1:
            result["error"] = "não foi possível verificar o cache auxiliar; reparo interrompido"
            return result
    rebuild = probe("ldconfig_rebuild", ["/sbin/ldconfig", *(["-i"] if ignore_auxiliary else [])], repair=True)
    if not rebuild.get("ok"):
        result["error"] = "reconstrução ldconfig falhou; dpkg não foi executado"
        return result
    recheck = probe("ldconfig_scan_after_rebuild", ["/sbin/ldconfig", "-N", "-X"])
    if not recheck.get("ok"):
        result["error"] = "ldconfig ainda falha após reconstrução; dpkg não foi executado"
        return result
    configure = probe("dpkg_configure", ["/usr/bin/dpkg", "--configure", "-a"], repair=True)
    if not configure.get("ok"):
        result["error"] = "dpkg retornou falha; recuperação não foi confirmada"
        return result
    package = probe("libc_bin_status", ["/usr/bin/dpkg-query", "-W", "-f=${Status}\\n", "libc-bin"])
    result["libc_bin_installed"] = bool(package.get("ok") and package.get("output", "").strip() in {"install ok installed", "hold ok installed"})
    audit = probe("dpkg_audit", ["/usr/bin/dpkg", "--audit"])
    result["dpkg_audit_empty"] = bool(audit.get("ok") and not audit.get("output", "").strip())
    cache = probe("ldconfig_cache", ["/sbin/ldconfig", "-p"])
    result["ldconfig_cache_readable"] = bool(cache.get("ok") and re.search(r"(?m)^[1-9][0-9]* libs found in cache ", cache.get("output", "")))
    result["recovered"] = bool(result["libc_bin_installed"] and result["dpkg_audit_empty"] and result["ldconfig_cache_readable"])
    if not result["recovered"]:
        result["error"] = "verificação final não confirmou libc-bin instalado, auditoria vazia e cache legível"
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repair", action="store_true", help="autoriza reconstrução ldconfig e configuração dos pacotes pendentes no guest existente")
    parser.add_argument("--timeout", type=float, default=120.0, help="prazo total de 1 a 300 segundos")
    arguments = parser.parse_args(argv)
    if not arguments.repair:
        print(json.dumps({"recovered": False, "error": "reparo opcional requer --repair; use diagnostic.py --probe-system para somente leitura"}, ensure_ascii=False))
        return 2
    if not 1 <= arguments.timeout <= 300:
        print(json.dumps({"recovered": False, "error": "timeout deve estar entre 1 e 300 segundos"}, ensure_ascii=False))
        return 2
    result = recover(timeout=arguments.timeout)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["recovered"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
