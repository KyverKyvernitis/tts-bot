import json
import os
import shutil
import time
from pathlib import Path
from .safe_json import load_context, ok_response, error_response, safe_path, dir_size, clean_text

SCHEMA = "core-worker-rootfs-state-v1"
MANIFEST_SCHEMA = "core-worker-rootfs-manifest-v1"
ROOTFS_KIND = "core-worker-rootfs-scaffold"
MIN_RECOMMENDED_FREE_BYTES = 512 * 1024 * 1024


def _now_ms():
    return int(time.time() * 1000)


def _write_json(path, payload):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


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


def _append_log(path, line):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as fh:
        fh.write(f"[{_now_ms()}] {clean_text(line, 1200)}\n")


def _free_bytes(path):
    try:
        return int(shutil.disk_usage(str(path)).free)
    except Exception:
        return 0


def _safe_remove_tree(path):
    p = Path(path)
    if p.exists():
        shutil.rmtree(str(p))


def _layout(core_linux_dir):
    core = Path(core_linux_dir)
    return {
        "core": core,
        "rootfs": core / "rootfs",
        "staging": core / "staging" / "rootfs-next",
        "stagingBase": core / "staging",
        "runtime": core / "runtime",
        "logs": core / "logs",
        "manifests": core / "manifests",
        "downloads": core / "downloads",
        "bedrock": core / "bedrock",
    }


def _ensure_base(layout):
    for key in ("core", "stagingBase", "runtime", "logs", "manifests", "downloads", "bedrock"):
        layout[key].mkdir(parents=True, exist_ok=True)


def _rootfs_manifest(rootfs, now, *, source="internal-scaffold"):
    return {
        "schema": MANIFEST_SCHEMA,
        "kind": ROOTFS_KIND,
        "version": 1,
        "name": "Core Linux internal rootfs scaffold",
        "source": source,
        "createdAt": now,
        "updatedAt": now,
        "arch": "aarch64",
        "distribution": "core-worker-scaffold",
        "distributionReady": False,
        "readyForBox64Install": True,
        "readyForBedrockStart": False,
        "path": safe_path(rootfs),
        "policy": {
            "noFreeShell": True,
            "noRemoteArbitraryCommand": True,
            "noAutoDownload": True,
            "noBedrockStart": True,
            "appSpecificStorage": True,
            "termuxFallbackOnly": True,
        },
        "layout": {
            "bin": True,
            "usr/bin": True,
            "etc": True,
            "tmp": True,
            "home/core": True,
            "var/log": True,
            "run": True,
            "opt/core-worker": True,
        },
        "notes": [
            "Scaffold validado para preparar a runtime interna; não é uma distro Ubuntu completa.",
            "Box64 e Bedrock ficam em etapas futuras; nenhum servidor é iniciado por este patch.",
        ],
    }


def _create_scaffold(rootfs, *, source="internal-scaffold"):
    now = _now_ms()
    dirs = [
        rootfs / "bin",
        rootfs / "usr" / "bin",
        rootfs / "etc",
        rootfs / "tmp",
        rootfs / "home" / "core",
        rootfs / "var" / "log",
        rootfs / "run",
        rootfs / "opt" / "core-worker",
    ]
    for d in dirs:
        d.mkdir(parents=True, exist_ok=True)
    manifest = _rootfs_manifest(rootfs, now, source=source)
    _write_json(rootfs / ".core-worker-rootfs-manifest.json", manifest)
    _write_json(rootfs / "opt" / "core-worker" / "rootfs-policy.json", manifest["policy"])
    _write_text(rootfs / "etc" / "os-release", "NAME=\"Core Worker Internal Rootfs Scaffold\"\nID=core-worker-rootfs\nVERSION_ID=\"0.1\"\nPRETTY_NAME=\"Core Worker Internal Rootfs Scaffold 0.1\"\n")
    _write_text(rootfs / "bin" / "sh", "Core Worker rootfs marker. This is not an executable Android shell.\n")
    _write_text(rootfs / "usr" / "bin" / "env", "Core Worker rootfs marker. This is not an executable Android binary.\n")
    _write_text(rootfs / "README.core-worker-rootfs.txt", "Rootfs interno assistido do Core Worker. Este scaffold existe para validar layout, estado e próxima etapa Box64. Não é shell livre e não inicia Bedrock.\n")
    _write_text(rootfs / ".core-worker-rootfs-ready", f"readyAt={now}\nkind={ROOTFS_KIND}\n")
    return manifest


