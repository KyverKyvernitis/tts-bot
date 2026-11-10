#!/usr/bin/env python3
"""Mandatory native TTS gate. Missing/incompatible native code is a failure.

Each pytest file runs in a separate process because legacy suites replace
Discord and provider modules in sys.modules during collection.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
REQUIRED_EXPORTS = (
    "FluxoDeAudio", "ReplayMemoryBudget", "NativeReplayBuffer", "policy_call",
    "NativePendingQueue", "NativeQueueCoordinator", "NativeProviderStream",
    "provider_start", "provider_save", "provider_voices", "provider_languages_json",
    "NativeSourceHandle", "NativeEngineCancellation", "NativeEsperResident",
    "native_worldline", "native_resampler", "NativeRenderCancellation",
    "ttv_plan_json", "ttv_worker_request_json", "ttv_render_json", "ttv_domain_json",
    "ttv_pcm_scale", "ttv_close_residents", "ttv_midi_json", "cd_scale_pcm16",
    "cd_stereo_mix", "cd_linear_overlap", "cd_first_nonzero_frame",
)
REQUIRED_PROTOCOLS = (
    "REPLAY_PROTOCOL_VERSION", "NATIVE_PROVIDERS_PROTOCOL_VERSION",
    "SOURCE_PROTOCOL_VERSION", "NATIVE_ENGINES_PROTOCOL_VERSION",
    "NATIVE_RENDER_PROTOCOL_VERSION", "CD_PCM_PROTOCOL_VERSION",
)


def artifact_provenance(path: Path) -> dict[str, object]:
    path = path.resolve()
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(128 * 1024):
            digest.update(chunk)
    return {"path": os.fspath(path), "bytes": path.stat().st_size,
            "sha256": digest.hexdigest()}


def load_builder():
    spec = importlib.util.spec_from_file_location("tts_native_build", ROOT / "scripts/build-tts-native.py")
    if spec is None or spec.loader is None:
        raise RuntimeError("build tooling unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def preflight() -> dict[str, object]:
    sys.path.insert(0, os.fspath(ROOT))
    from cogs.tts.fluxo_rust import _carregar_nucleo
    native = _carregar_nucleo()
    missing = [name for name in REQUIRED_EXPORTS if not callable(getattr(native, name, None))]
    incompatible = {name: getattr(native, name, None) for name in REQUIRED_PROTOCOLS
                    if getattr(native, name, None) != 1}
    if missing or incompatible:
        raise RuntimeError(f"incompatible native extension, missing={missing}, protocols={incompatible}")
    print("Native extension loaded: " + str(native.__file__), flush=True)
    return dict(artifact_provenance(Path(native.__file__)),
                protocols={name: getattr(native, name) for name in REQUIRED_PROTOCOLS})


def worker_binary(env: dict[str, str]) -> Path:
    configured = env.get("TTS_NATIVE_WORKER_TEST_BINARY")
    if configured:
        candidate = Path(configured).resolve()
    else:
        architecture = {"amd64": "x86_64", "arm64": "aarch64"}.get(platform.machine(), platform.machine())
        target = architecture + "-unknown-linux-gnu"
        target_root = Path(env.get("CARGO_TARGET_DIR", ROOT / "target"))
        if not target_root.is_absolute():
            target_root = ROOT / target_root
        candidates = (ROOT / "native-dist" / target / "tts-worker",
                      target_root / "release/tts-worker", target_root / target / "release/tts-worker",
                      target_root / "debug/tts-worker", target_root / target / "debug/tts-worker")
        candidate = next((path for path in candidates if path.is_file()), None)
        if candidate is None:
            raise RuntimeError("native tts-worker binary is required; run scripts/build-tts-native.py")
    if not candidate.is_file() or not os.access(candidate, os.X_OK):
        raise RuntimeError(f"native worker is missing or not executable: {candidate}")
    result = subprocess.run([os.fspath(candidate), "--check"], env=env, check=True,
                            text=True, capture_output=True, timeout=5)
    health = json.loads(result.stdout)
    if health.get("ok") is not True:
        raise RuntimeError(f"worker --check failed: {health}")
    return candidate.resolve()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python-only", action="store_true", help="skip cargo tests (extension remains mandatory)")
    parser.add_argument("--report", type=Path, default=ROOT / "native-dist/validation.json")
    args = parser.parse_args()
    builder = load_builder()
    report = {"schema": "tts-native-validation-v1", "passed": False, "completed": False,
              "started_utc": datetime.now(timezone.utc).isoformat(),
              "scope": "local-contracts-and-native-transport", "device_or_discord_validation": False,
              "commands": []}

    def save_report() -> None:
        builder.atomic_write(args.report, json.dumps(report, ensure_ascii=False, indent=2).encode() + b"\n")

    # Invalidate an older passing report even if native import/tool preflight fails.
    save_report()
    env = builder.environment()
    env["PYTHONPATH"] = os.pathsep.join((os.fspath(ROOT), os.fspath(ROOT / "tests"),
                                        os.fspath(ROOT / "vinculo_python/python"), env.get("PYTHONPATH", "")))
    # This run deliberately tests Python coordination separately from the new
    # replay implementation; structural legacy tests patch Python-only internals.
    env.update(TTS_REPLAY_RUST="0", TTS_POLICY_RUST="0", TTS_FLUXO_RUST="0",
               TTS_BACKEND="python", TTS_NATIVE_REQUIRED="1")
    if shutil.which("ffmpeg", path=env.get("PATH")) is None:
        raise RuntimeError("FFmpeg is required for the native PCM integration gate")
    if not args.python_only and not any(shutil.which(name, path=env.get("PATH")) for name in ("c++", "g++", "clang++")):
        raise RuntimeError("a C++ compiler is required for the real native engine ABI fixture")
    report["extension"] = preflight()
    env["TTS_NATIVE_WORKER_TEST_BINARY"] = os.fspath(worker_binary(env))
    report["worker"] = artifact_provenance(Path(env["TTS_NATIVE_WORKER_TEST_BINARY"]))
    save_report()
    commands: list[tuple[str, list[str], dict[str, str]]] = []
    if not args.python_only:
        commands.append(("rust-workspace", ["cargo", "test", "--locked", "--workspace",
                         "--exclude", "vinculo_fluxo_tts"], env))
    commands.extend((str(path.relative_to(ROOT)), [sys.executable, "-m", "pytest", "-q", os.fspath(path)], env)
                    for path in (ROOT / "tests/test_tts_runtime.py", ROOT / "tests/test_tts_prepared_streaming.py",
                                 ROOT / "tests/test_tts_local_benchmark.py", ROOT / "tests/test_tts_fluxo_rust.py"))
    native_env = dict(env, TTS_REPLAY_RUST="1")
    commands.append(("native-replay", [sys.executable, "-m", "pytest", "-q", "tests/test_tts_replay_rust.py"], native_env))
    source_env = dict(native_env, TTS_SOURCE_RUST="1")
    commands.append(("native-source", [sys.executable, "-m", "pytest", "-q", "tests/test_tts_source_rust.py"], source_env))
    commands.append(("music-native-tts", [sys.executable, "-m", "pytest", "-q", "tests/test_music_agent_native_tts.py"], env))
    commands.append(("cd-native-pcm", [sys.executable, "-m", "pytest", "-q", "tests/test_cd_pcm_rust.py"], env))
    native_files = sorted(set(ROOT.glob("tests/test_tts_*_rust_native.py"))
                          | set(ROOT.glob("tests/test_tts_native_*.py")))
    for path in native_files:
        commands.append((str(path.relative_to(ROOT)), [sys.executable, "-m", "pytest", "-q", os.fspath(path)], env))
    commands.append(("binding-integration", [sys.executable, "-m", "unittest", "discover", "-s",
                                             "vinculo_python/testes_python", "-p", "teste_*.py", "-v"], env))
    results = []
    for name, command, process_env in commands:
        print("+ " + " ".join(command), flush=True)
        result = subprocess.run(command, cwd=ROOT, env=process_env, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        print(result.stdout, end="", flush=True)
        # A skipped test is visible evidence, never proof of integration.
        mandatory = (name in {"native-replay", "native-source", "music-native-tts", "cd-native-pcm", "binding-integration"}
                     or "test_tts_native_" in name or "_rust_native.py" in name)
        skipped = bool(re.search(r"\b[1-9]\d* skipped\b|\bskipped=[1-9]\d*\b", result.stdout))
        results.append({"name": name, "returncode": result.returncode,
                        "mandatory_native": mandatory, "unexpected_native_skip": mandatory and skipped,
                        "output": result.stdout[-100_000:]})
    changed_artifacts = []
    for name in ("extension", "worker"):
        artifact = report[name]
        if artifact is not None:
            try:
                unchanged = artifact_provenance(Path(artifact["path"]))["sha256"] == artifact["sha256"]
            except OSError:
                unchanged = False
            if not unchanged:
                changed_artifacts.append(name)
    report.update(passed=not changed_artifacts and all(r["returncode"] == 0 and not r["unexpected_native_skip"] for r in results),
                  artifacts_unchanged=not changed_artifacts, changed_artifacts=changed_artifacts,
                  completed=True, finished_utc=datetime.now(timezone.utc).isoformat(), commands=results)
    save_report()
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
