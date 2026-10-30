from __future__ import annotations

import array
import base64
import contextlib
import hashlib
import io
import importlib.machinery
import importlib.util
import json
import math
import os
import queue
import re
import shlex
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import wave
import zlib
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Mapping

from .cache import FragmentCache
from .errors import TetoConfigurationError, TetoResourceError, TetoSynthesisError
from .phonemizer import phonemize
from .prosody import (
    RenderNote, align_pitch_to_timeline, build_notes, validate_performance,
    performance_hash, align_singing_syllables, align_singing_pitch, _relative_pitch,
    is_default_score, default_melody_score, speech_effect_segments,
)
from .voicebank import OtoEntry, VoicebankIndex


_NUMPY_DSP = None
_NUMPY_CHECKED = False
# Generated from the pinned bridge source, including its portable startup hook.
# Keeping this in an existing runtime member preserves bootstrap archive limits.
_BUNDLED_ESPER_BRIDGE_ZLIB_B64 = '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'


def _numpy_dsp():
    """Use the installed native array backend without adding a dependency."""
    global _NUMPY_DSP, _NUMPY_CHECKED
    if not _NUMPY_CHECKED:
        try:
            import numpy
            _NUMPY_DSP = numpy
        except (ImportError, OSError):
            pass
        _NUMPY_CHECKED = True
    return _NUMPY_DSP


