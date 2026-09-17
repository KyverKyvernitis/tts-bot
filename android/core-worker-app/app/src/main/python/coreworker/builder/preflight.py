"""Readiness and resource preflight for the APK self-builder."""
from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path
from typing import Any, Callable


def public_preflight(
    files_dir: str,
    native_dir: str,
    run_smoke: bool = False,
    *,
    resolve_toolchain: Callable[[Path], dict[str, Any]],
    toolchain_smoke: Callable[[Path, dict[str, Any]], dict[str, Any]],
    safe_json_load: Callable[[Path], dict[str, Any]],
    is_inside: Callable[[Path, Path], bool],
    atomic_json: Callable[[Path, dict[str, Any]], None],
    now_ms: Callable[[], int],
    schema: str,
) -> str:
    del native_dir
    files = Path(files_dir)
    builder = files / "apk-self-builder"
    toolchain = builder / "toolchain"
    tool = resolve_toolchain(toolchain)
    checks = {
        "toolchain": bool(tool.get("ok")),
        "systemShell": Path("/system/bin/sh").is_file(),
    }
    basic_missing = [key for key, ok in checks.items() if not ok]
    smoke = {
        "ok": False,
        "state": "toolchain_smoke_blocked" if basic_missing else "toolchain_smoke_pending",
        "summary": "smoke bloqueado por preflight básico" if basic_missing else "smoke real ainda não executado",
        "checks": [],
    }
    if not basic_missing:
        smoke = toolchain_smoke(files, tool, force=bool(run_smoke))
    checks["toolchainSmoke"] = bool(smoke.get("ok"))
    missing = list(basic_missing)
    if not smoke.get("ok") and "toolchainSmoke" not in missing:
        missing.append("toolchainSmoke")

    latest = safe_json_load(builder / "artifacts/latest-artifact.json")
    latest_path = Path(str(latest.get("artifact_path") or "")) if latest else Path()
    publish_ready = bool(
        latest
        and latest_path.is_file()
        and is_inside(latest_path, builder)
        and latest_path.stat().st_size > 1024 * 1024
    )
    ready = not missing
    out = {
        "ok": ready,
        "ready": ready,
        "publishReady": publish_ready,
        "schema": schema,
        "runtime": "android-private-toolchain-direct",
        "state": "apk_self_builder_ready" if ready else "apk_self_builder_blocked",
        "summary": "Autobuild do APK pronto e executável" if ready else "Autobuild do APK aguardando: " + ", ".join(missing),
        "checks": checks,
        "missing": missing,
        "toolchain": tool,
        "smoke": smoke,
        "paths": {"builder": str(builder), "toolchain": str(toolchain)},
        "latestArtifact": {
            "available": publish_ready,
            "filename": latest.get("filename", "") if latest else "",
            "versionName": latest.get("versionName", "") if latest else "",
            "versionCode": latest.get("versionCode", 0) if latest else 0,
        },
        "updatedAt": now_ms(),
    }
    atomic_json(builder / "state.json", out)
    return json.dumps(out, ensure_ascii=False, separators=(",", ":"))


def read_meminfo_bytes() -> dict[str, int]:
    result: dict[str, int] = {}
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8", errors="replace").splitlines():
            if ":" not in line:
                continue
            key, rest = line.split(":", 1)
            match = re.search(r"(\d+)", rest)
            if match:
                result[key] = int(match.group(1)) * 1024
    except Exception:
        pass
    return result


def tree_bytes(root: Path, *, entry_limit: int = 80_000) -> int:
    total = 0
    count = 0
    try:
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            count += 1
            if count > entry_limit:
                break
            try:
                total += int(path.stat().st_size)
            except OSError:
                continue
    except Exception:
        pass
    return total


