from pathlib import Path
from .safe_json import load_context, ok_response, error_response, safe_path, dir_size, clean_text


def run(context_json=None):
    try:
        ctx = load_context(context_json)
        core_linux_dir = Path(str(ctx.get("coreLinuxDir") or ""))
        bedrock_dir = Path(str(ctx.get("bedrockDir") or (core_linux_dir / "bedrock")))
        focus = clean_text(ctx.get("focus") or "probe", 40)
        server = bedrock_dir / "bedrock_server"
        properties = bedrock_dir / "server.properties"
        eula = bedrock_dir / "eula.txt"
        worlds = bedrock_dir / "worlds"
        logs = bedrock_dir / "logs"
        scripts = core_linux_dir / "scripts"
        eula_text = ""
        try:
            eula_text = eula.read_text(encoding="utf-8", errors="replace").lower().replace(" ", "") if eula.exists() else ""
        except Exception:
            eula_text = ""
        eula_ok = "eula=true" in eula_text
        scripts_ready = (scripts / "bedrock-start.plan.sh").exists() and (scripts / "bedrock-stop.plan.sh").exists()
        ready = server.exists() and properties.exists() and eula_ok and scripts_ready
        markers = {
            "bedrockDir": bedrock_dir.exists(),
            "bedrockServer": server.exists(),
            "serverProperties": properties.exists(),
            "eulaAccepted": eula_ok,
            "scriptsReady": scripts_ready,
            "worldsDir": worlds.exists(),
            "logsDir": logs.exists(),
            "officialServerBundled": bool(ctx.get("officialServerBundled")),
        }
        missing = [name for name, ok in markers.items() if not ok and name != "officialServerBundled"]
        summary = "Bedrock Manager: não configurado"
        if bedrock_dir.exists() and not server.exists():
            summary = "Bedrock Manager: pasta pronta · servidor não instalado"
        if server.exists() and properties.exists() and not eula_ok:
            summary = "Bedrock Manager: servidor encontrado · EULA pendente"
        if ready:
            summary = "Bedrock Manager: pronto para start seguro"
        return ok_response(
            "bedrock_probe",
            summary,
            ok=bool(bedrock_dir.exists()),
            ready=ready,
            state="ready-to-start" if ready else ("eula-pending" if server.exists() and properties.exists() and not eula_ok else "not-configured"),
            focus=focus,
            bedrockDir=safe_path(bedrock_dir),
            markers=markers,
            missing=missing[:12],
            size=dir_size(bedrock_dir, max_files=260),
            safety="diagnóstico somente leitura; não baixa, não instala, não aceita EULA e não inicia servidor",
            termuxInstalled=bool(ctx.get("termuxInstalled")),
        )
    except Exception as exc:
        return error_response("bedrock_probe", exc)
