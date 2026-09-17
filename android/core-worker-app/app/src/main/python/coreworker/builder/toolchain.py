"""Toolchain validation, environment and smoke execution for the APK self-builder."""
from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Callable


def _safe_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def resolve_toolchain(
    toolchain_dir: Path,
    *,
    safe_json_load: Callable[[Path], dict[str, Any]],
    safe_rel: Callable[[Any, str], str],
    toolchain_schema_v1: str,
    toolchain_schema_v2: str,
) -> dict[str, Any]:
    manifest_path = toolchain_dir / "manifest.json"
    manifest = safe_json_load(manifest_path)
    schema = str(manifest.get("schema") or "").strip()
    arch = str(manifest.get("arch") or "").strip().lower()
    manifest_version = _safe_int(manifest.get("version"))
    runtime_libraries = manifest.get("runtimeLibraries") if isinstance(manifest.get("runtimeLibraries"), dict) else {}
    gradle_launcher = manifest.get("gradleLauncher") if isinstance(manifest.get("gradleLauncher"), dict) else {}
    bootstrap_smoke = manifest.get("bootstrapSmoke") if isinstance(manifest.get("bootstrapSmoke"), dict) else {}
    validation = manifest.get("validation") if isinstance(manifest.get("validation"), dict) else {}
    versions = manifest.get("versions") if isinstance(manifest.get("versions"), dict) else {}
    raw_executables = manifest.get("executablePaths") if isinstance(manifest.get("executablePaths"), list) else []

    paths = manifest.get("paths") if isinstance(manifest.get("paths"), dict) else {}
    jdk_rel = safe_rel(paths.get("jdk") or "jdk", "")
    gradle_rel = safe_rel(paths.get("gradle") or "gradle/bin/gradle", "")
    sdk_rel = safe_rel(paths.get("androidSdk") or paths.get("android_sdk") or "android-sdk", "")
    aapt2_rel = safe_rel(paths.get("aapt2") or "bin/aapt2", "")

    jdk = toolchain_dir / jdk_rel
    java = jdk / "bin/java"
    javac = jdk / "bin/javac"
    jar = jdk / "bin/jar"
    gradle = toolchain_dir / gradle_rel
    sdk = toolchain_dir / sdk_rel
    aapt2 = toolchain_dir / aapt2_rel
    android_jar = sdk / "platforms/android-34/android.jar"
    try:
        executable_paths = {safe_rel(item, "") for item in raw_executables}
    except Exception:
        executable_paths = set()
    mandatory_executables = {
        f"{jdk_rel}/bin/java",
        f"{jdk_rel}/bin/javac",
        f"{jdk_rel}/bin/jar",
        gradle_rel,
        aapt2_rel,
    }
    jspawn_rel = f"{jdk_rel}/lib/jspawnhelper"
    declared_executables_valid = (
        bool(raw_executables)
        and len(raw_executables) == len(executable_paths)
        and mandatory_executables.issubset(executable_paths)
        and all((toolchain_dir / item).is_file() for item in executable_paths)
        and (not (toolchain_dir / jspawn_rel).is_file() or jspawn_rel in executable_paths)
    )
    smoke_checks = bootstrap_smoke.get("checks") if isinstance(bootstrap_smoke.get("checks"), list) else []
    smoke_by_name = {
        str(item.get("name") or ""): item
        for item in smoke_checks
        if isinstance(item, dict) and item.get("name")
    }
    bootstrap_smoke_valid = bootstrap_smoke.get("ok") is True
    for name in ("java", "javac", "jar", "gradle", "aapt2"):
        check = smoke_by_name.get(name) or {}
        try:
            returncode_ok = check.get("returncode") is not None and int(check.get("returncode")) == 0
        except (TypeError, ValueError):
            returncode_ok = False
        bootstrap_smoke_valid = bootstrap_smoke_valid and check.get("ok") is True and returncode_ok

    legacy_v1 = schema == toolchain_schema_v1
    external_v2 = schema == toolchain_schema_v2
    required_smoke_raw = validation.get("requiredSmokeChecks")
    required_smoke_valid = isinstance(required_smoke_raw, list) and all(isinstance(item, str) for item in required_smoke_raw)
    required_smoke = set(required_smoke_raw) if required_smoke_valid else set()
    exact_v2_versions = (
        _safe_int(versions.get("jdkMajor")) == 17
        and str(versions.get("gradle") or "") == "8.9"
        and str(versions.get("agp") or "") == "8.7.3"
        and _safe_int(versions.get("compileSdk")) == 34
        and str(versions.get("buildTools") or "") == "34.0.0"
        and str(versions.get("chaquopy") or "") == "17.0.0"
    )
    checks = {
        "manifest": manifest_path.is_file(),
        "schema": legacy_v1 or external_v2,
        "manifestVersion": (legacy_v1 and manifest_version >= 7) or (external_v2 and manifest_version >= 2),
        "versions": (not external_v2) or exact_v2_versions,
        "executablePaths": declared_executables_valid,
        "runtimeLibraries": runtime_libraries.get("strategy") == "dt-needed-transitive-v1",
        "gradleLauncher": (not legacy_v1) or gradle_launcher.get("strategy") == "android-sh-resolved-app-home-jvm-opts-v2",
        "validation": validation.get("strategy") == "required-executable-smoke-v2"
            and ((not external_v2) or (required_smoke_valid and required_smoke == {"java", "javac", "jar", "gradle", "aapt2"})),
        "bootstrapSmoke": bootstrap_smoke_valid,
        "arch": arch in {"aarch64", "arm64", "arm64-v8a"},
        "java": java.is_file() and java.stat().st_size > 0 and os.access(java, os.X_OK),
        "javac": javac.is_file() and javac.stat().st_size > 0 and os.access(javac, os.X_OK),
        "jar": jar.is_file() and jar.stat().st_size > 0 and os.access(jar, os.X_OK),
        "gradle": gradle.is_file() and gradle.stat().st_size > 0 and os.access(gradle, os.X_OK),
        "androidSdk": sdk.is_dir(),
        "androidJar34": android_jar.is_file() and android_jar.stat().st_size > 1024 * 1024,
        "aapt2": aapt2.is_file() and aapt2.stat().st_size > 0 and os.access(aapt2, os.X_OK),
        "jspawnhelper": not (toolchain_dir / jspawn_rel).is_file() or os.access(toolchain_dir / jspawn_rel, os.X_OK),
    }
    missing = [key for key, ok in checks.items() if not ok]
    return {
        "ok": not missing,
        "schema": schema,
        "arch": arch,
        "manifest": str(manifest_path),
        "checks": checks,
        "missing": missing,
        "paths": {
            "toolchain": str(toolchain_dir),
            "jdk": str(jdk),
            "java": str(java),
            "javac": str(javac),
            "jar": str(jar),
            "gradle": str(gradle),
            "androidSdk": str(sdk),
            "aapt2": str(aapt2),
            "androidJar34": str(android_jar),
        },
        "manifestData": manifest,
    }


