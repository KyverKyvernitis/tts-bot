import json
import time
from pathlib import Path
from .safe_json import load_context, ok_response, error_response, safe_path, clean_text, dir_size

LOG_LIMIT = 4000


def _read_text(path, limit=4000):
    try:
        p = Path(path)
        if not p.exists():
            return ""
        text = p.read_text(encoding="utf-8", errors="replace")
        if limit and len(text) > limit:
            return "… histórico anterior omitido …\n" + text[-limit:]
        return clean_text(text, limit or 8000)
    except Exception:
        return ""


def _write_text(path, value):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(value, encoding="utf-8")


def _append_text(path, value, max_bytes=512 * 1024):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    if p.exists() and p.stat().st_size > max_bytes:
        old = p.with_suffix(p.suffix + ".old")
        try:
            old.unlink(missing_ok=True)
        except Exception:
            pass
        try:
            p.rename(old)
        except Exception:
            p.write_text("", encoding="utf-8")
    with p.open("a", encoding="utf-8") as fh:
        fh.write(value)


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


def _tail_log(logs_dir, runtime_dir):
    candidates = []
    for path in [Path(logs_dir) / "bedrock-console.log", Path(logs_dir) / "bedrock-runtime.log", Path(runtime_dir) / "console.log"]:
        if path.exists():
            candidates.append(path)
    try:
        candidates.extend(sorted(Path(logs_dir).glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True)[:3])
    except Exception:
        pass
    seen = set()
    chunks = []
    for path in candidates:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        text = _read_text(path, 1800)
        if text:
            chunks.append(f"--- {path.name} ---\n{text}")
    return clean_text("\n".join(chunks), LOG_LIMIT)


def _command_allowed(command):
    command = clean_text(command or "", 256).strip()
    if not command:
        return False, ""
    if "\n" in command or "\r" in command or "\x00" in command:
        return False, ""
    # Console Bedrock, não shell Android. Bloqueia tentativas óbvias de virar shell.
    lowered = command.lower().strip()
    blocked_prefixes = ("sh ", "bash ", "su ", "cmd ", "am ", "pm ", "toybox ", "busybox ", "rm ", "chmod ", "curl ", "wget ")
    if lowered.startswith(blocked_prefixes):
        return False, command
    return True, command


def _queue_command(queue_path, command, source="app"):
    ok, command = _command_allowed(command)
    if not ok:
        return False, command, "comando bloqueado: o terminal é console Bedrock, não shell Android"
    item = {
        "at": int(time.time() * 1000),
        "source": clean_text(source, 40),
        "command": command,
    }
    _append_text(queue_path, json.dumps(item, ensure_ascii=False) + "\n", max_bytes=256 * 1024)
    return True, command, "comando enviado para a fila do console Bedrock"


def _build_preflight(core_linux_dir, bedrock_dir, service_active, foreground_active):
    core_linux_dir = Path(core_linux_dir)
    bedrock_dir = Path(bedrock_dir)
    runtime_dir = bedrock_dir / "runtime"
    logs_dir = bedrock_dir / "logs"
    for p in (core_linux_dir, bedrock_dir, runtime_dir, logs_dir, bedrock_dir / "worlds", bedrock_dir / "backups"):
        p.mkdir(parents=True, exist_ok=True)

    properties = bedrock_dir / "server.properties"
    eula = bedrock_dir / "eula.txt"
    server = bedrock_dir / "bedrock_server"
    internal_state = _read_json(core_linux_dir / "runtime" / "core-linux-internal-state.json")
    internal_preflight = internal_state.get("preflight") if isinstance(internal_state.get("preflight"), dict) else {}
    internal_executor_ready = bool(internal_preflight.get("executorReady"))
    rootfs_ready = bool((core_linux_dir / "rootfs" / ".core-worker-rootfs-ready").exists() or internal_preflight.get("rootfsReady"))
    box64_candidates = [core_linux_dir / "bin" / "box64", core_linux_dir / "box64" / "box64"]
    box64 = next((p for p in box64_candidates if p.exists()), None)
    box64_ready = bool(box64 is not None or internal_preflight.get("box64Ready"))
    eula_ok = _eula_accepted(eula)
    server_installed = server.exists()
    props_ready = properties.exists()

    blockers = []
    if not props_ready:
        blockers.append("server.properties ausente")
    if not server_installed:
        blockers.append("bedrock_server não instalado")
    if not eula_ok:
        blockers.append("EULA pendente")
    if not internal_executor_ready:
        blockers.append("executor interno pendente")
    if not rootfs_ready:
        blockers.append("rootfs pendente")
    if not box64_ready:
        blockers.append("Box64 pendente")
    if not service_active and not foreground_active:
        blockers.append("runtime/serviço visível ainda não ativo")

    return {
        "coreLinuxDir": core_linux_dir,
        "bedrockDir": bedrock_dir,
        "runtimeDir": runtime_dir,
        "logsDir": logs_dir,
        "properties": properties,
        "eula": eula,
        "server": server,
        "box64": box64,
        "box64Ready": box64_ready,
        "internalExecutorReady": internal_executor_ready,
        "coreLinuxInternal": internal_state,
        "rootfsReady": rootfs_ready,
        "eulaAccepted": eula_ok,
        "serverInstalled": server_installed,
        "serverProperties": props_ready,
        "serviceActive": bool(service_active),
        "foregroundRuntimeActive": bool(foreground_active),
        "readyToStart": not blockers,
        "blockers": blockers,
    }


