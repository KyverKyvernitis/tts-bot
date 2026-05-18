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
        worlds = bedrock_dir / "worlds"
        logs = bedrock_dir / "logs"
        ready = server.exists() and properties.exists()
        markers = {
            "bedrockDir": bedrock_dir.exists(),
            "bedrockServer": server.exists(),
            "serverProperties": properties.exists(),
            "worldsDir": worlds.exists(),
            "logsDir": logs.exists(),
            "officialServerBundled": bool(ctx.get("officialServerBundled")),
        }
        missing = [name for name, ok in markers.items() if not ok and name != "officialServerBundled"]
        summary = "Bedrock Manager: não configurado"
        if bedrock_dir.exists() and not server.exists():
            summary = "Bedrock Manager: pasta pronta · servidor não instalado"
        if ready:
            summary = "Bedrock Manager: arquivos principais encontrados"
        return ok_response(
            "bedrock_probe",
            summary,
            ok=bool(bedrock_dir.exists()),
            ready=ready,
            state="ready" if ready else "not-configured",
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