def _looks_like_ours(rootfs):
    manifest = _read_json(Path(rootfs) / ".core-worker-rootfs-manifest.json")
    if manifest.get("kind") == ROOTFS_KIND:
        return True
    if (Path(rootfs) / ".core-worker-rootfs-ready").exists() and (Path(rootfs) / "README.core-worker-rootfs.txt").exists():
        return True
    return False


def _has_unknown_existing_rootfs(rootfs):
    p = Path(rootfs)
    if not p.exists():
        return False
    try:
        items = [x.name for x in p.iterdir()]
    except Exception:
        return True
    if not items:
        return False
    return not _looks_like_ours(p)


def _validate(rootfs):
    rootfs = Path(rootfs)
    manifest = _read_json(rootfs / ".core-worker-rootfs-manifest.json")
    required = {
        "rootfsDir": rootfs.exists() and rootfs.is_dir(),
        "readyMarker": (rootfs / ".core-worker-rootfs-ready").exists(),
        "manifest": bool(manifest),
        "manifestSchema": manifest.get("schema") == MANIFEST_SCHEMA,
        "manifestKind": manifest.get("kind") == ROOTFS_KIND,
        "etcOsRelease": (rootfs / "etc" / "os-release").exists(),
        "binDir": (rootfs / "bin").is_dir(),
        "binShMarker": (rootfs / "bin" / "sh").exists(),
        "usrBinDir": (rootfs / "usr" / "bin").is_dir(),
        "tmpDir": (rootfs / "tmp").is_dir(),
        "homeCoreDir": (rootfs / "home" / "core").is_dir(),
        "varLogDir": (rootfs / "var" / "log").is_dir(),
        "policy": (rootfs / "opt" / "core-worker" / "rootfs-policy.json").exists(),
    }
    missing = [k for k, ok in required.items() if not ok]
    valid = not missing
    return {
        "ok": valid,
        "rootfsReady": valid,
        "state": "rootfs_validated" if valid else "rootfs_validation_failed",
        "validationLevel": "scaffold",
        "distributionReady": False,
        "readyForBox64Install": valid,
        "readyForBedrockStart": False,
        "checks": required,
        "missing": missing,
        "manifest": manifest,
    }


def _write_state(layout, state):
    _write_json(layout["runtime"] / "rootfs-state.json", state)
    _write_json(layout["manifests"] / "rootfs-manifest.json", state.get("manifest", {}))


def _status(layout, action):
    rootfs = layout["rootfs"]
    validation = _validate(rootfs)
    previous = _read_json(layout["runtime"] / "rootfs-state.json")
    free = _free_bytes(layout["core"])
    state = dict(previous) if isinstance(previous, dict) and previous else {}
    state.update({
        "schema": SCHEMA,
        "ok": bool(validation["ok"]),
        "action": action,
        "state": validation["state"] if rootfs.exists() else "rootfs_missing",
        "rootfsReady": bool(validation["rootfsReady"]),
        "validationLevel": validation["validationLevel"],
        "distributionReady": False,
        "readyForBox64Install": bool(validation["readyForBox64Install"]),
        "readyForBedrockStart": False,
        "rootfsDir": safe_path(rootfs),
        "stagingDir": safe_path(layout["staging"]),
        "freeBytes": free,
        "storageOk": free == 0 or free >= MIN_RECOMMENDED_FREE_BYTES,
        "recommendedFreeBytes": MIN_RECOMMENDED_FREE_BYTES,
        "manifest": validation.get("manifest", {}),
        "validation": validation,
        "blockers": [] if validation["ok"] else ["rootfs interno pendente/invalidado"],
        "warnings": ["rootfs atual é scaffold controlado; Ubuntu/Box64/Bedrock ficam para etapas futuras"],
        "updatedAt": _now_ms(),
        "summary": "Rootfs interno validado · pronto para etapa Box64" if validation["ok"] else "Rootfs interno pendente · preparar/validar no APK",
    })
    return state


