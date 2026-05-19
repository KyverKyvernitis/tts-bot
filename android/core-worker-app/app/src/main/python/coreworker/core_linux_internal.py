import json
import os
import platform
import time
from pathlib import Path
from .safe_json import load_context, ok_response, error_response, safe_path, dir_size, clean_text

STATE_VERSION = 1


def _write_json(path, payload):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _read_json(path):
    try:
        p = Path(path)
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        pass
    return {}


def _write_text(path, value):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(str(value or ""), encoding="utf-8")


def _exists(path):
    try:
        return Path(path).exists()
    except Exception:
        return False


def _nonempty_dir(path):
    try:
        p = Path(path)
        return p.is_dir() and any(p.iterdir())
    except Exception:
        return False


def _stat_size(path):
    try:
        p = Path(path)
        return p.stat().st_size if p.exists() and p.is_file() else 0
    except Exception:
        return 0


def _required_layout(core_linux_dir):
    return {
        "rootfs": core_linux_dir / "rootfs",
        "bin": core_linux_dir / "bin",
        "runtime": core_linux_dir / "runtime",
        "downloads": core_linux_dir / "downloads",
        "staging": core_linux_dir / "staging",
        "logs": core_linux_dir / "logs",
        "bedrock": core_linux_dir / "bedrock",
        "manifests": core_linux_dir / "manifests",
        "provision": core_linux_dir / "provision",
        "scripts": core_linux_dir / "scripts",
    }


def _ensure_layout(core_linux_dir):
    layout = _required_layout(core_linux_dir)
    for path in layout.values():
        path.mkdir(parents=True, exist_ok=True)
    return layout


def _embedded_candidates(native_lib_dir):
    native_lib_dir = Path(str(native_lib_dir or ""))
    candidates = {
        "executor": [
            native_lib_dir / "libcoreworker_executor.so",
            native_lib_dir / "libcoreworker_proot.so",
            native_lib_dir / "libcoreworker_busybox.so",
        ],
        "proot": [
            native_lib_dir / "libcoreworker_proot.so",
            native_lib_dir / "libproot.so",
        ],
        "box64": [
            native_lib_dir / "libcoreworker_box64.so",
            native_lib_dir / "libbox64.so",
        ],
        "busybox": [
            native_lib_dir / "libcoreworker_busybox.so",
            native_lib_dir / "libbusybox.so",
        ],
    }
    out = {}
    for key, values in candidates.items():
        found = next((p for p in values if _exists(p)), None)
        out[key] = {
            "present": found is not None,
            "path": safe_path(found) if found is not None else "",
            "size": _stat_size(found) if found is not None else 0,
            "expectedNames": [p.name for p in values],
        }
    return out


def _rootfs_markers(rootfs):
    rootfs = Path(rootfs)
    return {
        "dir": rootfs.exists(),
        "nonEmpty": _nonempty_dir(rootfs),
        "readyMarker": (rootfs / ".core-worker-rootfs-ready").exists(),
        "etcOsRelease": (rootfs / "etc" / "os-release").exists(),
        "binSh": (rootfs / "bin" / "sh").exists(),
        "usrBinEnv": (rootfs / "usr" / "bin" / "env").exists(),
    }


def _bedrock_markers(bedrock):
    bedrock = Path(bedrock)
    eula_text = ""
    try:
        eula_text = (bedrock / "eula.txt").read_text(encoding="utf-8", errors="replace").lower().replace(" ", "")
    except Exception:
        pass
    return {
        "dir": bedrock.exists(),
        "server": (bedrock / "bedrock_server").exists(),
        "serverProperties": (bedrock / "server.properties").exists(),
        "eulaAccepted": "eula=true" in eula_text,
        "worldsDir": (bedrock / "worlds").exists(),
        "logsDir": (bedrock / "logs").exists(),
    }


