"""Lifecycle do processo Music Agent visto pelo Phone Worker."""
from __future__ import annotations

import contextlib
import os
import subprocess
import time
from pathlib import Path
from typing import Any


def start_script(*, best_script) -> Path:
    explicit = str(os.getenv("MUSIC_AGENT_START_COMMAND") or "").strip()
    return Path(explicit).expanduser() if explicit else best_script("start-phone-music-agent.sh")


def pid_file(*, phone_worker_dir) -> Path:
    return Path(os.getenv("MUSIC_AGENT_PID_FILE") or (phone_worker_dir() / "music_agent.pid")).expanduser()


def log_file(*, phone_worker_dir) -> Path:
    return Path(os.getenv("MUSIC_AGENT_LOG_FILE") or (phone_worker_dir() / "music_agent.log")).expanduser()


def service_status(hooks: Any) -> dict[str, Any]:
    pid_path = pid_file(phone_worker_dir=hooks.phone_worker_dir)
    pid = hooks.read_pid_file(pid_path)
    snapshot = hooks.safe_telemetry(
        "music_agent", hooks.snapshot, {"ok": False, "available": False, "configured": False}
    )
    running = bool(snapshot.get("available") or hooks.pid_alive(pid) or hooks.pgrep_count("music_agent.py") > 0)
    return {
        "ok": True,
        "service": "music-agent",
        "manageable": True,
        "running": running,
        "available": bool(snapshot.get("available")),
        "configured": bool(snapshot.get("configured")),
        "version": snapshot.get("version") or snapshot.get("runtime_version") or "",
        "file_version": snapshot.get("file_version") or "",
        "needs_restart": bool(snapshot.get("needs_restart")),
        "pid_file": str(pid_path),
        "pid_file_pid": pid,
        "pid_file_alive": hooks.pid_alive(pid),
        "processes": hooks.pgrep_count("music_agent.py"),
        "script": str(start_script(best_script=hooks.best_script)),
        "log_file": str(log_file(phone_worker_dir=hooks.phone_worker_dir)),
        "health": snapshot,
    }


def run_service_action(action: str, hooks: Any) -> dict[str, Any]:
    action = str(action or "status").strip().lower().replace("-", "_")
    if action == "status":
        return service_status(hooks)
    if action not in {"start", "stop", "restart"}:
        raise ValueError("ação de serviço não permitida")
    script = start_script(best_script=hooks.best_script)
    pid_path = pid_file(phone_worker_dir=hooks.phone_worker_dir)
    if action in {"stop", "restart"}:
        pid = hooks.read_pid_file(pid_path)
        if pid:
            with contextlib.suppress(Exception):
                os.kill(int(pid), 15)
        with contextlib.suppress(Exception):
            pid_path.unlink()
        for _ in range(4):
            if hooks.pgrep_count("music_agent.py") <= 0:
                break
            time.sleep(0.25)
        if hooks.pgrep_count("music_agent.py") > 0:
            with contextlib.suppress(Exception):
                hooks.run_text_command(["pkill", "-f", "music_agent.py"], timeout=2.0, max_bytes=4096)
            time.sleep(0.5)
    result_extra: dict[str, Any] = {}
    if action in {"start", "restart"}:
        if not script.exists():
            raise FileNotFoundError(str(script))
        proc = subprocess.run(["bash", str(script)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=90.0)
        result_extra.update({
            "returncode": int(proc.returncode),
            "stdout": hooks.sanitize_log_text(proc.stdout.decode("utf-8", errors="replace"), limit=3000),
            "stderr": hooks.sanitize_log_text(proc.stderr.decode("utf-8", errors="replace"), limit=3000),
        })
    status = service_status(hooks) | {"action": action, **result_extra}
    if result_extra.get("returncode") not in (None, 0) and not bool(status.get("available")):
        status["ok"] = False
    return status
