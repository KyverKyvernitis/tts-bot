"""Pure Voice Agent projections and selection; runtime owns all maps, IO and locks."""
from __future__ import annotations

from collections.abc import Callable
from typing import Any


def public_session(raw: dict[str, Any], *, now_ms: int, int_value: Callable[[Any, int], int]) -> dict[str, Any]:
    expires_at_ms = int_value(raw.get("expires_at_ms"), 0)
    ttl_ms = max(0, expires_at_ms - now_ms) if expires_at_ms else 0
    voice = raw.get("discord_voice") if isinstance(raw.get("discord_voice"), dict) else {}
    return {
        "guild_id": str(raw.get("guild_id") or ""),
        "channel_id": str(raw.get("channel_id") or ""),
        "text_channel_id": str(raw.get("text_channel_id") or ""),
        "requester_id": str(raw.get("requester_id") or ""),
        "source": str(raw.get("source") or "")[:40],
        "state": str(raw.get("state") or "registered")[:60],
        "registered_by": str(raw.get("registered_by") or "vps_control_plane")[:60],
        "age_seconds": round(max(0, now_ms - int_value(raw.get("updated_at_ms"), now_ms)) / 1000.0, 1),
        "ttl_seconds": round(ttl_ms / 1000.0, 1) if ttl_ms else 0.0,
        "session_id_present": bool(voice.get("session_id_present")),
        "endpoint_present": bool(voice.get("endpoint_present")),
        "voice_token_present": bool(voice.get("voice_token_present")),
        "endpoint_host": str(voice.get("endpoint_host") or "")[:120],
        "connected": bool(voice.get("connected")),
        "direct_tts_enabled": bool(raw.get("direct_tts_enabled")),
    }


def public_handoff(raw: dict[str, Any], *, now_ms: int, int_value: Callable[[Any, int], int], clean_text: Callable[..., str]) -> dict[str, Any]:
    expires_at_ms = int_value(raw.get("expires_at_ms"), 0)
    ttl_ms = max(0, expires_at_ms - now_ms) if expires_at_ms else 0
    endpoint = clean_text(raw.get("endpoint_host") or raw.get("endpoint"), limit=160)
    return {
        "guild_id": str(raw.get("guild_id") or ""),
        "channel_id": str(raw.get("channel_id") or ""),
        "source": str(raw.get("source") or "")[:40],
        "state": str(raw.get("state") or "handoff_registered")[:60],
        "dry_run": bool(raw.get("dry_run", True)),
        "age_seconds": round(max(0, now_ms - int_value(raw.get("updated_at_ms"), now_ms)) / 1000.0, 1),
        "ttl_seconds": round(ttl_ms / 1000.0, 1) if ttl_ms else 0.0,
        "session_id_present": bool(raw.get("session_id")),
        "endpoint_present": bool(endpoint),
        "voice_token_present": bool(raw.get("voice_token")),
        "endpoint_host": endpoint[:120],
        "voice_owner": str(raw.get("voice_owner") or raw.get("transport_owner") or "vps")[:40],
        "transport_owner": str(raw.get("transport_owner") or raw.get("voice_owner") or "vps")[:40],
        "connection_policy": str(raw.get("connection_policy") or "handoff_only_wait_for_voice_ownership")[:80],
        "allow_connection_probe": bool(raw.get("allow_connection_probe") or raw.get("allow_probe")),
        "complete": bool(raw.get("session_id") and raw.get("voice_token") and endpoint),
    }


def public_connection(raw: dict[str, Any], *, now_ms: int, int_value: Callable[[Any, int], int]) -> dict[str, Any]:
    started_at = int_value(raw.get("started_at_ms"), now_ms)
    updated_at = int_value(raw.get("updated_at_ms"), started_at)
    return {
        "guild_id": str(raw.get("guild_id") or ""),
        "channel_id": str(raw.get("channel_id") or ""),
        "state": str(raw.get("state") or "unknown")[:80],
        "stage": str(raw.get("stage") or "")[:80],
        "dry_run": bool(raw.get("dry_run", True)),
        "connected_once": bool(raw.get("connected_once")),
        "closed_after_probe": bool(raw.get("closed_after_probe")),
        "ws_url_present": bool(raw.get("ws_url_present")),
        "hello_received": bool(raw.get("hello_received")),
        "ready_received": bool(raw.get("ready_received")),
        "udp_probe_attempted": bool(raw.get("udp_probe_attempted")),
        "udp_probe_ok": bool(raw.get("udp_probe_ok")),
        "ssrc_present": bool(raw.get("ssrc_present")),
        "selected_protocol_ready": bool(raw.get("selected_protocol_ready")),
        "endpoint_host": str(raw.get("endpoint_host") or "")[:120],
        "voice_ip": str(raw.get("voice_ip") or "")[:80],
        "voice_port": int_value(raw.get("voice_port"), 0),
        "latency_ms": raw.get("latency_ms"),
        "age_seconds": round(max(0, now_ms - started_at) / 1000.0, 1),
        "updated_age_seconds": round(max(0, now_ms - updated_at) / 1000.0, 1),
        "error": str(raw.get("error") or "")[:180],
    }


