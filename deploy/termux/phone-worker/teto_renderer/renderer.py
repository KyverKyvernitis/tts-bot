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
    RENDER_VERSION = "speech-4e-phrase-speech"
    PHONEMIZER_VERSION = "ptbr-g2p-xsampa-cvvc-v1"
    FRAGMENT_CACHE_SCHEMA = "teto-fragment-v3-continuous"
    LEGACY_FRAGMENT_RENDER_VERSION = "speech-3-natural"

    def __init__(self, *, resource_guard: Callable[[], dict[str, Any]] | None = None):
        self._resource_guard = resource_guard
        self._lock = threading.Lock()
        self._index: VoicebankIndex | None = None
        self._index_path = ""
        self._index_profile = "standard"
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

    def _english_voicebank_dir(self) -> str:
        configured = str(os.getenv("PHONE_WORKER_TETO_ENGLISH_VOICEBANK_DIR") or "").strip()
        if configured:
            return configured
        return str(Path.home() / "voicebanks" / "kasane-teto-english")

    def _voicebank_mode(self) -> str:
        mode = str(os.getenv("PHONE_WORKER_TETO_VOICEBANK_MODE") or "auto").strip().lower()
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
                return self._index
            try:
                index = VoicebankIndex.load(resolved, minimum_aliases=minimum_aliases)
            except Exception as exc:
                errors.append(f"{profile}: {type(exc).__name__}: {exc}")
                if self._voicebank_mode() != "auto":
                    raise
                continue
            self._index = index
            self._index_path = resolved
            self._index_profile = profile
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
            "voice": "kasane-teto-standard",
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
                    "voice": "kasane-teto-english-cvvc" if self._index_profile == "english-cvvc" else "kasane-teto-standard",
                    "voicebank_profile": self._index_profile,
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
        preutterance = max(0.0, float(entry.preutterance_ms))
        overlap = max(0.0, float(entry.overlap_ms))
        if note.role == "nucleus":
            cap = 180.0
        elif note.role in {"transition", "cluster"}:
            cap = 120.0
        elif note.role in {"coda", "glide", "nasal"}:
            cap = 90.0
        else:
            cap = 70.0
        return min(cap, max(preutterance, overlap))

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
        cursor = 0.0
        pending_onsets: list[int] = []

        for index, note in enumerate(notes):
            if note.role in {"transition", "cluster"}:
                pending_onsets.append(index)
                continue

            if note.role == "nucleus":
                if pending_onsets:
                    # Order onset pieces by their *sample starts*, not only by
                    # lexical anchors. A nucleus with a long preutterance can
                    # begin earlier than a short '- br' alias; if we ignored
                    # that, the nucleus would overwrite the cluster attack.
                    nucleus_sample_start = cursor - leads[index]
                    count = len(pending_onsets)
                    for local, pending in enumerate(pending_onsets):
                        before = min(42.0, (count - local) * 12.0)
                        desired_start = nucleus_sample_start - before
                        anchors[pending] = desired_start + leads[pending]
                    pending_onsets.clear()

                anchors[index] = cursor
                cursor += float(note.duration_ms)
            else:
                # Post-nucleus articulation starts at the current lexical
                # boundary.  Its own preutterance will move the sample earlier
                # and therefore replace/merge with the vowel tail.
                anchors[index] = cursor
                cursor += self._continuous_aux_advance_ms(note)

            if note.pause_after_ms >= 24:
                cursor += float(note.pause_after_ms)

        # Defensive handling for a malformed/planner-truncated sequence ending
        # in an onset alias without a following nucleus.
        if pending_onsets:
            for local, pending in enumerate(pending_onsets):
                anchors[pending] = cursor + local * 8.0
            cursor += max(0.0, (len(pending_onsets) - 1) * 8.0)

        placements: list[dict[str, float]] = []
        minimum_start = 0.0
        first = True
        for index, (note, entry) in enumerate(zip(notes, entries)):
            lead = leads[index]
            start = anchors[index] - lead
            overlap = 0.0
            if entry is not None:
                # OTO overlap is the real crossfade hint.  Add only a small
                # share of the preutterance as a safety floor; after this fade
                # the new VCV fragment owns the overlap instead of being mixed
                # at half volume for its entire consonant region.
                overlap = max(float(entry.overlap_ms), min(24.0, lead * 0.28))
                overlap = max(4.0, min(48.0, overlap))
            if first or start < minimum_start:
                minimum_start = start
                first = False
            placements.append({
                "anchor_ms": anchors[index],
                "start_ms": start,
                "lead_ms": lead,
                "fade_ms": overlap,
            })

        shift = -minimum_start if minimum_start < 0.0 else 0.0
        if shift:
            for placement in placements:
                placement["anchor_ms"] += shift
                placement["start_ms"] += shift
        return placements, cursor + shift

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

    def _compose_english_cvvc(
        self, *, notes: list[RenderNote], entries: list[OtoEntry | None],
        fragments: dict[int, tuple[Path, bool, bool]], deadline: float
    ) -> tuple[array.array, list[str], int, int, int, dict[str, float | int | str]]:
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

        for number, (note, entry, placement) in enumerate(zip(notes, entries, placements)):
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
            start_sample = round(self.SAMPLE_RATE * placement["start_ms"] / 1000.0)
            fade_samples = round(self.SAMPLE_RATE * placement["fade_ms"] / 1000.0)
            fragment, boundary_db, repaired = self._match_fragment_energy(
                combined, fragment, start_sample=start_sample,
                fade_samples=fade_samples, role=note.role,
            )
            max_energy_boundary_db = max(max_energy_boundary_db, boundary_db)
            continuity_repairs += int(repaired)
            self._place_timeline_fragment(
                combined, fragment, start_sample=start_sample, fade_samples=fade_samples
            )
            max_lead_ms = max(max_lead_ms, float(placement["lead_ms"]))
            auxiliary_overlays += int(note.role != "nucleus")
            rendered += 1

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
        }
        return combined, missing, rendered, pitchbend_fallbacks, legacy_fragment_hits, timeline

    def synthesize(
        self,
        text: str,
        *,
        timeout_seconds: float = 25.0,
        max_audio_bytes: int = 8 * 1024 * 1024,
        pitch_offset_semitones: float = 0.0,
    ) -> dict[str, Any]:
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
                voicebank_profile=self._index_profile,
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
                base_pitch=str(os.getenv("PHONE_WORKER_TETO_BASE_PITCH") or "C4"),
                speech_rate=self._speech_rate(),
                tempo=max(60, min(240, self._env_int("PHONE_WORKER_TETO_TEMPO", 140))),
                pitch_offset_semitones=pitch_offset,
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

                timeline_meta: dict[str, float | int | str] = {"mode": "serial"}
                if self._index_profile == "english-cvvc":
                    (
                        combined, missing, rendered, pitchbend_fallbacks,
                        legacy_fragment_hits, timeline_meta,
                    ) = self._compose_english_cvvc(
                        notes=notes, entries=entries, fragments=fragments, deadline=deadline
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
            nuclei = [note.duration_ms for note in notes if note.role == "nucleus"]
            mean_nucleus_ms = (sum(nuclei) / len(nuclei)) if nuclei else 0.0
            nucleus_stddev_ms = (
                math.sqrt(sum((value - mean_nucleus_ms) ** 2 for value in nuclei) / len(nuclei))
                if nuclei else 0.0
            )
            pitch_boundary_max = 0
            for left, right in zip(notes, notes[1:]):
                if left.phrase_end:
                    continue
                pitch_boundary_max = max(
                    pitch_boundary_max,
                    abs(int(left.pitch_end_cents) - int(right.pitch_start_cents)),
                )
            return {
                "audio": raw,
                "audio_format": "wav",
                "voicebank": index.name,
                "voicebank_fingerprint": index.fingerprint,
                "renderer_fingerprint": self._render_fingerprint(index),
                "renderer_version": self.RENDER_VERSION,
                "phonemizer_version": self.PHONEMIZER_VERSION,
                "speech_rate": self._speech_rate(),
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
                "coverage_percent": round(100.0 * sum(1 for note in notes if note.coverage in {"cvvc-direct", "cvvc-transition", "cluster-hit"}) / max(1, len(notes)), 1),
                "alias_path_cost": round(sum(float(note.planner_cost) for note in notes), 2),
                "mean_nucleus_ms": round(mean_nucleus_ms, 2),
                "nucleus_duration_stddev_ms": round(nucleus_stddev_ms, 2),
                "pitch_boundary_max_cents": pitch_boundary_max,
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
                "worker_synth_ms": round(elapsed_ms, 2),
                "sha256": hashlib.sha256(raw).hexdigest(),
            }
        finally:
            self._lock.release()
