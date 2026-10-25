"""Configuração e bootstrap de ambiente do Music Agent do Phone Worker."""
from __future__ import annotations

import contextlib
import hashlib
import json
import math
import os
import re
import secrets
import shutil
import stat
import tempfile
import threading
import time
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping
from urllib.parse import parse_qs, urlsplit


AUDIO_OUTPUT_PROFILES = {"balanced": None, "quality_192": 192, "quality_256": 256}
AUDIO_OUTPUT_OPUS_DIRECT_MODES = ("off", "auto")
AUDIO_OUTPUT_SETTINGS = frozenset({"profile", "fec", "expected_packet_loss", "opus_direct"})


def prepared_opus_cache_key(track: Any, bitrate_kbps: Any, *, resample_filter: str = "",
                            fec: bool = True, expected_packet_loss: float = 0.15) -> str | None:
    """Immutable media/preparation identity, never a signed playback URL."""
    if (isinstance(bitrate_kbps, bool) or not isinstance(bitrate_kbps, int)
            or not 16 <= bitrate_kbps <= 512 or getattr(track, "is_live", False)):
        return None
    try:
        if (not isinstance(resample_filter, str) or len(resample_filter) > 2048
                or not isinstance(fec, bool) or isinstance(expected_packet_loss, bool)
                or not isinstance(expected_packet_loss, (int, float))
                or not math.isfinite(expected_packet_loss) or not 0 <= expected_packet_loss <= 1):
            return None
        if float(getattr(track, "start_offset_seconds", 0) or 0) != 0:
            return None
        technical = {name: getattr(track, name, None) for name in (
            "audio_format_id", "audio_codec", "audio_ext", "audio_abr", "audio_stream_index",
            "audio_sample_rate", "audio_channels", "duration")}
        identity: dict[str, Any] = {"revision": "opus-prepared-cbr-48k-stereo-20ms-v2",
                                    "bitrate_kbps": bitrate_kbps, "technical": technical,
                                    "resample_filter": resample_filter, "fec": fec,
                                    "packet_loss_percent": int(expected_packet_loss * 100)}
        ref = getattr(track, "archive_ref", None) or getattr(track, "attachment_ref", None)
        if ref:
            if not isinstance(ref, dict):
                return None
            ids = {name: int(ref[name]) for name in ("guild_id", "channel_id", "message_id", "attachment_id")}
            if any(isinstance(ref[name], bool) or not 0 < value < 2 ** 64 for name, value in ids.items()):
                return None
            identity["reference"] = ids
            identity["archive_key"] = str(getattr(track, "archive_key", "") or "")
            identity["manifest_attachment_id"] = ref.get("manifest_attachment_id")
            parts = getattr(track, "archive_segments", ()) or ()
            if not isinstance(parts, (list, tuple)) or len(parts) > 4096:
                return None
            fields = ("order", "guild_id", "channel_id", "message_id", "attachment_id", "sha256",
                      "size_bytes", "filename", "duration", "offset_seconds", "audio_codec",
                      "audio_sample_rate", "audio_channels", "audio_stream_index")
            identity["segments"] = []
            for part in parts:
                if (not isinstance(part, dict)
                        or not re.fullmatch(r"[a-f0-9]{64}", str(part.get("sha256") or ""))):
                    return None
                identity["segments"].append({field: part.get(field) for field in fields})
        else:
            raw = str(getattr(track, "webpage_url", "") or getattr(track, "original_url", "") or "")
            parsed = urlsplit(raw)
            if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                    or parsed.username or parsed.password or len(raw) > 4096):
                return None
            # A general page or /latest.mp3 may serve different audio later.
            # Cache only a recognized video resource; unknown sources still
            # stream through preparation without persistent reuse.
            video_id = ""
            host = parsed.hostname.lower()
            if parsed.port is not None:
                return None
            if host in {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"}:
                if parsed.path.rstrip("/") == "/watch":
                    values = parse_qs(parsed.query).get("v", ())
                    video_id = values[0] if len(values) == 1 else ""
                else:
                    path = parsed.path.strip("/").split("/")
                    if len(path) == 2 and path[0] in {"shorts", "embed", "live"}:
                        video_id = path[1]
            elif host in {"youtu.be", "www.youtu.be"}:
                video_id = parsed.path.strip("/")
            if not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
                return None
            identity["youtube_video_id"] = video_id
        encoded = json.dumps(identity, sort_keys=True, ensure_ascii=True, separators=(",", ":"), allow_nan=False)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    except (AttributeError, KeyError, TypeError, ValueError, OverflowError):
        return None


class PreparedOpusCache:
    """Optional bounded disk cache. Only a validated stream may commit a writer.

    No media bytes are read by get(). Its caller must still validate Ogg/Opus
    while consuming a hit. Cache failures never become playback failures.
    """
    def __init__(self, path: str | Path, *, max_bytes: int = 256 * 1024 * 1024,
                 max_item_bytes: int = 64 * 1024 * 1024, min_free_bytes: int = 64 * 1024 * 1024) -> None:
        self.path = Path(path).expanduser()
        self.max_bytes = max(0, int(max_bytes))
        self.max_item_bytes = max(0, min(self.max_bytes, int(max_item_bytes)))
        self.min_free_bytes = max(0, int(min_free_bytes))
        self._lock = threading.RLock()
        self._writers: set[str] = set()
        self._temporary_names: set[str] = set()
        self._pending_bytes = 0
        self._stored_bytes = 0
        self._last_scan = 0.0
        self._hits = self._misses = 0

    @property
    def enabled(self) -> bool:
        return bool(self.max_bytes and self.max_item_bytes)

    @staticmethod
    def _valid_key(key: Any) -> bool:
        return isinstance(key, str) and re.fullmatch(r"[a-f0-9]{64}", key) is not None

    def _directory(self, *, create: bool = False) -> bool:
        if self.path.is_symlink():
            return False
        if create:
            self.path.mkdir(parents=True, exist_ok=True, mode=0o700)
        return self.path.is_dir() and not self.path.is_symlink()

    def _entries(self) -> list[tuple[float, int, Path]]:
        if not self._directory():
            raise OSError("diretório do cache preparado indisponível")
        entries = []
        self._foreign_temporary_bytes = 0
        with os.scandir(self.path) as directory:
            for count, entry in enumerate(directory):
                if count >= 8192:
                    raise OSError("cache preparado excede o limite de entradas")
                if not entry.is_file(follow_symlinks=False):
                    continue
                info = entry.stat(follow_symlinks=False)
                if re.fullmatch(r"[a-f0-9]{64}\.ogg", entry.name):
                    entries.append((info.st_mtime, info.st_size, Path(entry.path)))
                    continue
                if (not re.fullmatch(r"\.opus-prepare-[A-Za-z0-9_-]+\.tmp", entry.name)
                        or entry.name in self._temporary_names):
                    continue
                owner = re.fullmatch(r"\.opus-prepare-([1-9][0-9]*)-[A-Za-z0-9_-]+\.tmp", entry.name)
                stale = False
                if owner is not None:
                    try:
                        os.kill(int(owner[1]), 0)
                    except ProcessLookupError:
                        stale = True
                    except (OSError, ValueError, OverflowError):
                        pass  # Unknown ownership is not permission to delete.
                elif time.time() - info.st_mtime > 86400:
                    stale = True  # Legacy unowned temporary files, after a day.
                if stale:
                    try:
                        Path(entry.path).unlink()
                        continue
                    except OSError:
                        pass
                # Another live writer or an unknown old revision may own it.
                # Count it against the quota without removing its open stream.
                self._foreign_temporary_bytes += info.st_size
        return entries

    def _prune_locked(self, limit: int) -> None:
        entries = self._entries()
        total = sum(size for _mtime, size, _path in entries) + self._foreign_temporary_bytes
        for _mtime, size, path in sorted(entries):
            if total <= limit:
                break
            try:
                path.unlink()
            except OSError:
                continue
            total -= size
        self._stored_bytes = total
        self._last_scan = time.monotonic()

    def get(self, key: Any) -> Path | None:
        if not self.enabled or not self._valid_key(key):
            return None
        try:
            with self._lock:
                if not self._directory():
                    return None
                if time.monotonic() - self._last_scan > 60:
                    self._prune_locked(max(0, self.max_bytes - self._pending_bytes))
                path = self.path / (key + ".ogg")
                info = path.lstat()
                if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= self.max_item_bytes:
                    return None
                os.utime(path, None, follow_symlinks=False)
                self._hits += 1
                return path
        except (OSError, ValueError):
            pass
        self._misses += 1
        return None

    def writer(self, key: Any) -> Any:
        if not self.enabled or not self._valid_key(key):
            return None
        fd, name, output = None, None, None
        try:
            with self._lock:
                if key in self._writers or not self._directory(create=True):
                    return None
                target = self.path / (key + ".ogg")
                if target.exists() or target.is_symlink():
                    return None
                if shutil.disk_usage(self.path).free < self.min_free_bytes:
                    return None
                self._prune_locked(max(0, self.max_bytes - self._pending_bytes))
                fd, name = tempfile.mkstemp(prefix=f".opus-prepare-{os.getpid()}-", suffix=".tmp", dir=self.path)
                os.fchmod(fd, 0o600)
                output = os.fdopen(fd, "wb")
                fd = None  # fdopen now owns the descriptor.
                self._writers.add(key)
                self._temporary_names.add(Path(name).name)
                return _PreparedOpusCacheWriter(self, key, Path(name), output)
        except (OSError, ValueError):
            if output is not None:
                with contextlib.suppress(OSError):
                    output.close()
            elif fd is not None:
                with contextlib.suppress(OSError):
                    os.close(fd)
            if name is not None:
                with contextlib.suppress(OSError):
                    Path(name).unlink(missing_ok=True)
            return None

    def invalidate(self, key: Any) -> None:
        if not self._valid_key(key):
            return
        with self._lock:
            try:
                if not self._directory():
                    return
                path = self.path / (key + ".ogg")
                info = path.lstat()
                path.unlink()
                if stat.S_ISREG(info.st_mode):
                    self._stored_bytes = max(0, self._stored_bytes - info.st_size)
            except (OSError, ValueError):
                pass

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {"enabled": self.enabled, "max_bytes": self.max_bytes,
                    "max_item_bytes": self.max_item_bytes, "hits": self._hits, "misses": self._misses,
                    "active_writers": len(self._writers)}


class _PreparedOpusCacheWriter:
    def __init__(self, cache: PreparedOpusCache, key: str, temporary: Path, output: Any) -> None:
        self.cache, self.key, self.temporary, self.output = cache, key, temporary, output
        self._lock = threading.RLock()
        self._size = 0
        self._active = True
        self._committed = False
        self._disk_checked = 0.0

    @property
    def active(self) -> bool:
        return self._active

    def write(self, raw: bytes) -> int:
        size = len(raw)
        with self._lock:
            if not self._active:
                return size
            try:
                cache = self.cache
                if self._size + size > cache.max_item_bytes:
                    raise OSError("item preparado excede o orçamento do cache")
                now = time.monotonic()
                if now - self._disk_checked >= 1:
                    if shutil.disk_usage(cache.path).free - size < cache.min_free_bytes:
                        raise OSError("espaço insuficiente para cache opcional")
                    self._disk_checked = now
                with cache._lock:
                    required = cache._stored_bytes + cache._pending_bytes + size
                    if required > cache.max_bytes:
                        cache._prune_locked(max(0, cache.max_bytes - cache._pending_bytes - size))
                    if cache._stored_bytes + cache._pending_bytes + size > cache.max_bytes:
                        raise OSError("cache preparado sem orçamento disponível")
                    cache._pending_bytes += size
                self._size += size
                if self.output.write(raw) != size:
                    raise OSError("gravação parcial do cache preparado")
            except Exception:
                self.abort()
            return size

    def commit(self) -> bool:
        with self._lock:
            if not self._active or not self._size:
                self.abort()
                return self._committed
            try:
                self.output.flush()
                os.fsync(self.output.fileno())
                self.output.close()
                with self.cache._lock:
                    if not self.cache._directory():
                        raise OSError("diretório do cache preparado indisponível")
                    os.replace(self.temporary, self.cache.path / (self.key + ".ogg"))
                    self.cache._pending_bytes = max(0, self.cache._pending_bytes - self._size)
                    self.cache._stored_bytes += self._size
                    self.cache._writers.discard(self.key)
                    self.cache._temporary_names.discard(self.temporary.name)
                self._active, self._committed = False, True
                return True
            except Exception:
                self.abort()
                return False

    def abort(self) -> None:
        with self._lock:
            if not self._active:
                return
            self._active = False
            with contextlib.suppress(Exception):
                self.output.close()
            with contextlib.suppress(OSError):
                self.temporary.unlink(missing_ok=True)
            with self.cache._lock:
                self.cache._pending_bytes = max(0, self.cache._pending_bytes - self._size)
                self.cache._writers.discard(self.key)
                self.cache._temporary_names.discard(self.temporary.name)

    cleanup = abort


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
    # Original VBR packets are not produced by the fallback encoder. Their
    # budget follows the observed channel/configuration capacity, independently
    # of balanced/profile targets. Unknown channel capacity never authorizes
    # forwarding compressed packets.
    copy_bitrate = min(maximum, channel_kbps) if channel_kbps > 0 else 0
    return {
        "profile": profile,
        "bitrate_kbps": max(16, min(512, int(desired))),
        "target_bitrate_kbps": int(target),
        "channel_bitrate_kbps": channel_kbps,
        "copy_bitrate_kbps": copy_bitrate,
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
        "opus_direct_segmented_archive": True,
        "opus_direct_preparation": True,
        "opus_output_processing": True,
        "opus_processed_effects": ["bassboost", "nightcore", "slowed_reverb"],
        "opus_processed_volume_min": 0, "opus_processed_volume_max": 150,
        "original_packet_copy_with_effects": False,
    }


def opus_archive_segments_candidate(track: Any) -> bool:
    """Check the manifest shape; only the packet reader can prove continuity."""
    segments = getattr(track, "archive_segments", None)
    reference = getattr(track, "archive_ref", None)
    if (not isinstance(segments, list) or not 2 <= len(segments) <= 4096
            or not isinstance(reference, dict)
            or not re.fullmatch(r"[a-f0-9]{32}", str(getattr(track, "archive_key", "") or ""))):
        return False
    seen: set[tuple[int, int]] = set()
    offset = 0.0
    try:
        root = {key: int(reference[key]) for key in ("guild_id", "channel_id", "message_id", "attachment_id")}
        if any(isinstance(reference[key], bool) or not 0 < value < 2 ** 64 for key, value in root.items()):
            return False
        for order, segment in enumerate(segments):
            if (not isinstance(segment, dict) or isinstance(segment.get("order"), bool)
                    or segment.get("order") != order
                    or str(segment.get("audio_codec", "")).lower() not in {"opus", "libopus"}
                    or segment.get("audio_sample_rate") != 48000 or segment.get("audio_channels") != 2
                    or not re.fullmatch(r"[a-f0-9]{64}", str(segment.get("sha256") or ""))):
                return False
            ids = {key: int(segment[key]) for key in root}
            identity = (ids["message_id"], ids["attachment_id"])
            if (any(isinstance(segment[key], bool) or not 0 < ids[key] < 2 ** 64 for key in root)
                    or ids["guild_id"] != root["guild_id"] or ids["channel_id"] != root["channel_id"]
                    or identity in seen or (order == 0 and ids != root)):
                return False
            seen.add(identity)
            seconds, start = float(segment["duration"]), float(segment["offset_seconds"])
            size = segment["size_bytes"]
            filename = segment.get("filename")
            if (isinstance(segment["duration"], bool) or isinstance(segment["offset_seconds"], bool)
                    or not math.isfinite(seconds) or seconds <= 0 or not math.isfinite(start)
                    or abs(start - offset) > 0.05 or isinstance(size, bool)
                    or not isinstance(size, int) or size <= 0 or not isinstance(filename, str)
                    or not 0 < len(filename) <= 240 or "/" in filename or "\\" in filename):
                return False
            offset += seconds
        duration = getattr(track, "duration", None)
        if duration is not None and (not math.isfinite(float(duration))
                or abs(float(duration) - offset) > max(0.1, offset * 0.0001)):
            return False
    except (KeyError, TypeError, ValueError, OverflowError):
        return False
    return True


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
    if (len(getattr(track, "archive_segments", None) or ()) > 1
            and not opus_archive_segments_candidate(track)):
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


def opus_preparation_eligibility(
    agent: Any, track: Any, *, volume_percent: int | float,
    effects: tuple[Any, ...] = (), force_pcm: bool = False,
    state: Any = None, output_bitrate_kbps: int = 0,
) -> dict[str, Any]:
    """Allow one preparation pass, preserving every processing/session guard.

    The result authorizes preparation, never claims original-packet copy. AAC,
    mono, other sample rates or legacy archive parts can be decoded by the
    existing source path, then encoded once to the bounded Opus output format.
    """
    valid_capacity = (isinstance(output_bitrate_kbps, int) and not isinstance(output_bitrate_kbps, bool)
                      and 16 <= output_bitrate_kbps <= 512)
    try:
        result = opus_passthrough_eligibility(
            agent, track, volume_percent=volume_percent, effects=effects, force_pcm=force_pcm,
            state=state, output_bitrate_kbps=output_bitrate_kbps if valid_capacity else 0)
    except (TypeError, ValueError, OverflowError):
        return {"mode": "off", "eligible": False, "candidate": False,
                "reasons": ["invalid_metadata", "packet_validation_required"],
                "required_packet_duration_ms": 20, "packet_validation_required": True,
                "preparation_required": True}
    conversion = {"segmented_archive", "codec_not_opus", "sample_rate_not_48000",
                  "channels_not_stereo", "source_bitrate_limit", "packet_validation_required"}
    reasons = [reason for reason in result["reasons"] if reason not in conversion]
    if not valid_capacity:
        reasons.append("channel_bitrate_unknown")
    return {**result, "candidate": not reasons, "reasons": reasons + ["packet_validation_required"],
            "preparation_required": True,
            "prepared_bitrate_kbps": min(output_bitrate_kbps, 510) if valid_capacity else 0}


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
