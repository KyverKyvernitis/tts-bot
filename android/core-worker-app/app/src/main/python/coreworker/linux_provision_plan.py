import json
from pathlib import Path
from .safe_json import load_context, ok_response, error_response, safe_path, dir_size, clean_text


def _read_json(path):
    try:
        p = Path(path)
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        pass
    return {}


def _exists(path):
    try:
        return Path(path).exists()
    except Exception:
        return False


def run(context_json=None):
    try:
        ctx = load_context(context_json)
        core_linux_dir = Path(str(ctx.get("coreLinuxDir") or ""))
        focus = clean_text(ctx.get("focus") or "plan", 60)
        provision = core_linux_dir / "provision"
        rootfs_plan = provision / "rootfs-plan.json"
        box64_plan = provision / "box64-plan.json"
        bedrock_plan = provision / "bedrock-install-plan.json"
        markers = {
            "coreLinuxDir": core_linux_dir.exists(),
            "provisionDir": provision.exists(),
            "runtimeMarker": (core_linux_dir / "runtime-marker.json").exists(),
            "rootfsPlan": rootfs_plan.exists(),
            "box64Plan": box64_plan.exists(),
            "bedrockInstallPlan": bedrock_plan.exists(),
            "rootfsDir": (core_linux_dir / "rootfs").exists(),
            "binDir": (core_linux_dir / "bin").exists(),
            "scriptsDir": (core_linux_dir / "scripts").exists(),
            "logsDir": (core_linux_dir / "logs").exists(),
            "downloadsDir": (core_linux_dir / "downloads").exists(),
            "bedrockDir": (core_linux_dir / "bedrock").exists(),
        }
        missing = [name for name, value in markers.items() if not value]
        plan = {
            "rootfs": _read_json(rootfs_plan),
            "box64": _read_json(box64_plan),
            "bedrock": _read_json(bedrock_plan),
        }
        prepared = bool(markers["coreLinuxDir"] and markers["provisionDir"] and markers["runtimeMarker"])
        plan_ready = prepared and markers["rootfsPlan"] and markers["box64Plan"] and markers["bedrockInstallPlan"]
        summary = "Core Linux provisioner: plano pronto" if plan_ready else "Core Linux provisioner: plano incompleto"
        if focus == "prepare_directories" and plan_ready:
            summary = "Diretórios e planos Linux preparados"
        elif focus == "setup_plan" and plan_ready:
            summary = "Plano assistido Linux pronto · sem download automático"
        return ok_response(
            "linux_provision_plan",
            summary,
            ok=prepared,
            prepared=prepared,
            planReady=plan_ready,
            state="provision-plan-ready" if plan_ready else "provision-plan-pending",
            focus=focus,
            coreLinuxDir=safe_path(core_linux_dir),
            markers=markers,
            missing=missing[:20],
            plan=plan,
            size=dir_size(core_linux_dir, max_files=260),
            termuxInstalled=bool(ctx.get("termuxInstalled")),
            termuxApiInstalled=bool(ctx.get("termuxApiInstalled")),
            termuxBootInstalled=bool(ctx.get("termuxBootInstalled")),
            safety="provisioner somente preparatório: não baixa rootfs, não instala pacotes, não executa binário externo e não aceita EULA",
        )
    except Exception as exc:
        return error_response("linux_provision_plan", exc)
