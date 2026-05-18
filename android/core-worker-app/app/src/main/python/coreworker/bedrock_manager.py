import json
import time
from pathlib import Path
from .safe_json import load_context, ok_response, error_response, safe_path, dir_size, clean_text

DEFAULT_PROPERTIES = """server-name=Core Worker Bedrock
gamemode=survival
force-gamemode=false
difficulty=easy
allow-cheats=false
max-players=10
online-mode=true
allow-list=false
server-port=19132
server-portv6=19133
view-distance=10
tick-distance=4
player-idle-timeout=30
max-threads=4
level-name=Bedrock level
level-seed=
default-player-permission-level=member
texturepack-required=false
content-log-file-enabled=true
compression-threshold=1
server-authoritative-movement=server-auth
"""

START_SCRIPT = """#!/bin/sh
# Core Worker Bedrock start plan.
# This file is generated only as a plan and is not executed automatically.
# Requirements before enabling execution:
# - Linux rootfs ready
# - Box64 ready when using official x86_64 Bedrock on ARM64
# - bedrock_server installed from the official Minecraft download page
# - EULA accepted explicitly by the owner
cd \"$CORE_BEDROCK_DIR\" || exit 1
LD_LIBRARY_PATH=. ./bedrock_server
"""

STOP_SCRIPT = """#!/bin/sh
# Core Worker Bedrock stop plan.
# Future runner should stop the foreground-managed process gracefully.
# This placeholder intentionally does not kill arbitrary processes.
echo \"Bedrock stop is managed by Core Worker foreground runtime.\"
"""

STATUS_SCRIPT = """#!/bin/sh
# Core Worker Bedrock status plan.
# Future runner should inspect the managed PID file and UDP port 19132.
echo \"Bedrock status is collected by Core Worker Manager.\"
"""


def _write_text(path, value, overwrite=False):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    if p.exists() and not overwrite:
        return False
    p.write_text(value, encoding="utf-8")
    return True


def _read_text(path, limit=3000):
    try:
        p = Path(path)
        if not p.exists():
            return ""
        return clean_text(p.read_text(encoding="utf-8", errors="replace"), limit)
    except Exception:
        return ""


def _write_json(path, payload):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _eula_accepted(path):
    text = _read_text(path, 400).lower().replace(" ", "")
    return "eula=true" in text


def _latest_log(logs_dir):
    try:
        logs = sorted(Path(logs_dir).glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
        if logs:
            return logs[0]
    except Exception:
        pass
    return None


def run(context_json=None):
    try:
        ctx = load_context(context_json)
        focus = clean_text(ctx.get("focus") or "status", 80)
        core_linux_dir = Path(str(ctx.get("coreLinuxDir") or ""))
        bedrock_dir = Path(str(ctx.get("bedrockDir") or (core_linux_dir / "bedrock")))
        scripts_dir = core_linux_dir / "scripts"
        logs_dir = bedrock_dir / "logs"
        worlds_dir = bedrock_dir / "worlds"
        backups_dir = bedrock_dir / "backups"
        config_dir = bedrock_dir / "config"
        for p in (bedrock_dir, scripts_dir, logs_dir, worlds_dir, backups_dir, config_dir):
            p.mkdir(parents=True, exist_ok=True)

        properties = bedrock_dir / "server.properties"
        properties_template = bedrock_dir / "server.properties.template"
        eula = bedrock_dir / "eula.txt"
        eula_notice = bedrock_dir / "EULA_NOT_ACCEPTED.txt"
        server = bedrock_dir / "bedrock_server"
        start_script = scripts_dir / "bedrock-start.plan.sh"
        stop_script = scripts_dir / "bedrock-stop.plan.sh"
        status_script = scripts_dir / "bedrock-status.plan.sh"
        state_path = bedrock_dir / "bedrock-manager-state.json"

        if focus in {"prepare", "prepare_properties", "properties", "start_plan", "stop_plan", "status", "logs_status"}:
            _write_text(properties_template, DEFAULT_PROPERTIES, overwrite=False)
            _write_text(properties, DEFAULT_PROPERTIES, overwrite=False)
            _write_text(start_script, START_SCRIPT, overwrite=True)
            _write_text(stop_script, STOP_SCRIPT, overwrite=True)
            _write_text(status_script, STATUS_SCRIPT, overwrite=True)
            _write_text(eula_notice, "EULA ainda não aceita. Use confirmação explícita no app antes de iniciar o Bedrock.\n", overwrite=False)

        if focus == "accept_eula" and bool(ctx.get("acceptEula")):
            _write_text(eula, "# Accepted explicitly inside Core Worker APK by the device owner.\neula=true\n", overwrite=True)
            _write_json(bedrock_dir / "eula-accepted.json", {
                "accepted": True,
                "acceptedAt": int(time.time() * 1000),
                "source": "core-worker-apk-local-confirmation",
                "note": "EULA was accepted by explicit local confirmation in the APK UI.",
            })

        eula_ok = _eula_accepted(eula)
        server_installed = server.exists()
        props_ready = properties.exists()
        scripts_ready = start_script.exists() and stop_script.exists() and status_script.exists()
        ready_to_start = bool(server_installed and props_ready and eula_ok and scripts_ready)
        if ready_to_start:
            state = "ready-to-start"
            summary = "Bedrock pronto para iniciar · aguardando runner"
        elif server_installed and props_ready and not eula_ok:
            state = "eula-pending"
            summary = "Bedrock instalado · EULA pendente"
        elif props_ready and not server_installed:
            state = "server-not-installed"
            summary = "Bedrock Manager pronto · servidor não instalado"
        else:
            state = "manager-prepared"
            summary = "Bedrock Manager preparado · configure propriedades"

        latest = _latest_log(logs_dir)
        log_tail = _read_text(latest, 1800) if latest else ""
        manager_state = {
            "ok": True,
            "state": state,
            "summary": summary,
            "serverInstalled": server_installed,
            "serverProperties": props_ready,
            "eulaAccepted": eula_ok,
            "scriptsReady": scripts_ready,
            "readyToStart": ready_to_start,
            "bedrockDir": safe_path(bedrock_dir),
            "port": 19132,
            "portV6": 19133,
            "foregroundRequired": True,
            "officialServerBundled": False,
            "lastLog": safe_path(latest) if latest else "",
        }
        _write_json(state_path, manager_state)

        return ok_response(
            "bedrock_manager",
            summary,
            ok=True,
            ready=ready_to_start,
            state=state,
            focus=focus,
            bedrockDir=safe_path(bedrock_dir),
            markers={
                "bedrockDir": bedrock_dir.exists(),
                "serverProperties": props_ready,
                "propertiesTemplate": properties_template.exists(),
                "eulaAccepted": eula_ok,
                "bedrockServer": server_installed,
                "scriptsReady": scripts_ready,
                "logsDir": logs_dir.exists(),
                "worldsDir": worlds_dir.exists(),
                "backupsDir": backups_dir.exists(),
            },
            manager=manager_state,
            propertiesPreview=_read_text(properties, 1800) if focus in {"properties", "prepare_properties"} else "",
            logTail=log_tail if focus == "logs_status" else "",
            startPlan=safe_path(start_script) if start_script.exists() else "",
            stopPlan=safe_path(stop_script) if stop_script.exists() else "",
            statusPlan=safe_path(status_script) if status_script.exists() else "",
            size=dir_size(bedrock_dir, max_files=500),
            safety="não baixa servidor, não inicia processo e não executa shell; EULA só pode ser marcada com confirmação local explícita",
        )
    except Exception as exc:
        return error_response("bedrock_manager", exc)
