#!/usr/bin/env python3
"""Render independent Portuguese-adapted and English Miku samples with ESPER."""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib.util
import io
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import wave

TOOLKIT_DIR = Path(__file__).resolve().parent
WORKER_DIR = TOOLKIT_DIR.parents[1] / "termux" / "phone-worker"
MAX_AUDIO_BYTES = 8 * 1024 * 1024
MAX_REPORT_BYTES = 128 * 1024
SAMPLES = (
    ("01-miku-portugues", "pt-BR", "Olá. Eu sou a Miku. Como você está?"),
    ("02-miku-english", "en-US", "Hello. I am Miku. How are you?"),
)


def installer():
    spec = importlib.util.spec_from_file_location("_miku_voicebank_installer", TOOLKIT_DIR / "install-voicebank.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def controls(values: dict[str, str], *, directory: Path, container: str, timeout: float) -> dict[str, str]:
    # Supply a complete voice-local profile; never change os.environ or copy
    # the Teto timbre/flags into the new voice by accident.
    helper = installer()
    configured = helper.configuration(directory, values, container=container)
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith(("PHONE_WORKER_TETO_", "PHONE_WORKER_MIKU_", "PHONE_WORKER_ESPER_"))}
    environment.update({key: value for key, value in values.items() if key.startswith("PHONE_WORKER_ESPER_")})
    environment.update({
        "PHONE_WORKER_TETO_ENABLED": "true", "PHONE_WORKER_TETO_BACKEND": "utau",
        "PHONE_WORKER_TETO_VOICEBANK_MODE": "english",
        "PHONE_WORKER_TETO_VOICEBANK_DIR": str(directory),
        "PHONE_WORKER_TETO_ENGLISH_VOICEBANK_DIR": str(directory),
        "PHONE_WORKER_TETO_MIN_ALIASES": "500", "PHONE_WORKER_TETO_ENGLISH_MIN_ALIASES": "500",
        "PHONE_WORKER_TETO_ALIAS_SUFFIX": configured["PHONE_WORKER_MIKU_ALIAS_SUFFIX"],
        "PHONE_WORKER_TETO_RESAMPLER_COMMAND": configured["PHONE_WORKER_MIKU_RESAMPLER_COMMAND"],
        "PHONE_WORKER_TETO_BASE_PITCH": "C4", "PHONE_WORKER_TETO_SPEECH_RATE": "1.0",
        "PHONE_WORKER_TETO_TEMPO": "140", "PHONE_WORKER_TETO_VELOCITY": "100",
        "PHONE_WORKER_TETO_MODULATION": "15", "PHONE_WORKER_TETO_FLAGS": "",
        "PHONE_WORKER_TETO_LENGTH_MODE": "total",
        "PHONE_WORKER_TETO_RENDER_THREADS": values.get("PHONE_WORKER_MIKU_RENDER_THREADS") or "4",
        "PHONE_WORKER_TETO_MAX_CHARACTERS": "180", "PHONE_WORKER_TETO_MAX_PHONEMES": "240",
        "PHONE_WORKER_TETO_MAX_AUDIO_SECONDS": "20",
        "PHONE_WORKER_TETO_FRAGMENT_CACHE_DIR": values.get("PHONE_WORKER_MIKU_FRAGMENT_CACHE_DIR")
            or str(Path.home() / "phone-worker/cache/miku-fragments"),
        "PHONE_WORKER_ESPER_CONTAINER": container,
        "PHONE_WORKER_ESPER_TIMEOUT": str(min(timeout, 60.0)),
        "PHONE_WORKER_ESPER_DEADLINE": str(time.monotonic() + timeout),
    })
    return environment


def wav_evidence(audio: bytes) -> dict:
    if not 44 < len(audio) <= MAX_AUDIO_BYTES:
        raise ValueError("WAV vazio ou acima do limite de 8 MiB")
    with wave.open(io.BytesIO(audio), "rb") as wav:
        channels, width, rate, frames = wav.getnchannels(), wav.getsampwidth(), wav.getframerate(), wav.getnframes()
        if channels != 1 or width != 2 or rate != 44100 or wav.getcomptype() != "NONE" or not 0 < frames <= 20 * rate + 1:
            raise ValueError("esperado WAV PCM mono de 16 bits, 44100 Hz e até 20 segundos")
        pcm = wav.readframes(frames)
        if len(pcm) != frames * channels * width or not any(pcm):
            raise ValueError("WAV truncado ou silencioso")
    return {"wav_verified": True, "sample_rate": rate, "channels": channels,
            "sample_width_bytes": width, "sample_frames": frames, "duration_seconds": frames / rate}


