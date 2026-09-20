"""Compose generic heartbeat data without owning optional domain state."""
from __future__ import annotations

from typing import Any, Mapping


def build_payload(
    base: Mapping[str, Any], *, system: Mapping[str, Any], battery: Mapping[str, Any],
    network: Mapping[str, Any], updater: Mapping[str, Any],
) -> dict[str, Any]:
    """Extend one live runtime snapshot without modifying supplied mappings."""
    roles, capabilities = list(base["roles"]), list(base["capabilities"])
    for capability in ("ffmpeg", "ffprobe"):
        if system.get(capability) and capability not in capabilities:
            capabilities.append(capability)
    health = dict(base["health"])
    health.update({key: system.get(key) for key in (
        "pid", "uptime_seconds", "jobs_started", "jobs_failed", "ffmpeg", "ffprobe")})
    for output, section, key in (("scripts_ok", "scripts", "complete"), ("boot_ok", "boot", "ok"),
                                 ("supervisor_ok", "supervisor", "supervisor_ok"), ("sshd_ok", "sshd", "ok")):
        value = system.get(section)
        health[output] = value.get(key) if isinstance(value, dict) else None
    status = dict(base["status"])
    status["worker_update"] = {"transports": list(base["worker_update_transports"]), "updater": dict(updater)}
    status.update({key: system.get(key) for key in (
        "python", "platform", "disk_home", "loadavg", "scripts", "boot", "shell_autostart",
        "auto_boot_repair", "supervisor", "sshd")})
    return {**base, "roles": roles, "capabilities": capabilities,
            "battery": dict(battery), "network": dict(network), "health": health, "status": status}
