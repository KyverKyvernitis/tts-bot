from __future__ import annotations

import array
import contextlib
import hashlib
import json
import math
import os
import shlex
import shutil
import subprocess
import tempfile
import threading
import time
import wave
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable

from .cache import FragmentCache
from .errors import TetoConfigurationError, TetoResourceError, TetoSynthesisError
from .phonemizer import phonemize
from .prosody import RenderNote, build_notes
from .voicebank import OtoEntry, VoicebankIndex


class TetoRenderer:
    SAMPLE_RATE = 44100
    RENDER_VERSION = "speech-4-phonetic"
    PHONEMIZER_VERSION = "ptbr-g2p-v1"
    FRAGMENT_CACHE_SCHEMA = "teto-fragment-v2"
    LEGACY_FRAGMENT_RENDER_VERSION = "speech-3-natural"

    def __init__(self, *, resource_guard: Callable[[], dict[str, Any]] | None = None):
        self._resource_guard = resource_guard
        self._lock = threading.Lock()
        self._index: VoicebankIndex | None = None
        self._index_path = ""
        self._last_status_at = 0.0
        self._last_status: dict[str, Any] = {}
        cache_root = os.getenv("PHONE_WORKER_TETO_FRAGMENT_CACHE_DIR") or str(Path.home() / "phone-worker" / "cache" / "teto-fragments")
        self._cache = FragmentCache(cache_root, max_mb=self._env_int("PHONE_WORKER_TETO_FRAGMENT_CACHE_MB", 256))

    @staticmethod
    def _env_bool(name: str, default: bool = False) -> bool:
        value = str(os.getenv(name, "") or "").strip().lower()
        if value in {"1", "true", "yes", "on", "sim"}:
            return True
        if value in {"0", "false", "no", "off", "nao", "não"}:
            return False
        return default

    @staticmethod
    def _env_int(name: str, default: int) -> int:
        try:
            return int(str(os.getenv(name, default)).strip())
        except (TypeError, ValueError):
            return default

    def _voicebank_dir(self) -> str:
        return str(os.getenv("PHONE_WORKER_TETO_VOICEBANK_DIR") or "").strip()

    def _speech_rate(self) -> float:
        try:
            value = float(os.getenv("PHONE_WORKER_TETO_SPEECH_RATE", "1.0"))
            return max(0.75, min(1.5, value)) if math.isfinite(value) else 1.0
        except (TypeError, ValueError):
            return 1.0

    def _velocity(self) -> int:
        return max(1, min(200, self._env_int("PHONE_WORKER_TETO_VELOCITY", 100)))

    def _modulation(self) -> int:
        return max(0, min(100, self._env_int("PHONE_WORKER_TETO_MODULATION", 15)))

    def _length_mode(self) -> str:
        mode = str(os.getenv("PHONE_WORKER_TETO_LENGTH_MODE", "auto")).strip().lower()
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
            "pitch": os.getenv("PHONE_WORKER_TETO_BASE_PITCH", "C4"),
            "rate": self._speech_rate(),
            "velocity": self._velocity(),
            "modulation": self._modulation(),
            "flags": os.getenv("PHONE_WORKER_TETO_FLAGS", ""),
            "tempo": max(60, min(240, self._env_int("PHONE_WORKER_TETO_TEMPO", 140))),
            "length_mode": self._length_mode(),
        }
        return hashlib.sha256(json.dumps(profile, sort_keys=True).encode()).hexdigest()

    def _render_fingerprint(self, index: VoicebankIndex) -> str:
        return self._render_fingerprint_for_version(index, self.RENDER_VERSION)

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
            "flags": os.getenv("PHONE_WORKER_TETO_FLAGS", ""),
            "length_mode": self._length_mode(),
        }
        return hashlib.sha256(json.dumps(profile, sort_keys=True).encode()).hexdigest()

    def _resampler_command(self) -> list[str]:
        raw = str(os.getenv("PHONE_WORKER_TETO_RESAMPLER_COMMAND") or "").strip()
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

    def _load_index(self) -> VoicebankIndex:
        configured = self._voicebank_dir()
        if not configured:
            raise TetoConfigurationError("PHONE_WORKER_TETO_VOICEBANK_DIR não configurado")
        resolved = str(Path(configured).expanduser().resolve())
        if self._index is None or self._index_path != resolved:
            self._index = VoicebankIndex.load(resolved, minimum_aliases=self._env_int("PHONE_WORKER_TETO_MIN_ALIASES", 10))
            self._index_path = resolved
        return self._index

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
            "voice": "kasane-teto-standard",
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
                    "ok": True,
                    "available": True,
                    "ready": True,
                    "resampler": " ".join(command[:2]),
                    "voicebank_fingerprint": index.fingerprint,
                    "fingerprint": self._render_fingerprint(index),
                    "renderer_version": self.RENDER_VERSION,
                    "phonemizer_version": self.PHONEMIZER_VERSION,
                    "speech_rate": self._speech_rate(),
                    "last_error": "",
                })
            except Exception as exc:
                result["last_error"] = f"{type(exc).__name__}: {exc}"[:220]
        self._last_status_at = now
        self._last_status = dict(result)
        return result

    def fingerprint(self) -> str:
        try:
            return self._render_fingerprint(self._load_index())
        except Exception:
            return "unavailable"

    def _check_resources(self) -> None:
        if self._resource_guard is None:
            return
        snapshot = self._resource_guard() or {}
        if not snapshot.get("ok", False):
            raise TetoResourceError(str(snapshot.get("reason") or "recursos insuficientes para Teto"))

    @staticmethod
    def _format_number(value: float) -> str:
        return f"{float(value):.3f}".rstrip("0").rstrip(".") or "0"

    def _resampler_length(self, entry: OtoEntry, note: RenderNote) -> float:
        if self._length_mode() != "post-consonant":
            return float(note.duration_ms)
        # Straycat adds its consonantal region to LENGTH. WORLD uses 5 ms
        # frames, so budget that region before requesting the vowel duration.
        stretch = 2.0 ** (1.0 - self._velocity() / 100.0)
        consonant = max(0.0, entry.consonant_ms) * stretch
        consonant = math.floor(consonant / 5.0) * 5.0
        duration = math.ceil(note.duration_ms / 5.0) * 5.0
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
        return "|".join((
            fingerprint,
            entry.cache_identity(),
            note.pitch,
            note.pitchbend,
            str(note.duration_ms),
            str(self._velocity()),
            str(self._modulation()),
            str(os.getenv("PHONE_WORKER_TETO_FLAGS") or ""),
        ))

    def _resample_note(
        self, *, index: VoicebankIndex, entry: OtoEntry, note: RenderNote, workdir: Path, deadline: float
    ) -> tuple[Path, bool, bool]:
        payload = self._fragment_cache_payload(
            fingerprint=self._fragment_fingerprint(index), entry=entry, note=note
        )
        key = self._cache.key(payload)
        cached = self._cache.get(key)
        if cached is not None:
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
            return legacy_cached, False, True

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("tempo da renderização Teto esgotado")
        raw_output = workdir / f"{key}.raw.wav"
        normalized_output = workdir / f"{key}.wav"

        def run_resampler(pitchbend: str) -> tuple[subprocess.CompletedProcess[bytes], list[str]]:
            command = self._resampler_command() + [
                str(entry.wav_path),
                str(raw_output),
                note.pitch,
                str(self._velocity()),
                str(os.getenv("PHONE_WORKER_TETO_FLAGS") or ""),
                self._format_number(entry.offset_ms),
                self._format_number(self._resampler_length(entry, note)),
                self._format_number(entry.consonant_ms),
                self._format_number(entry.cutoff_ms),
                "100",
                str(self._modulation()),
                f"!{max(60, min(240, self._env_int('PHONE_WORKER_TETO_TEMPO', 140)))}",
                pitchbend,
            ]
            proc = subprocess.run(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=max(0.5, deadline - time.monotonic()),
                check=False,
            )
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
        )
        if ffmpeg.returncode != 0 or not normalized_output.is_file() or normalized_output.stat().st_size <= 44:
            error = ffmpeg.stderr.decode("utf-8", errors="replace")[-500:]
            raise TetoSynthesisError(f"ffmpeg não normalizou fragmento: {error or ffmpeg.returncode}")
        if pitchbend_fallback:
            return normalized_output, True, False
        return self._cache.put(key, normalized_output), False, False

    def _read_samples(self, path: Path) -> array.array:
        with wave.open(str(path), "rb") as wav:
            if wav.getnchannels() != 1 or wav.getsampwidth() != 2 or wav.getframerate() != self.SAMPLE_RATE:
                raise TetoSynthesisError(f"fragmento WAV inesperado: {path.name}")
            samples = array.array("h")
            samples.frombytes(wav.readframes(wav.getnframes()))
            if os.sys.byteorder != "little":
                samples.byteswap()
            return samples

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

    def synthesize(self, text: str, *, timeout_seconds: float = 25.0, max_audio_bytes: int = 8 * 1024 * 1024) -> dict[str, Any]:
        if not self.status().get("ready"):
            raise TetoConfigurationError(str(self.status().get("last_error") or "Teto indisponível"))
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
        try:
            index = self._load_index()
            max_moras = max(8, self._env_int("PHONE_WORKER_TETO_MAX_PHONEMES", 240))
            moras = phonemize(
                clean_text,
                max_moras=max_moras,
                resolve_alias=index.resolve,
            )
            notes = build_notes(
                moras,
                base_pitch=str(os.getenv("PHONE_WORKER_TETO_BASE_PITCH") or "C4"),
                speech_rate=self._speech_rate(),
                tempo=max(60, min(240, self._env_int("PHONE_WORKER_TETO_TEMPO", 140))),
            )
            if not notes:
                raise TetoSynthesisError("texto não gerou fonemas compatíveis")
            deadline = started + max(2.0, float(timeout_seconds))
            combined = array.array("h")
            missing: list[str] = []
            rendered = 0
            with tempfile.TemporaryDirectory(prefix="phone-worker-teto-") as temp:
                workdir = Path(temp)
                entries = [index.resolve(note.candidates) for note in notes]
                groups: dict[Path, list[tuple[int, RenderNote, OtoEntry]]] = {}
                pitchbend_fallbacks = 0
                legacy_fragment_hits = 0
                for number, (note, entry) in enumerate(zip(notes, entries)):
                    if entry is not None:
                        groups.setdefault(entry.wav_path, []).append((number, note, entry))

                def render_group(items):
                    # One group per source WAV avoids racing Straycat's .sc
                    # analysis cache when two notes use the same recording.
                    return {number: self._resample_note(index=index, entry=entry, note=note, workdir=workdir, deadline=deadline)
                            for number, note, entry in items}

                fragments: dict[int, tuple[Path, bool, bool]] = {}
                workers = max(1, min(2, self._env_int("PHONE_WORKER_TETO_RENDER_THREADS", 2)))
                with ThreadPoolExecutor(max_workers=workers) as pool:
                    futures = [pool.submit(render_group, items) for items in groups.values()]
                    try:
                        for future in as_completed(futures):
                            fragments.update(future.result())
                    except BaseException:
                        for future in futures:
                            future.cancel()
                        raise

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
                    self._append_crossfade(combined, fragment, int(self.SAMPLE_RATE * overlap_ms / 1000.0))
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
            return {
                "audio": raw,
                "audio_format": "wav",
                "voicebank": index.name,
                "voicebank_fingerprint": index.fingerprint,
                "renderer_fingerprint": self._render_fingerprint(index),
                "renderer_version": self.RENDER_VERSION,
                "phonemizer_version": self.PHONEMIZER_VERSION,
                "speech_rate": self._speech_rate(),
                "aliases": index.alias_count,
                "rendered_phonemes": rendered,
                "missing_phonemes": missing[:12],
                "phonetic_units": sum(len(note.source_phonemes) for note in notes),
                "epenthetic_phonemes": sum(1 for note in notes if note.role == "epenthetic"),
                "auxiliary_phonemes": sum(1 for note in notes if note.role != "nucleus"),
                "pitchbend_fallbacks": pitchbend_fallbacks,
                "legacy_fragment_hits": legacy_fragment_hits,
                "worker_synth_ms": round(elapsed_ms, 2),
                "sha256": hashlib.sha256(raw).hexdigest(),
            }
        finally:
            self._lock.release()
