#!/usr/bin/env python3
"""Pipeline local para preparar binários Core Linux embutidos no APK.

Este script é deliberadamente conservador:
- não baixa código por padrão;
- não executa binários de terceiros;
- não inicia Bedrock/Box64/proot/busybox;
- compila apenas o core-runner próprio quando houver compilador Android;
- delega a cópia/validação final ao core-linux-embedded-binaries-intake.py.

Fluxo esperado:
  1. python3 scripts/core-linux-embedded-binaries-build-pipeline.py plan
  2. python3 scripts/core-linux-embedded-binaries-build-pipeline.py build-runner --stage
  3. compilar/importar busybox/proot/box64 fora do APK
  4. criar metadata JSON com origem/licença/versão/hash/revisão auditados
  5. python3 scripts/core-linux-embedded-binaries-build-pipeline.py stage --input-dir <dir> --metadata-file <json>
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
APP_DIR = ROOT / "android/core-worker-app/app"
JNI_DIR = APP_DIR / "src/main/jniLibs/arm64-v8a"
ASSETS_DIR = APP_DIR / "src/main/assets/core-linux"
RUNNER_SOURCE = APP_DIR / "src/main/cpp/coreworker_runner.c"
BUILD_DIR = ROOT / "build/core-linux-embedded-binaries"
OUT_DIR = BUILD_DIR / "out"
INTAKE = ROOT / "scripts/core-linux-embedded-binaries-intake.py"
SOURCE_MANIFEST = ROOT / "scripts/core-linux-embedded-binaries-sources.json"

TARGETS: dict[str, dict[str, Any]] = {
    "runner": {
        "official": "libcoreworker_runner.so",
        "aliases": ["libcoreworker_runner.so", "coreworker_runner", "core-runner", "runner"],
        "source": str(RUNNER_SOURCE.relative_to(ROOT)),
        "origin": "local-core-worker",
        "sourceKind": "project-source",
        "license": "private-internal",
        "licenseStatus": "internal-project",
        "minBytes": 1024,
        "requiredAtBuild": True,
    },
    "proot": {
        "official": "libcoreworker_proot.so",
        "aliases": ["libcoreworker_proot.so", "libproot.so", "proot"],
        "origin": "termux-package-source-reference",
        "sourceKind": "external-source",
        "upstream": "https://github.com/termux/proot",
        "homepage": "https://proot-me.github.io/",
        "termuxBuildRecipe": "https://raw.githubusercontent.com/termux/termux-packages/master/packages/proot/build.sh",
        "packageVersion": "5.1.107.76",
        "sourceArchive": "https://github.com/termux/proot/archive/v5.1.107.76.zip",
        "sourceSha256": "3807871e8b8473cb254b648de40773515d52ea63ba240afc4596eefc644d9e29",
        "license": "GPL-2.0",
        "licenseStatus": "verify-before-bundling",
        "minBytes": 32768,
        "runtimeDependencies": ["libtalloc"],
        "dependencyPolicy": "se o binário não for estático, libtalloc precisa ser auditado/embutido junto",
        "buildNotes": [
            "usar build arm64/aarch64 auditado; não baixar em runtime",
            "Termux proot depende de libtalloc; não marcar pronto sem dependência estática ou libtalloc embutido",
            "PRoot usa ptrace/chroot-like sem root; testar dentro do rootfs real antes de liberar runner",
        ],
    },
    "libtalloc": {
        "official": "libtalloc.so",
        "aliases": ["libtalloc.so", "libcoreworker_libtalloc.so", "libtalloc.so.2", "libtalloc"],
        "origin": "termux-package-source-reference",
        "sourceKind": "external-source-dependency",
        "upstream": "https://www.samba.org/ftp/talloc/",
        "homepage": "https://talloc.samba.org/talloc/doc/html/index.html",
        "termuxBuildRecipe": "https://raw.githubusercontent.com/termux/termux-packages/master/packages/libtalloc/build.sh",
        "packageVersion": "2.4.3",
        "sourceArchive": "https://www.samba.org/ftp/talloc/talloc-2.4.3.tar.gz",
        "sourceSha256": "dc46c40b9f46bb34dd97fe41f548b0e8b247b77a918576733c528e83abd854dd",
        "license": "GPL-3.0",
        "licenseStatus": "verify-before-bundling",
        "minBytes": 8192,
        "onDeviceBuildSafe": False,
        "buildNotes": [
            "dependência do recipe Termux do proot",
            "recipe Termux remove/reescreve arquivos do prefixo; não buildar no Termux vivo do worker",
            "preferir build isolado/offline e empacotar apenas artefato auditado",
        ],
    },
    "busybox": {
        "official": "libcoreworker_busybox.so",
        "aliases": ["libcoreworker_busybox.so", "busybox"],
        "origin": "termux-package-source-reference",
        "sourceKind": "external-source",
        "upstream": "https://busybox.net/downloads/",
        "homepage": "https://busybox.net/",
        "termuxBuildRecipe": "https://raw.githubusercontent.com/termux/termux-packages/master/packages/busybox/build.sh",
        "packageVersion": "1.37.0-r3",
        "sourceArchive": "https://busybox.net/downloads/busybox-1.37.0.tar.bz2",
        "sourceSha256": "3311dff32e746499f4df0d5df04d7eb396382d7e108bb9250e7b519b837043a4",
        "license": "GPL-2.0",
        "licenseStatus": "verify-before-bundling",
        "minBytes": 4096,
        "runtimeDependencies": ["libbusybox", "libandroid_selinux", "libpcre2_8"],
        "dependencyPolicy": "binário Termux é dinâmico; libcoreworker_busybox.so é wrapper pequeno; payload real obrigatório em libbusybox.so + libandroid-selinux.so + libpcre2-8.so",
        "onDeviceBuildSafe": False,
        "buildNotes": [
            "o recipe Termux marca BusyBox como não seguro para build on-device",
            "não rodar build.sh no prefixo vivo do worker; usar ambiente isolado",
            "wrapper pode ser pequeno; o tamanho forte fica em libbusybox.so",
            "GPL-2.0 exige source correspondente, .config e notas de modificação ao distribuir binário",
        ],
    },
    "libbusybox": {
        "official": "libbusybox.so",
        "aliases": ["libbusybox.so", "libbusybox.so.1.37.0"],
        "origin": "termux-package-source-reference",
        "sourceKind": "external-source-dependency",
        "upstream": "https://busybox.net/downloads/",
        "homepage": "https://busybox.net/",
        "termuxBuildRecipe": "https://raw.githubusercontent.com/termux/termux-packages/master/packages/busybox/build.sh",
        "packageVersion": "1.37.0-r3",
        "sourceArchive": "https://busybox.net/downloads/busybox-1.37.0.tar.bz2",
        "sourceSha256": "3311dff32e746499f4df0d5df04d7eb396382d7e108bb9250e7b519b837043a4",
        "license": "GPL-2.0",
        "licenseStatus": "verify-before-bundling",
        "minBytes": 32768,
        "runtimeDependencies": ["libandroid_selinux", "libpcre2_8"],
        "dependencyPolicy": "dependência dinâmica do busybox; RUNPATH ajustado para $ORIGIN",
        "onDeviceBuildSafe": False,
        "buildNotes": [
            "extraído do mesmo pacote Termux do BusyBox",
            "empacotar como libbusybox.so para ser extraído pelo Android como native lib",
        ],
    },
    "libandroid_selinux": {
        "official": "libandroid-selinux.so",
        "aliases": ["libandroid-selinux.so", "libandroid_selinux.so"],
        "origin": "termux-package-source-reference",
        "sourceKind": "external-source-dependency",
        "upstream": "https://android.googlesource.com/platform/external/selinux/",
        "homepage": "https://selinuxproject.org/",
        "termuxBuildRecipe": "https://raw.githubusercontent.com/termux/termux-packages/master/packages/libandroid-selinux/build.sh",
        "packageVersion": "14.0.0.11-1",
        "license": "public-domain|BSD-style-android-platform",
        "licenseStatus": "verify-before-bundling",
        "minBytes": 32768,
        "runtimeDependencies": ["libpcre2_8"],
        "dependencyPolicy": "dependência dinâmica de libbusybox; RUNPATH ajustado para $ORIGIN",
        "onDeviceBuildSafe": False,
        "buildNotes": ["necessário para libbusybox.so do pacote Termux"],
    },
    "libpcre2_8": {
        "official": "libpcre2-8.so",
        "aliases": ["libpcre2-8.so", "pcre2-8.so"],
        "origin": "termux-package-source-reference",
        "sourceKind": "external-source-dependency",
        "upstream": "https://github.com/PCRE2Project/pcre2",
        "homepage": "https://pcre2project.github.io/pcre2/",
        "termuxBuildRecipe": "https://raw.githubusercontent.com/termux/termux-packages/master/packages/pcre2/build.sh",
        "packageVersion": "10.47",
        "license": "BSD-3-Clause",
        "licenseStatus": "verify-before-bundling",
        "minBytes": 32768,
        "runtimeDependencies": [],
        "dependencyPolicy": "dependência dinâmica de libandroid-selinux; RUNPATH ajustado para $ORIGIN",
        "onDeviceBuildSafe": False,
        "buildNotes": ["necessário para libandroid-selinux.so"],
    },
    "box64": {
        "official": "libcoreworker_box64.so",
        "aliases": ["libcoreworker_box64.so", "libbox64.so", "box64"],
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
        "minBytes": 131072,
        "binarySha256": "bae41f0619e51307f6e75e1d83b54137c5ba395ba46ba4394de264613bcd73ca",
        "binarySize": 28008024,
        "blockedUntil": "box64-version-smoke-v15",
        "buildNotes": [
            "Box64 é userspace Linux x86_64 emulator para hosts ARM64; validar dentro do rootfs Linux/proot",
            "não executar em Android puro; runner real continua bloqueado até preflight completo",
        ],
    },
}
APPROVED_EXTERNAL_LICENSE_STATUSES = ["redistributable-verified", "source-built", "verified-audited"]


def rel(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def sh(cmd: list[str], *, cwd: Path | None = None) -> None:
    print("$", " ".join(cmd))
    subprocess.run(cmd, cwd=str(cwd or ROOT), check=True)


def find_ndk_clang(ndk: Path) -> Path | None:
    prebuilt = ndk / "toolchains/llvm/prebuilt"
    if not prebuilt.exists():
        return None
    for host in sorted(prebuilt.iterdir()):
        for api in ("26", "24", "23"):
            candidate = host / "bin" / f"aarch64-linux-android{api}-clang"
            if candidate.exists():
                return candidate
    return None


def find_cc(explicit: str | None = None) -> str | None:
    if explicit:
        return explicit
    env_cc = os.environ.get("CC")
    if env_cc:
        return env_cc
    for env_name in ("ANDROID_NDK_HOME", "ANDROID_NDK_ROOT"):
        base = os.environ.get(env_name)
        if base:
            found = find_ndk_clang(Path(base))
            if found:
                return str(found)
    for sdk_env in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        sdk = os.environ.get(sdk_env)
        if sdk:
            ndk_dir = Path(sdk) / "ndk"
            if ndk_dir.exists():
                for child in sorted(ndk_dir.iterdir(), reverse=True):
                    found = find_ndk_clang(child)
                    if found:
                        return str(found)
    system_clang = shutil.which("aarch64-linux-android26-clang") or shutil.which("aarch64-linux-android24-clang")
    if system_clang:
        return system_clang
    generic_clang = shutil.which("clang")
    if generic_clang:
        return generic_clang
    return None


def source_manifest_payload() -> dict[str, Any]:
    return {
        "schema": "core-worker-embedded-binaries-source-plan-v11",
        "generatedAt": int(time.time()),
        "abi": "arm64-v8a",
        "androidMinSdk": 26,
        "policy": {
            "noRuntimeDownload": True,
            "noPlaceholder": True,
            "noBedrockBundledInApk": True,
            "noExecutionDuringBuild": True,
            "jniLibsOnlyForFutureExecution": True,
            "nativeLibExtractionRequiredForExternalTools": True,
            "apkZipEntryOnlyNotExecutable": True,
            "runnerRequiredAtBuild": True,
            "metadataRequiredBeforeBundling": True,
            "sizeAndSha256Required": True,
            "externalTargetsRequireApprovedMetadata": True,
            "approvedExternalLicenseStatuses": APPROVED_EXTERNAL_LICENSE_STATUSES,
        },
        "targets": TARGETS,
    }


def write_source_manifest() -> Path:
    ASSETS_DIR.mkdir(parents=True, exist_ok=True)
    SOURCE_MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(source_manifest_payload(), ensure_ascii=False, indent=2) + "\n"
    SOURCE_MANIFEST.write_text(text, encoding="utf-8")
    (ASSETS_DIR / "embedded-binaries-source-plan.json").write_text(text, encoding="utf-8")
    return SOURCE_MANIFEST


def status() -> dict[str, Any]:
    rows: dict[str, Any] = {}
    for key, meta in TARGETS.items():
        path = JNI_DIR / str(meta["official"])
        rows[key] = {
            "official": meta["official"],
            "present": path.exists(),
            "size": path.stat().st_size if path.exists() else 0,
            "path": rel(path),
        }
    return rows


def metadata_template() -> dict[str, Any]:
    targets: dict[str, Any] = {}
    for key, meta in TARGETS.items():
        if key == "runner":
            continue
        license_name = str(meta.get("license", ""))
        is_gpl = license_name.startswith("GPL")
        targets[key] = {
            "origin": meta.get("origin", ""),
            "sourceKind": meta.get("sourceKind", ""),
            "upstream": meta.get("upstream", ""),
            "homepage": meta.get("homepage", ""),
            "termuxBuildRecipe": meta.get("termuxBuildRecipe", ""),
            "license": license_name,
            "licenseStatus": "verified-audited",
            "packageVersion": meta.get("packageVersion", ""),
            "sourceArchive": meta.get("sourceArchive", ""),
            "sourceSha256": meta.get("sourceSha256", ""),
            "binarySha256": "preencha com o sha256 do binário arm64 final",
            "binarySize": "preencha com o tamanho em bytes do binário arm64 final",
            "linkMode": "static|self-contained|dynamic-with-bundled-dependencies",
            "runtimeDependencies": meta.get("runtimeDependencies", []),
            "dependencyPolicy": meta.get("dependencyPolicy", ""),
            "buildRecipe": "descreva ambiente isolado/toolchain/config usado; não usar build no Termux vivo se onDeviceBuildSafe=false",
            "onDeviceBuildSafe": meta.get("onDeviceBuildSafe", True),
            "auditedBy": "",
            "notes": meta.get("buildNotes", []),
            "sourceCompliance": {
                "required": is_gpl,
                "completeCorrespondingSourceReady": False if is_gpl else True,
                "licenseTextIncluded": False if is_gpl else True,
                "sourceUrl": meta.get("sourceArchive", ""),
                "configUrlOrPath": "preencha .config/patches/build scripts quando GPL exigir",
                "modifications": "none|descrever patches",
            },
        }
    return {
        "schema": "core-worker-embedded-binaries-metadata-v2",
        "generatedAt": int(time.time()),
        "policy": {
            "noRuntimeDownload": True,
            "noExecutionDuringAudit": True,
            "noOnDeviceUnsafeBuild": True,
            "gplSourceComplianceRequired": True,
            "dynamicDependenciesMustBeBundledOrStatic": True,
        },
        "targets": targets,
    }

def cmd_plan(args: argparse.Namespace) -> int:
    manifest = write_source_manifest()
    payload = {
        "ok": True,
        "stage": "core-linux-embedded-binaries-build-pipeline-v6",
        "sourceManifest": rel(manifest),
        "metadataTemplate": "use: python3 scripts/core-linux-embedded-binaries-build-pipeline.py metadata-template > /tmp/core-linux-binaries-metadata.json",
        "jniDir": rel(JNI_DIR),
        "runnerSource": rel(RUNNER_SOURCE),
        "targets": TARGETS,
        "current": status(),
        "tools": {
            "cc": find_cc(args.cc),
            "python": sys.executable,
            "platform": platform.platform(),
            "intake": rel(INTAKE),
        },
        "notes": [
            "O script não baixa binários automaticamente.",
            "BusyBox, PRoot, libtalloc, libbusybox, libandroid-selinux, libpcre2-8 e Box64 devem vir de build/import auditado e depois passar pelo intake.",
            "Cada asset externo precisa de tamanho, SHA-256 e metadados aprovados de origem/licença antes de ser aceito no stage real.",
            "PRoot/BusyBox precisam estar extraídos em nativeLibraryDir; ZipEntry dentro do APK é diagnóstico, não caminho executável.",
            "Bedrock não entra no APK e não é iniciado neste estágio.",
        ],
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def cmd_metadata_template(args: argparse.Namespace) -> int:
    write_source_manifest()
    print(json.dumps(metadata_template(), ensure_ascii=False, indent=2))
    return 0


def cc_is_android_target(cc: str) -> bool:
    name = Path(str(cc)).name
    return "aarch64-linux-android" in name


def cmd_build_runner(args: argparse.Namespace) -> int:
    cc = find_cc(args.cc)
    if not cc:
        raise SystemExit("compilador Android/clang não encontrado; defina CC ou ANDROID_NDK_HOME")
    if not RUNNER_SOURCE.exists():
        raise SystemExit(f"fonte do runner ausente: {RUNNER_SOURCE}")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / str(TARGETS["runner"]["official"])
    cmd = [cc]
    if not cc_is_android_target(cc):
        cmd.append("--target=aarch64-linux-android26")
    cmd.extend([
        "-O2",
        "-fPIC",
        "-shared",
        "-nostdlib",
        "-Wall",
        "-Wextra",
        "-Werror",
        "-DCORE_WORKER_RUNNER_SAFE_PREFLIGHT_ONLY=1",
        "-Wl,-soname,libcoreworker_runner.so",
        "-o",
        str(out),
        str(RUNNER_SOURCE),
    ])
    sh(cmd)
    print(f"runner compilado: {rel(out)}")
    if args.stage:
        sh([sys.executable, str(INTAKE), "--runner", str(out)])
    return 0


def find_alias(input_dir: Path, key: str) -> Path | None:
    meta = TARGETS[key]
    for name in meta["aliases"]:
        p = input_dir / str(name)
        if p.exists() and p.is_file():
            return p
    return None


def build_intake_command(
    input_dir: Path,
    *,
    dry_run: bool,
    metadata_file: Path | None,
    allow_unverified_external: bool = False,
    include_keys: set[str] | None = None,
    require_keys: set[str] | None = None,
) -> list[str]:
    command = [sys.executable, str(INTAKE)]
    found: list[str] = []
    allowed = include_keys or set(TARGETS)
    for key in TARGETS:
        if key not in allowed:
            continue
        p = find_alias(input_dir, key)
        if p:
            command.extend([f"--{key}", str(p)])
            found.append(key)
    required = require_keys or set()
    missing_required = sorted(required.difference(found))
    if missing_required:
        raise SystemExit("binário obrigatório ausente no input-dir: " + ", ".join(missing_required))
    if not found:
        wanted = ", ".join(sorted(allowed))
        raise SystemExit(f"nenhum binário conhecido encontrado no input-dir para: {wanted}")
    if metadata_file:
        command.extend(["--metadata-file", str(metadata_file.resolve())])
    if allow_unverified_external:
        command.append("--allow-unverified-external")
    if dry_run:
        command.append("--dry-run")
    return command


def cmd_stage(args: argparse.Namespace) -> int:
    write_source_manifest()
    input_dir = args.input_dir.resolve()
    if not input_dir.exists() or not input_dir.is_dir():
        raise SystemExit(f"input-dir inválido: {input_dir}")
    command = build_intake_command(
        input_dir,
        dry_run=args.dry_run,
        metadata_file=args.metadata_file,
        allow_unverified_external=args.allow_unverified_external,
    )
    sh(command)
    return 0


def cmd_audit_input(args: argparse.Namespace) -> int:
    write_source_manifest()
    input_dir = args.input_dir.resolve()
    command = build_intake_command(input_dir, dry_run=True, metadata_file=args.metadata_file)
    sh(command)
    return 0



def cmd_audit_base_tools(args: argparse.Namespace) -> int:
    """Valida apenas PRoot + BusyBox antes de chegar no Box64.

    Este comando é o próximo estágio seguro: ele não aceita Box64, não executa
    nada e força dry-run. Serve para conferir origem/licença/hash dos dois
    utilitários base que o runner vai precisar.
    """
    write_source_manifest()
    input_dir = args.input_dir.resolve()
    command = build_intake_command(
        input_dir,
        dry_run=True,
        metadata_file=args.metadata_file,
        include_keys={"proot", "busybox", "libtalloc", "libbusybox", "libandroid_selinux", "libpcre2_8"},
        require_keys={"proot", "busybox", "libtalloc", "libbusybox", "libandroid_selinux", "libpcre2_8"},
    )
    sh(command)
    return 0


def cmd_stage_base_tools(args: argparse.Namespace) -> int:
    """Copia PRoot + BusyBox auditados para jniLibs; Box64 continua fora."""
    write_source_manifest()
    input_dir = args.input_dir.resolve()
    command = build_intake_command(
        input_dir,
        dry_run=args.dry_run,
        metadata_file=args.metadata_file,
        include_keys={"proot", "busybox", "libtalloc", "libbusybox", "libandroid_selinux", "libpcre2_8"},
        require_keys={"proot", "busybox", "libtalloc", "libbusybox", "libandroid_selinux", "libpcre2_8"},
    )
    sh(command)
    return 0

def cmd_verify(args: argparse.Namespace) -> int:
    # Verificação local precisa ser read-only. O updater e os diagnósticos rodam
    # este comando com frequência; gerar `embedded-binaries-source-plan.json` aqui
    # sujava arquivos rastreados e bloqueava os próximos updates.
    command = [sys.executable, str(INTAKE), "--dry-run"]
    if args.metadata_file:
        command.extend(["--metadata-file", str(args.metadata_file.resolve())])
    any_present = False
    for key, meta in TARGETS.items():
        p = JNI_DIR / str(meta["official"])
        if p.exists():
            command.extend([f"--{key}", str(p)])
            any_present = True
    if not any_present:
        print(json.dumps({"ok": True, "present": 0, "message": "nenhum binário embutido ainda"}, ensure_ascii=False, indent=2))
        return 0 if not args.strict else 2
    sh(command)
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Core Linux embedded binaries build/import pipeline")
    parser.add_argument("--cc", help="compilador C explícito para build-runner")
    sub = parser.add_subparsers(dest="command", required=True)

    p_plan = sub.add_parser("plan", help="mostra plano e fontes sem baixar nada")
    p_plan.set_defaults(func=cmd_plan)

    p_template = sub.add_parser("metadata-template", help="gera template de metadados auditáveis para assets externos")
    p_template.set_defaults(func=cmd_metadata_template)

    p_runner = sub.add_parser("build-runner", help="compila o core-runner próprio seguro")
    p_runner.add_argument("--stage", action="store_true", help="copiar para jniLibs via intake após compilar")
    p_runner.set_defaults(func=cmd_build_runner)

    p_stage = sub.add_parser("stage", help="importa binários já compilados de um diretório")
    p_stage.add_argument("--input-dir", type=Path, required=True)
    p_stage.add_argument("--metadata-file", type=Path, help="JSON com origem/licença/versão/hash auditados")
    p_stage.add_argument("--dry-run", action="store_true")
    p_stage.add_argument("--allow-unverified-external", action="store_true", help="laboratório apenas; não usar em APK final")
    p_stage.set_defaults(func=cmd_stage)

    p_audit = sub.add_parser("audit-input", help="valida input-dir em dry-run com metadados")
    p_audit.add_argument("--input-dir", type=Path, required=True)
    p_audit.add_argument("--metadata-file", type=Path, help="JSON com origem/licença/versão/hash auditados")
    p_audit.set_defaults(func=cmd_audit_input)

    p_base_audit = sub.add_parser("audit-base-tools", help="valida só proot+busybox em dry-run; Box64 fica para depois")
    p_base_audit.add_argument("--input-dir", type=Path, required=True)
    p_base_audit.add_argument("--metadata-file", type=Path, required=True, help="JSON com origem/licença/versão/hash auditados")
    p_base_audit.set_defaults(func=cmd_audit_base_tools)

    p_base_stage = sub.add_parser("stage-base-tools", help="importa só proot+busybox auditados; não toca Box64")
    p_base_stage.add_argument("--input-dir", type=Path, required=True)
    p_base_stage.add_argument("--metadata-file", type=Path, required=True, help="JSON com origem/licença/versão/hash auditados")
    p_base_stage.add_argument("--dry-run", action="store_true")
    p_base_stage.set_defaults(func=cmd_stage_base_tools)

    p_verify = sub.add_parser("verify", help="valida os binários presentes em jniLibs")
    p_verify.add_argument("--metadata-file", type=Path, help="JSON com origem/licença/versão/hash auditados")
    p_verify.add_argument("--strict", action="store_true", help="falha se nenhum binário estiver presente")
    p_verify.set_defaults(func=cmd_verify)

    args = parser.parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
