#!/usr/bin/env python3
"""Gere amostras de canto MIDI e efeitos de TTV sem alterar o worker instalado."""
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
import subprocess
import sys
import tempfile
import time
import wave

TOOL_DIR = Path(__file__).resolve().parent
DEPLOY_DIR = TOOL_DIR.parents[1]
REPO_DIR = DEPLOY_DIR.parent
MAX_BYTES = 8 * 1024 * 1024


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def helpers():
    return load("_ttv_performance_quality", DEPLOY_DIR / "ttv-quality/termux/compare-quality.py")


def text_helper():
    return load("_ttv_performance_text", REPO_DIR / "cogs/tts/performance.py")


def score_helper():
    return load("_ttv_performance_score", REPO_DIR / "cogs/tts/score.py")


def demo_midi() -> bytes:
    """Four connected notes, then a rest and another note; 120 BPM, PPQ 480."""
    def vlq(number):
        parts = [number & 127]
        number >>= 7
        while number:
            parts.append((number & 127) | 128)
            number >>= 7
        return bytes(reversed(parts))
    track = bytearray(b"\x00\xff\x03\x06Melody\x00\xff\x51\x03\x07\xa1\x20")
    for index, pitch in enumerate((60, 64, 67, 72, 67)):
        track.extend(vlq(480 if index == 4 else 0))
        track.extend(bytes((0x90, pitch, 90)))
        track.extend(vlq(960))
        track.extend(bytes((0x80, pitch, 0)))
    track.extend(b"\x00\xff\x2f\x00")
    return b"MThd\x00\x00\x00\x06\x00\x00\x00\x01\x01\xe0MTrk" + len(track).to_bytes(4, "big") + track


def wav_evidence(raw: bytes) -> dict:
    if not 44 < len(raw) <= MAX_BYTES:
        raise ValueError("WAV vazio ou acima de 8 MiB")
    with wave.open(io.BytesIO(raw), "rb") as source:
        rate, frames = source.getframerate(), source.getnframes()
        if (source.getnchannels() != 1 or source.getsampwidth() != 2 or rate != 44100
                or source.getcomptype() != "NONE" or not 0 < frames <= 61 * rate):
            raise ValueError("WAV precisa ser PCM mono, 16 bits, 44100 Hz e até 61 segundos")
        pcm = source.readframes(frames)
        if len(pcm) != frames * 2 or not any(pcm):
            raise ValueError("WAV truncado ou silencioso")
    return {"wav_verified": True, "sample_rate": rate, "sample_frames": frames,
            "duration_seconds": frames / rate, "sha256": hashlib.sha256(raw).hexdigest()}


def render_job(job: dict) -> dict:
    quality = helpers()
    installer = quality.installer()
    environment = quality.controls(installer, job["settings"], voice=job["voice"],
        directory=Path(job["bank"]), container=job["container"], analysis=Path(job["analysis"]),
        fragments=Path(job["fragments"]), timeout=job["timeout"],
        esper_root=Path(job["esper_root"]) if job.get("esper_root") else None)
    sys.path.insert(0, str(DEPLOY_DIR / "termux/phone-worker"))
    from teto_renderer import TetoRenderer
    renderer = TetoRenderer(environment=environment, voice_id=quality.VOICES[job["voice"]][0],
        resource_guard=lambda: {"ok": True, "reason": "ttv-independent-performance-sample"})
    controls = {"timeout_seconds": job["timeout"], "max_audio_bytes": MAX_BYTES,
                "pitch_offset_semitones": 0.0, "speech_rate": 1.0,
                "reading_language": job["language"]}
    if job.get("score") is not None or job.get("segments") is not None:
        raw, metadata = renderer.synthesize_performance(job["text"],
            score=job.get("score"), segments=job.get("segments"), **controls)
    else:
        metadata = renderer.synthesize(job["text"], **controls)
        raw = metadata.pop("audio")
    expected_hash = text_helper().performance_hash(text=job["text"],
        score=job.get("score"), segments=job.get("segments"))
    if (metadata.get("voice_id") != quality.VOICES[job["voice"]][0]
            or metadata.get("reading_language") != job["language"]
            or metadata.get("pitch_offset_semitones") != 0.0
            or metadata.get("speech_rate") != 1.0
            or (expected_hash and metadata.get("ttv_performance_hash") != expected_hash)):
        raise ValueError("áudio retornado não corresponde à voz, idioma ou execução solicitada")
    evidence = wav_evidence(raw)
    installer.atomic_write(Path(job["output"]), raw)
    return {"ok": True, "voice_id": renderer.voice_id, "text": job["text"],
            "reading_language": job["language"], "render": {**metadata, **evidence,
            "output": job["output"], "bytes": len(raw)}}


def run_job(job: dict) -> dict:
    quality = helpers()
    with tempfile.TemporaryDirectory(prefix="ttv-performance-job-") as temporary:
        request, result = Path(temporary) / "request.json", Path(temporary) / "result.json"
        request.write_text(json.dumps(job, ensure_ascii=False, allow_nan=False), encoding="utf-8")
        quality.audio_helper().bounded([sys.executable, "-B", str(Path(__file__).resolve()),
            "--_render", str(request), str(result)], timeout=job["timeout"] + 5)
        report = json.loads(quality.installer().regular_bytes(result, 256 * 1024))
        if not isinstance(report, dict) or report.get("ok") is not True:
            raise ValueError(report.get("error", "amostra não retornou um resultado válido"))
        return report


