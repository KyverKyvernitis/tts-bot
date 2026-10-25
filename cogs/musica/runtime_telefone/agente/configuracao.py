"""Configuração e bootstrap de ambiente do Music Agent do Phone Worker."""
from __future__ import annotations

import contextlib
import json
import math
import os
import re
import secrets
import stat
import tempfile
import threading
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping


AUDIO_OUTPUT_PROFILES = {"balanced": None, "quality_192": 192, "quality_256": 256}
AUDIO_OUTPUT_OPUS_DIRECT_MODES = ("off", "auto")
AUDIO_OUTPUT_SETTINGS = frozenset({"profile", "fec", "expected_packet_loss", "opus_direct"})


class OpusDirectPreferenceStore:
    """Small persistent map owned by the single Music Agent command process.

    File reads happen at startup/reload. Audio policy reads an immutable view
    without acquiring the disk-writer lock, even while another guild fsyncs a
    change. An invalid file stays untouched and blocks writes, preserving opt-outs.
    """

    SCHEMA = "music-agent-opus-preferences-v1"
    MAX_BYTES = 256 * 1024
    MAX_GUILDS = 4096

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser()
        self.backup_path = self.path.with_name(self.path.name + ".bak")
        self._lock = threading.RLock()
        self._modes: dict[str, str] = {}
        self._last_raw: bytes | None = None
        self._read_view: tuple[bool, Mapping[str, str]] = (False, MappingProxyType({}))
        self.error_type = ""
        self.load()

    @property
    def ready(self) -> bool:
        return self._read_view[0]

    @staticmethod
    def _guild_key(guild_id: Any) -> str:
        if (isinstance(guild_id, bool) or not isinstance(guild_id, (int, str))
                or not str(guild_id).isdigit()):
            raise ValueError("guild_id inválido para preferências Opus")
        value = int(guild_id)
        if not 0 < value < 2 ** 64:
            raise ValueError("guild_id inválido para preferências Opus")
        return str(value)

    @staticmethod
    def _unique_object(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise ValueError("chave duplicada nas preferências Opus")
            result[key] = value
        return result

    def _read(self) -> tuple[bytes, dict[str, str]]:
        if self.path.is_symlink():
            raise ValueError("arquivo de preferências Opus não pode ser um link")
        fd = os.open(self.path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > self.MAX_BYTES:
                raise ValueError("arquivo de preferências Opus inválido")
            raw = source.read(self.MAX_BYTES + 1)
        if len(raw) > self.MAX_BYTES:
            raise ValueError("arquivo de preferências Opus excede limite")
        payload = json.loads(raw, object_pairs_hook=self._unique_object)
        if (not isinstance(payload, dict) or set(payload) != {"schema", "guilds"}
                or payload["schema"] != self.SCHEMA or not isinstance(payload["guilds"], dict)
                or len(payload["guilds"]) > self.MAX_GUILDS):
            raise ValueError("schema de preferências Opus incompatível")
        modes: dict[str, str] = {}
        for guild, mode in payload["guilds"].items():
            key = self._guild_key(guild)
            if key != guild or mode not in AUDIO_OUTPUT_OPUS_DIRECT_MODES:
                raise ValueError("preferência Opus inválida")
            modes[key] = mode
        return raw, modes

    def load(self) -> dict[str, Any]:
        with self._lock:
            try:
                raw, modes = self._read()
            except FileNotFoundError:
                raw, modes = None, {}
            except Exception as exc:
                self.error_type = type(exc).__name__
                self._read_view = (False, MappingProxyType({}))
                # Keep the last known map for diagnosis, but never use it as
                # proof that a corrupt/replaced file authorizes copy now.
                return self.status()
            self._last_raw, self._modes = raw, modes
            self.error_type = ""
            self._read_view = (True, MappingProxyType(dict(modes)))
            return self.status()

    def read_mode(self, guild_id: Any = None) -> tuple[bool, str | None]:
        """One atomic committed view; never waits for filesystem operations."""
        key = self._guild_key(guild_id) if guild_id is not None else None
        ready, modes = self._read_view
        return ready, modes.get(key) if ready and key is not None else None

    def mode(self, guild_id: Any) -> str | None:
        return self.read_mode(guild_id)[1]

    def snapshot(self) -> dict[str, str]:
        ready, modes = self._read_view
        return dict(modes) if ready else {}

    def status(self, guild_id: Any = None) -> dict[str, Any]:
        key = self._guild_key(guild_id) if guild_id is not None else None
        with self._lock:
            mode = self._modes.get(key) if self.ready and key is not None else None
            return {"persistent": True, "ready": bool(self.ready), "stored": mode is not None,
                    "mode": mode, "error_type": self.error_type}

    @staticmethod
    def _atomic_write(path: Path, raw: bytes) -> None:
        temporary: str | None = None
        try:
            fd, temporary = tempfile.mkstemp(prefix=".opus-preferences-", dir=path.parent)
            with os.fdopen(fd, "wb") as output:
                os.fchmod(output.fileno(), 0o600)
                output.write(raw)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, path)
            temporary = None
            # Replacement has committed; directory fsync is best effort and
            # must not leave the caller's cache pretending the old file won.
            with contextlib.suppress(OSError):
                directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
        finally:
            if temporary is not None:
                with contextlib.suppress(OSError):
                    os.unlink(temporary)

    def set_mode(self, guild_id: Any, mode: str | None) -> None:
        key = self._guild_key(guild_id)
        if mode is not None and mode not in AUDIO_OUTPUT_OPUS_DIRECT_MODES:
            raise ValueError("opus_direct deve ser off ou auto")
        with self._lock:
            if not self.ready:
                raise RuntimeError("preferências Opus indisponíveis; confira o arquivo de estado")
            following = dict(self._modes)
            if mode is None:
                following.pop(key, None)
            else:
                following[key] = mode
            if following == self._modes:
                return
            if len(following) > self.MAX_GUILDS:
                raise ValueError("quantidade de preferências Opus excede limite")
            raw = (json.dumps({"schema": self.SCHEMA, "guilds": following},
                              sort_keys=True, ensure_ascii=True, indent=2) + "\n").encode("utf-8")
            if len(raw) > self.MAX_BYTES:
                raise ValueError("preferências Opus excedem limite")
            published = (True, MappingProxyType(dict(following)))
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if self._last_raw is not None:
                self._atomic_write(self.backup_path, self._last_raw)
            self._atomic_write(self.path, raw)
            self._modes, self._last_raw = following, raw
            self._read_view = published


def effective_opus_direct_mode(agent: Any, state: Any = None, *, guild_id: Any = None) -> str:
    """Resolve a session override, persisted guild mode and installed default."""
    explicit = getattr(state, "audio_output_settings", {}) or {}
    if "opus_direct" in explicit:
        return validate_audio_output_settings({"opus_direct": explicit["opus_direct"]})["opus_direct"]
    store = getattr(agent, "opus_direct_preferences", None)
    if store is not None:
        selected_guild = guild_id if guild_id is not None else getattr(state, "guild_id", None)
        ready, saved = store.read_mode(selected_guild)
        if not ready:
            return "off"
        if saved is not None:
            return saved
    return validate_audio_output_settings({
        "opus_direct": getattr(agent, "audio_output_default_opus_direct", "auto"),
    })["opus_direct"]


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
        "opus_direct": effective_opus_direct_mode(agent, state),
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


def audio_output_capabilities(default_opus_direct: str = "auto") -> dict[str, Any]:
    """Descreve controles públicos, sem prometer recursos da libopus instalada."""
    direct_default = validate_audio_output_settings({"opus_direct": default_opus_direct})["opus_direct"]
    return {
        "profiles": list(AUDIO_OUTPUT_PROFILES),
        "profile_target_kbps": dict(AUDIO_OUTPUT_PROFILES),
        "settings": sorted(AUDIO_OUTPUT_SETTINGS),
        "sample_rate_hz": 48000, "channels": 2, "frame_duration_ms": 20,
        "loss_is_measured": False,
        "pcm_mixer_preserved": True,
        "passthrough_default": direct_default == "auto",
        "opus_direct_modes": list(AUDIO_OUTPUT_OPUS_DIRECT_MODES),
        "opus_direct_default": direct_default,
        "opus_direct_persistent": True,
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
    mode = effective_opus_direct_mode(agent, state)
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
