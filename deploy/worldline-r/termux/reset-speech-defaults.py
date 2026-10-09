#!/usr/bin/env python3
"""Restaura a fala da Teto: C4, velocidade 1.0 e parâmetros do teste WORLDLINE-R."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import time

DEFAULTS = {
    "PHONE_WORKER_TETO_BASE_PITCH": "C4",
    "PHONE_WORKER_TETO_SPEECH_RATE": "1.0",
    "PHONE_WORKER_TETO_TEMPO": "140",
    "PHONE_WORKER_TETO_VELOCITY": "100",
    "PHONE_WORKER_TETO_MODULATION": "15",
}
MAX_ENV_BYTES = 256 * 1024
KEY = re.compile(r"^[ \t]*(?:export[ \t]+)?([A-Za-z_][A-Za-z0-9_]*)[ \t]*=")


def reset_defaults(path: Path) -> dict:
    path = path.expanduser().absolute()
    if path.is_symlink() or not path.exists() or not stat.S_ISREG(path.stat().st_mode):
        raise ValueError("O .env deve ser um arquivo regular existente.")
    if path.stat().st_size > MAX_ENV_BYTES:
        raise ValueError("O .env excede o limite de 256 KiB.")
    original = path.read_bytes()
    if b"\x00" in original:
        raise ValueError("O .env contém caracteres inválidos.")
    text = original.decode("utf-8")
    # Preserve all unrelated lines, comments, credentials and shell syntax;
    # configuration is edited as text and is never sourced or executed here.
    lines = []
    seen = set()
    for line in text.splitlines(keepends=True):
        match = KEY.match(line)
        key = match.group(1) if match else None
        if key in DEFAULTS:
            if key not in seen:
                lines.append(f"{key}={DEFAULTS[key]}\n")
                seen.add(key)
        else:
            lines.append(line)
    if lines and not lines[-1].endswith(("\n", "\r")):
        lines[-1] += "\n"
    lines.extend(f"{key}={value}\n" for key, value in DEFAULTS.items() if key not in seen)
    updated = "".join(lines).encode("utf-8")
    result = {"ok": True, "changed": updated != original, "defaults": DEFAULTS,
              "restart_required": True, "required_bot_pitch_semitones": 0.0,
              "note": "O tom personalizado no painel do Discord continua valendo; use 0.0 para o tom do teste."}
    if updated == original:
        return result
    backup = path.with_name(path.name + ".bak-teto-" + str(time.time_ns()))
    backup_fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(backup_fd, "wb") as target:
        target.write(original)
        target.flush()
        os.fsync(target.fileno())
    descriptor, temporary = tempfile.mkstemp(prefix=path.name + ".teto-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as target:
            target.write(updated)
            target.flush()
            os.fsync(target.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
    result["backup"] = str(backup)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", type=Path, default=Path(os.getenv("PHONE_WORKER_ENV") or Path.home() / ".phone-worker.env"),
                        help="Arquivo .env existente do worker; padrão: ~/.phone-worker.env.")
    options = parser.parse_args(argv)
    try:
        report = reset_defaults(options.env)
    except (OSError, UnicodeError, ValueError) as error:
        report = {"ok": False, "error": str(error)}
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
