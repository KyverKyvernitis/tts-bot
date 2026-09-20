from __future__ import annotations

import os
from typing import Any


def voice_agent_snapshot(
    hooks: Any,
    *,
    music_agent: dict[str, Any] | None = None,
    tts_agent: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Compõe readiness da sessão de voz compartilhada Música + TTS.

    A infraestrutura genérica do Phone Worker fornece armazenamento de sessão,
    env e telemetria; a política que decide quando a música pode possuir/compartilhar
    a voz permanece neste domínio.
    """
    profile = hooks.current_profile()
    capabilities = hooks.env_list("CORE_WORKER_CAPABILITIES", hooks.profile_capabilities(profile))
    enabled = hooks.env_bool("PHONE_WORKER_VOICE_AGENT_ENABLED", True)
    shared_session_enabled = hooks.env_bool("PHONE_WORKER_VOICE_AGENT_SHARED_SESSION_ENABLED", True)
    direct_tts_enabled = hooks.env_bool("PHONE_WORKER_VOICE_AGENT_DIRECT_TTS_ENABLED", True)
    direct_music_enabled = hooks.env_bool("PHONE_WORKER_VOICE_AGENT_DIRECT_MUSIC_ENABLED", True)
    safe_mode = hooks.safe_mode_enabled()

    if music_agent is None:
        music_agent = hooks.safe_telemetry(
            "music_agent",
            hooks.music_agent_snapshot,
            {"ok": False, "available": False, "configured": False},
        )
    if tts_agent is None:
        tts_agent = hooks.safe_telemetry(
            "tts_agent",
            hooks.tts_agent_snapshot,
            {"ok": False, "available": False, "synth_ready": False},
        )

    music_ready = bool(
        (music_agent or {}).get("ok")
        or (music_agent or {}).get("available")
        or (music_agent or {}).get("discord_ready")
    )
    tts_ready = bool((tts_agent or {}).get("ok") and (tts_agent or {}).get("synth_ready"))
    has_voice_capability = "voice-agent" in capabilities or "worker-voice" in capabilities
    base_ready = bool(enabled and profile == "turbo" and has_voice_capability and not safe_mode)

    session_summary = hooks.session_summary(limit=5)
    handoff_summary = hooks.handoff_summary(limit=5)
    connection_summary = hooks.connection_summary(limit=5)
    transfer_summary = hooks.transfer_summary(limit=5)
    session_count = int(session_summary.get("session_count") or 0)
    handoff_count = int(handoff_summary.get("handoff_count") or 0)
    handoff_ready = bool(handoff_summary.get("handoff_ready"))
    connection_ready = bool(connection_summary.get("connection_ready"))
    transfer_ready = bool(transfer_summary.get("transfer_ready"))
    transfer_count = int(transfer_summary.get("transfer_count") or 0)
    shared_ready = bool(base_ready and shared_session_enabled and (music_ready or direct_music_enabled) and tts_ready)
    shared_session_ready = bool(shared_ready and session_count > 0)
    last_connection = dict(connection_summary.get("last_connection") or {})
    direct_connection_ready = bool(
        connection_ready and str(last_connection.get("state") or "").startswith("worker_direct_tts")
    )
    direct_tts_ready = bool(
        direct_tts_enabled
        and music_ready
        and tts_ready
        and (direct_connection_ready or (shared_session_ready and handoff_ready and connection_ready))
    )

    missing: list[str] = []
    if not enabled:
        missing.append("PHONE_WORKER_VOICE_AGENT_ENABLED=false")
    if profile != "turbo":
        missing.append("perfil turbo")
    if safe_mode:
        missing.append("safe mode")
    if not has_voice_capability:
        missing.append("capacidade voice-agent/worker-voice")
    if not shared_session_enabled:
        missing.append("sessão compartilhada desativada")
    if not tts_ready:
        missing.append("TTS Agent pronto")
    if not music_ready:
        missing.append("Music Agent/voz pronta")
    if shared_ready and session_count <= 0:
        missing.append("sessão de voz registrada pela VPS")
    if shared_session_ready and not handoff_ready:
        missing.append("handoff temporário de voz")
    if shared_session_ready and handoff_ready and transfer_count <= 0:
        missing.append("preparação de transferência de posse")
    if shared_session_ready and handoff_ready and transfer_count > 0 and not transfer_ready:
        missing.append("transferência explícita de posse da voz")
    if shared_session_ready and handoff_ready and transfer_ready and not connection_ready and not direct_connection_ready:
        missing.append("conexão worker autorizada ainda não testada")

    if direct_tts_ready:
        state = "direct_tts_voice_ready"
    elif shared_session_ready and handoff_ready and connection_ready:
        state = "voice_connection_dry_run_ready"
    elif shared_session_ready and handoff_ready and transfer_ready:
        state = "voice_ownership_granted_waiting_connection"
    elif shared_session_ready and handoff_ready and transfer_count > 0:
        state = "voice_transfer_staged_waiting_vps_release"
    elif shared_session_ready and handoff_ready:
        state = "voice_handoff_received_waiting_transfer"
    elif shared_session_ready:
        state = "shared_voice_session_registered"
    elif shared_ready:
        state = "waiting_shared_voice_session"
    elif base_ready:
        state = "waiting_dependencies"
    elif enabled:
        state = "not_ready"
    else:
        state = "disabled"

    return {
        "ok": bool(shared_ready),
        "available": bool(base_ready),
        "state": state,
        "enabled": bool(enabled),
        "profile": profile,
        "worker_id": str(os.getenv("CORE_WORKER_ID") or os.getenv("CORE_WORKER_WORKER_ID") or hooks.default_worker_id()).strip(),
        "worker_version": hooks.phone_worker_version,
        "control_plane": "vps",
        "audio_plane": "worker",
        "authority": "vps_control_plane_worker_audio_plane",
        "shared_session_enabled": bool(shared_session_enabled),
        "shared_session_ready": bool(shared_session_ready),
        "session_count": session_count,
        "active_guilds": list(session_summary.get("active_guilds") or [])[:12],
        "sessions": list(session_summary.get("sessions") or [])[:5],
        "last_session": dict(session_summary.get("last_session") or {}),
        "handoff_count": handoff_count,
        "handoff_complete_count": int(handoff_summary.get("handoff_complete_count") or 0),
        "handoff_ready": bool(handoff_ready),
        "handoff_guilds": list(handoff_summary.get("handoff_guilds") or [])[:12],
        "handoffs": list(handoff_summary.get("handoffs") or [])[:5],
        "last_handoff": dict(handoff_summary.get("last_handoff") or {}),
        "connection_count": int(connection_summary.get("connection_count") or 0),
        "connection_ready_count": int(connection_summary.get("connection_ready_count") or 0),
        "connection_probing_count": int(connection_summary.get("connection_probing_count") or 0),
        "connection_failed_count": int(connection_summary.get("connection_failed_count") or 0),
        "connection_ready": bool(connection_summary.get("connection_ready")),
        "connection_guilds": list(connection_summary.get("connection_guilds") or [])[:12],
        "connections": list(connection_summary.get("connections") or [])[:5],
        "last_connection": dict(connection_summary.get("last_connection") or {}),
        "transfer_count": transfer_count,
        "transfer_ready_count": int(transfer_summary.get("transfer_ready_count") or 0),
        "transfer_staged_count": int(transfer_summary.get("transfer_staged_count") or 0),
        "transfer_ready": bool(transfer_ready),
        "transfer_state": str(transfer_summary.get("transfer_state") or "")[:80],
        "current_voice_owner": str(transfer_summary.get("current_voice_owner") or "vps")[:40],
        "requested_voice_owner": str(transfer_summary.get("requested_voice_owner") or "")[:40],
        "transfer_guilds": list(transfer_summary.get("transfer_guilds") or [])[:12],
        "transfers": list(transfer_summary.get("transfers") or [])[:5],
        "last_transfer": dict(transfer_summary.get("last_transfer") or {}),
        "direct_tts_enabled": bool(direct_tts_enabled),
        "direct_tts_ready": bool(direct_tts_ready),
        "direct_music_enabled": bool(direct_music_enabled),
        "connection_auto_probe_enabled": bool(hooks.env_bool("PHONE_WORKER_VOICE_AGENT_CONNECTION_AUTO_PROBE_ENABLED", False)),
        "music_ready": bool(music_ready),
        "tts_ready": bool(tts_ready),
        "music_state": str((music_agent or {}).get("state") or (music_agent or {}).get("status") or "unknown")[:80],
        "tts_state": str((tts_agent or {}).get("state") or "unknown")[:80],
        "voice_transport": "worker_shared_voice_session" if shared_session_ready else ("music_agent_shared_session" if music_ready else "not_connected"),
        "ducking_ready": bool(shared_session_ready and music_ready and tts_ready),
        "missing": missing[:10],
        "note": "VPS segue como plano de controle; o Phone Worker assume o plano de voz/áudio quando autorizado.",
    }