def _manifest_payload(ctx, layout, embedded, blockers, now):
    abi = ctx.get("primaryAbi") or ""
    supported_abis = ctx.get("supportedAbis") if isinstance(ctx.get("supportedAbis"), list) else []
    if not supported_abis and abi:
        supported_abis = [abi]
    return {
        "schema": "core-worker-linux-internal-manifest-v1",
        "updatedAt": now,
        "target": "core-linux-internal-no-termux",
        "primaryAbi": abi,
        "supportedAbis": supported_abis,
        "androidSdk": ctx.get("androidSdk"),
        "targetSdk": ctx.get("targetSdk"),
        "downloadPolicy": "local-explicit-confirmation-only",
        "executionPolicy": "no-free-shell-no-remote-arbitrary-command",
        "androidRestriction": {
            "appHomeWritableExecBlocked": True,
            "note": "Android 10+ bloqueia execve direto de binários criados no diretório gravável do app; binários executáveis precisam ser embutidos/assinados de forma controlada no APK.",
        },
        "expectedEmbeddedBinaries": {
            "executor": embedded.get("executor", {}),
            "proot": embedded.get("proot", {}),
            "box64": embedded.get("box64", {}),
            "busybox": embedded.get("busybox", {}),
        },
        "expectedRootfs": {
            "family": "Ubuntu/Debian userland",
            "minimumTarget": "Ubuntu 22.04 LTS compatible for Bedrock Dedicated Server",
            "path": safe_path(layout["rootfs"]),
            "installMode": "staged-rootfs-after-local-confirmation",
        },
        "expectedBedrock": {
            "officialBundledInApk": False,
            "path": safe_path(layout["bedrock"]),
            "downloadMode": "manual-assisted-from-official-source",
            "eulaMode": "local-explicit-confirmation-only",
        },
        "blockers": blockers,
    }


def _summarize(blockers, action):
    if not blockers:
        if action in {"bootstrap", "prepare", "repair"}:
            return "Core Linux interno preparado para start sem Termux"
        return "Core Linux interno pronto para preflight avançado"
    if action in {"bootstrap", "prepare", "repair"}:
        return "Core Linux interno preparado · pendente: " + "; ".join(blockers[:3])
    return "Core Linux interno bloqueado · " + "; ".join(blockers[:3])