def _prepare(layout, *, repair=False):
    rootfs = layout["rootfs"]
    staging = layout["staging"]
    log = layout["logs"] / "rootfs-install.log"
    if _has_unknown_existing_rootfs(rootfs):
        state = _status(layout, "repair_blocked_unknown_rootfs" if repair else "prepare_blocked_unknown_rootfs")
        state["ok"] = False
        state["state"] = "rootfs_repair_needed"
        state["summary"] = "Rootfs existente desconhecido; não sobrescrevi automaticamente"
        state["blockers"] = ["rootfs existente não foi criada pelo Core Worker"]
        _write_state(layout, state)
        _append_log(log, state["summary"])
        return state

    _safe_remove_tree(staging)
    staging.mkdir(parents=True, exist_ok=True)
    _append_log(log, "criando rootfs staging controlado")
    manifest = _create_scaffold(staging, source="internal-scaffold-repair" if repair else "internal-scaffold")
    validation = _validate(staging)
    if not validation["ok"]:
        state = _status(layout, "repair_failed" if repair else "prepare_failed")
        state.update({
            "ok": False,
            "state": "rootfs_validation_failed",
            "summary": "Rootfs staging falhou na validação",
            "validation": validation,
            "blockers": validation.get("missing", []),
        })
        _write_state(layout, state)
        _append_log(log, state["summary"])
        return state

    if rootfs.exists():
        _safe_remove_tree(rootfs)
    rootfs.parent.mkdir(parents=True, exist_ok=True)
    try:
        staging.rename(rootfs)
    except Exception:
        shutil.copytree(str(staging), str(rootfs), dirs_exist_ok=True)
        _safe_remove_tree(staging)
    _append_log(log, "rootfs staging promovido para rootfs ativo")
    state = _status(layout, "repair" if repair else "prepare")
    state.update({
        "ok": True,
        "state": "rootfs_validated",
        "summary": "Rootfs interno validado · pronto para etapa Box64",
        "manifest": manifest,
        "installLog": safe_path(log),
        "preparedAt": _now_ms(),
    })
    _write_state(layout, state)
    _write_json(layout["runtime"] / "linux-runtime-state.json", {
        "ok": True,
        "state": "rootfs_validated",
        "rootfsReady": True,
        "rootfs": state,
        "summary": "Core Linux Runtime com rootfs interno validado",
        "updatedAt": state["updatedAt"],
    })
    _append_log(log, state["summary"])
    return state


def run(context_json=None):
    try:
        ctx = load_context(context_json)
        action = clean_text(ctx.get("action") or ctx.get("focus") or "status", 80)
        core_linux_dir = Path(str(ctx.get("coreLinuxDir") or "core-linux"))
        layout = _layout(core_linux_dir)
        _ensure_base(layout)

        if action in {"prepare", "install", "bootstrap"}:
            state = _prepare(layout, repair=False)
        elif action == "repair":
            state = _prepare(layout, repair=True)
        elif action in {"validate", "preflight"}:
            state = _status(layout, action)
            _write_state(layout, state)
            _append_log(layout["logs"] / "rootfs-validate.log", state["summary"])
        elif action in {"clean_staging", "cleanup_staging"}:
            _safe_remove_tree(layout["staging"])
            state = _status(layout, action)
            state["summary"] = "Staging da rootfs limpo; rootfs ativa preservada"
            _write_state(layout, state)
            _append_log(layout["logs"] / "rootfs-install.log", state["summary"])
        elif action == "manifest":
            state = _status(layout, action)
            if not state.get("manifest"):
                state["manifest"] = _rootfs_manifest(layout["rootfs"], _now_ms(), source="planned")
            _write_state(layout, state)
        else:
            state = _status(layout, action)
            _write_state(layout, state)

        return ok_response(
            "core_linux_rootfs",
            state.get("summary", "Rootfs interno atualizado"),
            ok=bool(state.get("ok")),
            state=state.get("state", "unknown"),
            action=action,
            rootfsReady=bool(state.get("rootfsReady")),
            readyForBox64Install=bool(state.get("readyForBox64Install")),
            readyForBedrockStart=bool(state.get("readyForBedrockStart")),
            rootfs=state,
            rootfsDir=safe_path(layout["rootfs"]),
            statePath=safe_path(layout["runtime"] / "rootfs-state.json"),
            manifestPath=safe_path(layout["manifests"] / "rootfs-manifest.json"),
            logs={
                "install": safe_path(layout["logs"] / "rootfs-install.log"),
                "validate": safe_path(layout["logs"] / "rootfs-validate.log"),
            },
            size=dir_size(layout["core"], max_files=900),
            safety="rootfs scaffold em armazenamento app-specific; sem download automático, sem shell livre, sem iniciar Bedrock",
        )
    except Exception as exc:
        return error_response("core_linux_rootfs", exc)
