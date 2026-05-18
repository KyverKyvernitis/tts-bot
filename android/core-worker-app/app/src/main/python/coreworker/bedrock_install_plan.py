import json
from pathlib import Path
from .safe_json import load_context, ok_response, error_response, safe_path, dir_size, clean_text


def _read_text(path, limit=4000):
    try:
        p = Path(path)
        if p.exists():
            text = p.read_text(encoding="utf-8", errors="replace")
            return clean_text(text, limit)
    except Exception:
        pass
    return ""


def _read_json(path):
    try:
        p = Path(path)
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        pass
    return {}


def run(context_json=None):
    try:
        ctx = load_context(context_json)
        core_linux_dir = Path(str(ctx.get("coreLinuxDir") or ""))
        bedrock_dir = Path(str(ctx.get("bedrockDir") or (core_linux_dir / "bedrock")))
        provision = core_linux_dir / "provision"
        focus = clean_text(ctx.get("focus") or "install_plan", 60)
        plan_path = provision / "bedrock-install-plan.json"
        properties_template = bedrock_dir / "server.properties.template"
        eula_notice = bedrock_dir / "EULA_NOT_ACCEPTED.txt"
        start_template = core_linux_dir / "scripts" / "bedrock-start.template.sh"
        markers = {
            "bedrockDir": bedrock_dir.exists(),
            "installPlan": plan_path.exists(),
            "propertiesTemplate": properties_template.exists(),
            "eulaNotice": eula_notice.exists(),
            "startTemplate": start_template.exists(),
            "bedrockServer": (bedrock_dir / "bedrock_server").exists(),
            "serverProperties": (bedrock_dir / "server.properties").exists(),
            "eulaAcceptedFile": (bedrock_dir / "eula.txt").exists(),
        }
        plan_ready = markers["bedrockDir"] and markers["installPlan"] and markers["propertiesTemplate"] and markers["eulaNotice"]
        server_installed = markers["bedrockServer"] and markers["serverProperties"]
        plan = _read_json(plan_path)
        summary = "Bedrock install plan pronto · servidor não instalado"
        if not plan_ready:
            summary = "Bedrock install plan incompleto"
        elif server_installed:
            summary = "Bedrock: arquivos de servidor detectados"
        if focus == "properties_template" and markers["propertiesTemplate"]:
            summary = "Template server.properties pronto"
        return ok_response(
            "bedrock_install_plan",
            summary,
            ok=bool(markers["bedrockDir"]),
            ready=bool(server_installed),
            planReady=bool(plan_ready),
            state="server-ready" if server_installed else ("install-plan-ready" if plan_ready else "install-plan-pending"),
            focus=focus,
            bedrockDir=safe_path(bedrock_dir),
            markers=markers,
            missing=[name for name, value in markers.items() if not value and name not in {"bedrockServer", "serverProperties", "eulaAcceptedFile"}][:16],
            plan=plan,
            propertiesTemplate=_read_text(properties_template, 1800) if focus == "properties_template" else "",
            eulaAccepted=False,
            officialLinux=clean_text(ctx.get("officialLinux") or "Ubuntu Linux 22.04 LTS+", 160),
            officialRamGb=int(ctx.get("officialRamGb") or 4),
            size=dir_size(bedrock_dir, max_files=260),
            safety="não baixa Bedrock, não aceita EULA, não inicia servidor; apenas plano e templates locais",
            termuxInstalled=bool(ctx.get("termuxInstalled")),
        )
    except Exception as exc:
        return error_response("bedrock_install_plan", exc)
