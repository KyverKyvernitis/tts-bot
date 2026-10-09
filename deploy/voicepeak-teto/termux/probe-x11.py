#!/usr/bin/env python3
"""Test Box64's Xlib bridge without launching VOICEPEAK or using a voice."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile

TOOLKIT_ROOT = Path(__file__).resolve().parent
if str(TOOLKIT_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOLKIT_ROOT))
from diagnostic import SIGNALLED_PROCESS
from launcher import ConfigurationError, load_config, login_command

SUCCESS_MARKER = "VOICEPEAK_X11_PROBE_OK"
BINARY = TOOLKIT_ROOT / "x11-probe-x86_64"


def run_probe(command: list[str], timeout: float) -> dict:
    result = {"ok": False}
    try:
        with tempfile.TemporaryFile() as log:
            process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT,
                                       start_new_session=True)
            try:
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
                result["error"] = "tempo esgotado"
            result["code"] = process.returncode
            log.seek(0)
            output = log.read(65537)
        if len(output) > 65536:
            result["error"] = "saída excede limite; trecho inicial preservado"
        result["output"] = output[:65536].decode("utf-8", errors="replace")
        crash = SIGNALLED_PROCESS.search(result["output"])
        if crash:
            result["signal"] = int(next(group for group in crash.groups() if group))
            result["error"] = "PRoot/Box64 reportou término por sinal"
        if process.returncode < 0:
            result["signal"] = -process.returncode
        result["ok"] = (not result.get("error") and process.returncode == 0
                        and SUCCESS_MARKER in result["output"].splitlines())
    except OSError as exc:
        result["error"] = str(exc)
    return result


def probe_command(config: dict[str, str], binary: Path, *, early_threads: bool) -> list[str]:
    options = ["BOX64_LOG=1", "BOX64_NOBANNER=0", "BOX64_DYNAREC=0",
               "BOX64_SHOWSEGV=1", "BOX64_SHOWBT=0", "BOX64_ROLLING_LOG=64",
               f"BOX64_X11THREADS={int(early_threads)}"]
    guest_directory = "/opt/voicepeak-x11-probe"
    # The inner env selects the probe profile without changing persistent config.
    command = login_command(dict(config, engine_directory=""),
                            ["/usr/bin/env", *options, config["guest_box64"],
                             guest_directory + "/x11-probe-x86_64"])
    separator = command.index("--")
    command[separator:separator] = ["--bind", f"{binary.resolve().parent}:{guest_directory}"]
    return command


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=float, default=30.0, help="prazo por teste, de 1 a 60 segundos")
    parser.add_argument("--output", type=Path, help="arquivo JSON; padrão: Downloads se disponível")
    args = parser.parse_args()
    if not 1 <= args.timeout <= 60:
        parser.error("timeout deve estar entre 1 e 60 segundos")
    os.environ.setdefault("VOICEPEAK_TERMUX_CONFIG", str(Path.home() / ".voicepeak-termux/config-box64.json"))
    report = {"probe_ok": False, "voicepeak_synthesis_verified": False,
              "note": "teste da ponte Xlib; não inicia VOICEPEAK nem verifica a voz Teto", "checks": []}
    try:
        config = load_config()
        if config.get("backend") != "box64":
            raise ConfigurationError("este teste exige a configuração Box64")
        if not config["display"]:
            config["display"] = ":1"
        raw = BINARY.read_bytes()
        if len(raw) > 1048576 or raw[:6] != b"\x7fELF\x02\x01" or raw[18:20] != b"\x3e\x00":
            raise ConfigurationError("probe ausente ou diferente de ELF x86_64")
        report.update(container=config["container"], display=config["display"],
                      binary_sha256=hashlib.sha256(raw).hexdigest())
        for early_threads in (False, True):
            result = run_probe(probe_command(config, BINARY, early_threads=early_threads), args.timeout)
            report["checks"].append(dict(result, early_x11_threads=early_threads))
        report["probe_ok"] = all(check["ok"] for check in report["checks"])
    except (ConfigurationError, OSError) as exc:
        report["error"] = str(exc)
    downloads = Path.home() / "storage/downloads"
    destination = args.output or (downloads if downloads.is_dir() else Path.home()) / "voicepeak-box64-x11-probe.json"
    encoded = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    print(encoded, end="")
    try:
        destination.write_text(encoded, encoding="utf-8")
        print(f"Relatório salvo: {destination}", file=sys.stderr)
    except OSError as exc:
        print(f"Não foi possível salvar o relatório: {exc}", file=sys.stderr)
        return 1
    return 0 if report["probe_ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
