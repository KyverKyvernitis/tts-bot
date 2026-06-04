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
  4. python3 scripts/core-linux-embedded-binaries-build-pipeline.py stage --input-dir <dir>
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

ROOT = Path(__file__).resolve().parents[1]
APP_DIR = ROOT / "android/core-worker-app/app"
JNI_DIR = APP_DIR / "src/main/jniLibs/arm64-v8a"
ASSETS_DIR = APP_DIR / "src/main/assets/core-linux"
RUNNER_SOURCE = APP_DIR / "src/main/cpp/coreworker_runner.c"
BUILD_DIR = ROOT / "build/core-linux-embedded-binaries"
OUT_DIR = BUILD_DIR / "out"
INTAKE = ROOT / "scripts/core-linux-embedded-binaries-intake.py"
SOURCE_MANIFEST = ROOT / "scripts/core-linux-embedded-binaries-sources.json"

TARGETS = {
    "runner": {
        "official": "libcoreworker_runner.so",
        "aliases": ["libcoreworker_runner.so", "coreworker_runner", "core-runner", "runner"],
        "source": str(RUNNER_SOURCE.relative_to(ROOT)),
        "origin": "local-core-worker",
        "minBytes": 1024,
        "requiredAtBuild": True,
    },
    "busybox": {
        "official": "libcoreworker_busybox.so",
        "aliases": ["libcoreworker_busybox.so", "libbusybox.so", "busybox"],
        "origin": "manual-build-from-upstream-source",
        "upstream": "https://busybox.net/downloads/",
        "minBytes": 32768,
    },
    "proot": {
        "official": "libcoreworker_proot.so",
        "aliases": ["libcoreworker_proot.so", "libproot.so", "proot"],
        "origin": "manual-build-or-audited-import",
        "upstream": "https://github.com/proot-me/proot",
        "minBytes": 32768,
    },
    "box64": {
        "official": "libcoreworker_box64.so",
        "aliases": ["libcoreworker_box64.so", "libbox64.so", "box64"],
        "origin": "manual-build-from-upstream-source",
        "upstream": "https://github.com/ptitSeb/box64",
        "minBytes": 131072,
    },
}


def rel(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def sh(cmd: list[str], *, cwd: Path | None = None) -> None:
    print("$", " ".join(cmd))
    subprocess.run(cmd, cwd=str(cwd or ROOT), check=True)


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
    # O runner próprio não usa libc. Em ambiente de auditoria/VPS, um clang
    # moderno com lld consegue gerar ELF AArch64 Android via --target sem NDK
    # completo. Isso só vale para o runner local seguro; busybox/proot/box64
    # continuam exigindo build/import auditado separado.
    generic_clang = shutil.which("clang")
    if generic_clang:
        return generic_clang
    return None


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


def write_source_manifest() -> Path:
    ASSETS_DIR.mkdir(parents=True, exist_ok=True)
    SOURCE_MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": "core-worker-embedded-binaries-source-plan-v2",
        "generatedAt": int(time.time()),
        "abi": "arm64-v8a",
        "androidMinSdk": 26,
        "policy": {
            "noRuntimeDownload": True,
            "noPlaceholder": True,
            "noBedrockBundledInApk": True,
            "noExecutionDuringBuild": True,
            "jniLibsOnlyForFutureExecution": True,
            "runnerRequiredAtBuild": True,
        },
        "targets": TARGETS,
    }
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    SOURCE_MANIFEST.write_text(text, encoding="utf-8")
    (ASSETS_DIR / "embedded-binaries-source-plan.json").write_text(text, encoding="utf-8")
    return SOURCE_MANIFEST


def status() -> dict:
    rows = {}
    for key, meta in TARGETS.items():
        path = JNI_DIR / meta["official"]
        rows[key] = {
            "official": meta["official"],
            "present": path.exists(),
            "size": path.stat().st_size if path.exists() else 0,
            "path": rel(path),
        }
    return rows


def cmd_plan(args: argparse.Namespace) -> int:
    manifest = write_source_manifest()
    payload = {
        "ok": True,
        "stage": "core-linux-embedded-binaries-build-pipeline-v1",
        "sourceManifest": rel(manifest),
        "jniDir": rel(JNI_DIR),
        "runnerSource": rel(RUNNER_SOURCE),
        "targets": TARGETS,
        "current": status(),
        "tools": {
            "cc": find_cc(args.cc),
            "python": sys.executable,
            "intake": rel(INTAKE),
        },
        "notes": [
            "O script não baixa binários automaticamente.",
            "BusyBox, PRoot e Box64 devem vir de build/import auditado e depois passar pelo intake.",
            "Bedrock não entra no APK e não é iniciado neste estágio.",
        ],
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
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
    out = OUT_DIR / TARGETS["runner"]["official"]
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
        p = input_dir / name
        if p.exists() and p.is_file():
            return p
    return None


def cmd_stage(args: argparse.Namespace) -> int:
    input_dir = args.input_dir.resolve()
    if not input_dir.exists() or not input_dir.is_dir():
        raise SystemExit(f"input-dir inválido: {input_dir}")
    command = [sys.executable, str(INTAKE)]
    found = []
    for key in TARGETS:
        p = find_alias(input_dir, key)
        if p:
            command.extend([f"--{key}", str(p)])
            found.append(key)
    if not found:
        raise SystemExit("nenhum binário conhecido encontrado no input-dir")
    if args.dry_run:
        command.append("--dry-run")
    sh(command)
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    write_source_manifest()
    command = [sys.executable, str(INTAKE), "--dry-run"]
    any_present = False
    for key, meta in TARGETS.items():
        p = JNI_DIR / meta["official"]
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

    p_runner = sub.add_parser("build-runner", help="compila o core-runner próprio seguro")
    p_runner.add_argument("--stage", action="store_true", help="copiar para jniLibs via intake após compilar")
    p_runner.set_defaults(func=cmd_build_runner)

    p_stage = sub.add_parser("stage", help="importa binários já compilados de um diretório")
    p_stage.add_argument("--input-dir", type=Path, required=True)
    p_stage.add_argument("--dry-run", action="store_true")
    p_stage.set_defaults(func=cmd_stage)

    p_verify = sub.add_parser("verify", help="valida os binários presentes em jniLibs")
    p_verify.add_argument("--strict", action="store_true", help="falha se nenhum binário estiver presente")
    p_verify.set_defaults(func=cmd_verify)

    args = parser.parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
