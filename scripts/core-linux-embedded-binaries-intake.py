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
ASSET_BIN_DIR = ASSET_DIR / "bin"
ASSET_MANIFEST = ASSET_DIR / "embedded-binaries-manifest.json"
OUT_MANIFEST = ASSET_DIR / "embedded-binaries-local.json"
SOURCE_MANIFEST = ROOT / "scripts/core-linux-embedded-binaries-sources.json"

TARGETS = {
    "runner": "libcoreworker_runner.so",
    "proot": "libcoreworker_proot.so",
    "busybox": "libcoreworker_busybox.so",
    "libtalloc": "libtalloc.so",
    "libbusybox": "libbusybox.so",
    "libandroid_selinux": "libandroid-selinux.so",
    "libpcre2_8": "libpcre2-8.so",
    "box64": "box64",
}

MIN_BYTES_BY_TARGET = {
    "runner": 1024,
    "proot": 32768,
    "busybox": 4096,
    "libtalloc": 8192,
    "libbusybox": 32768,
    "libandroid_selinux": 32768,
    "libpcre2_8": 32768,
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
        "origin": "termux-package-source-reference",
        "sourceKind": "external-source",
        "upstream": "https://github.com/termux/proot",
        "license": "GPL-2.0",
        "licenseStatus": "verify-before-bundling",
        "packageVersion": "5.1.107.76",
        "sourceSha256": "3807871e8b8473cb254b648de40773515d52ea63ba240afc4596eefc644d9e29",
        "runtimeDependencies": ["libtalloc"],
        "notes": "não baixar em runtime; importar somente build arm64 auditado; Termux proot depende de libtalloc quando não for estático",
    },
    "libtalloc": {
        "origin": "termux-package-source-reference",
        "sourceKind": "external-source-dependency",
        "upstream": "https://www.samba.org/ftp/talloc/",
        "license": "GPL-3.0",
        "licenseStatus": "verify-before-bundling",
        "packageVersion": "2.4.3",
        "sourceSha256": "dc46c40b9f46bb34dd97fe41f548b0e8b247b77a918576733c528e83abd854dd",
        "notes": "dependência de proot; build Termux não deve rodar no prefixo vivo do worker",
    },
    "busybox": {
        "origin": "termux-package-source-reference",
        "sourceKind": "external-source",
        "upstream": "https://busybox.net/downloads/",
        "license": "GPL-2.0",
        "licenseStatus": "verify-before-bundling",
        "packageVersion": "1.37.0-r3",
        "sourceSha256": "3311dff32e746499f4df0d5df04d7eb396382d7e108bb9250e7b519b837043a4",
        "runtimeDependencies": ["libbusybox", "libandroid_selinux", "libpcre2_8"],
        "notes": "não baixar em runtime; importar somente build arm64 auditado; recipe Termux não é seguro para build on-device; NEEDED/RUNPATH ajustados para jniLibs",
    },
    "libbusybox": {
        "origin": "termux-package-source-reference",
        "sourceKind": "external-source-dependency",
        "upstream": "https://busybox.net/downloads/",
        "license": "GPL-2.0",
        "licenseStatus": "verify-before-bundling",
        "packageVersion": "1.37.0-r3",
        "sourceSha256": "3311dff32e746499f4df0d5df04d7eb396382d7e108bb9250e7b519b837043a4",
        "runtimeDependencies": ["libandroid_selinux", "libpcre2_8"],
        "notes": "lib interna do BusyBox; RUNPATH ajustado para $ORIGIN",
    },
    "libandroid_selinux": {
        "origin": "termux-package-source-reference",
        "sourceKind": "external-source-dependency",
        "upstream": "https://android.googlesource.com/platform/external/selinux/",
        "license": "public-domain|BSD-style-android-platform",
        "licenseStatus": "verify-before-bundling",
        "packageVersion": "14.0.0.11-1",
        "runtimeDependencies": ["libpcre2_8"],
        "notes": "dependência de libbusybox.so; RUNPATH ajustado para $ORIGIN",
    },
    "libpcre2_8": {
        "origin": "termux-package-source-reference",
        "sourceKind": "external-source-dependency",
        "upstream": "https://github.com/PCRE2Project/pcre2",
        "license": "BSD-3-Clause",
        "licenseStatus": "verify-before-bundling",
        "packageVersion": "10.47",
        "notes": "dependência de libandroid-selinux.so; RUNPATH ajustado para $ORIGIN",
    },
    "box64": {
        "origin": "ryanfortner-box64-debs-prebuilt",
        "sourceKind": "external-prebuilt-deb",
        "upstream": "https://github.com/ptitSeb/box64",
        "homepage": "https://box86.org/",
        "packageRepository": "https://github.com/ryanfortner/box64-debs",
        "packageFile": "box64-android_0.4.3+20260606.e694f2c-1_arm64.deb",
        "packageVersion": "0.4.3+20260606.e694f2c-1",
        "sourceCommit": "e694f2c",
        "sourceArchive": "https://github.com/ptitSeb/box64/archive/e694f2c.tar.gz",
        "license": "MIT",
        "licenseStatus": "redistributable-verified",
        "binarySha256": "bae41f0619e51307f6e75e1d83b54137c5ba395ba46ba4394de264613bcd73ca",
        "binarySize": 28008024,
        "notes": "V14.2.1 embute/audita Box64 arm64 como asset core-linux/bin/box64; execução fica para smoke posterior dentro do rootfs Linux/proot",
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


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "sim", "ok"}
    return False


