from pathlib import Path
from .safe_json import load_context, ok_response, error_response, safe_path, dir_size, clean_text


def _exists(path):
    try:
        return Path(str(path or "")).exists()
    except Exception:
        return False


def _is_nonempty_dir(path):
    try:
        p = Path(str(path or ""))
        return p.is_dir() and any(p.iterdir())
    except Exception:
        return False


def run(context_json=None):
    try:
        ctx = load_context(context_json)
        core_linux_dir = Path(str(ctx.get("coreLinuxDir") or ""))
        focus = clean_text(ctx.get("focus") or "runtime", 40)
        rootfs = core_linux_dir / "rootfs"
        bin_dir = core_linux_dir / "bin"
        scripts = core_linux_dir / "scripts"
        logs = core_linux_dir / "logs"
        downloads = core_linux_dir / "downloads"
        bedrock = core_linux_dir / "bedrock"
        markers = {
            "coreLinuxDir": core_linux_dir.exists(),
            "runtimeMarker": (core_linux_dir / "runtime-marker.json").exists(),
            "rootfsDir": rootfs.exists(),
            "binDir": bin_dir.exists(),
            "scriptsDir": scripts.exists(),
            "logsDir": logs.exists(),
            "downloadsDir": downloads.exists(),
            "bedrockDir": bedrock.exists(),
            "box64Binary": (bin_dir / "box64").exists(),
            "prootBinary": (bin_dir / "proot").exists(),
            "rootfsPrepared": _is_nonempty_dir(rootfs),
            "bedrockServerFound": (bedrock / "bedrock_server").exists(),
        }
        missing_base = [k for k in ["coreLinuxDir", "runtimeMarker", "rootfsDir", "binDir", "scriptsDir", "logsDir", "downloadsDir", "bedrockDir"] if not markers[k]]
        rootfs_ready = bool(markers["rootfsPrepared"])
        box64_ready = bool(markers["box64Binary"])
        prepared = bool(markers["coreLinuxDir"] and markers["runtimeMarker"] and not missing_base)
        ok = prepared
        if focus == "rootfs":
            ok = prepared and rootfs_ready
        elif focus == "box64":
            ok = prepared and box64_ready
        state = "base-preparada" if prepared else "base-pendente"
        if focus == "rootfs" and not rootfs_ready:
            state = "rootfs-pendente"
        if focus == "box64" and not box64_ready:
            state = "box64-pendente"
        summary = "Core Linux Runtime: base pronta"
        if focus == "rootfs":
            summary = "Rootfs Linux ainda pendente" if not rootfs_ready else "Rootfs Linux detectado"
        elif focus == "box64":
            summary = "Box64 ainda pendente" if not box64_ready else "Box64 detectado"
        elif prepared and not rootfs_ready:
            summary = "Core Linux Runtime pronto · rootfs ainda pendente"
        elif prepared and rootfs_ready:
            summary = "Core Linux Runtime com rootfs detectado"
        return ok_response(
            "linux_runtime_probe",
            summary,
            ok=ok,
            prepared=prepared,
            state=state,
            focus=focus,
            rootfsReady=rootfs_ready,
            box64Ready=box64_ready,
            termuxInstalled=bool(ctx.get("termuxInstalled")),
            termuxApiInstalled=bool(ctx.get("termuxApiInstalled")),
            termuxBootInstalled=bool(ctx.get("termuxBootInstalled")),
            coreLinuxDir=safe_path(core_linux_dir),
            markers=markers,
            missing=missing_base[:12],
            size=dir_size(core_linux_dir, max_files=220),
            safety="diagnóstico somente leitura; não instala e não executa binário externo",
        )
    except Exception as exc:
        return error_response("linux_runtime_probe", exc)