def toolchain_fingerprint(tool: dict[str, Any], *, sha256_file: Callable[[Path], str]) -> str:
    """Lightweight fingerprint which invalidates stale smoke results."""
    import hashlib

    candidates = [
        Path(str(tool.get("manifest") or "")),
        Path(str((tool.get("paths") or {}).get("java") or "")),
        Path(str((tool.get("paths") or {}).get("javac") or "")),
        Path(str((tool.get("paths") or {}).get("jar") or "")),
        Path(str((tool.get("paths") or {}).get("gradle") or "")),
        Path(str((tool.get("paths") or {}).get("androidJar34") or "")),
        Path(str((tool.get("paths") or {}).get("aapt2") or "")),
    ]
    digest = hashlib.sha256()
    for path in candidates:
        try:
            stat = path.stat()
            digest.update(str(path).encode("utf-8", errors="replace"))
            digest.update(f"\0{stat.st_size}\0{stat.st_mtime_ns}\n".encode("ascii"))
            if path.name == "manifest.json" and stat.st_size <= 1024 * 1024:
                digest.update(sha256_file(path).encode("ascii"))
        except Exception:
            digest.update((str(path) + "\0missing\n").encode("utf-8", errors="replace"))
    return digest.hexdigest()


def toolchain_environment(
    tool: dict[str, Any],
    *,
    home: Path,
    temp: Path,
    gradle_home: Path,
    clean: bool,
) -> dict[str, str]:
    paths = tool["paths"]
    jdk = Path(paths["jdk"])
    sdk = Path(paths["androidSdk"])
    toolchain = Path(paths["toolchain"])
    runtime_libs = toolchain / "runtime-libs"
    library_paths = [runtime_libs, jdk / "lib", jdk / "lib/server", jdk / "lib/jli"]
    env = {} if clean else os.environ.copy()
    existing_library_path = "" if clean else str(env.get("LD_LIBRARY_PATH") or "").strip()
    resolved_library_paths = [str(path) for path in library_paths if path.is_dir()]
    if existing_library_path:
        resolved_library_paths.append(existing_library_path)
    env.update({
        "HOME": str(home),
        "TMPDIR": str(temp),
        "GRADLE_USER_HOME": str(gradle_home),
        "JAVA_HOME": str(jdk),
        "ANDROID_HOME": str(sdk),
        "ANDROID_SDK_ROOT": str(sdk),
        "PATH": os.pathsep.join((
            str(jdk / "bin"),
            str(sdk / "platform-tools"),
            str(sdk / "cmdline-tools/latest/bin"),
            "/system/bin",
            "/system/xbin",
        )),
        "LD_LIBRARY_PATH": os.pathsep.join(resolved_library_paths),
        "LANG": "C",
        "LC_ALL": "C",
    })
    return env