def encode_mp3(wav: Path, target: Path) -> dict:
    quality = helpers()
    quality.audio_helper().bounded(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
        "-i", str(wav), "-map", "0:a:0", "-c:a", "libmp3lame", "-b:a", "192k", str(target)], timeout=30)
    quality.audio_helper().bounded(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
        "-i", str(target), "-f", "null", "-"], timeout=30)
    raw = target.read_bytes()
    if not raw or len(raw) > MAX_BYTES:
        raise ValueError("MP3 vazio ou acima do limite")
    return {"mp3_verified": True, "output": str(target), "bytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest()}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--midi", type=Path, help="Sem arquivo, usa a melodia de demonstração")
    parser.add_argument("--text", help="Letra; na fala aceita < > | ~")
    parser.add_argument("--speech", action="store_true", help="Teste de fala, sem MIDI")
    parser.add_argument("--track", type=int, help="Índice da faixa do MIDI")
    parser.add_argument("--voice", choices=("teto", "miku", "both"), default="both")
    parser.add_argument("--language", choices=("pt-BR", "en-US"), default="pt-BR")
    parser.add_argument("--container", default="voicepeak-arm64")
    parser.add_argument("--esper-root", type=Path)
    parser.add_argument("--env", type=Path, default=Path(os.getenv("PHONE_WORKER_ENV") or Path.home() / ".phone-worker.env"))
    parser.add_argument("--teto-voicebank", type=Path)
    parser.add_argument("--miku-voicebank", type=Path)
    parser.add_argument("--output-dir", type=Path, default=Path.home() / "storage/downloads/ttv-canto")
    parser.add_argument("--timeout", type=float, default=120)
    parser.add_argument("--_render", nargs=2, help=argparse.SUPPRESS)
    options = parser.parse_args(argv)
    quality = helpers()
    installer = quality.installer()
    if options._render:
        try:
            job = json.loads(installer.regular_bytes(Path(options._render[0]), 256 * 1024))
            result = render_job(job)
        except Exception as exc:
            result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        installer.atomic_write(Path(options._render[1]), (json.dumps(result, ensure_ascii=False,
            allow_nan=False, indent=2) + "\n").encode())
        return 0
    report = {"ok": False, "samples": [], "worker_configuration_changed": False,
              "production_caches_changed": False, "phone_execution_verified": False}
    try:
        if not math.isfinite(options.timeout) or not 1 <= options.timeout <= 120:
            raise ValueError("--timeout deve estar entre 1 e 120 segundos")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", options.container):
            raise ValueError("nome do container inválido")
        if options.speech and options.midi:
            raise ValueError("Use --speech ou --midi")
        settings = quality.read_environment(installer, options.env.expanduser().absolute())
        voices = ("teto", "miku") if options.voice == "both" else (options.voice,)
        banks = {
            "teto": (options.teto_voicebank or Path(settings.get("PHONE_WORKER_TETO_ENGLISH_VOICEBANK_DIR")
                or Path.home() / "voicebanks/kasane-teto-english")).expanduser().resolve(),
            "miku": (options.miku_voicebank or Path(settings.get("PHONE_WORKER_MIKU_VOICEBANK_DIR")
                or Path.home() / "voicebanks/hatsune-miku-english")).expanduser().resolve(),
        }
        for voice in voices:
            if not banks[voice].is_dir():
                raise ValueError(f"voicebank English-CVVC da {voice} ausente: {banks[voice]}")
        chosen_root = options.esper_root.expanduser().resolve() if options.esper_root else None
        output = quality.safe_output(installer, options.output_dir, env=options.env,
            banks=[banks[voice] for voice in voices], values=settings, esper_root=chosen_root)
        source_text = options.text if options.text is not None else ("<aaa aaa>" if options.speech else "a ~ ~ ~ a")
        text, segments = text_helper().parse_performance_text(source_text, singing=not options.speech)
        score = None
        if not options.speech:
            data = installer.regular_bytes(options.midi.expanduser().absolute(), score_helper().MAX_MIDI_BYTES) if options.midi else demo_midi()
            score = score_helper().parse_midi(data, lyrics=text if options.text is not None or not options.midi else "",
                track_index=options.track)
            # Let timed MIDI lyrics or vowel fallback drive the renderer when
            # the user supplied no text for their own score.
            if options.midi and options.text is None:
                text, segments = "", None
            installer.atomic_write(output / "melodia.mid", data)
        with tempfile.TemporaryDirectory(prefix="ttv-performance-caches-") as temporary:
            cache = Path(temporary)
            for voice in voices:
                identity = voice + ("-fala" if options.speech else "-canto")
                print("Gerando " + identity + "...", file=sys.stderr, flush=True)
                started = time.monotonic()
                record = run_job({"voice": voice, "bank": str(banks[voice]), "settings": settings,
                    "text": text, "score": score, "segments": segments, "language": options.language,
                    "timeout": options.timeout, "container": options.container,
                    "esper_root": str(chosen_root) if chosen_root else "", "analysis": str(cache / (voice + "-analysis")),
                    "fragments": str(cache / (voice + "-fragments")), "output": str(output / (identity + ".wav"))})
                record["mp3"] = encode_mp3(Path(record["render"]["output"]), output / (identity + ".mp3"))
                record["elapsed_seconds"] = round(time.monotonic() - started, 3)
                if shutil.which("termux-media-scan"):
                    with contextlib.suppress(Exception):
                        quality.audio_helper().bounded(["termux-media-scan", record["mp3"]["output"]], timeout=10)
                report["samples"].append(record)
        report.update(ok=True, directory=str(output), temporary_files_removed=True,
            note="Teste independente: não seleciona vozes, não instala motores e não altera o worker. Ouça os MP3s para avaliar pronúncia e sonoridade.")
        installer.atomic_write(output / "ttv-canto.json", (json.dumps(report, ensure_ascii=False,
            indent=2, allow_nan=False) + "\n").encode())
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
    print(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
