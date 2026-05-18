import json
import time
from pathlib import Path
from .safe_json import load_context, ok_response, error_response, safe_path, clean_text, dir_size


def _read_text(path, limit=4000):
    try:
        p = Path(path)
        if not p.exists():
            return ""
        return clean_text(p.read_text(encoding="utf-8", errors="replace"), limit)
    except Exception:
        return ""


def _write_text(path, value):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(value, encoding="utf-8")


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


def _eula_accepted(path):
    text = _read_text(path, 600).lower().replace(" ", "")
    return "eula=true" in text


def _latest_log(logs_dir):
    try:
        logs = sorted(Path(logs_dir).glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
        return logs[0] if logs else None
    except Exception:
        return None


def run(context_json=None):
    try:
        ctx = load_context(context_json)
        action = clean_text(ctx.get("action") or ctx.get("focus") or "status", 80)
        core_linux_dir = Path(str(ctx.get("coreLinuxDir") or "core-linux"))
        bedrock_dir = Path(str(ctx.get("bedrockDir") or (core_linux_dir / "bedrock")))
        logs_dir = bedrock_dir / "logs"
        runtime_dir = bedrock_dir / "runtime"
        scripts_dir = core_linux_dir / "scripts"
        for p in (core_linux_dir, bedrock_dir, logs_dir, runtime_dir, scripts_dir):
            p.mkdir(parents=True, exist_ok=True)

        state_path = bedrock_dir / "bedrock-runtime-state.json"
        log_path = logs_dir / "bedrock-runtime.log"
        properties = bedrock_dir / "server.properties"
        eula = bedrock_dir / "eula.txt"
        server = bedrock_dir / "bedrock_server"
        rootfs_ready = (core_linux_dir / "rootfs" / ".core-worker-rootfs-ready").exists()
        box64_ready = (core_linux_dir / "bin" / "box64").exists() or (core_linux_dir / "box64" / "box64").exists()
        eula_ok = _eula_accepted(eula)
        server_installed = server.exists()
        props_ready = properties.exists()
        foreground_active = bool(ctx.get("foregroundRuntimeActive"))
        service_active = bool(ctx.get("bedrockServiceActive"))

        blockers = []
        if not props_ready:
            blockers.append("server.properties ausente")
        if not server_installed:
            blockers.append("bedrock_server não instalado")
        if not eula_ok:
            blockers.append("EULA pendente")
        if not foreground_active and not service_active:
            blockers.append("runtime persistente/serviço Bedrock não ativo")
        if not rootfs_ready:
            blockers.append("rootfs pendente")
        if not box64_ready:
            blockers.append("Box64 pendente")

        previous = _read_json(state_path)
        running = bool(previous.get("running"))
        pid = previous.get("pid") or ""
        now = int(time.time() * 1000)

        if action in {"start", "start_assisted", "start_plan"}:
            if blockers:
                state = "blocked"
                summary = "Bedrock bloqueado · " + "; ".join(blockers[:3])
                running = False
            else:
                state = "ready-to-start"
                summary = "Bedrock pronto para start assistido"
                running = False
            _write_text(log_path, _read_text(log_path, 8000) + f"\n[{now}] start_assisted: {summary}\n")
        elif action in {"stop", "stop_assisted", "stop_plan"}:
            state = "stopped"
            summary = "Bedrock runtime parado/limpo pelo gerenciador"
            running = False
            pid = ""
            _write_text(log_path, _read_text(log_path, 8000) + f"\n[{now}] stop_assisted: {summary}\n")
        elif action in {"logs", "logs_status"}:
            state = previous.get("state") or ("running" if running else "stopped")
            summary = "logs Bedrock coletados pelo runtime assistido"
        else:
            if running:
                state = "running"
                summary = "Bedrock marcado como rodando pelo runtime assistido"
            elif blockers:
                state = "not-ready"
                summary = "Bedrock ainda não pronto · " + "; ".join(blockers[:3])
            else:
                state = "ready-to-start"
                summary = "Bedrock pronto para start assistido"

        payload = {
            "ok": True,
            "state": state,
            "summary": summary,
            "action": action,
            "running": running,
            "pid": pid,
            "serviceActive": service_active,
            "foregroundRuntimeActive": foreground_active,
            "serverInstalled": server_installed,
            "serverProperties": props_ready,
            "eulaAccepted": eula_ok,
            "rootfsReady": rootfs_ready,
            "box64Ready": box64_ready,
            "readyToStart": not blockers,
            "blockers": blockers,
            "bedrockDir": safe_path(bedrock_dir),
            "logPath": safe_path(log_path),
            "updatedAt": now,
            "safety": "runner allowlist; não executa shell livre; não baixa binário; start real só após preflight local",
        }
        _write_json(state_path, payload)
        latest = _latest_log(logs_dir)
        log_tail = _read_text(latest, 2500) if latest else ""
        return ok_response(
            "bedrock_runtime",
            summary,
            ok=True,
            state=state,
            action=action,
            running=running,
            serviceActive=service_active,
            readyToStart=not blockers,
            blockers=blockers,
            bedrockRuntime=payload,
            logTail=log_tail,
            size=dir_size(bedrock_dir, max_files=800),
        )
    except Exception as exc:
        return error_response("bedrock_runtime", exc)
