from __future__ import annotations

from typing import Any, Mapping


def estender_payload(
    payload: Mapping[str, Any],
    *,
    music_node: Mapping[str, Any],
    music_agent: Mapping[str, Any],
) -> dict[str, Any]:
    """Anexa o contrato musical ao heartbeat genérico do Phone Worker.

    O worker base não precisa conhecer roles/capabilities, disponibilidade do
    Music Agent ou o snapshot do node legado. Esses detalhes pertencem ao
    domínio de música e são promovidos somente depois do payload genérico estar
    pronto.
    """
    result = dict(payload)
    roles = list(result.get("roles") or [])
    capabilities = list(result.get("capabilities") or [])
    safe_mode = bool(result.get("safe_mode"))
    profile = str(result.get("profile") or "").strip().lower()

    music_role_names = {"music", "music-agent", "music-ytdlp"}
    music_capability_names = {
        "music",
        "music-agent",
        "music-voice",
        "music-ytdlp",
        "music-ytdlp-resolve",
        "music-lavalink",
    }
    roles = [item for item in roles if str(item).lower() not in music_role_names]
    capabilities = [item for item in capabilities if str(item).lower() not in music_capability_names]

    # Resolver metadata/stream via yt-dlp continua disponível no perfil turbo
    # mesmo quando o player/agent ainda não subiu. Playback só é anunciado
    # quando o Music Agent está realmente disponível.
    if not safe_mode and profile == "turbo":
        resolver_roles = ("music-ytdlp",)
        resolver_caps = ("music-ytdlp", "music-ytdlp-resolve")
        role_at = 1 if roles else 0
        cap_at = 1 if capabilities else 0
        roles[role_at:role_at] = list(resolver_roles)
        capabilities[cap_at:cap_at] = list(resolver_caps)

    if not safe_mode and bool(music_agent.get("available")):
        playback_roles = ("music", "music-agent")
        playback_caps = ("music", "music-agent", "music-voice")
        role_at = 1 if roles else 0
        cap_at = 1 if capabilities else 0
        roles[role_at:role_at] = list(playback_roles)
        capabilities[cap_at:cap_at] = list(playback_caps)

    status = dict(result.get("status") or {})
    status["music_node"] = dict(music_node)
    status["music_agent"] = dict(music_agent)
    result["roles"] = roles[:16]
    result["capabilities"] = capabilities[:24]
    result["status"] = status
    return result