def render_sample(job: dict) -> dict:
    helper = installer()
    directory = Path(job["voicebank"])
    if helper.check_existing(directory) is None:
        raise ValueError("instalação Miku não confirmada; execute install-voicebank.py")
    if str(WORKER_DIR) not in sys.path:
        sys.path.insert(0, str(WORKER_DIR))
    from teto_renderer import TetoRenderer
    environment = controls(job["settings"], directory=directory, container=job["container"], timeout=job["timeout"])
    renderer = TetoRenderer(environment=environment, voice_id="hatsune-miku",
                            resource_guard=lambda: {"ok": True, "reason": "miku-independent-sample"})
    status = renderer.status(force=True)
    if not status.get("ready"):
        raise ValueError(status.get("last_error") or "Miku ou ESPER indisponível")
    rendered = renderer.synthesize(job["text"], timeout_seconds=job["timeout"], max_audio_bytes=MAX_AUDIO_BYTES,
                                   pitch_offset_semitones=0.0, speech_rate=1.0, reading_language=job["language"])
    audio = rendered.pop("audio", None)
    if not isinstance(audio, (bytes, bytearray)):
        raise ValueError("renderer não retornou WAV")
    # Verify the same request controls that the bot checks on its HTTP path.
    if (rendered.get("voice_id") != "hatsune-miku" or rendered.get("reading_language") != job["language"]
            or rendered.get("pitch_offset_semitones") != 0.0 or rendered.get("speech_rate") != 1.0):
        raise ValueError("renderer retornou voz, idioma, tom ou velocidade diferentes dos solicitados")
    evidence = wav_evidence(bytes(audio))
    helper.atomic_write(Path(job["output"]), bytes(audio))
    return {"ok": True, "id": job["id"], "text": job["text"], "reading_language": job["language"],
            "voice_id": "hatsune-miku", "status": status, "render": {**rendered, **evidence,
            "bytes": len(audio), "sha256": hashlib.sha256(audio).hexdigest(), "output": job["output"]},
            "miku_synthesis_verified": True, "portuguese_quality_verified": False}


def bounded(command: list[str], *, timeout: float) -> str:
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
                                   start_new_session=True)
        try:
            process.wait(timeout=timeout)
        except BaseException:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            process.wait()
            raise
        outputs = []
        for handle in (stdout, stderr):
            handle.seek(0, os.SEEK_END)
            handle.seek(max(0, handle.tell() - 65536))
            outputs.append(handle.read().decode("utf-8", errors="replace"))
    if process.returncode:
        raise ValueError(f"processo de áudio falhou ({process.returncode}): {outputs[1][-800:]}")
    return outputs[0]


def run_sample(job: dict) -> dict:
    helper = installer()
    with tempfile.TemporaryDirectory(prefix="miku-render-") as temporary:
        directory = Path(temporary)
        source, result = directory / "job.json", directory / "result.json"
        source.write_text(json.dumps(job, ensure_ascii=False, allow_nan=False), encoding="utf-8")
        bounded([sys.executable, "-B", str(Path(__file__).resolve()), "--_render", str(source), str(result)],
                timeout=job["timeout"] + 5)
        report = json.loads(helper.regular_bytes(result, MAX_REPORT_BYTES))
        if not isinstance(report, dict) or report.get("ok") is not True:
            raise ValueError(report.get("error", "teste não retornou resultado válido"))
        return report


