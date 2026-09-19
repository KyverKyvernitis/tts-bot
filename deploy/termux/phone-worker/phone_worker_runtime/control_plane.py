"""Compose heartbeat data without owning runtime state, probes or transport."""
from __future__ import annotations

from typing import Any, Mapping


def build_payload(
    base: Mapping[str, Any], *, system: Mapping[str, Any], music_node: Mapping[str, Any],
    music_agent: Mapping[str, Any], battery: Mapping[str, Any], network: Mapping[str, Any],
    updater: Mapping[str, Any],
) -> dict[str, Any]:
    """Extend one live runtime snapshot without modifying any supplied mapping/list."""
    roles, capabilities = list(base["roles"]), list(base["capabilities"])
    for capability in ("ffmpeg", "ffprobe"):
        if system.get(capability) and capability not in capabilities:
            capabilities.append(capability)
    # Playback de música é propriedade exclusiva do Music Agent. Lavalink não
    # participa mais do plano de áudio e o perfil turbo, sozinho, não prova que
    # o agente está pronto. Isso evita anunciar capacidade de playback falsa.
    music_ready = (not base["safe_mode"]) and bool(music_agent.get("available"))
    if music_ready:
        # Perfis como ``turbo`` têm mais itens do que o contrato de heartbeat
        # comporta. Promova as capacidades de reprodução antes do corte final
        # para que ``music-agent``/``music-voice`` não desapareçam em [:24].
        promoted_roles = ("music", "music-agent", "music-ytdlp")
        promoted_capabilities = ("music", "music-agent", "music-voice", "music-ytdlp", "music-ytdlp-resolve")
        roles = [item for item in roles if item not in promoted_roles]
        capabilities = [item for item in capabilities if item not in promoted_capabilities]
        role_at = 1 if roles else 0
        cap_at = 1 if capabilities else 0
        roles[role_at:role_at] = list(promoted_roles)
        capabilities[cap_at:cap_at] = list(promoted_capabilities)
    health = dict(base["health"])
    health.update({key: system.get(key) for key in (
        "pid", "uptime_seconds", "jobs_started", "jobs_failed", "ffmpeg", "ffprobe")})
    for output, section, key in (("scripts_ok", "scripts", "complete"), ("boot_ok", "boot", "ok"),
                                 ("supervisor_ok", "supervisor", "supervisor_ok"), ("sshd_ok", "sshd", "ok")):
        value = system.get(section)
        health[output] = value.get(key) if isinstance(value, dict) else None
    status = dict(base["status"])
    status.update({
        "worker_update": {"transports": list(base["worker_update_transports"]), "updater": dict(updater)},
        "music_node": dict(music_node),
        "music_agent": dict(music_agent),
    })
    status.update({key: system.get(key) for key in (
        "python", "platform", "disk_home", "loadavg", "scripts", "boot", "shell_autostart",
        "auto_boot_repair", "supervisor", "sshd")})
    return {**base, "roles": roles[:16], "capabilities": capabilities[:24],
            "battery": dict(battery), "network": dict(network), "health": health, "status": status}
