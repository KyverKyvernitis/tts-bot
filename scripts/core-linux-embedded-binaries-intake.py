#!/usr/bin/env python3
"""Valida e copia binários Core Linux arm64 para jniLibs do APK.

Uso seguro: este script não baixa nada e não executa binários. Ele só valida
cabeçalho ELF mínimo, tamanho, calcula SHA-256 e copia arquivos fornecidos pelo
operador para os nomes oficiais usados pelo preflight do APK.

Para binários externos (proot/busybox/box64), o stage real exige metadados de
origem/licença aprovados. Isso evita que um arquivo aleatório acabe embutido no
APK apenas porque é ELF arm64.
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
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
JNI_DIR = ROOT / "android/core-worker-app/app/src/main/jniLibs/arm64-v8a"
ASSET_DIR = ROOT / "android/core-worker-app/app/src/main/assets/core-linux"
ASSET_MANIFEST = ASSET_DIR / "embedded-binaries-manifest.json"
OUT_MANIFEST = ASSET_DIR / "embedded-binaries-local.json"
SOURCE_MANIFEST = ROOT / "scripts/core-linux-embedded-binaries-sources.json"

TARGETS = {
    "runner": "libcoreworker_runner.so",
    "proot": "libcoreworker_proot.so",
    "busybox": "libcoreworker_busybox.so",
    "box64": "libcoreworker_box64.so",
}

MIN_BYTES_BY_TARGET = {
    "runner": 1024,
    "proot": 32768,
    "busybox": 32768,
    "box64": 131072,
}

TARGET_METADATA = {
    "runner": {
        "origin": "local-core-worker",
        "sourceKind": "project-source",
        "licenseStatus": "internal-project",
        "license": "private-internal",
        "notes": "core-runner seguro próprio; preflight only",
    },
    "proot": {
        "origin": "manual-build-from-upstream-source",
        "sourceKind": "external-source",
        "upstream": "https://github.com/proot-me/proot",
        "license": "GPL-2.0-or-later",
        "licenseStatus": "verify-before-bundling",
        "notes": "não baixar em runtime; importar somente build arm64 auditado",
    },
    "busybox": {
        "origin": "manual-build-from-upstream-source",
        "sourceKind": "external-source",
        "upstream": "https://busybox.net/downloads/",
        "license": "GPL-2.0-only",
        "licenseStatus": "verify-before-bundling",
        "notes": "não baixar em runtime; importar somente build arm64 auditado",
    },
    "box64": {
        "origin": "manual-build-from-upstream-source",
        "sourceKind": "external-source",
        "upstream": "https://github.com/ptitSeb/box64",
        "license": "MIT",
        "licenseStatus": "verify-before-bundling",
        "notes": "não baixar em runtime; importar somente build arm64 auditado para ambiente Linux/proot",
    },
}

APPROVED_EXTERNAL_LICENSE_STATUSES = {
    "verified-audited",
    "source-built",
    "redistributable-verified",
}
EM_AARCH64 = 183


def load_json(path: Path) -> dict[str, Any]:
    if not path or not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def read_elf_header(path: Path) -> dict[str, Any]:
    head = path.read_bytes()[:64]
    info = {
        "isElf": len(head) >= 4 and head[:4] == b"\x7fELF",
        "elfClass": head[4] if len(head) > 4 else 0,
        "machine": int.from_bytes(head[18:20], "little") if len(head) >= 20 else 0,
    }
    info["isElf64"] = info["elfClass"] == 2
    info["isAarch64"] = info["machine"] == EM_AARCH64
    return info


def validate_source(path: Path, key: str) -> dict[str, Any]:
    if not path.exists() or not path.is_file():
        raise SystemExit(f"arquivo não encontrado: {path}")
    size = path.stat().st_size
    elf = read_elf_header(path)
    errors: list[str] = []
    min_bytes = int(MIN_BYTES_BY_TARGET.get(key, 4096))
    if size < min_bytes:
        errors.append(f"arquivo pequeno demais ({size} bytes; mínimo {min_bytes})")
    if not elf["isElf"]:
        errors.append("não parece ELF")
    if elf["isElf"] and not elf["isElf64"]:
        errors.append("ELF não é 64-bit")
    if elf["isElf"] and not elf["isAarch64"]:
        errors.append(f"machine={elf['machine']} não é AArch64")
    if errors:
        raise SystemExit(f"{path}: " + "; ".join(errors))
    return {**elf, "size": size, "sha256": sha256(path)}


def _nested_target_metadata(data: dict[str, Any], key: str) -> dict[str, Any]:
    targets = data.get("targets")
    if isinstance(targets, dict):
        raw = targets.get(key)
        if isinstance(raw, dict):
            metadata = raw.get("metadata")
            if isinstance(metadata, dict):
                merged = dict(raw)
                merged.pop("metadata", None)
                merged.update(metadata)
                return merged
            return dict(raw)
    raw = data.get(key)
    return dict(raw) if isinstance(raw, dict) else {}


def metadata_for(key: str, override: dict[str, Any], sources: dict[str, Any]) -> dict[str, Any]:
    merged: dict[str, Any] = dict(TARGET_METADATA.get(key, {}))
    for payload in (sources, override):
        item = _nested_target_metadata(payload, key)
        for k, v in item.items():
            if v is not None and v != "":
                merged[k] = v
    return merged


def metadata_errors(key: str, metadata: dict[str, Any]) -> list[str]:
    if key == "runner":
        return []
    errors: list[str] = []
    status = str(metadata.get("licenseStatus") or "").strip()
    if status not in APPROVED_EXTERNAL_LICENSE_STATUSES:
        errors.append(
            "licenseStatus precisa ser um de "
            + ", ".join(sorted(APPROVED_EXTERNAL_LICENSE_STATUSES))
            + f"; atual={status or 'vazio'}"
        )
    for required in ("origin", "sourceKind", "upstream", "license"):
        if not str(metadata.get(required) or "").strip():
            errors.append(f"metadata.{required} obrigatório")
    if not any(str(metadata.get(k) or "").strip() for k in ("sourceVersion", "sourceCommit", "packageVersion", "buildRecipe", "sourceSha256")):
        errors.append("informe ao menos sourceVersion/sourceCommit/packageVersion/buildRecipe/sourceSha256")
    return errors


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Core Linux embedded binaries intake")
    for key in TARGETS:
        parser.add_argument(f"--{key}", type=Path, help=f"caminho do binário {key} arm64")
    parser.add_argument("--metadata-file", type=Path, help="JSON com metadados auditados por target")
    parser.add_argument("--dry-run", action="store_true", help="validar sem copiar")
    parser.add_argument("--allow-unverified-external", action="store_true", help="somente para laboratório: não use em APK final")
    args = parser.parse_args(argv)

    provided = {key: getattr(args, key) for key in TARGETS if getattr(args, key) is not None}
    if not provided:
        parser.error("informe pelo menos um binário: --runner, --proot, --busybox ou --box64")

    metadata_override = load_json(args.metadata_file) if args.metadata_file else {}
    source_manifest = load_json(SOURCE_MANIFEST)

    JNI_DIR.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, Any] = {
        "schema": "core-worker-embedded-binaries-local-v3",
        "generatedAt": int(time.time()),
        "dryRun": bool(args.dry_run),
        "targets": {},
        "policy": {
            "downloadedByScript": False,
            "executedByScript": False,
            "bedrockBundled": False,
            "noPlaceholder": True,
            "sizeAndSha256Required": True,
            "licenseMetadataRequiredBeforeBundling": True,
            "externalTargetsRequireApprovedMetadata": True,
            "approvedExternalLicenseStatuses": sorted(APPROVED_EXTERNAL_LICENSE_STATUSES),
            "allowUnverifiedExternal": bool(args.allow_unverified_external),
        },
    }

    all_errors: list[str] = []
    for key, source in provided.items():
        source = source.expanduser().resolve()
        info = validate_source(source, key)
        metadata = metadata_for(key, metadata_override, source_manifest)
        errors = metadata_errors(key, metadata)
        metadata_ok = not errors
        if errors and key != "runner":
            all_errors.extend(f"{key}: {e}" for e in errors)
        dest = JNI_DIR / TARGETS[key]
        copied = False
        if not args.dry_run:
            if errors and key != "runner" and not args.allow_unverified_external:
                # Continue collecting every target error before exiting below.
                pass
            else:
                shutil.copy2(source, dest)
                os.chmod(dest, 0o644)
                copied = True
        manifest["targets"][key] = {
            "source": str(source),
            "dest": str(dest),
            "copied": copied,
            "metadata": metadata,
            "metadataOk": metadata_ok,
            "metadataErrors": errors,
            **info,
        }
        print(f"{key}: ok size={info['size']} sha256={info['sha256']} metadata={'ok' if metadata_ok else 'pendente'} -> {dest.name}")

    if all_errors and not args.dry_run and not args.allow_unverified_external:
        print(json.dumps(manifest, ensure_ascii=False, indent=2), file=sys.stderr)
        raise SystemExit("metadados externos insuficientes; ajuste --metadata-file ou use dry-run")

    if not args.dry_run:
        ASSET_DIR.mkdir(parents=True, exist_ok=True)
        text = json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
        OUT_MANIFEST.write_text(text, encoding="utf-8")
        ASSET_MANIFEST.write_text(text, encoding="utf-8")
        print(f"manifest escrito: {OUT_MANIFEST}")
        print(f"manifest de assets escrito: {ASSET_MANIFEST}")
    else:
        print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0 if not all_errors or args.dry_run or args.allow_unverified_external else 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