def encode_mp3(wav: Path, output: Path) -> dict:
    helper = installer()
    with tempfile.TemporaryDirectory(prefix="miku-mp3-") as temporary:
        target = Path(temporary) / "audio.mp3"
        bounded(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", str(wav),
                 "-c:a", "libmp3lame", "-b:a", "192k", str(target)], timeout=20)
        result = json.loads(bounded(["ffprobe", "-v", "error", "-show_entries", "stream=codec_name,channels,sample_rate:format=duration",
                                     "-of", "json", str(target)], timeout=10))
        streams = result.get("streams") or []
        duration = float(result.get("format", {}).get("duration", 0))
        if (len(streams) != 1 or streams[0].get("codec_name") != "mp3" or streams[0].get("channels") != 1
                or not math.isfinite(duration) or not 0 < duration <= 21):
            raise ValueError("FFmpeg não produziu MP3 mono válido dentro do limite")
        bounded(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-i", str(target), "-f", "null", "-"], timeout=10)
        audio = helper.regular_bytes(target, MAX_AUDIO_BYTES)
        helper.atomic_write(output, audio)
    return {"mp3_verified": True, "bytes": len(audio), "sha256": hashlib.sha256(audio).hexdigest(),
            "duration_seconds": duration, "output": str(output)}


def compare(*, env: Path, directory: Path, output: Path, container: str, timeout: float) -> dict:
    helper = installer()
    report = {"ok": False, "voice_id": "hatsune-miku", "backend": "esper-utau", "samples": [],
              "worker_configuration_changed": False, "production_voice_changed": False,
              "miku_synthesis_verified": False, "portuguese_quality_verified": False,
              "controls": {"base_pitch": "C4", "pitch_offset_semitones": 0.0, "speech_rate": 1.0},
              "note": "Ouça os MP3s para avaliar timbre e pronúncia. Português adaptado usa aproximações; gerar áudio não comprova naturalidade."}
    try:
        if not math.isfinite(timeout) or not 1 <= timeout <= 120:
            raise ValueError("--timeout deve estar entre 1 e 120 segundos")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", container):
            raise ValueError("nome do container inválido")
        _, values = helper.read_environment(env.expanduser().absolute())
        directory = directory.expanduser().absolute()
        helper.real_directories(directory)
        if helper.check_existing(directory) is None:
            raise ValueError("bank Miku não instalada pelo kit")
        # Downloads is normally an Android storage symlink; resolve that parent
        # intentionally, then require real output directories and regular files.
        output = output.expanduser()
        if output.is_symlink():
            raise ValueError("pasta de amostras deve ser real, sem link simbólico")
        output = output.resolve()
        helper.real_directories(output)
        output.mkdir(parents=True, exist_ok=True, mode=0o700)
        report["directory"] = str(output)
        for identity, language, text in SAMPLES:
            record = {"ok": False, "id": identity, "reading_language": language, "text": text}
            wav, mp3 = output / (identity + ".wav"), output / (identity + ".mp3")
            for path in (wav, mp3):
                if path.is_symlink() or (path.exists() and not path.is_file()):
                    raise ValueError("arquivo de amostra existente deve ser regular")
                path.unlink(missing_ok=True)
            started = time.monotonic()
            try:
                record = run_sample({"id": identity, "language": language, "text": text,
                    "voicebank": str(directory), "settings": values, "container": container,
                    "timeout": timeout, "output": str(wav)})
                record["mp3"] = encode_mp3(wav, mp3)
                # Publish to Android's media index when Termux:API is available.
                if shutil.which("termux-media-scan"):
                    with contextlib.suppress(OSError, ValueError, subprocess.TimeoutExpired):
                        bounded(["termux-media-scan", str(mp3)], timeout=10)
            except Exception as exc:
                record.update(ok=False, error=f"{type(exc).__name__}: {exc}")
            record["elapsed_seconds"] = round(time.monotonic() - started, 3)
            report["samples"].append(record)
        report["rendered"] = sum(sample["ok"] for sample in report["samples"])
        report["ok"] = report["rendered"] == len(SAMPLES)
        report["miku_synthesis_verified"] = any(sample.get("miku_synthesis_verified") for sample in report["samples"])
        helper.atomic_write(output / "miku-comparacao.json", (json.dumps(report, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", type=Path, default=Path(os.getenv("PHONE_WORKER_ENV") or Path.home() / ".phone-worker.env"))
    parser.add_argument("--voicebank", type=Path, default=Path.home() / "voicebanks/hatsune-miku-english")
    parser.add_argument("--output-dir", type=Path, default=Path.home() / "storage/downloads/miku-comparacao")
    parser.add_argument("--container", default="voicepeak-arm64")
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--_render", nargs=2, metavar=("JOB", "RESULT"), help=argparse.SUPPRESS)
    options = parser.parse_args(argv)
    if options._render:
        helper = installer()
        try:
            job = json.loads(helper.regular_bytes(Path(options._render[0]), MAX_REPORT_BYTES))
            report = render_sample(job)
        except Exception as exc:
            report = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        helper.atomic_write(Path(options._render[1]), (json.dumps(report, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
        return 0
    report = compare(env=options.env, directory=options.voicebank, output=options.output_dir,
                     container=options.container, timeout=options.timeout)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