def _license_is_gpl(metadata: dict[str, Any]) -> bool:
    return str(metadata.get("license") or "").strip().upper().startswith("GPL")


def _source_compliance_ok(metadata: dict[str, Any]) -> bool:
    if not _license_is_gpl(metadata):
        return True
    comp = metadata.get("sourceCompliance")
    if not isinstance(comp, dict):
        return False
    return bool(
        _as_bool(comp.get("completeCorrespondingSourceReady"))
        and _as_bool(comp.get("licenseTextIncluded"))
        and str(comp.get("sourceUrl") or metadata.get("sourceArchive") or metadata.get("upstream") or "").strip()
    )


def metadata_errors(key: str, metadata: dict[str, Any], info: dict[str, Any] | None = None) -> list[str]:
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
    expected_source_sha = str(TARGET_METADATA.get(key, {}).get("sourceSha256") or "").strip().lower()
    provided_source_sha = str(metadata.get("sourceSha256") or "").strip().lower()
    if expected_source_sha and provided_source_sha and provided_source_sha != expected_source_sha:
        errors.append(f"sourceSha256 diferente do recipe auditado: esperado={expected_source_sha} atual={provided_source_sha}")
    expected_version = str(TARGET_METADATA.get(key, {}).get("packageVersion") or "").strip()
    provided_version = str(metadata.get("packageVersion") or metadata.get("sourceVersion") or "").strip()
    if expected_version and provided_version and provided_version != expected_version:
        errors.append(f"versão diferente do recipe auditado: esperado={expected_version} atual={provided_version}")
    expected_binary_sha = str(metadata.get("binarySha256") or metadata.get("expectedBinarySha256") or "").strip().lower()
    actual_binary_sha = str((info or {}).get("sha256") or "").strip().lower()
    if expected_binary_sha and actual_binary_sha and expected_binary_sha != actual_binary_sha:
        errors.append(f"binarySha256 não confere: esperado={expected_binary_sha} atual={actual_binary_sha}")
    if _license_is_gpl(metadata) and not _source_compliance_ok(metadata):
        errors.append("licença GPL exige sourceCompliance.completeCorrespondingSourceReady=true, licenseTextIncluded=true e sourceUrl válido")
    link_mode = str(metadata.get("linkMode") or "").strip().lower()
    if key in {"proot", "busybox", "libtalloc", "libbusybox", "libandroid_selinux", "libpcre2_8"} and link_mode not in {"static", "self-contained", "dynamic-with-bundled-dependencies"}:
        errors.append("metadata.linkMode precisa ser static, self-contained ou dynamic-with-bundled-dependencies")
    if key in {"busybox", "libtalloc"} and TARGET_METADATA.get(key, {}).get("notes", "").find("não deve") >= 0:
        if _as_bool(metadata.get("builtOnLiveWorkerPrefix")):
            errors.append("build no prefixo vivo do worker não é aceito para este pacote")
    return errors


def cross_target_errors(provided: dict[str, Path], target_manifests: dict[str, dict[str, Any]]) -> list[str]:
    errors: list[str] = []
    if "proot" in provided:
        meta = target_manifests.get("proot", {})
        link_mode = str(meta.get("linkMode") or "").strip().lower()
        deps = meta.get("runtimeDependencies")
        dep_names = {str(x).strip().lower() for x in deps} if isinstance(deps, list) else set()
        needs_libtalloc = "libtalloc" in dep_names or link_mode == "dynamic-with-bundled-dependencies"
        if needs_libtalloc and link_mode != "static" and "libtalloc" not in provided:
            errors.append("proot dinâmico exige --libtalloc auditado/embutido ou metadata.linkMode=static")
    if "busybox" in provided:
        meta = target_manifests.get("busybox", {})
        link_mode = str(meta.get("linkMode") or "").strip().lower()
        if link_mode not in {"static", "self-contained"}:
            required = {"libbusybox", "libandroid_selinux", "libpcre2_8"}
            missing = sorted(required.difference(provided))
            if missing:
                errors.append("busybox dinâmico exige dependências auditadas/embutidas: " + ", ".join(missing))
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
        parser.error("informe pelo menos um binário conhecido do Core Linux")

    metadata_override = load_json(args.metadata_file) if args.metadata_file else {}
    source_manifest = load_json(SOURCE_MANIFEST)

    JNI_DIR.mkdir(parents=True, exist_ok=True)
    ASSET_BIN_DIR.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, Any] = {
        "schema": "core-worker-embedded-binaries-local-v6",
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
    target_metadatas: dict[str, dict[str, Any]] = {}
    for key, source in provided.items():
        source = source.expanduser().resolve()
        info = validate_source(source, key)
        metadata = metadata_for(key, metadata_override, source_manifest)
        errors = metadata_errors(key, metadata, info)
        metadata_ok = not errors
        if errors and key != "runner":
            all_errors.extend(f"{key}: {e}" for e in errors)
        dest = (ASSET_BIN_DIR / TARGETS[key]) if key == "box64" else (JNI_DIR / TARGETS[key])
        copied = False
        if not args.dry_run:
            if errors and key != "runner" and not args.allow_unverified_external:
                # Continue collecting every target error before exiting below.
                pass
            else:
                if source != dest:
                    shutil.copy2(source, dest)
                    copied = True
                else:
                    copied = False
                os.chmod(dest, 0o644)
        target_metadatas[key] = metadata
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

    all_errors.extend(cross_target_errors(provided, target_metadatas))

    if all_errors:
        manifest["errors"] = all_errors

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
    return 0 if not all_errors or args.allow_unverified_external else 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
