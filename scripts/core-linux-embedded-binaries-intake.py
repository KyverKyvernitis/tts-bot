#!/usr/bin/env python3
"""Valida e copia binários Core Linux arm64 para jniLibs do APK.

Uso seguro: este script não baixa nada e não executa binários. Ele só valida
cabeçalho ELF mínimo, tamanho, calcula SHA-256 e copia arquivos fornecidos pelo
operador para os nomes oficiais usados pelo preflight do APK.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
JNI_DIR = ROOT / "android/core-worker-app/app/src/main/jniLibs/arm64-v8a"
ASSET_MANIFEST = ROOT / "android/core-worker-app/app/src/main/assets/core-linux/embedded-binaries-manifest.json"
OUT_MANIFEST = ROOT / "android/core-worker-app/app/src/main/assets/core-linux/embedded-binaries-local.json"

TARGETS = {
    "runner": "libcoreworker_runner.so",
    "proot": "libcoreworker_proot.so",
    "busybox": "libcoreworker_busybox.so",
    "box64": "libcoreworker_box64.so",
}

MIN_BYTES = 4096
EM_AARCH64 = 183


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def read_elf_header(path: Path) -> dict:
    head = path.read_bytes()[:64]
    info = {
        "isElf": len(head) >= 4 and head[:4] == b"\x7fELF",
        "elfClass": head[4] if len(head) > 4 else 0,
        "machine": int.from_bytes(head[18:20], "little") if len(head) >= 20 else 0,
    }
    info["isElf64"] = info["elfClass"] == 2
    info["isAarch64"] = info["machine"] == EM_AARCH64
    return info


def validate_source(path: Path) -> dict:
    if not path.exists() or not path.is_file():
        raise SystemExit(f"arquivo não encontrado: {path}")
    size = path.stat().st_size
    elf = read_elf_header(path)
    errors: list[str] = []
    if size < MIN_BYTES:
        errors.append(f"arquivo pequeno demais ({size} bytes)")
    if not elf["isElf"]:
        errors.append("não parece ELF")
    if elf["isElf"] and not elf["isElf64"]:
        errors.append("ELF não é 64-bit")
    if elf["isElf"] and not elf["isAarch64"]:
        errors.append(f"machine={elf['machine']} não é AArch64")
    if errors:
        raise SystemExit(f"{path}: " + "; ".join(errors))
    return {**elf, "size": size, "sha256": sha256(path)}


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Core Linux embedded binaries intake")
    for key in TARGETS:
        parser.add_argument(f"--{key}", type=Path, help=f"caminho do binário {key} arm64")
    parser.add_argument("--dry-run", action="store_true", help="validar sem copiar")
    args = parser.parse_args(argv)

    provided = {key: getattr(args, key) for key in TARGETS if getattr(args, key) is not None}
    if not provided:
        parser.error("informe pelo menos um binário: --runner, --proot, --busybox ou --box64")

    JNI_DIR.mkdir(parents=True, exist_ok=True)
    manifest = {
        "schema": "core-worker-embedded-binaries-local-v1",
        "generatedAt": int(time.time()),
        "dryRun": bool(args.dry_run),
        "targets": {},
        "policy": {
            "downloadedByScript": False,
            "executedByScript": False,
            "bedrockBundled": False,
        },
    }

    for key, source in provided.items():
        source = source.expanduser().resolve()
        info = validate_source(source)
        dest = JNI_DIR / TARGETS[key]
        if not args.dry_run:
            shutil.copy2(source, dest)
            os.chmod(dest, 0o644)
            copied = True
        else:
            copied = False
        manifest["targets"][key] = {
            "source": str(source),
            "dest": str(dest),
            "copied": copied,
            **info,
        }
        print(f"{key}: ok size={info['size']} sha256={info['sha256']} -> {dest.name}")

    if not args.dry_run:
        OUT_MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"manifest escrito: {OUT_MANIFEST}")
    else:
        print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