def run(context_json=None):
    try:
        ctx = load_context(context_json)
        action = clean_text(ctx.get("action") or ctx.get("focus") or "status", 80)
        core_linux_dir = Path(str(ctx.get("coreLinuxDir") or "core-linux"))
        bedrock_dir = Path(str(ctx.get("bedrockDir") or (core_linux_dir / "bedrock")))
        service_active = bool(ctx.get("bedrockServiceActive"))
        foreground_active = bool(ctx.get("foregroundRuntimeActive"))
        preflight = _build_preflight(core_linux_dir, bedrock_dir, service_active, foreground_active)
        runtime_dir = preflight["runtimeDir"]
        logs_dir = preflight["logsDir"]
        state_path = runtime_dir / "runner-state.json"
        legacy_state_path = bedrock_dir / "bedrock-runtime-state.json"
        queue_path = runtime_dir / "command-queue.jsonl"
        log_path = logs_dir / "bedrock-console.log"
        previous = _read_json(state_path) or _read_json(legacy_state_path)
        now = int(time.time() * 1000)

        state = previous.get("state") or "not-ready"
        running = bool(previous.get("running"))
        summary = previous.get("summary") or "Bedrock runtime aguardando"
        ok = True
        message_extra = {}

        if action in {"preflight", "status"}:
            if preflight["readyToStart"]:
                state = "ready" if not running else "running"
                summary = "Bedrock pronto para start real" if not running else "Servidor Bedrock rodando"
            else:
                state = "blocked"
                summary = "Bedrock bloqueado · " + "; ".join(preflight["blockers"][:3])
        elif action in {"start", "start_assisted", "start_plan"}:
            if preflight["blockers"]:
                state = "blocked"
                running = False
                summary = "Bedrock não pode iniciar · " + "; ".join(preflight["blockers"][:3])
            else:
                # O Foreground Service é quem tenta manter o processo real vivo.
                state = "start-requested"
                summary = "start real solicitado ao serviço Bedrock"
            _append_text(log_path, f"[{now}] start: {summary}\n")
        elif action in {"stop", "stop_assisted", "stop_plan"}:
            _queue_command(queue_path, "stop", source="app-stop")
            state = "stop-requested"
            summary = "stop enviado ao runner Bedrock"
            _append_text(log_path, f"[{now}] stop solicitado\n")
        elif action in {"console_command"}:
            command = str(ctx.get("consoleCommand") or "")
            sent, cleaned, msg = _queue_command(queue_path, command, source="app-terminal")
            ok = sent
            state = previous.get("state") or ("running" if running else "queued")
            summary = msg if sent else msg
            message_extra["queuedCommand"] = cleaned
        elif action in {"console_command_remote_blocked"}:
            ok = False
            state = previous.get("state") or "blocked"
            summary = "comando remoto bloqueado: use o terminal local do app"
        elif action in {"console_tail", "logs", "logs_status"}:
            state = previous.get("state") or ("running" if running else "stopped")
            summary = "console Bedrock atualizado"
        elif action == "repair":
            # Recria arquivos de runtime sem tocar em mundos/configs.
            state = "repaired"
            summary = "runtime Bedrock reparado sem apagar mundo/configuração"
            _write_text(runtime_dir / "command-queue.jsonl", "")
            _append_text(log_path, f"[{now}] repair: fila do console reiniciada\n")
        else:
            summary = "ação Bedrock desconhecida, status retornado"

        runner_state = _read_json(state_path)
        if runner_state:
            state = runner_state.get("state", state)
            running = bool(runner_state.get("running", running))
            if action in {"status", "console_tail"}:
                summary = runner_state.get("summary", summary)

        log_tail = _tail_log(logs_dir, runtime_dir)
        payload = {
            "ok": ok,
            "state": state,
            "summary": summary,
            "action": action,
            "running": running,
            "serviceActive": service_active,
            "foregroundRuntimeActive": foreground_active,
            "serverInstalled": preflight["serverInstalled"],
            "serverProperties": preflight["serverProperties"],
            "eulaAccepted": preflight["eulaAccepted"],
            "rootfsReady": preflight["rootfsReady"],
            "box64Ready": bool(preflight.get("box64Ready")),
            "internalExecutorReady": bool(preflight.get("internalExecutorReady")),
            "readyToStart": preflight["readyToStart"],
            "blockers": preflight["blockers"],
            "bedrockDir": safe_path(bedrock_dir),
            "runtimeDir": safe_path(runtime_dir),
            "commandQueue": safe_path(queue_path),
            "logPath": safe_path(log_path),
            "updatedAt": now,
            "safety": "runner real com preflight; console Bedrock via fila; sem shell Android livre; comando remoto bloqueado",
        }
        payload.update(message_extra)
        _write_json(legacy_state_path, payload)
        if not state_path.exists() or action in {"preflight", "status", "repair", "console_command", "console_tail"}:
            merged = dict(payload)
            if runner_state:
                merged.update({k: runner_state.get(k, v) for k, v in payload.items()})
            _write_json(state_path, merged)
        return ok_response(
            "bedrock_runtime",
            summary,
            ok=ok,
            state=state,
            action=action,
            running=running,
            serviceActive=service_active,
            readyToStart=preflight["readyToStart"],
            blockers=preflight["blockers"],
            bedrockRuntime=payload,
            logTail=log_tail,
            size=dir_size(bedrock_dir, max_files=1200),
        )
    except Exception as exc:
        return error_response("bedrock_runtime", exc)