class TetoRenderer:
    SAMPLE_RATE = 44100
    RENDER_VERSION = "speech-4g-cvvc-pauses"
    PHONEMIZER_VERSION = "ptbr-g2p-xsampa-cvvc-v4"
    ENGLISH_PHONEMIZER_VERSION = "en-us-cmudict-xsampa-cvvc-v2"
    FRAGMENT_CACHE_SCHEMA = "teto-fragment-v4-timeline-pitch"
    LEGACY_FRAGMENT_RENDER_VERSION = "speech-3-natural"
    PERFORMANCE_VERSION = "ttv-performance-8-ordered-articulation"
    PERFORMANCE_PROTOCOL = 2
    SINGING_PCM_PROTOCOL = 1
    SUPPORTS_BACKGROUND_CANCELLATION = True
    # This bridge closes/waits for its ESPER supervisor on KeyboardInterrupt.
    # Custom resamplers retain foreground support, without speculative warming.
    BACKGROUND_BRIDGE_SHA256 = '75a256a704afca3b835fd2fc8e273f3a6f488fd37c1f180291992aa2289a5698'
    COMPATIBLE_ESPER_BRIDGES = frozenset({
        "b80dc4c34717611a6f7dd7056dcc6c31f1e59b3b0d660b9fe746773ce16e4f74",
    })

    def __init__(
        self, *, resource_guard: Callable[[], dict[str, Any]] | None = None,
        environment: Mapping[str, str] | None = None, voice_id: str = "kasane-teto",
    ):
        self._configure_voice(environment=environment, voice_id=voice_id)
        self._resource_guard = resource_guard
        self._lock = threading.Lock()
        self._reading_context = threading.local()
        self._english_index_lock = threading.Lock()
        self._english_index: VoicebankIndex | None = None
        self._english_index_path = ""
        self._index: VoicebankIndex | None = None
        self._index_path = ""
        self._index_profile = "standard"
        self._voicebank_fallback_reason = ""
        self._last_status_at = 0.0
        self._last_status: dict[str, Any] = {}
        cache_root = self._getenv("PHONE_WORKER_TETO_FRAGMENT_CACHE_DIR") or str(Path.home() / "phone-worker" / "cache" / ("miku-fragments" if self.voice_id == "hatsune-miku" else "teto-fragments"))
        self._cache = FragmentCache(cache_root, max_mb=self._env_int("PHONE_WORKER_TETO_FRAGMENT_CACHE_MB", 256))
        # A decoded fragment can be reused by many successive short singing
        # windows. Keep immutable native PCM, not a mutable array later edited
        # by gain/envelope/overlap operations.
        self._pcm_cache_lock = threading.RLock()
        self._pcm_cache: OrderedDict[str, tuple[tuple[int, ...], bytes]] = OrderedDict()
        self._pcm_cache_limit = max(0, min(4096, self._env_int('PHONE_WORKER_TETO_PCM_CACHE_MB', 256))) * 1024 * 1024
        self._pcm_cache_bytes = 0
        self._pcm_cache_hits = 0
        self._pcm_cache_misses = 0
        self._bridge_identity_cache = {}
        self._esper_client_cache = None
        self._esper_client_lock = threading.Lock()

    def _configure_voice(self, *, environment: Mapping[str, str] | None, voice_id: str) -> None:
        if voice_id not in {"kasane-teto", "hatsune-miku"}:
            raise ValueError("voz TTV desconhecida")
        if voice_id == "hatsune-miku" and environment is None:
            raise TetoConfigurationError("Hatsune Miku exige configuração independente da Teto")
        self.voice_id = voice_id
        # A per-voice snapshot is authoritative. Falling back to os.environ here
        # could silently render Teto for an incompletely configured Miku request.
        self._environment = MappingProxyType(dict(environment)) if environment is not None else None

    def _getenv(self, name: str, default: Any = None) -> Any:
        return os.getenv(name, default) if self._environment is None else self._environment.get(name, default)

    def _subprocess_environment(self) -> dict[str, str] | None:
        if self._environment is None:
            return None
        # Runtime settings such as PATH and the shared ESPER installation stay
        # available. Explicit per-instance bridge settings override them, while
        # unrelated Teto voice settings never leak into a Miku child process.
        child = {key: value for key, value in os.environ.items()
                 if not key.startswith(("PHONE_WORKER_TETO_", "PHONE_WORKER_MIKU_"))}
        child.update(self._environment)
        return child

    def _alias_suffix(self) -> str:
        return str(self._getenv("PHONE_WORKER_TETO_ALIAS_SUFFIX") or "").strip()

    def _voice_label(self) -> str:
        return "Hatsune Miku" if self.voice_id == "hatsune-miku" else "Teto"

    def _status_voice(self) -> str:
        profile = "english-cvvc" if self._index_profile == "english-cvvc" else "standard"
        return f"{self.voice_id}-{profile}"

    def _phonemizer_options(self) -> dict[str, str]:
        return {"inventory_profile": "miku-v4-english"} if self.voice_id == "hatsune-miku" else {}

    @property
    def _index_profile(self) -> str:
        return getattr(self._reading_context, "profile", self._default_index_profile)

    @_index_profile.setter
    def _index_profile(self, profile: str) -> None:
        self._default_index_profile = profile

    @staticmethod
    def _effective_reading_language(requested: str | None) -> str:
        if requested is None:
            return "pt-BR"
        if isinstance(requested, str):
            language = requested.strip().lower().replace("_", "-")
            if language in {"pt", "pt-br"}:
                return "pt-BR"
            if language in {"en", "en-us"}:
                return "en-US"
        raise ValueError("idioma TTV deve ser Português adaptado ou Inglês")

    def _load_english_index(self) -> VoicebankIndex:
        path = Path(self._english_voicebank_dir()).expanduser()
        if not path.is_dir():
            raise TetoConfigurationError(f"Inglês exige a voicebank English-CVVC da {self._voice_label()} instalada")
        resolved = str(path.resolve())
        with self._english_index_lock:
            if self._english_index is None or self._english_index_path != resolved:
                minimum = max(1, self._env_int("PHONE_WORKER_TETO_ENGLISH_MIN_ALIASES", 500))
                try:
                    index = VoicebankIndex.load(resolved, minimum_aliases=minimum,
                                                alias_suffix=self._alias_suffix(),
                                                case_sensitive=self.voice_id == "hatsune-miku")
                except Exception as exc:
                    raise TetoConfigurationError(f"voicebank English-CVVC indisponível: {exc}") from exc
                self._english_index = index
                self._english_index_path = resolved
            return self._english_index

    def _load_reading_index(self, language: str) -> VoicebankIndex:
        if language == "pt-BR":
            return self._load_index()
        index = self._load_english_index()
        # The English choice belongs to this request, not the global bank mode.
        # Status probes on another thread keep the configured Portuguese bank.
        self._reading_context.profile = "english-cvvc"
        return index

    def reading_languages(self) -> list[str]:
        languages = ["pt-BR"]
        try:
            self._load_english_index()
        except TetoConfigurationError:
            pass
        else:
            languages.append("en-US")
        return languages

    def singing_fingerprint(self, *, reading_language="pt-BR") -> str:
        """Music depends on the English-CVVC bank even for Portuguese lyrics."""
        if self.RENDER_VERSION.startswith('worldline'):
            return 'unavailable'
        try:
            index = self._load_english_index()
            self._reading_context.profile = "english-cvvc"
            language = self._effective_reading_language(reading_language)
            return hashlib.sha256((self._reading_fingerprint(index, language)
                + self.PERFORMANCE_VERSION).encode()).hexdigest()
        except TetoConfigurationError:
            return "unavailable"
        finally:
            self._clear_reading_context()

    def _clear_reading_context(self) -> None:
        for name in ("profile", "render_metrics", "performance_sources", "performance_singing", "cancelled"):
            if hasattr(self._reading_context, name):
                delattr(self._reading_context, name)

    def _record_render_metric(self, name: str) -> None:
        # Each source group owns its counters. Status probes and other render
        # threads do not share mutable request metrics.
        metrics = getattr(self._reading_context, "render_metrics", None)
        if metrics is not None:
            metrics[name] += 1

    def _env_bool(self, name: str, default: bool = False) -> bool:
        value = str(self._getenv(name, "") or "").strip().lower()
        if value in {"1", "true", "yes", "on", "sim"}:
            return True
        if value in {"0", "false", "no", "off", "nao", "não"}:
            return False
        return default

    def _env_int(self, name: str, default: int) -> int:
        try:
            return int(str(self._getenv(name, default)).strip())
        except (TypeError, ValueError):
            return default

    def _voicebank_dir(self) -> str:
        return str(self._getenv("PHONE_WORKER_TETO_VOICEBANK_DIR") or "").strip()

    def _english_voicebank_dir(self) -> str:
        configured = str(self._getenv("PHONE_WORKER_TETO_ENGLISH_VOICEBANK_DIR") or "").strip()
        if configured:
            return configured
        if self.voice_id == "hatsune-miku":
            return self._voicebank_dir()
        return str(Path.home() / "voicebanks" / "kasane-teto-english")

    def _voicebank_mode(self) -> str:
        mode = str(self._getenv("PHONE_WORKER_TETO_VOICEBANK_MODE") or "auto").strip().lower()
        if self.voice_id == "hatsune-miku":
            return "english"
        return mode if mode in {"auto", "english", "standard"} else "auto"

    def _voicebank_candidates(self) -> list[tuple[str, str, int]]:
        mode = self._voicebank_mode()
        standard = self._voicebank_dir()
        english = self._english_voicebank_dir()
        english_min = max(1, self._env_int("PHONE_WORKER_TETO_ENGLISH_MIN_ALIASES", 500))
        standard_min = max(1, self._env_int("PHONE_WORKER_TETO_MIN_ALIASES", 10))
        candidates: list[tuple[str, str, int]] = []
        if mode in {"auto", "english"} and english:
            candidates.append((english, "english-cvvc", english_min))
        if mode in {"auto", "standard"} and standard:
            candidates.append((standard, "standard", standard_min))
        return candidates

    def _speech_rate(self) -> float:
        try:
            value = float(self._getenv("PHONE_WORKER_TETO_SPEECH_RATE", "1.0"))
            return max(0.75, min(1.5, value)) if math.isfinite(value) else 1.0
        except (TypeError, ValueError):
            return 1.0

    def _effective_speech_rate(self, requested: float | None) -> float:
        if requested is None:
            return self._speech_rate()
        if isinstance(requested, bool) or not isinstance(requested, (int, float)):
            raise ValueError("velocidade TTV deve estar entre 0.5 e 1.5")
        try:
            value = float(requested)
        except (ValueError, OverflowError) as exc:
            raise ValueError("velocidade TTV deve estar entre 0.5 e 1.5") from exc
        if not math.isfinite(value) or not 0.5 <= value <= 1.5:
            raise ValueError("velocidade TTV deve estar entre 0.5 e 1.5")
        return value

    def _velocity(self) -> int:
        return max(1, min(200, self._env_int("PHONE_WORKER_TETO_VELOCITY", 100)))

    def _modulation(self) -> int:
        if getattr(self._reading_context, "performance_singing", False):
            # Source F0 modulation can pull a sung note hundreds of cents away
            # from its score. Musical expression comes from our shared curve.
            return 0
        return max(0, min(100, self._env_int("PHONE_WORKER_TETO_MODULATION", 15)))

    def _length_mode(self) -> str:
        mode = str(self._getenv("PHONE_WORKER_TETO_LENGTH_MODE", "auto")).strip().lower()
        if mode in {"total", "post-consonant"}:
            return mode
        executable = Path(self._resampler_command()[0]).name.lower()
        return "post-consonant" if executable.startswith("straycat") else "total"

    def _render_fingerprint_for_version(self, index: VoicebankIndex, version: str) -> str:
        # Whole-utterance caches must change when the prosody implementation
        # changes, even when most low-level fragments remain reusable.
        command = self._resampler_command()
        executable = Path(shutil.which(command[0]) or command[0])
        stamp = executable.stat()
        profile = {
            "version": str(version),
            "voicebank": index.fingerprint,
            "resampler": command,
            "resampler_stamp": [stamp.st_size, stamp.st_mtime_ns],
            "pitch": self._getenv("PHONE_WORKER_TETO_BASE_PITCH", "C4"),
            "rate": self._speech_rate(),
            "velocity": self._velocity(),
            "modulation": self._modulation(),
            "flags": self._getenv("PHONE_WORKER_TETO_FLAGS", ""),
            "tempo": max(60, min(240, self._env_int("PHONE_WORKER_TETO_TEMPO", 140))),
            "length_mode": self._length_mode(),
        }
        if self.voice_id == "hatsune-miku":
            profile.update(voice_id=self.voice_id, inventory_profile="miku-v4-english")
        return hashlib.sha256(json.dumps(profile, sort_keys=True).encode()).hexdigest()

    def _render_fingerprint(self, index: VoicebankIndex) -> str:
        return self._render_fingerprint_for_version(index, self.RENDER_VERSION)

    def _reading_fingerprint(self, index: VoicebankIndex, language: str) -> str:
        renderer = self._render_fingerprint(index)
        source_hash = getattr(self, "_english_phonemizer_sha", "")
        if not source_hash:
            digest = hashlib.sha256()
            for name in ("phonemizer.py", "ptbr_g2p.py"):
                digest.update(name.encode("ascii"))
                digest.update(Path(__file__).with_name(name).read_bytes())
            source_hash = digest.hexdigest()
            self._english_phonemizer_sha = source_hash
        return hashlib.sha256(json.dumps({
            "renderer": renderer, "reading_language": language,
            "phonemizer": (self.PHONEMIZER_VERSION if language == "pt-BR"
                           else self.ENGLISH_PHONEMIZER_VERSION),
            "phonemizer_sha256": source_hash,
        }, sort_keys=True).encode()).hexdigest()

    def _request_fingerprint(
        self, index: VoicebankIndex, *, pitch_offset_semitones: float,
        speech_rate: float, character: str | None = None, reading_language: str = "pt-BR",
    ) -> str:
        # Status describes the installed renderer. A rendered utterance also
        # depends on the user's controls, captured locally for this request.
        profile = {
            "renderer": self._reading_fingerprint(index, reading_language),
            "character": character if character is not None else self.voice_id,
            "pitch_offset_semitones": pitch_offset_semitones,
            "speech_rate": speech_rate,
        }
        if reading_language == "en-US":
            profile.update(reading_language=reading_language, phonemizer=self.ENGLISH_PHONEMIZER_VERSION)
        return hashlib.sha256(json.dumps(profile, sort_keys=True).encode()).hexdigest()

    def _fragment_fingerprint(self, index: VoicebankIndex) -> str:
        # Fragment identity is intentionally independent from the high-level
        # prosody revision. duration/pitch/pitchbend are already part of each
        # fragment key. This prevents a quality-only update from making every
        # Straycat fragment cold at once.
        command = self._resampler_command()
        executable = Path(shutil.which(command[0]) or command[0])
        stamp = executable.stat()
        profile = {
            "schema": self.FRAGMENT_CACHE_SCHEMA,
            "voicebank": index.fingerprint,
            "resampler": command,
            "resampler_stamp": [stamp.st_size, stamp.st_mtime_ns],
            "velocity": self._velocity(),
            "modulation": self._modulation(),
            "flags": self._getenv("PHONE_WORKER_TETO_FLAGS", ""),
            "length_mode": self._length_mode(),
        }
        return hashlib.sha256(json.dumps(profile, sort_keys=True).encode()).hexdigest()

    def _resampler_command(self) -> list[str]:
        raw = str(self._getenv("PHONE_WORKER_TETO_RESAMPLER_COMMAND") or "").strip()
        if not raw:
            raise TetoConfigurationError("PHONE_WORKER_TETO_RESAMPLER_COMMAND não configurado")
        command = shlex.split(raw)
        if not command:
            raise TetoConfigurationError("comando do resampler vazio")
        executable = command[0]
        if os.path.sep in executable:
            if not Path(executable).expanduser().is_file():
                raise TetoConfigurationError(f"resampler não encontrado: {executable}")
            command[0] = str(Path(executable).expanduser())
        elif not shutil.which(executable):
            raise TetoConfigurationError(f"executável do resampler não encontrado: {executable}")
        return command

    def _verified_esper_bridge(self, command=None):
        """Recognize only the shipped bridge, following real file versions."""
        try:
            for candidate in (command or self._resampler_command())[:2]:
                path = Path(candidate).expanduser()
                try:
                    info = path.stat()
                except OSError:
                    continue
                if not path.is_file() or not 1 <= info.st_size <= 128 * 1024:
                    continue
                stamp = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
                key = str(path.absolute())
                cached = self._bridge_identity_cache.get(key)
                if cached is None or cached[0] != stamp:
                    raw = path.read_bytes()
                    after = path.stat()
                    if (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns) != stamp:
                        continue
                    digest = hashlib.sha256(raw).hexdigest()
                    cached = (stamp, digest == self.BACKGROUND_BRIDGE_SHA256 or
                              bool(_BUNDLED_ESPER_BRIDGE_ZLIB_B64) and digest in self.COMPATIBLE_ESPER_BRIDGES)
                    self._bridge_identity_cache[key] = cached
                    while len(self._bridge_identity_cache) > 4:
                        self._bridge_identity_cache.pop(next(iter(self._bridge_identity_cache)))
                if cached[1]:
                    return path.absolute(), stamp
        except (OSError, TetoConfigurationError):
            pass
        return None

    def supports_background_cancellation(self) -> bool:
        return not self.RENDER_VERSION.startswith('worldline') and self._verified_esper_bridge() is not None

    def _esper_inprocess_client(self, command):
        """Load a trusted bridge in-process; custom commands retain subprocesses."""
        verified = self._verified_esper_bridge(command)
        if verified is None:
            return None
        path, stamp = verified
        position = next((number for number, value in enumerate(command[:2])
                         if Path(value).expanduser().absolute() == path), None)
        if position is None:
            return None
        configured_source = path.read_bytes()
        configured_digest = hashlib.sha256(configured_source).hexdigest()
        current = path.stat()
        if (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns, current.st_ctime_ns) != stamp:
            return None
        allowed = {'--root', '--cache-dir', '--container', '--timeout', '--implementation-id'}
        values, tokens, cursor = {}, command[position + 1:], 0
        while cursor < len(tokens):
            name, equal, value = tokens[cursor].partition('=')
            if name not in allowed or name in values:
                return None
            cursor += 1
            if not equal:
                if cursor >= len(tokens):
                    return None
                value = tokens[cursor]
                cursor += 1
            values[name] = value
        if values.get('--implementation-id', configured_digest).lower() != configured_digest:
            return None
        environment = self._subprocess_environment()
        environment = os.environ if environment is None else environment
        root = Path(values.get('--root') or environment.get('PHONE_WORKER_ESPER_ROOT') or Path.home() / '.esper-utau').expanduser()
        cache = Path(values.get('--cache-dir') or environment.get('PHONE_WORKER_ESPER_CACHE_DIR') or root / 'cache').expanduser()
        container = values.get('--container') or environment.get('PHONE_WORKER_ESPER_CONTAINER') or 'voicepeak-arm64'
        timeout = values.get('--timeout') or environment.get('PHONE_WORKER_ESPER_TIMEOUT') or '60'
        identity = (str(path), stamp, str(root), str(cache), container, timeout,
                    self.BACKGROUND_BRIDGE_SHA256)
        with self._esper_client_lock:
            saved = self._esper_client_cache
            if saved is not None and saved[0] == identity:
                return saved[1]
            source_directory = None
            source = configured_source
            if configured_digest != self.BACKGROUND_BRIDGE_SHA256:
                if configured_digest not in self.COMPATIBLE_ESPER_BRIDGES:
                    return None
                try:
                    source = zlib.decompress(base64.b64decode(
                        _BUNDLED_ESPER_BRIDGE_ZLIB_B64, validate=True))
                except (ValueError, zlib.error):
                    return None
                if (not 1 <= len(source) <= 128 * 1024
                        or hashlib.sha256(source).hexdigest() != self.BACKGROUND_BRIDGE_SHA256):
                    return None
                source_directory = tempfile.TemporaryDirectory(prefix='phone-worker-esper-runtime-')
                path = Path(source_directory.name) / 'resampler.py'
                path.write_bytes(source)
            elif hashlib.sha256(source).hexdigest() != self.BACKGROUND_BRIDGE_SHA256:
                return None
            name = '_ttv_verified_esper_' + self.BACKGROUND_BRIDGE_SHA256
            loader = importlib.machinery.SourceFileLoader(name, str(path))
            spec = importlib.util.spec_from_loader(name, loader)
            module = importlib.util.module_from_spec(spec)
            # Execute exactly the bytes just verified, never reread a mutable
            # pathname through the import loader after checking its digest.
            exec(compile(source, str(path), 'exec'), module.__dict__)
            factory = getattr(module, 'InProcessEsper', None)
            if factory is None:
                self._esper_client_cache = (identity, None)
                return None
            client = factory(root=root, cache=cache, container=container, timeout=float(timeout))
            if source_directory is not None:
                client._bundled_source_directory = source_directory
            if saved is not None and saved[1] is not None:
                close = getattr(saved[1], 'close', None)
                if callable(close):
                    close()
            self._esper_client_cache = (identity, client)
            return client

    def _load_index(self) -> VoicebankIndex:
        candidates = self._voicebank_candidates()
        if not candidates:
            raise TetoConfigurationError("nenhuma voicebank Teto configurada")
        errors: list[str] = []
        for configured, profile, minimum_aliases in candidates:
            resolved_path = Path(configured).expanduser()
            if not resolved_path.is_dir():
                errors.append(f"{profile}: ausente ({resolved_path})")
                continue
            resolved = str(resolved_path.resolve())
            if self._index is not None and self._index_path == resolved:
                self._index_profile = profile
                self._voicebank_fallback_reason = "; ".join(errors)[:420]
                return self._index
            try:
                index = VoicebankIndex.load(resolved, minimum_aliases=minimum_aliases,
                                            alias_suffix=self._alias_suffix(),
                                            case_sensitive=self.voice_id == "hatsune-miku")
            except Exception as exc:
                errors.append(f"{profile}: {type(exc).__name__}: {exc}")
                if self._voicebank_mode() != "auto":
                    raise
                continue
            self._index = index
            self._index_path = resolved
            self._index_profile = profile
            self._voicebank_fallback_reason = "; ".join(errors)[:420]
            return index
        detail = "; ".join(errors) or "nenhuma voicebank encontrada"
        raise TetoConfigurationError(detail[:420])

    def status(self, *, force: bool = False) -> dict[str, Any]:
        now = time.monotonic()
        ttl = max(1, self._env_int("PHONE_WORKER_TETO_STATUS_CACHE_SECONDS", 15))
        if not force and self._last_status and now - self._last_status_at <= ttl:
            return dict(self._last_status)
        enabled = self._env_bool("PHONE_WORKER_TETO_ENABLED", False)
        result: dict[str, Any] = {
            "ok": False,
            "available": False,
            "ready": False,
            "enabled": enabled,
            "engine": "teto",
            "voice_id": self.voice_id,
            "voice": f"{self.voice_id}-standard",
            "voicebank_profile": "standard",
        }
        if not enabled:
            result["last_error"] = "PHONE_WORKER_TETO_ENABLED=false"
        else:
            try:
                command = self._resampler_command()
                if not shutil.which("ffmpeg"):
                    raise TetoConfigurationError("ffmpeg não encontrado")
                index = self._load_index()
                result.update(index.snapshot())
                result.update({
                    "voice": self._status_voice(),
                    "voicebank_profile": self._index_profile,
                    "voicebank_mode": self._voicebank_mode(),
                    "voicebank_fallback": bool(self._voicebank_fallback_reason),
                    "voicebank_fallback_reason": self._voicebank_fallback_reason,
                    "ok": True,
                    "available": True,
                    "ready": True,
                    "resampler": " ".join(command[:2]),
                    "voicebank_fingerprint": index.fingerprint,
                    "fingerprint": self._render_fingerprint(index),
                    "renderer_version": self.RENDER_VERSION,
                    "phonemizer_version": self.PHONEMIZER_VERSION,
                    "pitch_alignment_mode": "absolute-timeline" if self._index_profile == "english-cvvc" else "lexical",
                    "speech_rate": self._speech_rate(),
                    "ttv_languages": self.reading_languages(),
                    "last_error": "",
                })
            except Exception as exc:
                result["last_error"] = f"{type(exc).__name__}: {exc}"[:220]
        self._last_status_at = now
        self._last_status = dict(result)
        return result

    def fingerprint(self, *, reading_language: str | None = None) -> str:
        try:
            language = self._effective_reading_language(reading_language)
            return self._reading_fingerprint(self._load_reading_index(language), language)
        except Exception:
            return "unavailable"
        finally:
            self._clear_reading_context()

    def _check_resources(self) -> None:
        if self._resource_guard is None:
            return
        snapshot = self._resource_guard() or {}
        if not snapshot.get("ok", False):
            raise TetoResourceError(str(snapshot.get("reason") or "recursos insuficientes para Teto"))

    @staticmethod
    def _format_number(value: float) -> str:
        return f"{float(value):.3f}".rstrip("0").rstrip(".") or "0"

    def _english_lead_ms(self, entry: OtoEntry, note: RenderNote) -> float:
        """Amount of OTO preutterance that must live *before* the note anchor.

        speech-4B rendered every alias as a self-contained block and only
        crossfaded the finished WAVs.  A CVVC/VCV oto is authored differently:
        preutterance belongs before the lexical note boundary.  Keeping this
        lead inside the rendered fragment lets the compositor place the sample
        on an UTAU-like timeline instead of serializing consonant attacks.
        """
        if self._index_profile != "english-cvvc":
            return 0.0
        stretch = 2.0 ** (1.0 - self._velocity() / 100.0)
        preutterance = max(0.0, float(entry.preutterance_ms)) * stretch
        overlap = max(0.0, float(entry.overlap_ms)) * stretch
        if note.role == "nucleus":
            cap = 180.0
        elif note.role in {"transition", "vowel-exit", "cluster"}:
            cap = 120.0
        elif note.role in {"coda", "glide", "nasal"}:
            cap = 90.0
        else:
            cap = 70.0
        return min(cap, max(preutterance, overlap))

    def _english_render_entry(self, entry: OtoEntry, note: RenderNote) -> OtoEntry:
        """Remove excess recorded vowel lead before resampling, not from PCM.

        English VC recordings can put the consonant 250 ms into a sample.
        Capping only the timeline lead would place it late and then clip it.
        Move the source offset by the discarded lead, preserving the absolute
        fixed-region and cutoff endpoints. Pitchbend time zero then matches the
        actual trimmed sample start, including at non-default UTAU velocity.
        """
        if self._index_profile != "english-cvvc":
            return entry
        stretch = 2.0 ** (1.0 - self._velocity() / 100.0)
        raw_lead = max(0.0, entry.preutterance_ms, entry.overlap_ms)
        trim_ms = max(0.0, raw_lead - self._english_lead_ms(entry, note) / stretch)
        if trim_ms < 0.001:
            return entry
        cutoff = entry.cutoff_ms
        if cutoff < 0:
            cutoff += trim_ms
            if cutoff >= 0:
                raise TetoSynthesisError(f"OTO sem região útil após preutterance: {entry.alias!r}")
        return replace(
            entry,
            offset_ms=entry.offset_ms + trim_ms,
            consonant_ms=max(0.0, entry.consonant_ms - trim_ms),
            preutterance_ms=max(0.0, entry.preutterance_ms - trim_ms),
            overlap_ms=max(0.0, entry.overlap_ms - trim_ms),
            cutoff_ms=cutoff,
        )

    def _resampler_length(self, entry: OtoEntry, note: RenderNote) -> float:
        # For English CVVC the output needs to contain the preutterance region
        # *and* the lexical duration after the anchor.  Without this budget a
        # VCV fragment placed early on the timeline ends early as well, which
        # sounds like a series of clipped/restarted syllables.
        target_duration = float(note.duration_ms) + self._english_lead_ms(entry, note)
        if self._length_mode() != "post-consonant":
            return target_duration
        # Straycat adds its consonantal region to LENGTH. WORLD uses 5 ms
        # frames, so budget that region before requesting the vowel duration.
        stretch = 2.0 ** (1.0 - self._velocity() / 100.0)
        consonant = max(0.0, entry.consonant_ms) * stretch
        consonant = math.floor(consonant / 5.0) * 5.0
        duration = math.ceil(target_duration / 5.0) * 5.0
        return max(5.0, duration - consonant)

    def _native_wav(self, path: Path) -> bool:
        try:
            with wave.open(str(path), "rb") as wav:
                return (wav.getnchannels() == 1 and wav.getsampwidth() == 2
                        and wav.getframerate() == self.SAMPLE_RATE
                        and wav.getcomptype() == "NONE" and wav.getnframes() > 0)
        except (OSError, EOFError, wave.Error):
            return False

    def _fragment_cache_payload(
        self, *, fingerprint: str, entry: OtoEntry, note: RenderNote
    ) -> str:
        if getattr(self._reading_context, "performance_sources", None) is not None:
            # Existing fragments can contain the old bridge's copied FRQ or
            # wrapped PCM. Isolate only the new presentation path; ordinary
            # speech keeps every previous cache identity.
            fingerprint += "|ttv-performance-native-pitch-v4-volume50"
        return "|".join((
            fingerprint,
            entry.cache_identity(),
            note.pitch,
            note.pitchbend,
            str(note.duration_ms),
            note.role,
            self._format_number(self._english_lead_ms(entry, note)),
            str(self._velocity()),
            str(self._modulation()),
            str(self._getenv("PHONE_WORKER_TETO_FLAGS") or ""),
        ))

    def _resample_note(
        self, *, index: VoicebankIndex, entry: OtoEntry, note: RenderNote, workdir: Path, deadline: float
    ) -> tuple[Path, bool, bool]:
        self._check_render_cancelled()
        payload = self._fragment_cache_payload(
            fingerprint=self._fragment_fingerprint(index), entry=entry, note=note
        )
        key = self._cache.key(payload)
        cached = self._cache.get(key)
        if cached is not None:
            self._record_render_metric("fragment_cache_hits")
            return cached, False, False

        # Compatibility lookup for the stable speech-3 fragment cache. 3B
        # changes the whole-utterance fingerprint but must not force every
        # source through a cold WORLD/Straycat render again.
        legacy_payload = self._fragment_cache_payload(
            fingerprint=self._render_fingerprint_for_version(index, self.LEGACY_FRAGMENT_RENDER_VERSION),
            entry=entry,
            note=note,
        )
        legacy_cached = self._cache.get(self._cache.key(legacy_payload))
        if legacy_cached is not None:
            self._record_render_metric("fragment_cache_hits")
            return legacy_cached, False, True

        self._record_render_metric("fragment_cache_misses")

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("tempo da renderização Teto esgotado")
        raw_output = workdir / f"{key}.raw.wav"
        normalized_output = workdir / f"{key}.wav"

        def run_resampler(pitchbend: str) -> tuple[subprocess.CompletedProcess[bytes], list[str]]:
            source = entry.wav_path
            performance_sources = getattr(self._reading_context, "performance_sources", None)
            resampler_command = self._resampler_command()
            bridge = self._esper_inprocess_client(resampler_command)
            if performance_sources is not None and self._verified_esper_bridge(resampler_command) is None:
                # The legacy bridge discovers FRQ by adjacent filename. Give
                # it a private WAV without the original FRQ; its own analysis
                # cache is content-addressed and therefore remains reusable
                # even though this staging directory is request-scoped.
                source_key = hashlib.sha256(str(source).encode()).hexdigest()
                private_source = performance_sources / (source_key + ".wav")
                if not private_source.exists():
                    if source.stat().st_size > 64 * 1024 * 1024:
                        raise TetoSynthesisError("fonte musical grande demais")
                    shutil.copyfile(source, private_source)
                source = private_source
            command = resampler_command + [
                str(source),
                str(raw_output),
                note.pitch,
                str(self._velocity()),
                str(self._getenv("PHONE_WORKER_TETO_FLAGS") or ""),
                self._format_number(entry.offset_ms),
                self._format_number(self._resampler_length(entry, note)),
                self._format_number(entry.consonant_ms),
                self._format_number(entry.cutoff_ms),
                # ESPER 2.5 can wrap overflowing PCM16 samples internally.
                # Headroom must be applied before that irreversible writer.
                "50" if performance_sources is not None else "100",
                str(self._modulation()),
                f"!{max(60, min(240, self._env_int('PHONE_WORKER_TETO_TEMPO', 140)))}",
                pitchbend,
            ]
            self._record_render_metric("resampler_calls")
            cancelled = getattr(self._reading_context, "cancelled", None)
            if bridge is not None:
                try:
                    result = bridge.synthesize(command[-13:], deadline=deadline,
                                               cancelled=cancelled, environment=self._subprocess_environment())
                    proc = subprocess.CompletedProcess(command, 0,
                        stdout=json.dumps(result, ensure_ascii=False, allow_nan=False).encode(), stderr=b'')
                except Exception as error:
                    self._check_render_cancelled()
                    if isinstance(error, TimeoutError):
                        raise
                    proc = subprocess.CompletedProcess(command, 1, stdout=b'',
                        stderr=f'{type(error).__name__}: {error}'.encode('utf-8', errors='replace'))
            elif cancelled is None:
                proc = subprocess.run(
                    command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    timeout=max(0.5, deadline - time.monotonic()), check=False,
                    env=self._subprocess_environment(),
                )
            else:
                proc = self._run_cancellable_resampler(command, deadline, cancelled)
            return proc, command

        proc, _ = run_resampler(note.pitchbend)
        pitchbend_fallback = False
        primary_error = proc.stderr.decode("utf-8", errors="replace")[-500:]
        primary_ok = proc.returncode == 0 and raw_output.is_file() and raw_output.stat().st_size > 44

        if not primary_ok and note.pitchbend != "AA" and time.monotonic() < deadline:
            # A quality curve is never allowed to knock the whole engine down to
            # gTTS. Retry this single fragment with neutral UTAU pitchbend. The
            # neutral retry is intentionally not persisted under the expressive
            # cache key, so a future compatible renderer can try the curve again.
            with contextlib.suppress(OSError):
                raw_output.unlink()
            proc, _ = run_resampler("AA")
            pitchbend_fallback = True

        if proc.returncode != 0 or not raw_output.is_file() or raw_output.stat().st_size <= 44:
            retry_error = proc.stderr.decode("utf-8", errors="replace")[-500:]
            detail = retry_error or primary_error or str(proc.returncode)
            raise TetoSynthesisError(f"resampler falhou para {entry.alias!r}: {detail}")

        if self._native_wav(raw_output):
            if pitchbend_fallback:
                return raw_output, True, False
            return self._cache.put(key, raw_output), False, False

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("tempo da normalização Teto esgotado")
        ffmpeg = subprocess.run(
            [
                "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
                "-i", str(raw_output), "-ac", "1", "-ar", str(self.SAMPLE_RATE),
                "-c:a", "pcm_s16le", str(normalized_output),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=max(0.5, remaining),
            check=False,
            env=self._subprocess_environment(),
        )
        if ffmpeg.returncode != 0 or not normalized_output.is_file() or normalized_output.stat().st_size <= 44:
            error = ffmpeg.stderr.decode("utf-8", errors="replace")[-500:]
            raise TetoSynthesisError(f"ffmpeg não normalizou fragmento: {error or ffmpeg.returncode}")
        if pitchbend_fallback:
            return normalized_output, True, False
        return self._cache.put(key, normalized_output), False, False

    def _check_render_cancelled(self) -> None:
        cancelled = getattr(self._reading_context, "cancelled", None)
        if cancelled is not None and cancelled():
            raise TetoResourceError("renderização TTV cancelada")

    def _run_cancellable_resampler(self, command, deadline, cancelled):
        """Interrupt the Python bridge gracefully, retaining its physical lease.

        The ESPER bridge catches KeyboardInterrupt around its supervisor, closes
        the control pipe and waits for guest cleanup before exiting. SIGKILL
        would let the caller observe bridge exit before that cleanup completes.
        """
        if cancelled():
            raise TetoResourceError("renderização TTV cancelada")
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   env=self._subprocess_environment(), start_new_session=True)
        interrupted = False
        try:
            while True:
                if cancelled() and not interrupted:
                    interrupted = True
                    with contextlib.suppress(ProcessLookupError):
                        process.send_signal(signal.SIGINT)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(command, 0)
                try:
                    stdout, stderr = process.communicate(timeout=min(0.1, remaining))
                    if interrupted or cancelled():
                        raise TetoResourceError("renderização TTV cancelada")
                    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
                except subprocess.TimeoutExpired:
                    continue
        finally:
            if process.poll() is None:
                if not interrupted:
                    with contextlib.suppress(ProcessLookupError):
                        process.send_signal(signal.SIGINT)
                try:
                    process.communicate(timeout=2.0)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.communicate()
            for pipe in (process.stdout, process.stderr):
                if pipe is not None:
                    pipe.close()

    def _read_samples(self, path: Path) -> array.array:
        path = Path(path)
        key = str(path.expanduser().absolute())
        def signature(info):
            return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
        try:
            stamp = signature(path.stat())
        except OSError:
            with self._pcm_cache_lock:
                self._drop_pcm_cache_entry(key)
            raise
        with self._pcm_cache_lock:
            saved = self._pcm_cache.get(key)
            if saved is not None and saved[0] == stamp:
                self._pcm_cache.move_to_end(key)
                self._pcm_cache_hits += 1
                pcm = saved[1]
            else:
                self._drop_pcm_cache_entry(key)
                self._pcm_cache_misses += 1
                pcm = None
        if pcm is None:
            with path.open('rb') as source:
                opened = signature(os.fstat(source.fileno()))
                with wave.open(source, "rb") as wav:
                    if (wav.getnchannels() != 1 or wav.getsampwidth() != 2
                            or wav.getframerate() != self.SAMPLE_RATE or wav.getcomptype() != 'NONE'):
                        raise TetoSynthesisError(f"fragmento WAV inesperado: {path.name}")
                    frames = wav.getnframes()
                    pcm = wav.readframes(frames)
                    if len(pcm) != frames * 2:
                        raise TetoSynthesisError(f"fragmento WAV truncado: {path.name}")
                final = signature(os.fstat(source.fileno()))
            if opened != final or signature(path.stat()) != final:
                raise TetoSynthesisError(f"fragmento WAV mudou durante leitura: {path.name}")
            with self._pcm_cache_lock:
                self._drop_pcm_cache_entry(key)
                if self._pcm_cache_limit and len(pcm) <= self._pcm_cache_limit:
                    while self._pcm_cache and self._pcm_cache_bytes + len(pcm) > self._pcm_cache_limit:
                        _, (_, evicted) = self._pcm_cache.popitem(last=False)
                        self._pcm_cache_bytes -= len(evicted)
                    self._pcm_cache[key] = (final, pcm)
                    self._pcm_cache_bytes += len(pcm)
        samples = array.array('h')
        samples.frombytes(pcm)
        if os.sys.byteorder != "little":
            samples.byteswap()
        return samples

    def _drop_pcm_cache_entry(self, key):
        saved = self._pcm_cache.pop(key, None)
        if saved is not None:
            self._pcm_cache_bytes -= len(saved[1])

    def _pcm_cache_statistics(self):
        with self._pcm_cache_lock:
            return {'pcm_cache_hits': self._pcm_cache_hits,
                    'pcm_cache_misses': self._pcm_cache_misses,
                    'pcm_cache_bytes': self._pcm_cache_bytes}

    @staticmethod
    def _ordered_source_groups(groups, placements=None):
        """Prioritize the earliest audible recording without racing a source.

        CVVC preutterance can make the audible order differ from lexical note
        order. Every recording still belongs to exactly one serial group.
        """
        def start(item):
            number = item[0]
            return (placements[number]['start_ms'], number) if placements is not None else (number, number)
        ordered = [sorted(items, key=start) for items in groups.values()]
        return sorted(ordered, key=lambda items: start(items[0]))

    @staticmethod
    def _apply_gain(fragment: array.array, gain: float) -> array.array:
        if not fragment or abs(float(gain) - 1.0) < 0.002:
            return fragment
        scale = max(0.60, min(1.20, float(gain)))
        native = _numpy_dsp() if len(fragment) >= 4096 else None
        if native is not None:
            # int() truncates toward zero; rint() would change authored PCM.
            values = native.frombuffer(fragment, dtype=native.int16).astype(native.float64)
            values *= scale
            native.trunc(values, out=values)
            native.clip(values, -32768, 32767, out=values)
            output = array.array('h')
            output.frombytes(values.astype(native.int16).tobytes())
            return output
        output = array.array("h", fragment)
        for index, sample in enumerate(output):
            output[index] = max(-32768, min(32767, int(sample * scale)))
        return output

    @staticmethod
    def _rms(samples: array.array, start: int = 0, end: int | None = None) -> float:
        if not samples:
            return 0.0
        lo = max(0, int(start))
        hi = len(samples) if end is None else max(lo, min(len(samples), int(end)))
        if hi <= lo:
            return 0.0
        total = 0.0
        count = 0
        for sample in samples[lo:hi]:
            total += float(sample) * float(sample)
            count += 1
        return math.sqrt(total / max(1, count))

    def _match_fragment_energy(
        self, target: array.array, fragment: array.array, *, start_sample: int,
        fade_samples: int, role: str,
    ) -> tuple[array.array, float, bool]:
        """Match local RMS conservatively before a CVVC boundary is mixed.

        Resampler fragments come from different recordings and often have visibly
        different loudness. A global final normalization cannot fix those local
        steps; it only scales the whole phrase. This keeps each join within a small
        gain window so consonants retain their character while the perceived voice
        no longer pumps at every alias boundary.
        """
        if not target or not fragment or start_sample < 0 or start_sample >= len(target):
            return fragment, 0.0, False
        overlap = min(len(fragment), len(target) - start_sample)
        if overlap <= 16:
            return fragment, 0.0, False
        window = min(overlap, max(96, min(round(self.SAMPLE_RATE * 0.010), max(1, fade_samples))))
        target_rms = self._rms(target, start_sample, start_sample + window)
        fragment_rms = self._rms(fragment, 0, window)
        if target_rms < 48.0 or fragment_rms < 48.0:
            return fragment, 0.0, False
        delta_db = 20.0 * math.log10(max(1e-6, fragment_rms / target_rms))
        if abs(delta_db) < 1.25:
            return fragment, abs(delta_db), False
        desired = target_rms / fragment_rms
        if role == "nucleus":
            scale = max(0.82, min(1.18, desired))
        else:
            scale = max(0.86, min(1.14, desired))
        if abs(scale - 1.0) < 0.015:
            return fragment, abs(delta_db), False
        matched = self._apply_gain(fragment, scale)
        return matched, abs(delta_db), True

    @staticmethod
    def _oto_join_ms(entry: OtoEntry) -> float:
        # OTO preutterance tells us how early the next consonant wants to enter.
        # Use a conservative share of it instead of treating fragments as
        # independent blocks. overlap remains the floor; the clamp avoids a
        # malformed oto.ini swallowing a large part of the preceding mora.
        preutterance = max(0.0, float(entry.preutterance_ms))
        overlap = max(0.0, float(entry.overlap_ms))
        return max(10.0, min(55.0, max(16.0, overlap, preutterance * 0.45)))

    @staticmethod
    def _append_crossfade(target: array.array, fragment: array.array, overlap_samples: int) -> None:
        if not target or not fragment or overlap_samples <= 0:
            target.extend(fragment)
            return
        overlap = min(len(target), len(fragment), overlap_samples)
        start = len(target) - overlap
        for index in range(overlap):
            ratio = (index + 1) / (overlap + 1)
            # Smoothstep removes the linear crossfade's audible change of slope.
            ratio = ratio * ratio * (3.0 - 2.0 * ratio)
            mixed = int(target[start + index] * (1.0 - ratio) + fragment[index] * ratio)
            target[start + index] = max(-32768, min(32767, mixed))
        target.extend(fragment[overlap:])

    @staticmethod
    def _continuous_aux_advance_ms(note: RenderNote) -> float:
        # CVVC transition aliases articulate a boundary; they are not separate
        # spoken notes.  Give post-nucleus tails a tiny amount of real timeline
        # space while onset transitions consume none of the lexical duration.
        if note.role == "nasal":
            return max(10.0, min(26.0, note.duration_ms * 0.34))
        if note.role in {"coda", "glide"}:
            return max(8.0, min(22.0, note.duration_ms * 0.30))
        return 0.0

    @staticmethod
    def _english_fade_ms(entry: OtoEntry | None, lead_ms: float) -> float:
        if entry is None:
            return 0.0
        return max(4.0, min(48.0, max(float(entry.overlap_ms), min(24.0, lead_ms * 0.28))))

    def _english_timeline_placements(
        self, notes: list[RenderNote], entries: list[OtoEntry | None]
    ) -> tuple[list[dict[str, float]], float]:
        """Schedule English CVVC fragments around lexical note anchors.

        Nuclei advance the speech clock.  Onset/cluster aliases lead into the
        next nucleus and coda/glide/nasal aliases overlap the tail of the
        previous nucleus.  This is deliberately different from speech-4B's
        serial append model, where every auxiliary alias created a new attack.
        """
        anchors = [0.0 for _ in notes]
        leads = [
            self._english_lead_ms(entry, note) if entry is not None else 0.0
            for note, entry in zip(notes, entries)
        ]
        fades = [self._english_fade_ms(entry, lead) for entry, lead in zip(entries, leads)]
        cursor = 0.0
        pending_onsets: list[int] = []
        pending_exits: list[int] = []
        trim_leads = [0.0 for _ in notes]
        protected_until = 0.0
        pause_floor: float | None = None
        sample_end = 0.0
        has_lexical_audio = False

        for index, note in enumerate(notes):
            if entries[index] is None and note.role != "nucleus":
                # A missing auxiliary emits no PCM. Keep diagnostics and its
                # punctuation, but do not reserve a silent articulation for it.
                anchors[index] = cursor
                if note.pause_after_ms >= 24:
                    pause_floor = max(cursor, sample_end) + float(note.pause_after_ms)
                    cursor = pause_floor
                continue
            if note.role in {"transition", "cluster"}:
                pending_onsets.append(index)
                continue
            if note.role == "vowel-exit":
                pending_exits.append(index)
                continue

            if note.role == "nucleus":
                onset_starts: dict[int, float] = {}
                next_start = cursor - leads[index]
                next_fade = fades[index]
                if pending_onsets:
                    # Reserve each helper's actual lead + articulation, walking
                    # backwards from the incoming CV. A fixed 12 ms stagger let
                    # that CV replace a helper before its consonant even began.
                    for pending in reversed(pending_onsets):
                        overlap = min(next_fade, notes[pending].duration_ms * 0.35)
                        total_ms = leads[pending] + float(notes[pending].duration_ms)
                        next_start = next_start + overlap - total_ms
                        onset_starts[pending] = next_start
                        next_fade = fades[pending]

                earliest_start = next_start
                floor = protected_until - next_fade if has_lexical_audio else earliest_start
                if pause_floor is not None:
                    floor = max(floor, pause_floor)
                shift = max(0.0, floor - earliest_start)
                cursor += shift
                for pending, start in onset_starts.items():
                    anchors[pending] = start + shift + leads[pending]
                    sample_end = max(sample_end, anchors[pending] + notes[pending].duration_ms)
                if pending_onsets:
                    pending_onsets.clear()

                for pending in pending_exits:
                    # The VC consonant cue and the CV consonant attack describe
                    # the SAME boundary. Align them instead of speaking two
                    # consonants in series before the incoming vowel nucleus.
                    anchors[pending] = cursor - leads[index]
                    raw_start = anchors[pending] - leads[pending]
                    floor = protected_until - fades[pending] if has_lexical_audio else anchors[pending]
                    if pause_floor is not None:
                        floor = max(floor, pause_floor)
                    # Retain the source's raw time zero for shared F0 and cache
                    # identity, but discard unused recorded vowel at mix time.
                    # Even a short preceding vowel never loses the VC cue.
                    trim_leads[pending] = max(0.0, min(leads[pending], floor - raw_start))
                    sample_end = max(sample_end, anchors[pending] + notes[pending].duration_ms)
                pending_exits.clear()

                anchors[index] = cursor
                # Keep a useful vowel body before the following attack. Longer
                # OTO leads extend the articulation budget instead of erasing it.
                protected_until = cursor + min(45.0, float(note.duration_ms) * 0.40)
                has_lexical_audio = True
                pause_floor = None
                cursor += float(note.duration_ms)
            else:
                # Successive nasal/glide/coda units must survive until their
                # own consonantal region, rather than replace one another while
                # still in the recorded vowel preutterance.
                if has_lexical_audio:
                    cursor = max(cursor, protected_until + leads[index] - fades[index])
                if pause_floor is not None:
                    cursor = max(cursor, pause_floor + leads[index])
                anchors[index] = cursor
                protected_until = cursor + float(note.duration_ms)
                has_lexical_audio = True
                pause_floor = None
                cursor += self._continuous_aux_advance_ms(note)

            sample_end = max(sample_end, anchors[index] + float(note.duration_ms))
            if note.pause_after_ms >= 24:
                pause_floor = max(cursor, sample_end) + float(note.pause_after_ms)
                cursor = pause_floor

        # Defensive handling for a malformed/planner-truncated sequence ending
        # in an onset alias without a following nucleus.
        if pending_onsets:
            for pending in pending_onsets:
                start = max(cursor - leads[pending], protected_until - fades[pending])
                if pause_floor is not None:
                    start = max(start, pause_floor)
                anchors[pending] = start + leads[pending]
                protected_until = anchors[pending] + notes[pending].duration_ms
                cursor = protected_until
                sample_end = max(sample_end, cursor)
        for pending in pending_exits:
            anchors[pending] = cursor
            raw_start = cursor - leads[pending]
            floor = protected_until - fades[pending] if has_lexical_audio else cursor
            if pause_floor is not None:
                floor = max(floor, pause_floor)
            trim_leads[pending] = max(0.0, min(leads[pending], floor - raw_start))
            sample_end = max(sample_end, cursor + notes[pending].duration_ms)

        placements: list[dict[str, float]] = []
        minimum_start = 0.0
        first = True
        for index, (note, entry) in enumerate(zip(notes, entries)):
            lead = leads[index]
            start = anchors[index] - lead
            overlap = fades[index]
            audible_start = start + trim_leads[index]
            if first or audible_start < minimum_start:
                minimum_start = audible_start
                first = False
            placement = {
                "anchor_ms": anchors[index],
                "start_ms": start,
                "lead_ms": lead,
                "fade_ms": overlap,
            }
            if note.role == "vowel-exit":
                placement["trim_lead_ms"] = trim_leads[index]
            placements.append(placement)

        shift = -minimum_start if minimum_start < 0.0 else 0.0
        if shift:
            for placement in placements:
                placement["anchor_ms"] += shift
                placement["start_ms"] += shift
        return placements, max(cursor, sample_end) + shift

    @staticmethod
    def _place_timeline_fragment(
        target: array.array, fragment: array.array, *, start_sample: int, fade_samples: int
    ) -> None:
        if not fragment:
            return
        start = int(start_sample)
        source_offset = 0
        if start < 0:
            source_offset = min(len(fragment), -start)
            start = 0
        if source_offset >= len(fragment):
            return
        fragment = fragment[source_offset:]
        original_length = len(target)
        if start > original_length:
            target.extend([0] * (start - original_length))
            original_length = len(target)

        overlap = max(0, min(len(fragment), original_length - start))
        fade = max(0, min(overlap, int(fade_samples)))
        for offset in range(fade):
            phase = (offset + 1) / (fade + 1) * (math.pi / 2.0)
            # sin²/cos² is constant-sum but has zero slope at both ends, so
            # a join does not announce itself as a new envelope attack.
            ratio = math.sin(phase) ** 2
            position = start + offset
            mixed = int(target[position] * (1.0 - ratio) + fragment[offset] * ratio)
            target[position] = max(-32768, min(32767, mixed))
        if overlap > fade:
            target[start + fade:start + overlap] = fragment[fade:overlap]

        remainder = fragment[overlap:]
        if remainder:
            if start + overlap == len(target):
                target.extend(remainder)
            else:
                needed = start + len(fragment) - len(target)
                if needed > 0:
                    target.extend([0] * needed)
                target[start + overlap:start + len(fragment)] = remainder

    @classmethod
    def _fade_timeline_gaps(
        cls, samples: array.array, intervals: list[tuple[int, int]]
    ) -> int:
        """Soften exposed PCM edges at intentional pauses without changing joins.

        Overlapping CVVC fragments, including auxiliary aliases, occupy one
        continuous region. Only a gap at least as long as a punctuation pause
        exposes two new edges; waveform zero crossings inside an occupied
        region are not pause boundaries. Work from actual fragment lengths so
        resampler frame rounding cannot leave an unfaded edge near the plan.
        """
        regions: list[list[int]] = []
        for start, end in sorted(intervals):
            start = max(0, min(len(samples), int(start)))
            end = max(start, min(len(samples), int(end)))
            if end <= start:
                continue
            if regions and start <= regions[-1][1]:
                regions[-1][1] = max(regions[-1][1], end)
            else:
                regions.append([start, end])

        edge_frames = round(cls.SAMPLE_RATE * 0.004)
        pause_frames = round(cls.SAMPLE_RATE * 0.024)
        faded_gaps = 0
        for left, right in zip(regions, regions[1:]):
            if right[0] - left[1] < pause_frames:
                continue
            # A tiny isolated region may border two pauses. Limit each fade
            # to half its length so its incoming and outgoing fades stay apart.
            outgoing = min(edge_frames, (left[1] - left[0]) // 2)
            incoming = min(edge_frames, (right[1] - right[0]) // 2)
            for length, boundary, step in (
                (outgoing, left[1] - 1, -1), (incoming, right[0], 1),
            ):
                for offset in range(length):
                    ratio = math.sin(offset / max(1, length - 1) * math.pi / 2.0) ** 2
                    position = boundary + step * offset
                    samples[position] = round(samples[position] * ratio)
            faded_gaps += int(outgoing > 0 or incoming > 0)
        return faded_gaps

    def _compose_english_cvvc(
        self, *, notes: list[RenderNote], entries: list[OtoEntry | None],
        fragments: dict[int, tuple[Path, bool, bool]], deadline: float,
        placements: list[dict[str, float]] | None = None, planned_end_ms: float | None = None,
        on_prefix=None,
    ) -> tuple[array.array, list[str], int, int, int, dict[str, float | int | str]]:
        if placements is None or planned_end_ms is None:
            placements, planned_end_ms = self._english_timeline_placements(notes, entries)
        combined = array.array("h")
        missing: list[str] = []
        rendered = 0
        pitchbend_fallbacks = 0
        legacy_fragment_hits = 0
        auxiliary_overlays = 0
        max_lead_ms = 0.0
        continuity_repairs = 0
        max_energy_boundary_db = 0.0
        rendered_intervals: list[tuple[int, int]] = []

        for number, (note, entry, placement) in enumerate(zip(notes, entries, placements)):
            self._check_render_cancelled()
            if time.monotonic() >= deadline:
                raise TimeoutError("renderização Teto excedeu o timeout")
            if entry is None:
                missing.append(note.candidates[0] if note.candidates else "?")
                # Do not serialize an explicit silence for a missing auxiliary
                # CVVC unit.  The surrounding lexical samples usually cover
                # that boundary more naturally than a hard gap would.
                continue

            fragment_path, neutral_pitch, legacy_fragment = fragments[number]
            pitchbend_fallbacks += int(neutral_pitch)
            legacy_fragment_hits += int(legacy_fragment)
            fragment = self._apply_gain(self._read_samples(fragment_path), note.gain)
            # Some resamplers round output up. Do not let that extra tail occupy
            # a later articulation or fill a punctuation pause.
            frames = round(self.SAMPLE_RATE * (note.duration_ms + placement["lead_ms"]) / 1000.0)
            fragment = fragment[:max(1, frames)]
            start_sample = round(self.SAMPLE_RATE * placement["start_ms"] / 1000.0)
            fade_samples = round(self.SAMPLE_RATE * placement["fade_ms"] / 1000.0)
            trimmed_frames = min(len(fragment), max(0, round(self.SAMPLE_RATE * placement.get("trim_lead_ms", 0.0) / 1000.0)))
            if trimmed_frames:
                fragment = fragment[trimmed_frames:]
                start_sample += trimmed_frames
            fragment, boundary_db, repaired = self._match_fragment_energy(
                combined, fragment, start_sample=start_sample,
                fade_samples=fade_samples, role=note.role,
            )
            max_energy_boundary_db = max(max_energy_boundary_db, boundary_db)
            continuity_repairs += int(repaired)
            self._place_timeline_fragment(
                combined, fragment, start_sample=start_sample, fade_samples=fade_samples
            )
            rendered_intervals.append((max(0, start_sample), start_sample + len(fragment)))
            max_lead_ms = max(max_lead_ms, float(placement["lead_ms"]))
            auxiliary_overlays += int(note.role != "nucleus")
            rendered += 1
            if on_prefix is not None:
                on_prefix(combined, number, rendered_intervals)

        silence_gap_fades = self._fade_timeline_gaps(combined, rendered_intervals)
        if combined:
            # Phrase-level micro fades eliminate the only two raw PCM edges
            # that remain exposed after timeline composition.
            edge = min(len(combined) // 2, round(self.SAMPLE_RATE * 0.004))
            for offset in range(edge):
                ratio = (offset + 1) / max(1, edge)
                combined[offset] = int(combined[offset] * ratio)
                end = len(combined) - 1 - offset
                combined[end] = int(combined[end] * ratio)

        sequential_ms = sum(float(note.duration_ms) for note in notes) + sum(
            float(note.pause_after_ms) for note in notes if note.pause_after_ms >= 24
        )
        timeline = {
            "mode": "phrase-continuous",
            "planned_ms": round(planned_end_ms, 2),
            "audio_ms": round(1000.0 * len(combined) / self.SAMPLE_RATE, 2),
            "serialized_ms": round(sequential_ms, 2),
            "aux_overlays": auxiliary_overlays,
            "max_preutterance_ms": round(max_lead_ms, 2),
            "continuity_repairs": continuity_repairs,
            "energy_boundary_max_db": round(max_energy_boundary_db, 2),
            "silence_gap_fades": silence_gap_fades,
        }
        return combined, missing, rendered, pitchbend_fallbacks, legacy_fragment_hits, timeline

    @staticmethod
    def _performance_envelope(samples: array.array, start: int, end: int, *, fade_in=False, fade_out=False,
                              sample_offset=0, total_frames=None) -> None:
        total_frames = len(samples) if total_frames is None else total_frames
        start, end = max(0, int(start)), min(total_frames, int(end))
        if end <= start:
            return
        # Keep most of a short word fully audible; the first consonant gets
        # a gradual attack rather than spending its whole lifetime at silence.
        length = min((end - start) // 3, round(TetoRenderer.SAMPLE_RATE * 0.26))
        if not length:
            return
        for offset in range(length):
            gain = 0.12 + 0.88 * math.sin(offset / max(1, length - 1) * math.pi / 2.0) ** 2
            incoming = start + offset - sample_offset
            outgoing = end - 1 - offset - sample_offset
            if fade_in and 0 <= incoming < len(samples):
                samples[incoming] = round(samples[incoming] * gain)
            if fade_out and 0 <= outgoing < len(samples):
                samples[outgoing] = round(samples[outgoing] * gain)

    def _performance_phonemize(self, text, index, language):
        return phonemize(text, max_moras=1025, resolve_alias=index.resolve,
                         voicebank_profile=self._index_profile,
                         reading_language=language, **self._phonemizer_options())

    @classmethod
    def _apply_midi_velocity(cls, samples: array.array, notes, preroll: float, *, check_cancelled=None,
                             sample_offset=0, total_frames=None) -> None:
        """Keep the original PCM rounding, evaluating sin only during the ramp."""
        previous_gain = previous_end = None
        total_frames = len(samples) if total_frames is None else total_frames
        for event in notes:
            if check_cancelled is not None:
                check_cancelled()
            first = max(0, round(cls.SAMPLE_RATE * (preroll + event['start_ms']) / 1000))
            end = min(total_frames, round(cls.SAMPLE_RATE * (preroll + event['start_ms'] + event['duration_ms']) / 1000))
            gain = 0.72 + 0.28 * math.sqrt(event['velocity'] / 127.0)
            incoming = previous_gain if previous_gain is not None and first - previous_end <= round(cls.SAMPLE_RATE * 0.02) else gain
            smooth_frames = min(end - first, round(cls.SAMPLE_RATE * 0.008))
            ramp_end = min(end, first + max(1, smooth_frames - 1))
            if incoming != gain:
                for number in range(max(first, sample_offset), min(ramp_end, sample_offset + len(samples))):
                    ratio = math.sin((number - first) / max(1, smooth_frames - 1) * math.pi / 2.0) ** 2
                    position = number - sample_offset
                    samples[position] = round(samples[position] * (incoming + (gain - incoming) * ratio))
            else:
                ramp_end = first
            # Preserve this arithmetic rather than substituting `gain`: the
            # former expression can round differently by one PCM unit.
            tail_gain = incoming + (gain - incoming)
            if tail_gain != 1.0:
                lo, hi = max(ramp_end, sample_offset) - sample_offset, min(end, sample_offset + len(samples)) - sample_offset
                native = _numpy_dsp() if hi - lo >= 4096 else None
                if native is not None:
                    values = native.frombuffer(samples, dtype=native.int16, count=hi - lo, offset=lo * 2)
                    values[:] = native.rint(values.astype(native.float64) * tail_gain).astype(native.int16)
                else:
                    for number in range(lo, hi):
                        samples[number] = round(samples[number] * tail_gain)
            previous_gain, previous_end = gain, end

    @staticmethod
    def _singing_message_tokens(text, segments=None):
        if not segments:
            return text.split()
        tokens = []
        for segment in segments:
            for token in segment['text'].split():
                if token == '~' and not segment['hold']:
                    continue
                if token != '|':
                    tokens.append(token)
            if segment['pause_after_ms']:
                tokens.append('|')
        return tokens

    def _default_singing_lyrics(self, text, index, language, segments=None):
        moras, slots, boundaries = [], [], []
        word_offset, explicit_holds, after_boundary = 0, False, False
        for token in self._singing_message_tokens(text, segments):
            if token == '|':
                if not slots or after_boundary:
                    raise ValueError('use | entre duas palavras do canto')
                boundaries.append(len(slots))
                moras[-1] = replace(moras[-1], phrase_end='|', pause_after_ms=0)
                after_boundary = True
                continue
            if token == '~':
                if not slots or after_boundary:
                    raise ValueError('use ~ depois de uma sílaba, sem atravessar uma pausa')
                slots.append(None)
                explicit_holds = True
                continue
            chunk = self._performance_phonemize(token.strip('-'), index, language)
            nuclei = [mora for mora in chunk if mora.role == 'nucleus']
            if not nuclei:
                continue
            first = sum(mora.role == 'nucleus' for mora in moras)
            slots.extend(range(first, first + len(nuclei)))
            moras.extend(replace(mora, word_index=mora.word_index + word_offset,
                pause_after_ms=0, phrase_end='') for mora in chunk)
            word_offset += max(mora.word_index for mora in chunk) + 1
            after_boundary = False
        if not slots or after_boundary:
            raise ValueError('a letra precisa conter uma vogal e terminar em uma sílaba')
        if len(moras) > 1024:
            raise ValueError('a letra contém fonemas demais')
        count = len(slots) if explicit_holds or boundaries else max(4, len(slots))
        score = default_melody_score(count, boundaries=boundaries)
        groups = align_singing_syllables(moras, score, slots=slots if explicit_holds else None)
        return score, moras, groups

    def _singing_lyrics(self, text, score, index, language, segments=None):
        moras, slots = [], []
        word_offset = 0
        explicit_holds = False
        if score['lyrics_source'] == 'message':
            tokens = self._singing_message_tokens(text, segments)
        elif score['lyrics_source'] == 'vowel':
            # One continuous vowel per voiced phrase, including its note changes.
            tokens = []
            for number, event in enumerate(score['notes']):
                previous = score['notes'][number - 1] if number else None
                continuation = previous is not None and event['start_ms'] - previous['start_ms'] - previous['duration_ms'] <= 20
                tokens.append('~' if continuation else 'a')
        else:
            tokens = [str(event.get('lyric') or '~').strip() for event in score['notes']]
        for token_index, token in enumerate(tokens):
            if token == '|':
                if moras:
                    moras[-1] = replace(moras[-1], phrase_end='|', pause_after_ms=0)
                continue
            if token in {'~', '-', '_'}:
                if score['lyrics_source'] == 'midi' and token_index and token_index < len(score['notes']):
                    previous, event = score['notes'][token_index - 1], score['notes'][token_index]
                    if event['start_ms'] - previous['start_ms'] - previous['duration_ms'] > 20:
                        nucleus = next((item for item in reversed(moras) if item.role == 'nucleus'), None)
                        if nucleus is None:
                            token = 'a'
                        else:
                            # A rest ends the sustain. Restart the same vowel
                            # from its steady alias, never its old consonant.
                            candidates = tuple(dict.fromkeys(candidate.split()[-1] for candidate in nucleus.candidates))
                            if index.resolve(candidates) is None:
                                raise ValueError('a voicebank não contém a vogal isolada depois da pausa; escreva a sílaba nessa nota MIDI')
                            first = sum(item.role == 'nucleus' for item in moras)
                            moras.append(replace(nucleus, candidates=candidates, word_index=word_offset,
                                                 syllable_index=0, word_moras=1, mora_index=0,
                                                 word_end=True, pause_after_ms=0, phrase_end='',
                                                 source_phonemes=nucleus.source_phonemes[-1:]))
                            word_offset += 1
                            slots.append(first)
                            continue
                if token == 'a':
                    pass
                else:
                    explicit_holds = True
                    slots.append(None)
                    continue
            chunk = self._performance_phonemize(token.strip('-'), index, language)
            nuclei = [item for item in chunk if item.role == 'nucleus']
            if score['lyrics_source'] == 'midi' and len(nuclei) != 1:
                raise ValueError('cada letra MIDI deve conter uma sílaba; envie a letra na mensagem para alinhamento automático')
            if not nuclei:
                continue
            first = sum(item.role == 'nucleus' for item in moras)
            slots.extend(range(first, first + len(nuclei)))
            moras.extend(replace(item, word_index=item.word_index + word_offset,
                                 pause_after_ms=0, phrase_end='') for item in chunk)
            word_offset += max(item.word_index for item in chunk) + 1
        if len(moras) > 1024:
            raise ValueError('letra MIDI contém fonemas demais')
        groups = align_singing_syllables(moras, score, slots=slots if explicit_holds or score['lyrics_source'] == 'midi' else None)
        return moras, groups

    def _singing_notes(self, moras, groups, score):
        nucleus_keys = [(item.word_index, item.syllable_index) for item in moras if item.role == 'nucleus']
        lookup = {key: number for number, key in enumerate(nucleus_keys)}
        notes, spans = [], []
        events = score['notes']
        for mora in moras:
            group = groups[lookup[(mora.word_index, mora.syllable_index)]]
            first, last = events[group[0]], events[group[-1]]
            start = float(first['start_ms'])
            end = float(last['start_ms'] + last['duration_ms'])
            # Centre the coarse UTAU note so a wide melisma stays within the
            # signed 12-bit bend range; absolute MIDI notes are not transposed.
            pitches = [events[number]['midi_note'] for number in group]
            base_midi = round((min(pitches) + max(pitches)) / 2)
            duration = max(1, round(end - start)) if mora.role == 'nucleus' else max(30, min(70, mora.duration_ms))
            notes.append(RenderNote(candidates=mora.candidates, pitch=_relative_pitch('C4', base_midi - 60),
                                    duration_ms=duration, pause_after_ms=0, gain=1.0,
                                    stressed=mora.stressed, contour='singing', role=mora.role,
                                    source_phonemes=mora.source_phonemes, coverage=mora.coverage,
                                    word_index=mora.word_index, mora_index=mora.mora_index,
                                    word_moras=mora.word_moras, word_end=mora.word_end,
                                    phrase_end=mora.phrase_end, profile=mora.profile,
                                    planner_cost=mora.planner_cost, syllable_index=mora.syllable_index,
                                    word_syllables=mora.word_syllables))
            spans.append((start, end, group))
        return notes, spans

    def _singing_placements(self, notes, entries, spans, score):
        """Fit ordered articulation around fixed MIDI vowel anchors.

        Auxiliary recordings include an old vowel before their consonant.
        Placing every tail at ``end - duration`` lets a later recording erase
        the previous nasal/coda while still playing that old vowel. Reserve
        separate consonant bodies, and trim only redundant recorded lead.
        Unlike speech, the musical clock must never move to make room.
        """
        leads = [self._english_lead_ms(entry, note) if entry else 0.0
                 for note, entry in zip(notes, entries)]
        fades = [self._english_fade_ms(entry, lead)
                 for entry, lead in zip(entries, leads)]
        groups = {}
        placements = []
        for number, (note, (start, end, group)) in enumerate(zip(notes, spans)):
            item = groups.setdefault(tuple(group), {'start': start, 'end': end,
                                                    'onsets': [], 'tails': [], 'exits': []})
            if note.role == 'nucleus':
                item['nucleus'] = number
            elif entries[number] is not None:
                role = ('onsets' if note.role in {'transition', 'cluster', 'epenthetic'}
                        else 'exits' if note.role == 'vowel-exit' else 'tails')
                item[role].append(number)
            placements.append({'anchor_ms': start, 'start_ms': start - leads[number],
                               'lead_ms': leads[number], 'fade_ms': fades[number],
                               'score_group': group})

        ordered = sorted(groups.values(), key=lambda item: item['start'])
        for item in ordered:
            nucleus = item['nucleus']
            attack = leads[nucleus]
            next_fade = fades[nucleus]
            for number in reversed(item['onsets']):
                overlap = min(next_fade, notes[number].duration_ms * .35)
                attack += leads[number] + notes[number].duration_ms - overlap
                next_fade = fades[number]
            item['attack'] = attack
            item['attack_scale'] = 1.0
            item['protected'] = item['start'] + min(45.0, (item['end'] - item['start']) * .40)
            tail = sum(notes[number].duration_ms for number in item['tails'])
            item['tail'] = tail
            item['tail_scale'] = min(1.0, (item['end'] - item['protected']) / tail) if tail else 1.0

        for position, item in enumerate(ordered):
            if position == 0:
                # Keep the existing 350 ms musical preroll bound, even for
                # clusters with several authored preutterances.
                available = item['start'] + 350.0
                if item['attack']:
                    item['attack_scale'] = min(1.0, available / item['attack'])
                continue
            previous = ordered[position - 1]
            gap = item['start'] - previous['end']
            if gap > 20:
                # A rest provides room for the incoming attack; do not steal
                # the preceding word's nasal/coda across that musical break.
                if item['attack']:
                    item['attack_scale'] = min(1.0, gap / item['attack'])
            else:
                tail = previous['tail'] * previous['tail_scale']
                required = tail + item['attack']
                available = max(0.0, item['start'] - previous['protected'])
                scale = min(1.0, available / required) if required else 1.0
                previous['tail_scale'] *= scale
                item['attack_scale'] = scale

        def place(number, anchor, *, audible_start, duration=None, fade=None):
            placement = placements[number]
            placement['anchor_ms'] = anchor
            placement['start_ms'] = anchor - leads[number]
            placement['trim_lead_ms'] = max(0.0, min(leads[number],
                audible_start - placement['start_ms']))
            if duration is not None:
                placement['duration_ms'] = duration
            if fade is not None:
                placement['fade_ms'] = fade

        # Establish incoming attacks first so the previous tail can end before
        # the next recording starts replacing it in the compositor.
        for item in ordered:
            nucleus = item['nucleus']
            scale = item['attack_scale']
            cursor = item['start'] - leads[nucleus] * scale
            place(nucleus, item['start'], audible_start=cursor,
                  fade=fades[nucleus] * scale)
            next_fade = fades[nucleus] * scale
            for number in reversed(item['onsets']):
                duration = max(1, math.floor(notes[number].duration_ms * scale))
                fade = min(fades[number] * scale, duration * .35)
                overlap = min(next_fade, duration * .35)
                anchor = cursor + overlap - duration
                cursor = anchor - leads[number] * scale
                place(number, anchor, audible_start=cursor, duration=duration, fade=fade)
                next_fade = fade
            item['attack_start'] = cursor
            for number in item['exits']:
                # VC and CV articulate the SAME consonant boundary. Discard
                # the VC's old vowel instead of replaying it over the last
                # word's nasal or glide.
                boundary = item['start'] - leads[nucleus] * scale
                fade = fades[number] * scale
                place(number, boundary, audible_start=cursor - fade, fade=fade)

        for position, item in enumerate(ordered):
            end = item['end']
            if position + 1 < len(ordered):
                incoming = ordered[position + 1]
                if incoming['start'] - end <= 20:
                    end = min(end, incoming['attack_start'])
            durations = [max(1, math.floor(notes[number].duration_ms * item['tail_scale']))
                         for number in item['tails']]
            cursor = end - sum(durations)
            protected = item['protected']
            previous_duration = None
            for number, duration in zip(item['tails'], durations):
                fade = min(fades[number], duration * .35)
                if previous_duration is not None:
                    fade = min(fade, previous_duration * .35)
                place(number, cursor, audible_start=max(cursor - leads[number], protected - fade),
                      duration=duration, fade=fade)
                protected = cursor + duration
                previous_duration = duration
                cursor += duration

        preroll = max(0.0, -min((placement['start_ms'] + placement.get('trim_lead_ms', 0.0)
                                for placement, entry in zip(placements, entries) if entry is not None),
                               default=0.0))
        if preroll > 350.0 + 1e-6:
            raise TetoSynthesisError('ataque consonantal longo demais para o MIDI')
        for placement in placements:
            placement['anchor_ms'] += preroll
            placement['start_ms'] += preroll
        return placements, preroll

    @staticmethod
    def _performance_wav(samples, rate):
        output = io.BytesIO()
        with wave.open(output, 'wb') as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(rate)
            data = samples
            if os.sys.byteorder != 'little':
                data = array.array('h', samples)
                data.byteswap()
            wav.writeframes(memoryview(data).cast('B'))
        return output.getvalue()

    @classmethod
    def _sealed_pcm_range(cls, combined, first, end, intervals, *, total_frames,
                          score, preroll, envelopes):
        """Apply the final presentation effects to one immutable safe prefix.

        ``combined`` stays raw for later CVVC energy matching. The caller leaves
        the last edge and every future fragment's start unpublished, so a later
        overlap or pause fade cannot change these samples.
        """
        samples = combined[first:end]
        regions = []
        for start, stop in sorted(intervals):
            start, stop = max(0, start), min(len(combined), stop)
            if stop <= start:
                continue
            if regions and start <= regions[-1][1]:
                regions[-1][1] = max(regions[-1][1], stop)
            else:
                regions.append([start, stop])
        edge = round(cls.SAMPLE_RATE * .004)
        pause = round(cls.SAMPLE_RATE * .024)
        for left, right in zip(regions, regions[1:]):
            if right[0] - left[1] < pause:
                continue
            for length, boundary, step in (
                (min(edge, (left[1] - left[0]) // 2), left[1] - 1, -1),
                (min(edge, (right[1] - right[0]) // 2), right[0], 1),
            ):
                for offset in range(length):
                    position = boundary + step * offset - first
                    if 0 <= position < len(samples):
                        ratio = math.sin(offset / max(1, length - 1) * math.pi / 2.0) ** 2
                        samples[position] = round(samples[position] * ratio)
        # The outgoing phrase edge remains reserved until final composition.
        phrase_edge = min(len(combined) // 2, edge)
        for position in range(first, min(end, phrase_edge)):
            samples[position - first] = int(samples[position - first] * ((position + 1) / max(1, phrase_edge)))
        cls._apply_midi_velocity(samples, score['notes'], preroll,
                                 sample_offset=first, total_frames=total_frames)
        for start, stop, fade_in, fade_out in envelopes:
            cls._performance_envelope(samples, start, stop, fade_in=fade_in, fade_out=fade_out,
                                      sample_offset=first, total_frames=total_frames)
        return samples

    def synthesize_performance_stream(self, text, *, emit_pcm, **kwargs):
        """Publish sealed musical PCM while later fragments are still rendering."""
        return self.synthesize_performance(text, emit_pcm=emit_pcm, **kwargs)

    def _worldline_effects(self, text, segments, *, pitch_offset_semitones, speech_rate, reading_language,
                          timeout_seconds, max_audio_bytes):
        # WORLDLINE owns a separate phrase API. Plain fades/pauses can use it;
        # vowel holds need the musical resampler and must not time-stretch PCM.
        if any(segment['hold'] for segment in segments):
            raise ValueError('prolongar vogais exige o motor ESPER; WORLDLINE aceita fades e pausas')
        started = time.monotonic()
        combined = array.array('h')
        last = {}
        for segment in segments:
            if segment['text'].strip() in {'|', '~'}:
                continue
            remaining = timeout_seconds - (time.monotonic() - started)
            if remaining <= 0:
                raise TimeoutError('efeitos TTV excederam o timeout')
            result = self.synthesize(segment['text'], timeout_seconds=remaining, max_audio_bytes=max_audio_bytes,
                                     pitch_offset_semitones=pitch_offset_semitones, speech_rate=speech_rate,
                                     reading_language=reading_language)
            with wave.open(io.BytesIO(result['audio']), 'rb') as wav:
                fragment = array.array('h', wav.readframes(wav.getnframes()))
                if os.sys.byteorder != 'little':
                    fragment.byteswap()
            self._performance_envelope(fragment, 0, len(fragment), fade_in=segment['fade_in'], fade_out=segment['fade_out'])
            combined.extend(fragment)
            combined.extend([0] * round(self.SAMPLE_RATE * segment['pause_after_ms'] / 1000.0))
            last = {key: value for key, value in result.items() if key != 'audio'}
            if len(combined) > self.SAMPLE_RATE * 20:
                raise TetoSynthesisError('efeitos TTV excederam 20 segundos')
        raw = self._performance_wav(combined, self.SAMPLE_RATE)
        if len(raw) > max_audio_bytes or not combined:
            raise TetoSynthesisError('áudio com efeitos TTV inválido ou grande demais')
        return raw, {**last, 'ttv_performance_hash': performance_hash(text, segments=segments),
                     'performance_version': self.PERFORMANCE_VERSION, 'performance_mode': 'speech-effects',
                     'sha256': hashlib.sha256(raw).hexdigest(), 'worker_synth_ms': round((time.monotonic() - started) * 1000, 2)}

    def synthesize_performance(self, text: str, *, score=None, segments=None,
                               pitch_offset_semitones=0.0, speech_rate=1.0, reading_language='pt-BR',
                               timeout_seconds=120.0, max_audio_bytes=8 * 1024 * 1024,
                               cancelled: Callable[[], bool] | None = None, emit_pcm=None):
        """Render score vowels on a musical clock, or a phrase with inline effects.

        MIDI uses original pitches/tempo and a consonantal preroll reported in
        metadata. Every melisma is one nucleus render with multiple F0 targets.
        """
        if emit_pcm is not None and (not callable(emit_pcm) or score is None):
            raise ValueError('PCM progressivo exige uma partitura e callback de áudio')
        validate_performance(text, score=score, segments=segments)
        digest = performance_hash(text, score=score, segments=segments)
        language = self._effective_reading_language(reading_language)
        rate = self._effective_speech_rate(speech_rate)
        if not math.isfinite(float(timeout_seconds)) or not 0 < float(timeout_seconds) <= 120:
            raise ValueError('timeout TTV deve estar entre 0 e 120 segundos')
        pitch = float(pitch_offset_semitones)
        if not math.isfinite(pitch) or not -4 <= pitch <= 4:
            raise ValueError('tom TTV deve estar entre -4 e 4 semitons')
        if score is not None and (pitch != 0.0 or rate != 1.0):
            raise ValueError('canto MIDI preserva tom e andamento; use tom 0 e velocidade 1')
        pitch = round(pitch * 2.0) / 2.0
        if self.RENDER_VERSION.startswith('worldline'):
            if cancelled is not None:
                raise TetoResourceError('motor WORLDLINE sem cancelamento de canto em segundo plano')
            if score is not None:
                raise ValueError('canto MIDI exige ESPER; o motor WORLDLINE ainda não aceita partituras')
            return self._worldline_effects(text, segments, pitch_offset_semitones=pitch,
                                           speech_rate=rate, reading_language=language,
                                           timeout_seconds=timeout_seconds, max_audio_bytes=max_audio_bytes)
        if not self._env_bool('PHONE_WORKER_TETO_ENABLED', False):
            raise TetoConfigurationError('PHONE_WORKER_TETO_ENABLED=false')
        self._resampler_command()
        if not shutil.which('ffmpeg'):
            raise TetoConfigurationError('ffmpeg não encontrado')
        self._check_resources()
        if not self._lock.acquire(blocking=False):
            raise TetoResourceError('renderer TTV ocupado')
        started = time.monotonic()
        deadline = started + float(timeout_seconds)
        cache_before = self._cache.statistics()
        pcm_before = self._pcm_cache_statistics()
        cache_started = False
        render_stopped = threading.Event()
        def should_cancel():
            return render_stopped.is_set() or (cancelled is not None and cancelled())
        try:
            self._reading_context.cancelled = should_cancel if cancelled is not None or emit_pcm is not None else None
            self._check_render_cancelled()
            self._cache.begin_render()
            cache_started = True
            self._reading_context.performance_singing = score is not None
            if score is not None:
                # Singing always uses this character's CVVC bank. Its spoken
                # Portuguese voicebank may independently remain Standard.
                index = self._load_english_index()
                self._reading_context.profile = 'english-cvvc'
            else:
                index = self._load_reading_index(language)
            tempo = max(60, min(240, self._env_int('PHONE_WORKER_TETO_TEMPO', 140)))
            effect_ranges = []
            spans, groups, preroll = None, None, 0.0
            if score is not None:
                if is_default_score(score):
                    score, moras, groups = self._default_singing_lyrics(text, index, language, segments)
                else:
                    moras, groups = self._singing_lyrics(text, score, index, language, segments)
                notes, spans = self._singing_notes(moras, groups, score)
            else:
                spoken = speech_effect_segments(segments)
                moras = self._performance_phonemize(' '.join(segment['text'] for segment in spoken), index, language)
                word_offset = 0
                for segment in spoken:
                    chunk = self._performance_phonemize(segment['text'], index, language)
                    count = max((item.word_index for item in chunk), default=-1) + 1
                    selected = [number for number, mora in enumerate(moras)
                                if word_offset <= mora.word_index < word_offset + count]
                    if selected:
                        first, end = selected[0], selected[-1] + 1
                        if segment['pause_after_ms']:
                            moras[end - 1] = replace(moras[end - 1], pause_after_ms=180, phrase_end='|')
                        effect_ranges.append((first, end, segment))
                    word_offset += count
                notes = build_notes(moras, base_pitch=str(self._getenv('PHONE_WORKER_TETO_BASE_PITCH') or 'C4'),
                                    speech_rate=rate, tempo=tempo, pitch_offset_semitones=pitch)
                for first, end, segment in effect_ranges:
                    if segment['hold_count']:
                        nucleus = next((number for number in range(end - 1, first - 1, -1) if notes[number].role == 'nucleus'), None)
                        if nucleus is not None:
                            notes[nucleus] = replace(notes[nucleus], duration_ms=notes[nucleus].duration_ms + 1000 * segment['hold_count'])
            if not notes:
                raise TetoSynthesisError('texto não gerou vogais compatíveis')
            entries = [index.resolve(note.candidates) for note in notes]
            if score is not None:
                for number, (note, entry) in enumerate(zip(notes, entries)):
                    if entry is None or note.role != 'nucleus':
                        continue
                    if len(note.source_phonemes) == 1:
                        # Attack aliases can contain mostly silence (notably
                        # Miku Sil V). A long sustain uses the authored steady
                        # vowel instead of stretching that silent attack.
                        candidates = tuple(dict.fromkeys(candidate.split()[-1] for candidate in note.candidates))
                        vowel_entry = index.resolve(candidates)
                        if vowel_entry is not None:
                            # ESPER's cut-combine needs a nonempty fixed head,
                            # even for an OTO vowel authored with consonant=0.
                            useful_ms = -vowel_entry.cutoff_ms
                            if vowel_entry.cutoff_ms >= 0:
                                with wave.open(str(vowel_entry.wav_path), 'rb') as source:
                                    useful_ms = source.getnframes() * 1000 / source.getframerate() - vowel_entry.offset_ms - vowel_entry.cutoff_ms
                            # A long authored sustained-vowel region can have
                            # evolving phase/voicing. Compressing its seconds
                            # into a short note caused destructive cancellation
                            # in native ESPER. Keep a stable 250 ms vowel body
                            # for its sustain model; leave attacks and source
                            # files intact. Both voices use one nucleus render.
                            cutoff = -250.0 if useful_ms > 500.0 else vowel_entry.cutoff_ms
                            entries[number] = replace(vowel_entry, consonant_ms=max(45.0, vowel_entry.consonant_ms), cutoff_ms=cutoff)
                    elif note.duration_ms >= 400:
                        # Preserve the recorded consonant and a short stable
                        # vowel head. The stretch region then starts in voicing
                        # rather than expanding an unvoiced onset for seconds.
                        useful_ms = -entry.cutoff_ms if entry.cutoff_ms < 0 else None
                        fixed_ms = max(entry.consonant_ms, entry.preutterance_ms + 30.0)
                        if useful_ms is not None:
                            fixed_ms = min(fixed_ms, max(entry.consonant_ms, useful_ms - 25.0))
                        entries[number] = replace(entry, consonant_ms=fixed_ms)
            if any(entry is None and note.role == 'nucleus' for note, entry in zip(notes, entries)):
                raise TetoSynthesisError('a voicebank não contém uma vogal necessária para esta apresentação')
            if self._index_profile == 'english-cvvc':
                entries = [self._english_render_entry(entry, note) if entry else None for note, entry in zip(notes, entries)]
            if score is not None:
                placements, preroll = self._singing_placements(notes, entries, spans, score)
                notes = [replace(note, duration_ms=placement.get('duration_ms', note.duration_ms))
                         for note, placement in zip(notes, placements)]
                planned_ms = float(score['duration_ms']) + preroll
                notes = align_singing_pitch(notes, placements, score, preroll_ms=preroll, tempo=tempo)
            else:
                if self._index_profile == 'english-cvvc':
                    placements, planned_ms = self._english_timeline_placements(notes, entries)
                    notes = align_pitch_to_timeline(notes, placements, tempo=tempo)
                else:
                    placements, cursor = [], 0.0
                    for note in notes:
                        placements.append({'anchor_ms': cursor, 'start_ms': cursor, 'lead_ms': 0.0, 'fade_ms': 4.0})
                        cursor += note.duration_ms + note.pause_after_ms
                    planned_ms = cursor
                if planned_ms > 20000:
                    raise TetoSynthesisError('áudio com efeitos TTV excedeu 20 segundos')
            target_frames = round(self.SAMPLE_RATE * planned_ms / 1000)
            if target_frames * 2 + 44 > max_audio_bytes:
                raise TetoSynthesisError('áudio TTV grande demais')
            stream_envelopes = []
            stream_cursor, stream_started_ms = 0, None
            stream_metadata = {
                'pcm_protocol': self.SINGING_PCM_PROTOCOL, 'audio_format': 'pcm_s16le',
                'sample_rate': self.SAMPLE_RATE, 'channels': 1, 'sample_width_bytes': 2,
                'normalization_gain': 1.0, 'midi_preroll_ms': round(preroll, 3),
                'voice_id': self.voice_id, 'reading_language': language,
                'pitch_offset_semitones': pitch, 'speech_rate': rate,
                'ttv_performance_hash': digest, 'expected_frames': target_frames,
                'duration_seconds': target_frames / self.SAMPLE_RATE,
                'pcm_normalization_policy': 'unitgain-native-v1',
            }
            if emit_pcm is not None:
                if segments:
                    word_index = 0
                    for segment in segments:
                        if segment['text'].strip() in {'|', '~'}:
                            continue
                        words = self._performance_phonemize(segment['text'], index, language)
                        count = max((item.word_index for item in words), default=-1) + 1
                        selected = [number for number, note in enumerate(notes)
                                    if word_index <= note.word_index < word_index + count]
                        if selected:
                            first = min(placements[number]['start_ms'] for number in selected)
                            end = max(spans[number][1] + preroll for number in selected)
                            stream_envelopes.append((round(first * self.SAMPLE_RATE / 1000),
                                round(end * self.SAMPLE_RATE / 1000),
                                segment['fade_in'], segment['fade_out']))
                        word_index += count
                # Format and musical preroll are fixed before publishing PCM.
                if emit_pcm(b'', dict(stream_metadata)) is False:
                    raise TetoResourceError('renderização TTV cancelada')
            future_starts = [target_frames] * (len(notes) + 1)
            for number in range(len(notes) - 1, -1, -1):
                future_starts[number] = min(future_starts[number + 1],
                    round((placements[number]['start_ms'] + placements[number].get('trim_lead_ms', 0.0))
                          * self.SAMPLE_RATE / 1000)
                    if entries[number] is not None else target_frames)

            def publish_pcm(samples):
                nonlocal stream_cursor, stream_started_ms
                for first in range(0, len(samples), self.SAMPLE_RATE // 2):
                    self._check_render_cancelled()
                    data = samples[first:first + self.SAMPLE_RATE // 2]
                    if os.sys.byteorder != 'little':
                        data.byteswap()
                    if emit_pcm(data.tobytes(), dict(stream_metadata)) is False:
                        raise TetoResourceError('renderização TTV cancelada')
                    stream_cursor += len(data)
                    if stream_started_ms is None:
                        stream_started_ms = (time.monotonic() - started) * 1000

            def publish_prefix(combined, number, intervals):
                if emit_pcm is None:
                    return
                reserve = round(self.SAMPLE_RATE * .004)
                end = max(0, min(target_frames, len(combined) - reserve,
                                 future_starts[number + 1] - reserve))
                if end - stream_cursor < self.SAMPLE_RATE // 10:
                    return
                pcm = self._sealed_pcm_range(combined, stream_cursor, end, intervals,
                    total_frames=target_frames, score=score, preroll=preroll, envelopes=stream_envelopes)
                publish_pcm(pcm)

            metrics = {'fragment_cache_hits': 0, 'fragment_cache_misses': 0, 'resampler_calls': 0}
            with tempfile.TemporaryDirectory(prefix='phone-worker-ttv-performance-') as temp:
                workdir = Path(temp)
                performance_sources = workdir / 'native-sources'
                performance_sources.mkdir()
                source_groups = {}
                for number, (note, entry) in enumerate(zip(notes, entries)):
                    if entry is not None:
                        source_groups.setdefault(entry.wav_path, []).append((number, note, entry))
                request_profile = self._index_profile
                notifications = queue.Queue() if emit_pcm is not None else None
                def render_group(items):
                    self._reading_context.profile = request_profile
                    self._reading_context.performance_sources = performance_sources
                    self._reading_context.performance_singing = score is not None
                    self._reading_context.cancelled = should_cancel if cancelled is not None or emit_pcm is not None else None
                    local = {key: 0 for key in metrics}
                    self._reading_context.render_metrics = local
                    try:
                        fragments = {}
                        for number, note, entry in items:
                            if time.monotonic() >= deadline:
                                raise TimeoutError('apresentação TTV excedeu o timeout')
                            self._check_resources()
                            fragments[number] = self._resample_note(index=index, entry=entry, note=note, workdir=workdir, deadline=deadline)
                            if notifications is not None:
                                notifications.put(('fragment', number, fragments[number]))
                        return fragments, local
                    finally:
                        self._clear_reading_context()
                maximum = 4 if self.voice_id == 'hatsune-miku' else 2
                workers = max(1, min(maximum, len(source_groups), self._env_int('PHONE_WORKER_TETO_RENDER_THREADS', maximum)))
                fragments = {}
                resample_start = time.monotonic()
                with ThreadPoolExecutor(max_workers=workers) as pool:
                    futures = [pool.submit(render_group, items)
                               for items in self._ordered_source_groups(source_groups, placements)]
                    try:
                        if notifications is not None:
                            renderer = self
                            for future in futures:
                                future.add_done_callback(lambda done: notifications.put(('done', done, None)))

                            class ReadyFragments(dict):
                                def __getitem__(self, number):
                                    while number not in self:
                                        renderer._check_render_cancelled()
                                        if time.monotonic() >= deadline:
                                            raise TimeoutError('apresentação TTV excedeu o timeout')
                                        try:
                                            kind, identity, value = notifications.get(timeout=.025)
                                        except queue.Empty:
                                            continue
                                        if kind == 'done':
                                            identity.result()
                                        else:
                                            if score is not None and value[1]:
                                                raise TetoSynthesisError('o resampler não aceitou a curva de notas MIDI; atualize o ESPER antes de cantar')
                                            self[identity] = value
                                    return super().__getitem__(number)

                            fragments = ReadyFragments()
                            compose_started = time.monotonic()
                            combined, missing, rendered, fallback, legacy, timeline = self._compose_english_cvvc(
                                notes=notes, entries=entries, fragments=fragments, deadline=deadline,
                                placements=placements, planned_end_ms=planned_ms, on_prefix=publish_prefix)
                            compose_ms = (time.monotonic() - compose_started) * 1000
                        for future in as_completed(futures):
                            result, local = future.result()
                            fragments.update(result)
                            for key in metrics:
                                metrics[key] += local[key]
                    except BaseException:
                        render_stopped.set()
                        for future in futures:
                            future.cancel()
                        raise
                resample_ms = (time.monotonic() - resample_start) * 1000
                if score is not None and any(fragment[1] for fragment in fragments.values()):
                    raise TetoSynthesisError('o resampler não aceitou a curva de notas MIDI; atualize o ESPER antes de cantar')
                self._check_render_cancelled()
                if emit_pcm is None:
                    compose_started = time.monotonic()
                    combined, missing, rendered, fallback, legacy, timeline = self._compose_english_cvvc(
                        notes=notes, entries=entries, fragments=fragments, deadline=deadline,
                        placements=placements, planned_end_ms=planned_ms)
                    compose_ms = (time.monotonic() - compose_started) * 1000
            postprocess_started = time.monotonic()
            target_frames = round(self.SAMPLE_RATE * planned_ms / 1000)
            if len(combined) < target_frames:
                combined.extend([0] * (target_frames - len(combined)))
            elif len(combined) > target_frames:
                del combined[target_frames:]
            peak = max((abs(sample) for sample in combined), default=0)
            if not peak:
                raise TetoSynthesisError('a apresentação TTV gerou áudio silencioso')
            scale = min(3.6, 30000.0 / peak) if emit_pcm is None else 1.0
            # Report the gain _apply_gain actually applies, including its
            # unity tolerance and headroom clamps. Progressive composition can
            # compensate per-request normalization before using a stable mix
            # gain across windows; reporting the requested scale would pump.
            normalization_gain = 1.0 if abs(float(scale) - 1.0) < 0.002 else max(0.60, min(1.20, float(scale)))
            self._check_render_cancelled()
            combined = self._apply_gain(combined, scale)
            self._check_render_cancelled()
            if score is not None:
                # MIDI velocity controls musical intensity, not UTAU consonant
                # velocity. Apply it after common peak normalization.
                self._apply_midi_velocity(combined, score['notes'], preroll,
                                          check_cancelled=self._check_render_cancelled if cancelled is not None else None)
                if segments:
                    word_index = 0
                    for segment in segments:
                        self._check_render_cancelled()
                        if segment['text'].strip() in {'|', '~'}:
                            continue
                        words = self._performance_phonemize(segment['text'], index, language)
                        count = max((item.word_index for item in words), default=-1) + 1
                        selected = [number for number, note in enumerate(notes) if word_index <= note.word_index < word_index + count]
                        if selected:
                            first = min(placements[number]['start_ms'] for number in selected)
                            end = max(spans[number][1] + preroll for number in selected)
                            self._performance_envelope(combined, round(first * self.SAMPLE_RATE / 1000), round(end * self.SAMPLE_RATE / 1000),
                                                       fade_in=segment['fade_in'], fade_out=segment['fade_out'])
                        word_index += count
            else:
                for first, end, segment in effect_ranges:
                    self._check_render_cancelled()
                    first_ms = min(placement['start_ms'] for placement in placements[first:end])
                    end_ms = max(placements[number]['anchor_ms'] + notes[number].duration_ms for number in range(first, end))
                    self._performance_envelope(combined, round(first_ms * self.SAMPLE_RATE / 1000), round(end_ms * self.SAMPLE_RATE / 1000),
                                               fade_in=segment['fade_in'], fade_out=segment['fade_out'])
            raw = self._performance_wav(combined, self.SAMPLE_RATE)
            self._check_render_cancelled()
            if emit_pcm is not None:
                publish_pcm(combined[stream_cursor:])
            postprocess_ms = (time.monotonic() - postprocess_started) * 1000
            if len(raw) > max_audio_bytes:
                raise TetoSynthesisError('áudio TTV grande demais')
            renderer_fingerprint = hashlib.sha256((self._request_fingerprint(index, pitch_offset_semitones=pitch,
                                                   speech_rate=rate, reading_language=language) + self.PERFORMANCE_VERSION + digest
                                                   + (':unitgain-native-v1' if emit_pcm is not None else '')).encode()).hexdigest()
            metadata = {'voice_id': self.voice_id, 'audio_format': 'wav', 'voicebank': index.name,
                        'voicebank_fingerprint': index.fingerprint, 'renderer_fingerprint': renderer_fingerprint,
                        'renderer_version': self.RENDER_VERSION, 'performance_version': self.PERFORMANCE_VERSION,
                        'performance_mode': 'singing' if score is not None else 'speech-effects',
                        'phonemizer_version': self.ENGLISH_PHONEMIZER_VERSION if language == 'en-US' else self.PHONEMIZER_VERSION,
                        'reading_language': language, 'pitch_offset_semitones': pitch, 'speech_rate': rate,
                        'ttv_performance_hash': digest, 'aliases': index.alias_count, 'voicebank_profile': self._index_profile,
                        'rendered_phonemes': rendered, 'missing_phonemes': missing, 'pitchbend_fallbacks': fallback,
                        'legacy_fragment_hits': legacy, 'render_workers': workers, 'source_groups': len(source_groups),
                        'resample_ms': round(resample_ms, 2), **metrics,
                        'compose_ms': round(compose_ms, 2), 'postprocess_ms': round(postprocess_ms, 2),
                        'normalization_gain': normalization_gain,
                        'worker_synth_ms': round((time.monotonic() - started) * 1000, 2),
                        'timeline_mode': 'midi-vowel-continuous' if score is not None else 'phrase-effects-continuous',
                        'timeline_planned_ms': round(planned_ms, 3), 'timeline_audio_ms': round(len(combined) * 1000 / self.SAMPLE_RATE, 3),
                        'midi_preroll_ms': round(preroll, 3), 'midi_notes': len(score['notes']) if score is not None else 0,
                        'musical_modulation': 0 if score is not None else self._modulation(),
                        'singing_nuclei': sum(note.role == 'nucleus' for note in notes) if score is not None else 0,
                        'melisma_notes': sum(max(0, len(group) - 1) for group in groups) if groups is not None else 0,
                        'duration_seconds': len(combined) / self.SAMPLE_RATE, 'sample_rate': self.SAMPLE_RATE,
                        'channels': 1, 'sample_width_bytes': 2, 'sha256': hashlib.sha256(raw).hexdigest()}
            cache_after = self._cache.statistics()
            metadata.update({key: round(cache_after[key] - cache_before[key], 2)
                             for key in ('cache_scans', 'cache_evictions', 'cache_maintenance_ms')})
            metadata['cache_size_bytes'] = cache_after['cache_size_bytes']
            pcm_after = self._pcm_cache_statistics()
            metadata.update({key: pcm_after[key] - pcm_before[key]
                             for key in ('pcm_cache_hits', 'pcm_cache_misses')})
            metadata['pcm_cache_bytes'] = pcm_after['pcm_cache_bytes']
            if score is not None:
                metadata['effective_score'] = score
            if emit_pcm is not None:
                metadata.update(pcm_protocol=self.SINGING_PCM_PROTOCOL,
                    pcm_normalization_policy='unitgain-native-v1',
                    first_pcm_ms=round(stream_started_ms or 0, 2), streamed_frames=stream_cursor)
            return raw, metadata
        finally:
            if cache_started:
                self._cache.end_render()
            self._clear_reading_context()
            self._lock.release()

    def synthesize(
        self,
        text: str,
        *,
        timeout_seconds: float = 25.0,
        max_audio_bytes: int = 8 * 1024 * 1024,
        pitch_offset_semitones: float = 0.0,
        speech_rate: float | None = None,
        reading_language: str | None = None,
    ) -> dict[str, Any]:
        effective_rate = self._effective_speech_rate(speech_rate)
        language = self._effective_reading_language(reading_language)
        if language == "pt-BR":
            if not self.status().get("ready"):
                raise TetoConfigurationError(str(self.status().get("last_error") or "Teto indisponível"))
        else:
            if not self._env_bool("PHONE_WORKER_TETO_ENABLED", False):
                raise TetoConfigurationError("PHONE_WORKER_TETO_ENABLED=false")
            self._resampler_command()
            if not shutil.which("ffmpeg"):
                raise TetoConfigurationError("ffmpeg não encontrado")
        clean_text = " ".join(str(text or "").strip().split())
        max_chars = max(16, self._env_int("PHONE_WORKER_TETO_MAX_CHARACTERS", 180))
        if not clean_text:
            raise ValueError("texto vazio")
        if len(clean_text) > max_chars:
            raise ValueError(f"texto grande demais para Teto ({len(clean_text)} > {max_chars})")
        self._check_resources()
        if not self._lock.acquire(blocking=False):
            raise TetoResourceError("renderer Teto ocupado")

        started = time.monotonic()
        pcm_before = self._pcm_cache_statistics()
        cache_started = False
        try:
            self._cache.begin_render()
            cache_started = True
            index = self._load_reading_index(language)
            max_moras = max(8, self._env_int("PHONE_WORKER_TETO_MAX_PHONEMES", 240))
            moras = phonemize(
                clean_text,
                max_moras=max_moras,
                resolve_alias=index.resolve,
                voicebank_profile=self._index_profile,
                **self._phonemizer_options(),
                **({"reading_language": language} if reading_language is not None else {}),
            )
            try:
                pitch_offset = float(pitch_offset_semitones)
            except (TypeError, ValueError):
                pitch_offset = 0.0
            if not math.isfinite(pitch_offset):
                pitch_offset = 0.0
            pitch_offset = max(-4.0, min(4.0, round(pitch_offset * 2.0) / 2.0))
            notes = build_notes(
                moras,
                base_pitch=str(self._getenv("PHONE_WORKER_TETO_BASE_PITCH") or "C4"),
                speech_rate=effective_rate,
                tempo=max(60, min(240, self._env_int("PHONE_WORKER_TETO_TEMPO", 140))),
                pitch_offset_semitones=pitch_offset,
            )
            if not notes:
                raise TetoSynthesisError("texto não gerou fonemas compatíveis")
            # Backward-compatible metric of the lexical plan, before fragment
            # boundaries move to their OTO sample coordinates. This is not an
            # F0 measurement of the synthesized WAV.
            pitch_boundary_max = max((
                abs(int(left.pitch_end_cents) - int(right.pitch_start_cents))
                for left, right in zip(notes, notes[1:]) if not left.phrase_end
            ), default=0)
            deadline = started + max(2.0, float(timeout_seconds))
            combined = array.array("h")
            missing: list[str] = []
            rendered = 0
            with tempfile.TemporaryDirectory(prefix="phone-worker-teto-") as temp:
                workdir = Path(temp)
                entries = [index.resolve(note.candidates) for note in notes]
                placements = None
                planned_end_ms = None
                if self._index_profile == "english-cvvc":
                    entries = [
                        self._english_render_entry(entry, note) if entry is not None else None
                        for note, entry in zip(notes, entries)
                    ]
                    # The post-preutterance fixed consonant must fit too. A
                    # generic 42 ms helper cap cannot truncate a recorded 50 ms
                    # nasal simply because its original vowel lead was long.
                    stretch = 2.0 ** (1.0 - self._velocity() / 100.0)
                    for number, (note, entry) in enumerate(zip(notes, entries)):
                        if entry is None:
                            continue
                        fixed_ms = math.floor(entry.consonant_ms * stretch / 5.0) * 5.0
                        required_ms = max(note.duration_ms, math.ceil(fixed_ms - self._english_lead_ms(entry, note) + 5.0))
                        if required_ms > 500:
                            raise TetoSynthesisError(f"região consonantal OTO longa demais: {entry.alias!r}")
                        if required_ms > note.duration_ms:
                            notes[number] = replace(note, duration_ms=required_ms)
                    placements, planned_end_ms = self._english_timeline_placements(notes, entries)
                    max_seconds = max(2, self._env_int("PHONE_WORKER_TETO_MAX_AUDIO_SECONDS", 20))
                    if planned_end_ms > max_seconds * 1000.0:
                        raise TetoSynthesisError(f"áudio Teto planejado excedeu {max_seconds}s")
                    notes = align_pitch_to_timeline(
                        notes, placements,
                        tempo=max(60, min(240, self._env_int("PHONE_WORKER_TETO_TEMPO", 140))),
                    )
                groups: dict[Path, list[tuple[int, RenderNote, OtoEntry]]] = {}
                pitchbend_fallbacks = 0
                legacy_fragment_hits = 0
                for number, (note, entry) in enumerate(zip(notes, entries)):
                    if entry is not None:
                        groups.setdefault(entry.wav_path, []).append((number, note, entry))

                request_profile = self._index_profile

                def render_group(items):
                    # One group per source WAV avoids racing Straycat's .sc
                    # analysis cache when two notes use the same recording.
                    self._reading_context.profile = request_profile
                    metrics = {"fragment_cache_hits": 0, "fragment_cache_misses": 0, "resampler_calls": 0}
                    self._reading_context.render_metrics = metrics
                    try:
                        return ({number: self._resample_note(index=index, entry=entry, note=note, workdir=workdir, deadline=deadline)
                                 for number, note, entry in items}, metrics)
                    finally:
                        self._clear_reading_context()

                fragments: dict[int, tuple[Path, bool, bool]] = {}
                # Only Miku opts into the larger pool. Notes sharing a recording
                # stay serial so ESPER/Straycat analysis files cannot race.
                maximum_workers = 4 if self.voice_id == "hatsune-miku" else 2
                workers = max(1, min(maximum_workers, len(groups),
                                    self._env_int("PHONE_WORKER_TETO_RENDER_THREADS", maximum_workers)))
                render_metrics = {"fragment_cache_hits": 0, "fragment_cache_misses": 0, "resampler_calls": 0}
                resample_started = time.monotonic()
                with ThreadPoolExecutor(max_workers=workers) as pool:
                    futures = [pool.submit(render_group, items) for items in self._ordered_source_groups(
                        groups, placements if self._index_profile == 'english-cvvc' else None)]
                    try:
                        for future in as_completed(futures):
                            group_fragments, group_metrics = future.result()
                            fragments.update(group_fragments)
                            for name, count in group_metrics.items():
                                render_metrics[name] += count
                    except BaseException:
                        for future in futures:
                            future.cancel()
                        raise
                resample_ms = (time.monotonic() - resample_started) * 1000.0

                timeline_meta: dict[str, float | int | str] = {"mode": "serial"}
                if self._index_profile == "english-cvvc":
                    (
                        combined, missing, rendered, pitchbend_fallbacks,
                        legacy_fragment_hits, timeline_meta,
                    ) = self._compose_english_cvvc(
                        notes=notes, entries=entries, fragments=fragments, deadline=deadline,
                        placements=placements, planned_end_ms=planned_end_ms,
                    )
                else:
                    for number, (note, entry) in enumerate(zip(notes, entries)):
                        if time.monotonic() >= deadline:
                            raise TimeoutError("renderização Teto excedeu o timeout")
                        if entry is None:
                            missing.append(note.candidates[0] if note.candidates else "?")
                            pause = int(self.SAMPLE_RATE * min(note.duration_ms, 160) / 1000)
                            combined.extend([0] * pause)
                            continue
                        fragment_path, neutral_pitch, legacy_fragment = fragments[number]
                        pitchbend_fallbacks += int(neutral_pitch)
                        legacy_fragment_hits += int(legacy_fragment)
                        fragment = self._apply_gain(self._read_samples(fragment_path), note.gain)
                        overlap_ms = self._oto_join_ms(entry)
                        if note.role == "epenthetic":
                            # Hide the artificial vowel required by a Japanese CV
                            # cluster approximation behind the neighbouring unit.
                            overlap_ms = min(60.0, max(overlap_ms, note.duration_ms * 0.58))
                        elif note.role in {"coda", "glide", "nasal"}:
                            overlap_ms = min(58.0, max(overlap_ms, note.duration_ms * 0.42))
                        self._append_crossfade(
                            combined, fragment, int(self.SAMPLE_RATE * overlap_ms / 1000.0)
                        )
                        # The phonemizer's tiny 6 ms word separator should not
                        # become a hard stop. Punctuation pauses remain explicit.
                        if note.pause_after_ms >= 24:
                            combined.extend([0] * int(self.SAMPLE_RATE * note.pause_after_ms / 1000.0))
                        rendered += 1

                if rendered <= 0:
                    raise TetoSynthesisError("nenhum alias da voicebank correspondeu ao texto")
                max_seconds = max(2, self._env_int("PHONE_WORKER_TETO_MAX_AUDIO_SECONDS", 20))
                max_samples = self.SAMPLE_RATE * max_seconds
                if len(combined) > max_samples:
                    raise TetoSynthesisError(f"áudio Teto excedeu {max_seconds}s")
                peak = max((abs(sample) for sample in combined), default=0)
                if peak > 0:
                    scale = min(1.8, 30000.0 / peak)
                    if abs(scale - 1.0) > 0.01:
                        for index_sample, sample in enumerate(combined):
                            combined[index_sample] = max(-32768, min(32767, int(sample * scale)))

                output = workdir / "teto.wav"
                with wave.open(str(output), "wb") as wav:
                    wav.setnchannels(1)
                    wav.setsampwidth(2)
                    wav.setframerate(self.SAMPLE_RATE)
                    data = array.array("h", combined)
                    if os.sys.byteorder != "little":
                        data.byteswap()
                    wav.writeframes(data.tobytes())
                raw = output.read_bytes()
            if not raw or len(raw) > max_audio_bytes:
                raise TetoSynthesisError(f"áudio Teto inválido ou grande demais ({len(raw)} bytes)")
            elapsed_ms = (time.monotonic() - started) * 1000.0
            pcm_after = self._pcm_cache_statistics()
            nuclei = [note.duration_ms for note in notes if note.role == "nucleus"]
            mean_nucleus_ms = (sum(nuclei) / len(nuclei)) if nuclei else 0.0
            nucleus_stddev_ms = (
                math.sqrt(sum((value - mean_nucleus_ms) ** 2 for value in nuclei) / len(nuclei))
                if nuclei else 0.0
            )
            return {
                "audio": raw,
                "voice_id": self.voice_id,
                "audio_format": "wav",
                "voicebank": index.name,
                "voicebank_fingerprint": index.fingerprint,
                "renderer_fingerprint": self._request_fingerprint(
                    index, pitch_offset_semitones=pitch_offset, speech_rate=effective_rate,
                    reading_language=language,
                ),
                "renderer_version": self.RENDER_VERSION,
                "phonemizer_version": self.ENGLISH_PHONEMIZER_VERSION if language == "en-US" else self.PHONEMIZER_VERSION,
                "reading_language": language,
                "speech_rate": effective_rate,
                "pitch_offset_semitones": pitch_offset,
                "aliases": index.alias_count,
                "voicebank_profile": self._index_profile,
                "rendered_phonemes": rendered,
                "missing_phonemes": missing[:12],
                "phonetic_units": sum(len(note.source_phonemes) for note in notes),
                "epenthetic_phonemes": sum(1 for note in notes if note.role == "epenthetic"),
                "auxiliary_phonemes": sum(1 for note in notes if note.role != "nucleus"),
                "cvvc_direct": sum(1 for note in notes if note.coverage == "cvvc-direct"),
                "cvvc_transitions": sum(1 for note in notes if note.coverage == "cvvc-transition"),
                "cluster_hits": sum(1 for note in notes if note.coverage == "cluster-hit"),
                "approximated_phonemes": sum(1 for note in notes if note.coverage == "approximation"),
                "coverage_percent": round(100.0 * sum(
                    1 for note, entry in zip(notes, entries)
                    if entry is not None and note.coverage in {"cvvc-direct", "cvvc-transition", "cluster-hit"}
                ) / max(1, len(notes)), 1),
                "alias_path_cost": round(sum(float(note.planner_cost) for note in notes), 2),
                "mean_nucleus_ms": round(mean_nucleus_ms, 2),
                "nucleus_duration_stddev_ms": round(nucleus_stddev_ms, 2),
                "pitch_boundary_max_cents": pitch_boundary_max,
                "pitch_boundary_metric": "lexical-plan-not-audio",
                "pitch_alignment_mode": "absolute-timeline" if self._index_profile == "english-cvvc" else "lexical",
                "pitchbend_fallbacks": pitchbend_fallbacks,
                "legacy_fragment_hits": legacy_fragment_hits,
                "timeline_mode": str(timeline_meta.get("mode") or "serial"),
                "timeline_planned_ms": timeline_meta.get("planned_ms"),
                "timeline_audio_ms": timeline_meta.get("audio_ms"),
                "timeline_serialized_ms": timeline_meta.get("serialized_ms"),
                "timeline_aux_overlays": timeline_meta.get("aux_overlays", 0),
                "timeline_max_preutterance_ms": timeline_meta.get("max_preutterance_ms"),
                "continuity_repairs": timeline_meta.get("continuity_repairs", 0),
                "energy_boundary_max_db": timeline_meta.get("energy_boundary_max_db", 0.0),
                "silence_gap_fades": timeline_meta.get("silence_gap_fades", 0),
                "render_workers": workers,
                "source_groups": len(groups),
                **render_metrics,
                **{key: pcm_after[key] - pcm_before[key]
                   for key in ('pcm_cache_hits', 'pcm_cache_misses')},
                'pcm_cache_bytes': pcm_after['pcm_cache_bytes'],
                "resample_ms": round(resample_ms, 2),
                "worker_synth_ms": round(elapsed_ms, 2),
                "sha256": hashlib.sha256(raw).hexdigest(),
            }
        finally:
            if cache_started:
                self._cache.end_render()
            self._clear_reading_context()
            self._lock.release()