def run(context_json=None):
    try:
        ctx = load_context(context_json)
        action = clean_text(ctx.get("action") or ctx.get("focus") or "probe", 80)
        core_linux_dir = Path(str(ctx.get("coreLinuxDir") or "core-linux"))
        native_lib_dir = Path(str(ctx.get("nativeLibDir") or ""))
        layout = _ensure_layout(core_linux_dir)
        now = int(time.time() * 1000)

        embedded = _embedded_candidates(native_lib_dir)
        native_executor = ctx.get("nativeExecutor") if isinstance(ctx.get("nativeExecutor"), dict) else {}
        rootfs = _rootfs_markers(layout["rootfs"])
        bedrock = _bedrock_markers(layout["bedrock"])

        executor_embedded = bool(
            embedded.get("executor", {}).get("present")
            or embedded.get("proot", {}).get("present")
            or embedded.get("busybox", {}).get("present")
            or native_executor.get("embeddedExecutorPresent")
        )
        executor_test = native_executor.get("test") if isinstance(native_executor.get("test"), dict) else {}
        executor_test_attempted = bool(executor_test.get("attempted"))
        executor_test_ok = bool(executor_test.get("ok"))
        executor_ready = bool(executor_embedded and (not executor_test_attempted or executor_test_ok))
        box64_embedded = bool(embedded["box64"]["present"] or native_executor.get("embeddedBox64Present"))
        rootfs_ready = bool(rootfs["readyMarker"] and rootfs["binSh"])
        bedrock_ready = bool(bedrock["server"] and bedrock["serverProperties"] and bedrock["eulaAccepted"])

        blockers = []
        warnings = []
        if not executor_embedded:
            blockers.append("executor nativo interno embutido pendente")
        elif executor_test_attempted and not executor_test_ok:
            blockers.append("executor nativo interno falhou no teste")
        if not rootfs_ready:
            blockers.append("rootfs interno pendente")
        if not box64_embedded:
            blockers.append("Box64 interno embutido pendente")
        if not bedrock["server"]:
            blockers.append("bedrock_server não instalado")
        if bedrock["server"] and not bedrock["eulaAccepted"]:
            blockers.append("EULA pendente")

        if bool(ctx.get("termuxInstalled")):
            warnings.append("Termux detectado apenas como fallback legado; não é caminho principal do Core Linux interno")
        if int(ctx.get("androidSdk") or 0) >= 29:
            warnings.append("execução de binários baixados no app home é bloqueada por Android 10+; usar binários embutidos/controlados")

        manifest = _manifest_payload(ctx, layout, embedded, blockers, now)
        executor_state = {
            "ok": executor_ready,
            "state": "ready_for_rootfs" if executor_ready else ("test_failed" if executor_test_attempted else "blocked"),
            "mode": "embedded-native-executor",
            "embedded": embedded,
            "nativeExecutor": native_executor,
            "nativeLibDir": safe_path(native_lib_dir),
            "testAttempted": executor_test_attempted,
            "testOk": executor_test_ok,
            "readyForRootfs": executor_ready,
            "blockers": [] if executor_ready else (["executor nativo interno falhou no teste"] if executor_test_attempted else ["executor nativo interno embutido pendente"]),
            "updatedAt": now,
            "safety": "não executa shell livre e não aceita comando arbitrário da VPS",
        }
        rootfs_state = {
            "ok": rootfs_ready,
            "state": "ready" if rootfs_ready else "pending",
            "markers": rootfs,
            "path": safe_path(layout["rootfs"]),
            "blockers": [] if rootfs_ready else ["rootfs interno pendente"],
            "updatedAt": now,
        }
        box64_state = {
            "ok": box64_embedded,
            "state": "ready" if box64_embedded else "pending",
            "embedded": embedded["box64"],
            "blockers": [] if box64_embedded else ["Box64 interno embutido pendente"],
            "updatedAt": now,
        }
        preflight = {
            "ok": not blockers,
            "state": "ready" if not blockers else "blocked",
            "executorReady": executor_ready,
            "rootfsReady": rootfs_ready,
            "box64Ready": box64_embedded,
            "bedrockReady": bedrock_ready,
            "blockers": blockers,
            "warnings": warnings,
            "updatedAt": now,
        }
        state = {
            "ok": not blockers if action in {"preflight", "bedrock_preflight"} else True,
            "state": "ready" if not blockers else "blocked",
            "action": action,
            "prepared": True,
            "layoutReady": True,
            "coreLinuxDir": safe_path(core_linux_dir),
            "nativeLibDir": safe_path(native_lib_dir),
            "manifest": manifest,
            "executor": executor_state,
            "rootfs": rootfs_state,
            "box64": box64_state,
            "bedrock": bedrock,
            "preflight": preflight,
            "blockers": blockers,
            "warnings": warnings,
            "summary": _summarize(blockers, action),
            "updatedAt": now,
        }

        _write_json(layout["runtime"] / "core-linux-internal-state.json", state)
        _write_json(layout["runtime"] / "executor-state.json", executor_state)
        _write_json(layout["runtime"] / "native-runtime-state.json", native_executor if native_executor else executor_state)
        _write_json(layout["runtime"] / "rootfs-state.json", rootfs_state)
        _write_json(layout["runtime"] / "box64-state.json", box64_state)
        _write_json(layout["runtime"] / "bedrock-internal-preflight.json", preflight)
        _write_json(layout["manifests"] / "core-linux-internal-manifest.json", manifest)

        if action in {"bootstrap", "prepare", "repair"}:
            _write_text(layout["scripts"] / "core-linux-internal.NOT_EXECUTABLE.txt",
                        "Plano interno: executor/rootfs/box64 precisam ser provisionados por arquivos controlados. "
                        "Não execute binários baixados diretamente do app home.\n")
            _write_text(layout["logs"] / "core-linux-internal.log",
                        f"[{now}] {state['summary']}\n")

        ok = True if action in {"probe", "bootstrap", "prepare", "repair", "manifest", "executor", "rootfs", "box64"} else not blockers
        return ok_response(
            "core_linux_internal",
            state["summary"],
            ok=ok,
            state=state["state"],
            action=action,
            prepared=True,
            coreLinuxInternal=state,
            executor=executor_state,
            nativeExecutor=native_executor,
            rootfs=rootfs_state,
            box64=box64_state,
            preflight=preflight,
            blockers=blockers,
            warnings=warnings,
            manifestPath=safe_path(layout["manifests"] / "core-linux-internal-manifest.json"),
            size=dir_size(core_linux_dir, max_files=900),
            safety="bootstrap interno sem Termux; não baixa, não executa binário externo e não abre shell livre",
        )
    except Exception as exc:
        return error_response("core_linux_internal", exc)