def run_smoke_command(
    name: str,
    command: list[str],
    env: dict[str, str],
    timeout: int,
    *,
    short: Callable[[Any, int], str],
) -> dict[str, Any]:
    started = time.time()
    try:
        completed = subprocess.run(
            command,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
            timeout=timeout,
            check=False,
        )
        output = short(completed.stdout, 6000)
        return {
            "name": name,
            "ok": completed.returncode == 0,
            "returncode": int(completed.returncode),
            "durationMs": int((time.time() - started) * 1000),
            "output": output,
        }
    except subprocess.TimeoutExpired as exc:
        output = exc.stdout.decode("utf-8", errors="replace") if isinstance(exc.stdout, bytes) else str(exc.stdout or "")
        return {
            "name": name,
            "ok": False,
            "returncode": 124,
            "durationMs": int((time.time() - started) * 1000),
            "output": short(output, 6000),
            "error": f"timeout após {timeout}s",
        }
    except Exception as exc:
        return {
            "name": name,
            "ok": False,
            "returncode": -1,
            "durationMs": int((time.time() - started) * 1000),
            "output": "",
            "error": f"{type(exc).__name__}: {short(exc, 600)}",
        }


def toolchain_smoke(
    files: Path,
    tool: dict[str, Any],
    *,
    force: bool,
    fingerprint: Callable[[dict[str, Any]], str],
    safe_json_load: Callable[[Path], dict[str, Any]],
    atomic_json: Callable[[Path, dict[str, Any]], None],
    environment: Callable[..., dict[str, str]],
    run_command: Callable[[str, list[str], dict[str, str], int], dict[str, Any]],
    now_ms: Callable[[], int],
) -> dict[str, Any]:
    builder = files / "apk-self-builder"
    state_path = builder / "toolchain-smoke.json"
    current_fingerprint = fingerprint(tool)
    cached = safe_json_load(state_path)
    if (
        not force
        and cached.get("fingerprint") == current_fingerprint
        and cached.get("schema") == "core-worker-apk-self-builder-smoke-v3"
    ):
        return cached

    runtime = builder / "runtime/smoke"
    home = runtime / "home"
    temp = runtime / "tmp"
    gradle_home = runtime / "gradle-home"
    shutil.rmtree(runtime, ignore_errors=True)
    for path in (home, temp, gradle_home):
        path.mkdir(parents=True, exist_ok=True)
    env = environment(tool, home=home, temp=temp, gradle_home=gradle_home, clean=True)
    paths = tool["paths"]
    commands = [
        ("java", [paths["java"], "-version"], 45),
        ("javac", [paths["javac"], "-version"], 45),
        ("jar", [paths["jar"], "--version"], 45),
        ("gradle", ["/system/bin/sh", paths["gradle"], "--version", "--no-daemon"], 90),
        ("aapt2", [paths["aapt2"], "version"], 45),
    ]
    checks: list[dict[str, Any]] = []
    try:
        for name, command, timeout in commands:
            result = run_command(name, command, env, timeout)
            checks.append(result)
            if not result.get("ok"):
                break
    finally:
        shutil.rmtree(runtime, ignore_errors=True)
    ok = len(checks) == len(commands) and all(bool(item.get("ok")) for item in checks)
    result = {
        "schema": "core-worker-apk-self-builder-smoke-v3",
        "ok": ok,
        "state": "toolchain_smoke_ok" if ok else "toolchain_smoke_failed",
        "summary": "Java, Javac, Jar, Gradle e aapt2 executaram no APK" if ok else "toolchain não executou no ambiente privado do APK",
        "fingerprint": current_fingerprint,
        "checks": checks,
        "updatedAt": now_ms(),
    }
    atomic_json(state_path, result)
    return result
