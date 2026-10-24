"""Configuração e bootstrap de ambiente do Music Agent do Phone Worker."""
from __future__ import annotations

import contextlib
import math
import os
import re
import secrets
from pathlib import Path
from typing import Any


AUDIO_OUTPUT_PROFILES = {"balanced": None, "quality_192": 192, "quality_256": 256}
AUDIO_OUTPUT_OPUS_DIRECT_MODES = ("off", "auto")
AUDIO_OUTPUT_SETTINGS = frozenset({"profile", "fec", "expected_packet_loss", "opus_direct"})


def validate_audio_output_settings(
    values: dict[str, Any], current: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Valida um ajuste parcial sem alterar configurações ou estado recebidos."""
    if not isinstance(values, dict) or (current is not None and not isinstance(current, dict)):
        raise ValueError("configuração da saída deve ser um objeto")
    merged = {**(current or {}), **values}
    if set(merged) - AUDIO_OUTPUT_SETTINGS:
        raise ValueError("configuração de saída não suportada")
    if "profile" in merged:
        profile = merged["profile"]
        if not isinstance(profile, str) or profile.strip().lower() not in AUDIO_OUTPUT_PROFILES:
            raise ValueError("perfil de saída deve ser balanced, quality_192 ou quality_256")
        merged["profile"] = profile.strip().lower()
    if "opus_direct" in merged:
        mode = merged["opus_direct"]
        if not isinstance(mode, str) or mode.strip().lower() not in AUDIO_OUTPUT_OPUS_DIRECT_MODES:
            raise ValueError("opus_direct deve ser off ou auto")
        merged["opus_direct"] = mode.strip().lower()
    if "fec" in merged and not isinstance(merged["fec"], bool):
        raise ValueError("fec deve ser booleano")
    if "expected_packet_loss" in merged:
        loss = merged["expected_packet_loss"]
        if (not isinstance(loss, (int, float)) or isinstance(loss, bool)
                or not math.isfinite(float(loss)) or not 0 <= loss <= 1):
            raise ValueError("expected_packet_loss deve ser um número entre 0 e 1")
        merged["expected_packet_loss"] = float(loss)
    return merged


def audio_output_policy(agent: Any, voice_client: Any, track: Any = None, state: Any = None) -> dict[str, Any]:
    """Escolhe somente parâmetros de saída; não altera fonte, volume ou player."""
    defaults = validate_audio_output_settings({
        "profile": getattr(agent, "audio_output_default_profile", "balanced"),
        "fec": getattr(agent, "discord_opus_fec", True),
        "expected_packet_loss": getattr(agent, "discord_opus_expected_packet_loss", 0.15),
        "opus_direct": getattr(agent, "audio_output_default_opus_direct", "off"),
    })
    settings = validate_audio_output_settings(getattr(state, "audio_output_settings", {}) or {}, defaults)
    channel = getattr(voice_client, "channel", None)
    channel_bps = max(0, int(getattr(channel, "bitrate", 0) or 0))
    channel_kbps = channel_bps // 1000
    source_abr = max(0, int(getattr(track, "audio_abr", 0) or 0))
    profile = settings["profile"]
    target = AUDIO_OUTPUT_PROFILES[profile]
    maximum = max(16, min(512, int(getattr(agent, "discord_opus_max_bitrate", 256))))
    if target is None:
        target = (source_abr + int(getattr(agent, "discord_opus_source_headroom", 32))) if source_abr > 0 else int(
            getattr(agent, "discord_opus_default_bitrate", 160))
        target = max(int(getattr(agent, "discord_opus_min_bitrate", 96)), target)
    desired = min(maximum, target)
    if channel_kbps > 0:
        desired = min(desired, channel_kbps)
    return {
        "profile": profile,
        "bitrate_kbps": max(16, min(512, int(desired))),
        "target_bitrate_kbps": int(target),
        "channel_bitrate_kbps": channel_kbps,
        "source_abr_kbps": source_abr,
        "limited_by_channel": bool(channel_kbps and channel_kbps < min(maximum, target)),
        "limited_by_config": target > maximum,
        "fec": settings["fec"],
        "expected_packet_loss": settings["expected_packet_loss"],
        "opus_direct": settings["opus_direct"],
        "application": "audio", "bandwidth": "full", "signal_type": "music",
        "revision": max(0, int(getattr(state, "audio_output_revision", 0) or 0)),
    }


def audio_output_capabilities() -> dict[str, Any]:
    """Descreve controles públicos, sem prometer recursos da libopus instalada."""
    return {
        "profiles": list(AUDIO_OUTPUT_PROFILES),
        "profile_target_kbps": dict(AUDIO_OUTPUT_PROFILES),
        "settings": sorted(AUDIO_OUTPUT_SETTINGS),
        "sample_rate_hz": 48000, "channels": 2, "frame_duration_ms": 20,
        "loss_is_measured": False,
        "pcm_mixer_preserved": True,
        "passthrough_default": False,
        "opus_direct_modes": list(AUDIO_OUTPUT_OPUS_DIRECT_MODES),
        "opus_direct_default": "off",
    }


def opus_passthrough_eligibility(
    agent: Any, track: Any, *, volume_percent: int | float,
    effects: tuple[Any, ...] = (), force_pcm: bool = False,
    state: Any = None, output_bitrate_kbps: int = 0,
) -> dict[str, Any]:
    """Pré-valida a cópia de Opus sem desativar o mixer ou os controles.

    Codec/rate/canais corretos não comprovam cabeçalho e pacotes compatíveis.
    O chamador ainda precisa validar o fluxo antes de encaminhar cada pacote.
    O mixer híbrido mantém PCM pronto para volume, efeitos e overlays TTS.
    """
    settings = validate_audio_output_settings(
        getattr(state, "audio_output_settings", {}) or {},
        {"opus_direct": getattr(agent, "audio_output_default_opus_direct", "off")},
    )
    mode = settings["opus_direct"]
    reasons: list[str] = []
    if mode != "auto":
        reasons.append("disabled")
    if force_pcm:
        reasons.append("pcm_mixer_required")
    if len(getattr(track, "archive_segments", None) or ()) > 1:
        reasons.append("segmented_archive")
    try:
        unity_gain = math.isfinite(float(volume_percent)) and abs(float(volume_percent) - 100) < 0.000001
    except (TypeError, ValueError):
        unity_gain = False
    if not unity_gain:
        reasons.append("volume_requires_pcm")
    if any(effects):
        reasons.append("effects_require_pcm")
    options = " " + str(getattr(agent, "ffmpeg_options", "") or "") + " "
    if any(marker in options for marker in (" -af ", " -af=", " -filter:a", " -filter_complex")):
        reasons.append("custom_audio_filter")
    if str(getattr(track, "audio_codec", "") or "").strip().lower() not in {"opus", "libopus"}:
        reasons.append("codec_not_opus")
    if int(getattr(track, "audio_sample_rate", 0) or 0) != 48000:
        reasons.append("sample_rate_not_48000")
    if int(getattr(track, "audio_channels", 0) or 0) != 2:
        reasons.append("channels_not_stereo")
    if bool(getattr(track, "is_live", False)):
        reasons.append("live_stream")
    if float(getattr(track, "start_offset_seconds", 0) or 0) != 0:
        reasons.append("seek_requires_pcm")
    source_abr = max(0, int(getattr(track, "audio_abr", 0) or 0))
    output_limit = max(0, int(output_bitrate_kbps or 0))
    if output_limit and source_abr > output_limit:
        reasons.append("source_bitrate_limit")
    candidate = not reasons
    # A pré-validação nunca alega cópia ativa. Isso pertence ao source após
    # validar o cabeçalho e os pacotes e é consultado separadamente no status.
    reasons.append("packet_validation_required")
    return {"mode": mode, "eligible": False, "candidate": candidate,
            "reasons": reasons, "required_packet_duration_ms": 20,
            "packet_validation_required": True}


def load_env_file(path: Path, *, override: bool = False) -> None:
    try:
        lines = path.expanduser().read_text("utf-8", errors="replace").splitlines()
    except Exception:
        return
    for line in lines:
        raw = line.strip()
        if not raw or raw.startswith("#") or "=" not in raw:
            continue
        key, value = raw.split("=", 1)
        key = key.strip()
        if not key or not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", key):
            continue
        if override or key not in os.environ:
            os.environ[key] = value.strip().strip('"').strip("'")


def bootstrap_env() -> None:
    worker_dir = Path(os.getenv("PHONE_WORKER_DIR") or Path.home() / "phone-worker").expanduser()
    load_env_file(Path(os.getenv("PHONE_WORKER_ENV") or Path.home() / ".phone-worker.env"), override=False)
    env_file = Path(os.getenv("MUSIC_AGENT_ENV") or worker_dir / "secrets" / "music-agent.env").expanduser()
    load_env_file(env_file, override=False)
    if not str(os.getenv("MUSIC_AGENT_TOKEN") or "").strip():
        token = secrets.token_urlsafe(32)
        os.environ["MUSIC_AGENT_TOKEN"] = token
        try:
            env_file.parent.mkdir(parents=True, exist_ok=True)
            old = env_file.read_text("utf-8", errors="replace") if env_file.exists() else ""
            lines: list[str] = []
            replaced = False
            for line in old.splitlines():
                if re.match(r"^\s*MUSIC_AGENT_TOKEN\s*=", line):
                    if not replaced:
                        lines.append("MUSIC_AGENT_TOKEN=" + token)
                        replaced = True
                    continue
                lines.append(line)
            if not replaced:
                lines.append("MUSIC_AGENT_TOKEN=" + token)
            env_file.write_text("\n".join(lines).rstrip() + "\n", "utf-8")
            with contextlib.suppress(Exception):
                os.chmod(env_file, 0o600)
        except Exception:
            pass


def truthy(value: object, default: bool = False) -> bool:
    if value is None:
        return default
    text = str(value).strip().lower().strip('"\'')
    if not text:
        return default
    if text in {"0", "false", "no", "n", "off", "nao", "não"}:
        return False
    return text in {"1", "true", "yes", "y", "on", "sim"}


def env_int(name: str, default: int) -> int:
    try:
        return int(float(os.getenv(name, str(default))))
    except Exception:
        return default


def env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except Exception:
        return default