def public_transfer(raw: dict[str, Any], *, now_ms: int, int_value: Callable[[Any, int], int]) -> dict[str, Any]:
    expires_at_ms = int_value(raw.get("expires_at_ms"), 0)
    ttl_ms = max(0, expires_at_ms - now_ms) if expires_at_ms else 0
    return {
        "guild_id": str(raw.get("guild_id") or ""),
        "channel_id": str(raw.get("channel_id") or ""),
        "state": str(raw.get("state") or "transfer_unknown")[:80],
        "current_owner": str(raw.get("current_owner") or raw.get("voice_owner") or "vps")[:40],
        "voice_owner": str(raw.get("voice_owner") or raw.get("current_owner") or "vps")[:40],
        "requested_owner": str(raw.get("requested_owner") or "worker")[:40],
        "lease_id": str(raw.get("lease_id") or "")[:80],
        "allow_connection_probe": bool(raw.get("allow_connection_probe")),
        "probe_authorized": bool(raw.get("probe_authorized")),
        "age_seconds": round(max(0, now_ms - int_value(raw.get("updated_at_ms"), now_ms)) / 1000.0, 1),
        "ttl_seconds": round(ttl_ms / 1000.0, 1) if ttl_ms else 0.0,
        "reason": str(raw.get("reason") or "")[:140],
        "error": str(raw.get("error") or "")[:160],
    }


def expired_keys(records: dict[str, Any], *, now_ms: int, int_value: Callable[[Any, int], int]) -> list[str]:
    return [key for key, data in records.items() if not isinstance(data, dict) or (
        int_value(data.get("expires_at_ms"), 0) and int_value(data.get("expires_at_ms"), 0) <= now_ms)]


def session_summary(records: dict[str, dict[str, Any]], *, now_ms: int, guild_id: int | None, limit: int, public_record: Callable[..., dict[str, Any]], state_file: str) -> dict[str, Any]:
    sessions = []
    for key, raw in records.items():
        if guild_id is not None and str(key) != str(int(guild_id)):
            continue
        if isinstance(raw, dict):
            sessions.append(public_record(raw, now_ms=now_ms))
    sessions.sort(key=lambda item: float(item.get("age_seconds", 999999)))
    return {
        "session_count": len(sessions),
        "active_guilds": [str(item.get("guild_id") or "") for item in sessions[:12] if item.get("guild_id")],
        "sessions": sessions[:limit],
        "last_session": sessions[0] if sessions else {},
        "state_file": state_file,
    }


def handoff_summary(records: dict[str, dict[str, Any]], *, now_ms: int, guild_id: int | None, limit: int, public_record: Callable[..., dict[str, Any]]) -> dict[str, Any]:
    handoffs = []
    for key, raw in records.items():
        if guild_id is not None and str(key) != str(int(guild_id)):
            continue
        handoffs.append(public_record(raw, now_ms=now_ms))
    handoffs.sort(key=lambda item: float(item.get("age_seconds", 999999)))
    complete_count = sum(1 for item in handoffs if item.get("complete"))
    return {
        "handoff_count": len(handoffs),
        "handoff_complete_count": complete_count,
        "handoff_ready": complete_count > 0,
        "handoff_guilds": [str(item.get("guild_id") or "") for item in handoffs[:12] if item.get("guild_id")],
        "handoffs": handoffs[:limit],
        "last_handoff": handoffs[0] if handoffs else {},
    }


def connection_summary(records: dict[str, dict[str, Any]], *, now_ms: int, guild_id: int | None, limit: int, public_record: Callable[..., dict[str, Any]]) -> dict[str, Any]:
    connections = []
    for key, raw in records.items():
        if guild_id is not None and str(key) != str(int(guild_id)):
            continue
        connections.append(public_record(raw, now_ms=now_ms))
    connections.sort(key=lambda item: float(item.get("updated_age_seconds", 999999)))
    ready_count = sum(1 for item in connections if item.get("state") in {"connected_dry_run", "probe_ok", "voice_ws_ready"} or item.get("connected_once"))
    probing_count = sum(1 for item in connections if item.get("state") in {"probing", "connecting", "voice_ws_connecting"})
    failed_count = sum(1 for item in connections if str(item.get("state") or "").endswith("failed") or item.get("state") == "failed")
    return {
        "connection_count": len(connections),
        "connection_ready_count": ready_count,
        "connection_probing_count": probing_count,
        "connection_failed_count": failed_count,
        "connection_ready": ready_count > 0,
        "connection_guilds": [str(item.get("guild_id") or "") for item in connections[:12] if item.get("guild_id")],
        "connections": connections[:limit],
        "last_connection": connections[0] if connections else {},
    }


def transfer_summary(records: dict[str, dict[str, Any]], *, now_ms: int, guild_id: int | None, limit: int, public_record: Callable[..., dict[str, Any]]) -> dict[str, Any]:
    transfers = []
    for key, raw in records.items():
        if guild_id is not None and str(key) != str(int(guild_id)):
            continue
        transfers.append(public_record(raw, now_ms=now_ms))
    transfers.sort(key=lambda item: float(item.get("age_seconds", 999999)))
    ready_count = sum(1 for item in transfers if item.get("voice_owner") == "worker" and item.get("probe_authorized"))
    staged_count = sum(1 for item in transfers if str(item.get("state") or "").startswith("transfer_staged"))
    last = transfers[0] if transfers else {}
    return {
        "transfer_count": len(transfers),
        "transfer_ready_count": ready_count,
        "transfer_staged_count": staged_count,
        "transfer_ready": ready_count > 0,
        "transfer_guilds": [str(item.get("guild_id") or "") for item in transfers[:12] if item.get("guild_id")],
        "transfers": transfers[:limit],
        "last_transfer": last,
        "transfer_state": str(last.get("state") or ""),
        "current_voice_owner": str(last.get("voice_owner") or last.get("current_owner") or "vps") if last else "vps",
        "requested_voice_owner": str(last.get("requested_owner") or "") if last else "",
    }
