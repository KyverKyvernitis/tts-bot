"""Termux phrase adapter for the pinned native WORLDLINE-R library.

The library runs in the existing ARM64 glibc guest.  Android never loads it,
and neither Box64 nor an external UTAU resampler is involved. Source WAVs are
decoded into a private job directory; voicebanks are kept read only.
"""
from __future__ import annotations

import array
import contextlib
import hashlib
import json
import math
import os
import re
import shutil
import signal
import struct
import subprocess
import tempfile
import threading
import time
import wave
from bisect import bisect_right
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable

from .errors import TetoConfigurationError, TetoResourceError, TetoSynthesisError
from .phonemizer import phonemize
from .prosody import RenderNote, _pitch_midi, build_notes
from .renderer import TetoRenderer
from .voicebank import OtoEntry, VoicebankIndex


ARM64_LIBRARY_SHA256 = "80fb77357de4fae608e2d2fa12db867d0c48d0366263e6a280eccd90a1584dfe"
SOURCE_COMMIT = "a60ca5830b9064556157245d4bf8f5920d93e5f8"
SOURCE_VERSION = "0.1.565"


class WorldlineRenderer(TetoRenderer):
    RENDER_VERSION = "worldline-r-phrase-1"
    FRAME_MS = 10.0
    MAX_SOURCE_BYTES = 30 * 1024 * 1024
    MAX_DECODED_BYTES = 64 * 1024 * 1024
    MAX_SOURCE_SECONDS = 20

    def __init__(self, *, resource_guard: Callable[[], dict[str, Any]] | None = None):
        # No fragment cache: the native phrase API shares one F0 envelope and
        # spectral timeline across all requests in the utterance.
        self._resource_guard = resource_guard
        self._lock = threading.Lock()
        self._index = None
        self._index_path = ""
        self._index_profile = "standard"
        self._voicebank_fallback_reason = ""
        self._last_status_at = 0.0
        self._last_status: dict[str, Any] = {}

    def _container(self) -> str:
        container = str(os.getenv("PHONE_WORKER_WORLDLINE_CONTAINER") or
                        os.getenv("PHONE_WORKER_TETO_WORLDLINE_CONTAINER") or "voicepeak-arm64").strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", container):
            raise TetoConfigurationError("container WORLDLINE-R inválido")
        return container

    def _library(self) -> Path:
        return Path(os.getenv("PHONE_WORKER_WORLDLINE_LIBRARY") or
                    os.getenv("PHONE_WORKER_TETO_WORLDLINE_LIBRARY") or
                    str(Path.home() / ".worldline-r" / "lib" / "libworldline.so")).expanduser().resolve()

    def _library_metadata(self) -> dict[str, Any]:
        path = self._library()
        if not path.is_file() or not 64 <= path.stat().st_size <= 8 * 1024 * 1024:
            raise TetoConfigurationError("biblioteca WORLDLINE-R não instalada; execute setup.py")
        data = path.read_bytes()
        if (data[:6] != b"\x7fELF\x02\x01" or struct.unpack_from("<H", data, 16)[0] != 3
                or struct.unpack_from("<H", data, 18)[0] != 183):
            raise TetoConfigurationError("WORLDLINE-R precisa da biblioteca Linux ARM64 oficial")
        digest = hashlib.sha256(data).hexdigest()
        if digest != ARM64_LIBRARY_SHA256:
            raise TetoConfigurationError("SHA-256 WORLDLINE-R difere do release fixado 0.1.565")
        return {"library_sha256": digest, "native_architecture": "arm64", "library": str(path)}

    @staticmethod
    def _adapter_hash() -> str:
        digest = hashlib.sha256()
        for name in ("worldline.py", "worldline_native.py", "renderer.py", "voicebank.py",
                     "prosody.py", "phonemizer.py", "ptbr_g2p.py"):
            path = Path(__file__).with_name(name)
            if not path.is_file():
                raise TetoConfigurationError(f"adaptador WORLDLINE-R incompleto: {name}")
            digest.update(name.encode("ascii"))
            digest.update(path.read_bytes())
        return digest.hexdigest()

    def _render_fingerprint(self, index: VoicebankIndex) -> str:
        profile = {
            "backend": "worldline-r", "version": self.RENDER_VERSION,
            "native_sha256": self._library_metadata()["library_sha256"],
            "source_commit": SOURCE_COMMIT, "adapter_sha256": self._adapter_hash(),
            "voicebank": index.fingerprint, "voicebank_profile": self._index_profile,
            "voicebank_mode": self._voicebank_mode(),
            "pitch": os.getenv("PHONE_WORKER_TETO_BASE_PITCH", "C4"),
            "rate": self._speech_rate(), "velocity": self._velocity(),
            "modulation": self._modulation(), "tempo": self._tempo(),
        }
        return hashlib.sha256(json.dumps(profile, sort_keys=True).encode()).hexdigest()

    def _tempo(self) -> int:
        return max(60, min(240, self._env_int("PHONE_WORKER_TETO_TEMPO", 140)))

    def _velocity(self) -> int:
        return max(20, min(200, self._env_int("PHONE_WORKER_TETO_VELOCITY", 100)))

    def _guest_command(self, library: Path, workdir: Path | None = None) -> list[str]:
        if not shutil.which("proot-distro"):
            raise TetoConfigurationError("proot-distro não encontrado")
        if ":" in str(library.parent) or ":" in str(Path(__file__).parent) or (workdir and ":" in str(workdir)):
            raise TetoConfigurationError("caminho WORLDLINE-R incompatível com bind do proot")
        command = ["proot-distro", "login", self._container(),
                   "--bind", f"{library.parent}:/opt/worldline-lib",
                   "--bind", f"{Path(__file__).resolve().parent}:/opt/worldline-code"]
        if workdir is not None:
            command += ["--bind", f"{workdir}:/opt/worldline-job"]
        command += ["--", "/usr/bin/env", "LANG=C", "LC_ALL=C", "/usr/bin/python3",
                    "/opt/worldline-code/worldline_native.py", "--library", f"/opt/worldline-lib/{library.name}"]
        if workdir is None:
            return command + ["--probe", "--timeout", "6"]
        return command + ["--job", "/opt/worldline-job/job.json", "--output", "/opt/worldline-job/output.wav"]

    @staticmethod
    def _run(command: list[str], *, deadline: float) -> tuple[str, str]:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("tempo WORLDLINE-R esgotado")
        # File-backed output prevents a noisy guest/native process from filling
        # the host's memory. A new session lets a timeout stop the full tree.
        with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
            process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
                                       start_new_session=True)
            try:
                process.wait(timeout=remaining)
            except BaseException:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                process.wait()
                raise
            stdout.seek(0, os.SEEK_END)
            stdout.seek(max(0, stdout.tell() - 65536))
            output = stdout.read().decode("utf-8", errors="replace")
            stderr.seek(0, os.SEEK_END)
            stderr.seek(max(0, stderr.tell() - 65536))
            error = stderr.read().decode("utf-8", errors="replace")
        if process.returncode or re.search(r"terminated with signal|segmentation fault|uncaught target signal", output + error, re.I):
            raise TetoSynthesisError(f"WORLDLINE-R falhou ({process.returncode}): {error[-500:]}")
        return output, error

    @staticmethod
    def _proof(output: str) -> dict[str, Any]:
        try:
            result = json.loads(output.strip().splitlines()[-1])
        except (IndexError, ValueError, TypeError) as exc:
            raise TetoSynthesisError("WORLDLINE-R não retornou confirmação JSON") from exc
        if not isinstance(result, dict) or result.get("ok") is not True:
            raise TetoSynthesisError(str(result.get("error") if isinstance(result, dict) else "resposta WORLDLINE-R inválida")[:500])
        return result

    def status(self, *, force: bool = False) -> dict[str, Any]:
        now = time.monotonic()
        ttl = max(5, min(30, self._env_int("PHONE_WORKER_TETO_STATUS_CACHE_SECONDS", 15)))
        if not force and self._last_status and now - self._last_status_at <= ttl:
            return dict(self._last_status)
        enabled = self._env_bool("PHONE_WORKER_TETO_ENABLED", False)
        result: dict[str, Any] = {
            "ok": False, "available": False, "ready": False, "enabled": enabled,
            "engine": "teto", "backend": "worldline-r", "voice": "kasane-teto-standard",
            "voicebank_profile": "standard", "renderer_version": self.RENDER_VERSION,
            "phrase_adapter_available": True, "box64_required": False, "voicepeak_required": False,
            "portuguese_speech_verified": False,
        }
        try:
            if not enabled:
                raise TetoConfigurationError("PHONE_WORKER_TETO_ENABLED=false")
            if not shutil.which("ffmpeg"):
                raise TetoConfigurationError("ffmpeg não encontrado")
            metadata = self._library_metadata()
            fingerprint = self._adapter_hash()
            index = self._load_index()
            output, _ = self._run(self._guest_command(self._library()), deadline=now + 8.0)
            probe = self._proof(output)
            if not (probe.get("api_verified") is True and probe.get("abi_verified") is True
                    and probe.get("library_hash_verified") is True
                    and probe.get("library_sha256") == metadata["library_sha256"]
                    and probe.get("native_architecture") == metadata["native_architecture"]
                    and probe.get("source_commit") == SOURCE_COMMIT
                    and probe.get("source_version") == SOURCE_VERSION):
                raise TetoConfigurationError("probe WORLDLINE-R não confirmou API, ABI e biblioteca fixada")
            result.update(index.snapshot())
            result.update(metadata)
            result.update({
                "ok": True, "available": True, "ready": True,
                "voice": "kasane-teto-english-cvvc" if self._index_profile == "english-cvvc" else "kasane-teto-standard",
                "voicebank_profile": self._index_profile, "voicebank_mode": self._voicebank_mode(),
                "voicebank_fallback": bool(self._voicebank_fallback_reason),
                "voicebank_fallback_reason": self._voicebank_fallback_reason,
                "voicebank_fingerprint": index.fingerprint, "fingerprint": self._render_fingerprint(index),
                "adapter_sha256": fingerprint, "container": self._container(),
                "runtime_ready": True, "tts_ready": True, "native_c_api": True,
                "phonemizer_version": self.PHONEMIZER_VERSION,
                "pitch_alignment_mode": "absolute-timeline", "speech_rate": self._speech_rate(), "last_error": "",
            })
        except Exception as exc:
            result["last_error"] = f"{type(exc).__name__}: {exc}"[:260]
        self._last_status_at = time.monotonic()
        self._last_status = dict(result)
        return result

    def _placements(self, notes: list[RenderNote], entries: list[OtoEntry | None]) -> tuple[list[dict[str, float]], float]:
        if self._index_profile == "english-cvvc":
            placements, end = self._english_timeline_placements(notes, entries)
        else:
            placements = []
            cursor = 0.0
            after_pause = True
            for note, entry in zip(notes, entries):
                fade = 0.0 if after_pause or entry is None else min(self._oto_join_ms(entry), note.duration_ms * 0.35)
                if note.role == "epenthetic" and not after_pause:
                    fade = min(60.0, note.duration_ms * 0.58)
                start = max(0.0, cursor - fade)
                placements.append({"anchor_ms": start, "start_ms": start, "lead_ms": 0.0, "fade_ms": fade})
                cursor = start + note.duration_ms
                after_pause = note.pause_after_ms >= 24
                if after_pause:
                    cursor += note.pause_after_ms
            end = cursor
        # WORLDLINE's phrase compositor uses 10 ms frames. Quantizing positions
        # before generating F0 ensures the curve and request coordinates agree.
        for placement in placements:
            original_fade = placement["fade_ms"]
            for key in ("start_ms", "lead_ms", "fade_ms"):
                placement[key] = round(placement[key] / self.FRAME_MS) * self.FRAME_MS
            placement["anchor_ms"] = placement["start_ms"] + placement["lead_ms"]
            if original_fade > 0:
                placement["fade_ms"] = max(self.FRAME_MS, placement["fade_ms"])
        end = max(end, max((p["start_ms"] + p["lead_ms"] + n.duration_ms for p, n in zip(placements, notes)), default=0))
        return placements, math.ceil(end / self.FRAME_MS) * self.FRAME_MS

    def _curves(self, notes: list[RenderNote], placements: list[dict[str, float]], end_ms: float,
                pauses: list[list[float]]) -> dict[str, list[float]]:
        count = math.ceil(end_ms / self.FRAME_MS) + 1
        f0 = [0.0] * count
        voicing = [0.0] * count
        first = 0
        spans = []
        for number, note in enumerate(notes):
            if note.phrase_end:
                spans.append((first, number))
                first = number + 1
        if first < len(notes):
            spans.append((first, len(notes) - 1))
        for first, last in spans:
            nuclei = [i for i in range(first, last + 1) if notes[i].role == "nucleus"] or list(range(first, last + 1))
            initial, terminal = notes[nuclei[0]], notes[nuclei[-1]]
            knots = [(placements[nuclei[0]]["anchor_ms"], _pitch_midi(initial.pitch) * 100 + initial.pitch_start_cents)]
            for i in nuclei:
                note = notes[i]
                peak = note.pitch_peak_cents if note.pitch_peak_cents is not None else (note.pitch_start_cents + note.pitch_end_cents) / 2
                knots.append((placements[i]["anchor_ms"] + note.duration_ms * 0.48, _pitch_midi(note.pitch) * 100 + peak))
            knots.append((placements[nuclei[-1]]["anchor_ms"] + terminal.duration_ms,
                          _pitch_midi(terminal.pitch) * 100 + terminal.pitch_end_cents))
            ordered = dict(sorted(knots))
            times = sorted(ordered)
            start = min(placements[i]["start_ms"] for i in range(first, last + 1))
            stop = max(placements[i]["start_ms"] + placements[i]["lead_ms"] + notes[i].duration_ms for i in range(first, last + 1))
            for frame in range(max(0, math.floor(start / self.FRAME_MS)), min(count, math.ceil(stop / self.FRAME_MS) + 1)):
                point = frame * self.FRAME_MS
                right = bisect_right(times, point)
                if right == 0:
                    cents = ordered[times[0]]
                elif right == len(times):
                    cents = ordered[times[-1]]
                else:
                    lo, hi = times[right - 1], times[right]
                    ratio = max(0.0, min(1.0, (point - lo) / (hi - lo)))
                    ratio = ratio * ratio * (3 - 2 * ratio)
                    cents = ordered[lo] + (ordered[hi] - ordered[lo]) * ratio
                f0[frame] = 440 * 2 ** ((cents / 100 - 69) / 12)
                voicing[frame] = 1.0
        for start, stop in pauses:
            for frame in range(max(0, math.ceil(start / self.FRAME_MS)), min(count, math.ceil(stop / self.FRAME_MS))):
                f0[frame] = 0.0
                voicing[frame] = 0.0
        return {"f0": f0, "gender": [0.5] * count, "tension": [0.5] * count,
                "breathiness": [0.5] * count, "voicing": voicing}

    def _decode_sources(self, entries: list[OtoEntry | None], workdir: Path, deadline: float) -> dict[Path, str]:
        sources: dict[Path, str] = {}
        total = 0
        total_frames = 0
        for entry in entries:
            if entry is None or entry.wav_path in sources:
                continue
            self._check_resources()
            source = entry.wav_path
            if not source.is_file() or source.stat().st_size > self.MAX_SOURCE_BYTES:
                raise TetoSynthesisError(f"WAV da voicebank ausente ou grande demais: {source.name}")
            name = f"source_{len(sources)}.wav"
            target = workdir / name
            if self._native_wav(source):
                with wave.open(str(source), "rb") as wav:
                    if wav.getnframes() > self.SAMPLE_RATE * self.MAX_SOURCE_SECONDS:
                        raise TetoSynthesisError(f"WAV da voicebank excede {self.MAX_SOURCE_SECONDS}s: {source.name}")
                shutil.copyfile(source, target)
            else:
                # One extra millisecond lets the length check reject, rather
                # than silently truncate, an oversized source recording.
                self._run(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
                           "-i", str(source), "-t", str(self.MAX_SOURCE_SECONDS + 0.001),
                           "-ac", "1", "-ar", str(self.SAMPLE_RATE), "-c:a", "pcm_s16le", str(target)],
                          deadline=min(deadline, time.monotonic() + 30.0))
            if not self._native_wav(target):
                raise TetoSynthesisError(f"FFmpeg não decodificou WAV da voicebank: {source.name}")
            with wave.open(str(target), "rb") as wav:
                if wav.getnframes() > self.SAMPLE_RATE * self.MAX_SOURCE_SECONDS:
                    raise TetoSynthesisError(f"WAV da voicebank excede {self.MAX_SOURCE_SECONDS}s: {source.name}")
                total_frames += wav.getnframes()
            total += target.stat().st_size
            if total > self.MAX_DECODED_BYTES or total_frames > self.SAMPLE_RATE * 180:
                raise TetoResourceError("WAVs decodificados da frase excedem o limite de memória")
            sources[source] = name
        return sources

    def _job(self, notes: list[RenderNote], entries: list[OtoEntry | None], sources: dict[Path, str],
             max_seconds: int) -> dict[str, Any]:
        placements, end_ms = self._placements(notes, entries)
        if end_ms > max_seconds * 1000:
            raise TetoSynthesisError(f"áudio Teto planejado excedeu {max_seconds}s")
        requests = []
        pauses: list[list[float]] = []
        for note, entry, placement in zip(notes, entries, placements):
            length_ms = math.ceil((note.duration_ms + placement["lead_ms"]) / self.FRAME_MS) * self.FRAME_MS
            stop = placement["start_ms"] + length_ms
            if entry is None and (self._index_profile != "english-cvvc" or note.role == "nucleus"):
                pauses.append([placement["start_ms"], stop])
            elif entry is not None:
                requests.append({
                    "sample_file": sources[entry.wav_path], "tone": _pitch_midi(note.pitch),
                    "con_vel": self._velocity(), "offset_ms": entry.offset_ms,
                    "required_length_ms": length_ms, "consonant_ms": entry.consonant_ms,
                    "cutoff_ms": entry.cutoff_ms, "volume": note.gain * 100,
                    "modulation": self._modulation(), "tempo": self._tempo(),
                    "position_ms": placement["start_ms"], "skip_ms": 0.0,
                    "length_ms": length_ms, "fade_in_ms": placement["fade_ms"], "fade_out_ms": 10.0,
                })
            if note.pause_after_ms >= 24:
                pauses.append([stop, min(end_ms, stop + note.pause_after_ms)])
        if not requests:
            raise TetoSynthesisError("nenhum alias da voicebank correspondeu ao texto")
        requests.sort(key=lambda req: (req["position_ms"] + req["length_ms"], req["position_ms"]))
        # Merge adjacent/missing-note pauses so the native bounded interval
        # list stays bounded by the number of requests and note boundaries.
        merged: list[list[float]] = []
        for start, stop in sorted(pauses):
            if stop <= start:
                continue
            if merged and start <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], stop)
            else:
                merged.append([start, stop])
        pauses = merged
        return {"schema_version": 1, "sample_rate": self.SAMPLE_RATE, "frame_ms": self.FRAME_MS,
                "duration_ms": end_ms, "max_output_seconds": max_seconds, "requests": requests,
                "curves": self._curves(notes, placements, end_ms, pauses), "silence_intervals_ms": pauses}

    def _read_output(self, path: Path, *, max_audio_bytes: int, max_seconds: int) -> bytes:
        if not path.is_file() or not 44 < path.stat().st_size <= max_audio_bytes:
            raise TetoSynthesisError("WORLDLINE-R retornou áudio ausente ou grande demais")
        if not self._native_wav(path):
            raise TetoSynthesisError("WORLDLINE-R retornou WAV em formato inesperado")
        with wave.open(str(path), "rb") as wav:
            frames = wav.getnframes()
            if frames > self.SAMPLE_RATE * max_seconds + 1:
                raise TetoSynthesisError("WORLDLINE-R excedeu o limite de duração")
            pcm = wav.readframes(frames)
            if len(pcm) != frames * 2:
                raise TetoSynthesisError("WORLDLINE-R retornou WAV truncado")
            samples = array.array("h", pcm)
            if not any(samples):
                raise TetoSynthesisError("WORLDLINE-R retornou silêncio")
        return path.read_bytes()

    def synthesize(self, text: str, *, timeout_seconds: float = 25.0, max_audio_bytes: int = 8 * 1024 * 1024,
                   pitch_offset_semitones: float = 0.0) -> dict[str, Any]:
        started = time.monotonic()
        timeout = float(timeout_seconds)
        if not math.isfinite(timeout) or timeout <= 0 or timeout > 120:
            raise ValueError("timeout Teto deve estar entre 0 e 120 segundos")
        deadline = started + timeout
        clean_text = " ".join(str(text or "").strip().split())
        max_chars = max(16, min(2000, self._env_int("PHONE_WORKER_TETO_MAX_CHARACTERS", 180)))
        if not clean_text or len(clean_text) > max_chars:
            raise ValueError("texto vazio ou grande demais para Teto")
        if not self._env_bool("PHONE_WORKER_TETO_ENABLED", False):
            raise TetoConfigurationError("PHONE_WORKER_TETO_ENABLED=false")
        self._check_resources()
        if not self._lock.acquire(blocking=False):
            raise TetoResourceError("renderer Teto ocupado")
        try:
            if not shutil.which("ffmpeg"):
                raise TetoConfigurationError("ffmpeg não encontrado")
            metadata = self._library_metadata()
            fingerprint = self._render_fingerprint(self._load_index())
            index = self._load_index()
            max_moras = max(8, min(256, self._env_int("PHONE_WORKER_TETO_MAX_PHONEMES", 240)))
            moras = phonemize(clean_text, max_moras=max_moras + 1, resolve_alias=index.resolve, voicebank_profile=self._index_profile)
            if len(moras) > max_moras:
                raise ValueError(f"texto gerou mais de {max_moras} fonemas para Teto")
            try:
                offset = float(pitch_offset_semitones)
            except (ValueError, TypeError):
                offset = 0.0
            offset = max(-4, min(4, round(offset * 2) / 2)) if math.isfinite(offset) else 0.0
            notes = build_notes(moras, base_pitch=os.getenv("PHONE_WORKER_TETO_BASE_PITCH") or "C4",
                                speech_rate=self._speech_rate(), tempo=self._tempo(), pitch_offset_semitones=offset)
            if not notes:
                raise TetoSynthesisError("texto não gerou fonemas compatíveis")
            entries = [index.resolve(note.candidates) for note in notes]
            entries = [self._english_render_entry(entry, note) if entry is not None else None for note, entry in zip(notes, entries)]
            stretch = 2 ** (1 - self._velocity() / 100)
            for number, (note, entry) in enumerate(zip(notes, entries)):
                if entry is None:
                    continue
                lead = self._english_lead_ms(entry, note)
                fixed = math.ceil(max(0, entry.consonant_ms * stretch - lead + 10) / self.FRAME_MS) * self.FRAME_MS
                duration = math.ceil(max(note.duration_ms, fixed) / self.FRAME_MS) * self.FRAME_MS
                if duration > 500:
                    raise TetoSynthesisError(f"região consonantal OTO longa demais: {entry.alias!r}")
                notes[number] = replace(note, duration_ms=int(duration))
            max_seconds = max(2, min(120, self._env_int("PHONE_WORKER_TETO_MAX_AUDIO_SECONDS", 20)))
            # Reject oversized phrases before decoding any source WAVs.
            if self._placements(notes, entries)[1] > max_seconds * 1000:
                raise TetoSynthesisError(f"áudio Teto planejado excedeu {max_seconds}s")
            with tempfile.TemporaryDirectory(prefix="phone-worker-worldline-") as temporary:
                workdir = Path(temporary)
                sources = self._decode_sources(entries, workdir, deadline)
                job = self._job(notes, entries, sources, max_seconds)
                (workdir / "job.json").write_text(json.dumps(job, ensure_ascii=False, allow_nan=False), encoding="utf-8")
                self._check_resources()
                command = self._guest_command(self._library(), workdir)
                command += ["--timeout", str(max(0.01, deadline - time.monotonic()))]
                output, _ = self._run(command, deadline=deadline)
                proof = self._proof(output)
                if (proof.get("phrase_render_verified") is not True or
                        proof.get("library_sha256") != metadata["library_sha256"] or
                        proof.get("api_verified") is not True or proof.get("abi_verified") is not True or
                        proof.get("native_architecture") != metadata["native_architecture"] or
                        proof.get("source_commit") != SOURCE_COMMIT or proof.get("source_version") != SOURCE_VERSION):
                    raise TetoSynthesisError("WORLDLINE-R não confirmou a renderização da frase e seu pin")
                audio_path = workdir / "output.wav"
                raw = self._read_output(audio_path, max_audio_bytes=max_audio_bytes, max_seconds=max_seconds)
                with wave.open(str(audio_path), "rb") as audio:
                    if type(proof.get("frames")) is not int or proof["frames"] != audio.getnframes():
                        raise TetoSynthesisError("confirmação WORLDLINE-R difere do número de frames do WAV")
            missing = [note.candidates[0] if note.candidates else "?" for note, entry in zip(notes, entries) if entry is None]
            nuclei = [note.duration_ms for note in notes if note.role == "nucleus"]
            mean = sum(nuclei) / max(1, len(nuclei))
            return {
                "audio": raw, "audio_format": "wav", "backend": "worldline-r", "voicebank": index.name,
                "voicebank_fingerprint": index.fingerprint, "renderer_fingerprint": fingerprint,
                "renderer_version": self.RENDER_VERSION, "phonemizer_version": self.PHONEMIZER_VERSION,
                "library_sha256": metadata["library_sha256"], "native_phrase_render_verified": True,
                "portuguese_speech_verified": False, "speech_rate": self._speech_rate(), "pitch_offset_semitones": offset,
                "aliases": index.alias_count, "voicebank_profile": self._index_profile,
                "rendered_phonemes": len(job["requests"]), "missing_phonemes": missing[:12],
                "phonetic_units": sum(len(n.source_phonemes) for n in notes),
                "epenthetic_phonemes": sum(n.role == "epenthetic" for n in notes),
                "auxiliary_phonemes": sum(n.role != "nucleus" for n in notes),
                "cvvc_direct": sum(n.coverage == "cvvc-direct" for n in notes),
                "cvvc_transitions": sum(n.coverage == "cvvc-transition" for n in notes),
                "cluster_hits": sum(n.coverage == "cluster-hit" for n in notes),
                "approximated_phonemes": sum(n.coverage == "approximation" for n in notes),
                "coverage_percent": round(100 * sum(e is not None for e in entries) / max(1, len(entries)), 1),
                "alias_path_cost": round(sum(n.planner_cost for n in notes), 2),
                "mean_nucleus_ms": round(mean, 2),
                "nucleus_duration_stddev_ms": round(math.sqrt(sum((v - mean) ** 2 for v in nuclei) / max(1, len(nuclei))), 2),
                "pitch_alignment_mode": "absolute-timeline", "pitch_boundary_metric": "not-measured",
                "pitch_boundary_max_cents": None, "f0_control_mode": "phrase-shared-absolute-timeline",
                "pitchbend_fallbacks": 0, "legacy_fragment_hits": 0,
                "timeline_mode": "worldline-phrase-continuous", "timeline_planned_ms": job["duration_ms"],
                "timeline_audio_ms": 1000 * proof.get("frames", 0) / self.SAMPLE_RATE,
                "timeline_serialized_ms": sum(n.duration_ms + (n.pause_after_ms if n.pause_after_ms >= 24 else 0) for n in notes),
                "timeline_aux_overlays": sum(n.role != "nucleus" for n in notes),
                "timeline_max_preutterance_ms": max((self._english_lead_ms(e, n) for n, e in zip(notes, entries) if e is not None), default=0),
                "continuity_repairs": 0, "energy_boundary_max_db": None, "energy_boundary_metric": "not-measured",
                "worker_synth_ms": round((time.monotonic() - started) * 1000, 2), "sha256": hashlib.sha256(raw).hexdigest(),
            }
        finally:
            self._lock.release()
