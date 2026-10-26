from __future__ import annotations

import array
import contextlib
import hashlib
import io
import json
import math
import os
import re
import shlex
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import wave
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
    is_default_score, default_melody_score,
)
from .voicebank import OtoEntry, VoicebankIndex


class TetoRenderer:
    SAMPLE_RATE = 44100
    RENDER_VERSION = "speech-4g-cvvc-pauses"
    PHONEMIZER_VERSION = "ptbr-g2p-xsampa-cvvc-v3"
    ENGLISH_PHONEMIZER_VERSION = "en-us-cmudict-xsampa-cvvc-v2"
    FRAGMENT_CACHE_SCHEMA = "teto-fragment-v4-timeline-pitch"
    LEGACY_FRAGMENT_RENDER_VERSION = "speech-3-natural"
    PERFORMANCE_VERSION = "ttv-performance-5-default-melody"
    PERFORMANCE_PROTOCOL = 2
    SUPPORTS_BACKGROUND_CANCELLATION = True
    # This bridge closes/waits for its ESPER supervisor on KeyboardInterrupt.
    # Custom resamplers retain foreground support, without speculative warming.
    BACKGROUND_BRIDGE_SHA256 = "b80dc4c34717611a6f7dd7056dcc6c31f1e59b3b0d660b9fe746773ce16e4f74"

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
        if language == "pt-BR":
            return renderer
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
            "phonemizer": self.ENGLISH_PHONEMIZER_VERSION, "phonemizer_sha256": source_hash,
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

    def supports_background_cancellation(self) -> bool:
        if self.RENDER_VERSION.startswith('worldline'):
            return False
        try:
            for candidate in self._resampler_command()[:2]:
                path = Path(candidate).expanduser()
                if path.is_file() and path.stat().st_size <= 128 * 1024:
                    if hashlib.sha256(path.read_bytes()).hexdigest() == self.BACKGROUND_BRIDGE_SHA256:
                        return True
        except (OSError, TetoConfigurationError):
            pass
        return False

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
            if performance_sources is not None:
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
            command = self._resampler_command() + [
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
            if cancelled is None:
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
        for offset in range(overlap):
            if fade > 0 and offset < fade:
                phase = (offset + 1) / (fade + 1) * (math.pi / 2.0)
                # sin²/cos² is constant-sum but has zero slope at both ends, so
                # a join does not announce itself as a new envelope attack.
                ratio = math.sin(phase) ** 2
            else:
                ratio = 1.0
            position = start + offset
            mixed = int(target[position] * (1.0 - ratio) + fragment[offset] * ratio)
            target[position] = max(-32768, min(32767, mixed))

        remainder = fragment[overlap:]
        if remainder:
            if start + overlap == len(target):
                target.extend(remainder)
            else:
                needed = start + len(fragment) - len(target)
                if needed > 0:
                    target.extend([0] * needed)
                for offset, sample in enumerate(remainder, start=overlap):
                    target[start + offset] = sample

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
    def _performance_envelope(samples: array.array, start: int, end: int, *, fade_in=False, fade_out=False) -> None:
        start, end = max(0, int(start)), min(len(samples), int(end))
        if end <= start:
            return
        # Keep most of a short word fully audible; the first consonant gets
        # a gradual attack rather than spending its whole lifetime at silence.
        length = min((end - start) // 3, round(TetoRenderer.SAMPLE_RATE * 0.26))
        if not length:
            return
        for offset in range(length):
            gain = 0.12 + 0.88 * math.sin(offset / max(1, length - 1) * math.pi / 2.0) ** 2
            if fade_in:
                samples[start + offset] = round(samples[start + offset] * gain)
            if fade_out:
                samples[end - 1 - offset] = round(samples[end - 1 - offset] * gain)

    def _performance_phonemize(self, text, index, language):
        return phonemize(text, max_moras=1025, resolve_alias=index.resolve,
                         voicebank_profile=self._index_profile,
                         reading_language=language, **self._phonemizer_options())

    @classmethod
    def _apply_midi_velocity(cls, samples: array.array, notes, preroll: float, *, check_cancelled=None) -> None:
        """Keep the original PCM rounding, evaluating sin only during the ramp."""
        previous_gain = previous_end = None
        for event in notes:
            if check_cancelled is not None:
                check_cancelled()
            first = max(0, round(cls.SAMPLE_RATE * (preroll + event['start_ms']) / 1000))
            end = min(len(samples), round(cls.SAMPLE_RATE * (preroll + event['start_ms'] + event['duration_ms']) / 1000))
            gain = 0.72 + 0.28 * math.sqrt(event['velocity'] / 127.0)
            incoming = previous_gain if previous_gain is not None and first - previous_end <= round(cls.SAMPLE_RATE * 0.02) else gain
            smooth_frames = min(end - first, round(cls.SAMPLE_RATE * 0.008))
            ramp_end = min(end, first + max(1, smooth_frames - 1))
            if incoming != gain:
                for number in range(first, ramp_end):
                    ratio = math.sin((number - first) / max(1, smooth_frames - 1) * math.pi / 2.0) ** 2
                    samples[number] = round(samples[number] * (incoming + (gain - incoming) * ratio))
            else:
                ramp_end = first
            # Preserve this arithmetic rather than substituting `gain`: the
            # former expression can round differently by one PCM unit.
            tail_gain = incoming + (gain - incoming)
            if tail_gain != 1.0:
                for number in range(ramp_end, end):
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
        placements = []
        for note, entry, (start, end, group) in zip(notes, entries, spans):
            lead = self._english_lead_ms(entry, note) if entry else 0.0
            nucleus_number = next(number for number, (other, span) in enumerate(zip(notes, spans))
                                  if other.role == 'nucleus' and span[2] == group)
            nucleus_entry = entries[nucleus_number]
            nucleus_lead = self._english_lead_ms(nucleus_entry, notes[nucleus_number]) if nucleus_entry else 0.0
            if note.role == 'nucleus':
                anchor = start
            elif note.role == 'vowel-exit':
                anchor = start - nucleus_lead
            elif note.role in {'transition', 'cluster', 'epenthetic'}:
                anchor = start - nucleus_lead - min(45.0, note.duration_ms * 0.6)
            else:
                anchor = end - note.duration_ms
            placements.append({'anchor_ms': anchor, 'start_ms': anchor - lead,
                               'lead_ms': lead, 'fade_ms': self._english_fade_ms(entry, lead),
                               'score_group': group})
        preroll = max(0.0, -min((placement['start_ms'] for placement in placements), default=0.0))
        if preroll > 350:
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
            data = array.array('h', samples)
            if os.sys.byteorder != 'little':
                data.byteswap()
            wav.writeframes(data.tobytes())
        return output.getvalue()

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
                               cancelled: Callable[[], bool] | None = None):
        """Render score vowels on a musical clock, or a phrase with inline effects.

        MIDI uses original pitches/tempo and a consonantal preroll reported in
        metadata. Every melisma is one nucleus render with multiple F0 targets.
        """
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
            return render_stopped.is_set() or cancelled()
        try:
            self._reading_context.cancelled = should_cancel if cancelled is not None else None
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
                spoken = [segment for segment in segments if segment['text'].strip() not in {'|', '~'}]
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
                    if segment['hold']:
                        nucleus = next((number for number in range(end - 1, first - 1, -1) if notes[number].role == 'nucleus'), None)
                        if nucleus is not None:
                            notes[nucleus] = replace(notes[nucleus], duration_ms=min(1200, notes[nucleus].duration_ms + round(280 / rate)))
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
                def render_group(items):
                    self._reading_context.profile = request_profile
                    self._reading_context.performance_sources = performance_sources
                    self._reading_context.performance_singing = score is not None
                    self._reading_context.cancelled = should_cancel if cancelled is not None else None
                    local = {key: 0 for key in metrics}
                    self._reading_context.render_metrics = local
                    try:
                        fragments = {}
                        for number, note, entry in items:
                            if time.monotonic() >= deadline:
                                raise TimeoutError('apresentação TTV excedeu o timeout')
                            self._check_resources()
                            fragments[number] = self._resample_note(index=index, entry=entry, note=note, workdir=workdir, deadline=deadline)
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
            scale = min(3.6, 30000.0 / peak)
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
            postprocess_ms = (time.monotonic() - postprocess_started) * 1000
            if len(raw) > max_audio_bytes:
                raise TetoSynthesisError('áudio TTV grande demais')
            renderer_fingerprint = hashlib.sha256((self._request_fingerprint(index, pitch_offset_semitones=pitch,
                                                   speech_rate=rate, reading_language=language) + self.PERFORMANCE_VERSION + digest).encode()).hexdigest()
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