def active_heavy_build_processes(*, short: Callable[[Any, int], str]) -> list[dict[str, Any]]:
    current = os.getpid()
    found: list[dict[str, Any]] = []
    proc = Path("/proc")
    if not proc.is_dir():
        return found
    needles = ("org.gradle.launcher", "gradledaemon", "gradleworker", "aapt2")
    for item in proc.iterdir():
        if not item.name.isdigit() or int(item.name) == current:
            continue
        try:
            raw = (item / "cmdline").read_bytes().replace(b"\x00", b" ")
            text = raw.decode("utf-8", errors="replace").strip()
        except Exception:
            continue
        lower = text.lower()
        if text and any(needle in lower for needle in needles):
            found.append({"pid": int(item.name), "cmd": short(text, 220)})
            if len(found) >= 8:
                break
    return found


def effective_gradle_heap_mb(available_bytes: int) -> int:
    available_mb = max(0, int(available_bytes // (1024 * 1024)))
    if available_mb <= 0:
        return 512
    usable = max(256, available_mb - max(384, int(available_mb * 0.18)))
    return max(384, min(1280, int(usable * 0.55)))


def resource_preflight(
    files: Path,
    project: Path | None,
    payload: dict[str, Any],
    tool: dict[str, Any],
    *,
    read_meminfo: Callable[[], dict[str, int]],
    tree_size: Callable[..., int],
    active_processes: Callable[[], list[dict[str, Any]]],
    heap_mb: Callable[[int], int],
    max_source_expanded_bytes: int,
    min_build_battery_percent: int,
) -> dict[str, Any]:
    mem = read_meminfo()
    total = int(mem.get("MemTotal") or 0)
    available = int(mem.get("MemAvailable") or mem.get("MemFree") or 0)
    xmx_mb = heap_mb(available)
    metaspace_mb = max(192, min(384, xmx_mb // 3))
    builder = files / "apk-self-builder"
    free = int(shutil.disk_usage(builder if builder.exists() else files).free)
    source_compressed = int(payload.get("source_bytes") or payload.get("sourceBytes") or 0)
    project_bytes = tree_size(project) if project is not None and project.is_dir() else 0
    toolchain = Path(str((tool.get("paths") or {}).get("toolchain") or ""))
    toolchain_bytes = tree_size(toolchain, entry_limit=60_000) if toolchain.is_dir() else 0
    source_estimate = project_bytes or min(max_source_expanded_bytes, max(source_compressed * 4, source_compressed))
    required_temp = max(1024 * 1024 * 1024, source_compressed + source_estimate * 2 + 512 * 1024 * 1024)

    supplied = payload.get("builderResources") if isinstance(payload.get("builderResources"), dict) else {}
    battery_percent = int(supplied.get("batteryPercent", -1) or -1)
    charging = bool(supplied.get("charging", False))
    try:
        temperature_c = float(supplied.get("temperatureC", -1.0))
    except Exception:
        temperature_c = -1.0
    try:
        thermal_status = int(supplied.get("thermalStatus", -1))
    except Exception:
        thermal_status = -1
    heavy = active_processes()

    blockers: list[str] = []
    if available and available < (xmx_mb + 256) * 1024 * 1024:
        blockers.append("memory_low")
    if free < required_temp:
        blockers.append("storage_low")
    if battery_percent >= 0 and battery_percent < min_build_battery_percent and not charging:
        blockers.append("battery_low")
    if temperature_c >= 45.0:
        blockers.append("temperature_high")
    if thermal_status >= 3:
        blockers.append("thermal_severe")
    if heavy:
        blockers.append("builder_busy")

    return {
        "ok": not blockers,
        "state": "ready" if not blockers else "preflight_blocked",
        "blockers": blockers,
        "memoryTotalBytes": total,
        "memoryAvailableBytes": available,
        "storageFreeBytes": free,
        "sourceCompressedBytes": source_compressed,
        "sourceEstimatedExpandedBytes": source_estimate,
        "projectTreeBytes": project_bytes,
        "toolchainBytes": toolchain_bytes,
        "estimatedRequiredTempBytes": required_temp,
        "batteryPercent": battery_percent,
        "charging": charging,
        "temperatureC": temperature_c,
        "thermalStatus": thermal_status,
        "concurrentBuildProcesses": heavy,
        "xmxMb": xmx_mb,
        "maxMetaspaceMb": metaspace_mb,
    }
