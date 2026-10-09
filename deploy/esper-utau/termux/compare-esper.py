#!/usr/bin/env python3
"""Compare Teto with WORLDLINE-R and ESPER without changing the worker."""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shlex
import signal
import stat
import subprocess
import sys
import tempfile
import time
import types

TOOLKIT_ROOT = Path(__file__).resolve().parent
WORKER_DIR = TOOLKIT_ROOT.parents[1] / "termux" / "phone-worker"
DEFAULT_TEXT = "Olá. Eu sou a Teto. Como você está?"
MAX_AUDIO_BYTES = 8 * 1024 * 1024
MAX_REPORT_BYTES = 128 * 1024


def load_validator():
    if str(WORKER_DIR) not in sys.path:
        sys.path.insert(0, str(WORKER_DIR))
    path = WORKER_DIR / "scripts" / "validate-teto-assets.py"
    module = types.ModuleType("_esper_phrase_validator")
    module.__file__ = str(path)
    exec(compile(path.read_bytes(), str(path), "exec"), module.__dict__)
    return module


def load_renderers():
    from teto_renderer import TetoRenderer, WorldlineRenderer
    return {"esper-utau": TetoRenderer, "worldline-r": WorldlineRenderer}


@contextlib.contextmanager
def settings(values: dict[str, str]):
    previous = {key: os.environ.get(key) for key in values}
    try:
        os.environ.update(values)
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def controls(job: dict) -> dict[str, str]:
    root = Path(job["esper_root"])
    wrapper = root / "bin" / "resampler.py"
    if job["engine"] == "esper-utau":
        info = wrapper.lstat()
        if not stat.S_ISREG(info.st_mode) or not 1 <= info.st_size <= 256 * 1024:
            raise ValueError("wrapper ESPER instalado inválido; execute setup.py")
        wrapper_hash = hashlib.sha256(wrapper.read_bytes()).hexdigest()
    else:
        wrapper_hash = "worldline"
    bank_key = "PHONE_WORKER_TETO_ENGLISH_VOICEBANK_DIR" if job["mode"] == "english" else "PHONE_WORKER_TETO_VOICEBANK_DIR"
    return {
        "PHONE_WORKER_TETO_ENABLED": "true",
        "PHONE_WORKER_TETO_BACKEND": "utau" if job["engine"] == "esper-utau" else "worldline-r",
        "PHONE_WORKER_TETO_VOICEBANK_MODE": job["mode"],
        "PHONE_WORKER_TETO_VOICEBANK_DIR": job["voicebank"],
        "PHONE_WORKER_TETO_ENGLISH_VOICEBANK_DIR": "",
        bank_key: job["voicebank"],
        "PHONE_WORKER_TETO_MIN_ALIASES": "10",
        "PHONE_WORKER_TETO_ENGLISH_MIN_ALIASES": "500",
        "PHONE_WORKER_TETO_RESAMPLER_COMMAND": shlex.join([sys.executable, str(wrapper)]),
        "PHONE_WORKER_TETO_LENGTH_MODE": "total",
        "PHONE_WORKER_TETO_FRAGMENT_CACHE_DIR": str(root / "cache" / "fragments" / wrapper_hash),
        "PHONE_WORKER_TETO_BASE_PITCH": "C4", "PHONE_WORKER_TETO_SPEECH_RATE": "1.0",
        "PHONE_WORKER_TETO_TEMPO": "140", "PHONE_WORKER_TETO_VELOCITY": "100",
        "PHONE_WORKER_TETO_MODULATION": "15", "PHONE_WORKER_TETO_FLAGS": "",
        "PHONE_WORKER_TETO_RENDER_THREADS": "2", "PHONE_WORKER_TETO_MAX_CHARACTERS": "180",
        "PHONE_WORKER_TETO_MAX_PHONEMES": "240", "PHONE_WORKER_TETO_MAX_AUDIO_SECONDS": "20",
        "PHONE_WORKER_WORLDLINE_CONTAINER": job["container"],
        "PHONE_WORKER_WORLDLINE_LIBRARY": job["worldline_library"],
        "PHONE_WORKER_ESPER_ROOT": str(root),
        "PHONE_WORKER_ESPER_CONTAINER": job["container"],
        "PHONE_WORKER_ESPER_CACHE_DIR": str(root / "cache"),
        "PHONE_WORKER_ESPER_TIMEOUT": str(min(60.0, job["timeout"])),
        "PHONE_WORKER_ESPER_DEADLINE": str(job["deadline"]),
    }


def render_job(job: dict) -> dict:
    validator = load_validator()
    report = {"ok": False, "comparison_engine": job["engine"], "text": job["text"],
              "worker_configuration_changed": False, "teto_synthesis_verified": False,
              "portuguese_speech_verified": False, "portuguese_quality_verified": False}
    with settings(controls(job)):
        renderer = load_renderers()[job["engine"]](resource_guard=lambda: {"ok": True, "reason": "isolated-comparison"})
        if job["engine"] == "worldline-r":
            native_run = renderer._run

            def limited_run(command, *, deadline):
                return native_run(command, deadline=min(deadline, job["deadline"]))

            renderer._run = limited_run
        status = renderer.status(force=True)
        report["status"] = status
        expected = "english-cvvc" if job["mode"] == "english" else "standard"
        if not status.get("ready") or status.get("voicebank_profile") != expected:
            raise ValueError(status.get("last_error") or "voicebank ou resampler indisponível")
        remaining = job["deadline"] - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("prazo da amostra esgotado no diagnóstico")
        rendered = renderer.synthesize(job["text"], timeout_seconds=remaining,
                                      max_audio_bytes=MAX_AUDIO_BYTES, pitch_offset_semitones=0.0)
        audio = rendered.pop("audio", None)
        if not isinstance(audio, (bytes, bytearray)) or not 44 < len(audio) <= MAX_AUDIO_BYTES:
            raise ValueError("renderer não retornou WAV dentro do limite")
        evidence = validator._wav_evidence(audio)
        if not evidence["wav_verified"] or evidence["sample_frames"] > 20 * 44100 + 1:
            raise ValueError("WAV PCM audível de até 20 segundos não confirmado")
        if rendered.get("voicebank_profile") != expected:
            raise ValueError("renderer trocou o perfil solicitado")
        if job["engine"] == "worldline-r" and not (
                rendered.get("backend") == "worldline-r" and rendered.get("native_phrase_render_verified") is True):
            raise ValueError("síntese WORLDLINE-R nativa não confirmada")
        validator._atomic_write(Path(job["output"]), bytes(audio))
        rendered.update(evidence)
        rendered.update({"bytes": len(audio), "sha256": hashlib.sha256(audio).hexdigest(),
                         "output": job["output"], "portuguese_speech_verified": False,
                         "portuguese_quality_verified": False})
        report.update(ok=True, render=rendered, teto_synthesis_verified=True)
    return report


def bounded(command: list[str], timeout: float) -> tuple[str, str]:
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
    if process.returncode or re.search(r"terminated with signal|segmentation fault|uncaught target signal", "\n".join(outputs), re.I):
        raise ValueError(f"processo de áudio falhou ({process.returncode}): {outputs[1][-600:]}")
    return outputs[0], outputs[1]


def run_job(job: dict) -> dict:
    with tempfile.TemporaryDirectory(prefix="teto-esper-comparison-") as temporary:
        directory = Path(temporary)
        source, result = directory / "job.json", directory / "result.json"
        source.write_text(json.dumps(job, allow_nan=False), encoding="utf-8")
        bounded([sys.executable, "-B", str(Path(__file__).resolve()), "--_render", str(source), str(result)],
                job["timeout"] + 5.0)
        if result.is_symlink() or not result.is_file() or result.stat().st_size > MAX_REPORT_BYTES:
            raise ValueError("processo não retornou relatório JSON limitado")
        report = json.loads(result.read_text(encoding="utf-8"))
        if not isinstance(report, dict) or report.get("ok") is not True or report.get("comparison_engine") != job["engine"]:
            raise ValueError(str(report.get("error") if isinstance(report, dict) else "relatório inválido"))
        return report


def remove_previous(path: Path) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("amostra anterior não é arquivo regular; arquivo preservado")
    path.unlink()


def encode_mp3(wav: Path, output: Path) -> dict:
    validator = load_validator()
    with tempfile.TemporaryDirectory(prefix="teto-esper-mp3-") as temporary:
        target = Path(temporary) / "audio.mp3"
        bounded(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", str(wav),
                 "-c:a", "libmp3lame", "-b:a", "192k", str(target)], 15)
        if not target.is_file() or not 1 < target.stat().st_size <= MAX_AUDIO_BYTES:
            raise ValueError("MP3 inválido ou acima do limite")
        data = target.read_bytes()
    validator._atomic_write(output, data)
    return {"output": str(output), "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) == 3 and argv[0] == "--_render":
        validator = load_validator()
        job = json.loads(Path(argv[1]).read_text(encoding="utf-8"))
        try:
            report = render_job(job)
        except Exception as exc:
            report = {"ok": False, "comparison_engine": job.get("engine"), "error": f"{type(exc).__name__}: {exc}"[:800]}
        validator._atomic_write(Path(argv[2]), (json.dumps(report, ensure_ascii=False, allow_nan=False) + "\n").encode())
        return 0
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--voicebank", type=Path, default=Path.home() / "voicebanks" / "kasane-teto-english")
    parser.add_argument("--mode", choices=("english", "standard"), default="english")
    parser.add_argument("--esper-root", type=Path, default=Path.home() / ".esper-utau")
    parser.add_argument("--worldline-library", type=Path, default=Path.home() / ".worldline-r" / "lib" / "libworldline.so")
    parser.add_argument("--container", default="voicepeak-arm64")
    parser.add_argument("--text", default=DEFAULT_TEXT)
    parser.add_argument("--timeout", type=float, default=180, help="Prazo por amostra, 1 a 300 segundos; limpeza permite até 5 segundos extras.")
    parser.add_argument("--output-dir", type=Path, default=Path.home() / "storage" / "downloads" / "teto-comparacao")
    parser.add_argument("--esper-only", action="store_true")
    parser.add_argument("--mp3", action="store_true", help="Também gera MP3 192 kb/s, com até 15 segundos extras por arquivo.")
    args = parser.parse_args(argv)
    args.text = " ".join(args.text.split())
    if not math.isfinite(args.timeout) or not 1 <= args.timeout <= 300:
        parser.error("--timeout deve estar entre 1 e 300 segundos")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", args.container):
        parser.error("--container inválido")
    if not 1 <= len(args.text) <= 180:
        parser.error("--text deve conter de 1 a 180 caracteres")
    validator = load_validator()
    directory = args.output_dir.expanduser().resolve()
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 2
    presets = [("05-esper-english" if args.mode == "english" else "05-esper-standard", "esper-utau")]
    if not args.esper_only:
        presets.insert(0, ("04-worldline-english" if args.mode == "english" else "04-worldline-standard", "worldline-r"))
    samples = []
    for stem, engine in presets:
        output = directory / (stem + ".wav")
        mp3 = directory / (stem + ".mp3")
        started = time.monotonic()
        job = {"engine": engine, "text": args.text, "mode": args.mode,
               "voicebank": str(args.voicebank.expanduser().resolve()), "esper_root": str(args.esper_root.expanduser().resolve()),
               "worldline_library": str(args.worldline_library.expanduser().resolve()), "container": args.container,
               "output": str(output), "timeout": args.timeout, "deadline": started + args.timeout}
        record = {"ok": False, "id": stem, "comparison_engine": engine, "text": args.text}
        try:
            remove_previous(output)
            remove_previous(mp3)
            record.update(run_job(job))
            evidence = validator._wav_evidence(output.read_bytes())
            if not evidence["wav_verified"] or evidence["sample_frames"] > 20 * 44100 + 1:
                raise ValueError("arquivo WAV publicado não passou na verificação")
            if args.mp3:
                record["mp3"] = encode_mp3(output, mp3)
        except Exception as exc:
            record.update(ok=False, error=f"{type(exc).__name__}: {exc}"[:800])
        record["elapsed_seconds"] = round(time.monotonic() - started, 3)
        try:
            validator._atomic_write(directory / (stem + ".json"), (json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n").encode())
        except Exception as exc:
            record.update(ok=False, report_error=f"{type(exc).__name__}: {exc}"[:500])
        samples.append(record)
    summary = {"ok": all(sample["ok"] for sample in samples), "text": args.text,
               "worker_configuration_changed": False, "production_backend_changed": False,
               "portuguese_speech_verified": False, "portuguese_quality_verified": False,
               "controls": {"base_pitch": "C4", "pitch_offset_semitones": 0, "speech_rate": 1.0},
               "note": "Mesma bank, texto, tom e velocidade; a montagem e a duração podem diferir entre os motores. Ouça os arquivos para comparar clareza e timbre.",
               "rendered": sum(sample["ok"] for sample in samples), "samples": samples}
    try:
        validator._atomic_write(directory / "esper-summary.json", (json.dumps(summary, ensure_ascii=False, allow_nan=False) + "\n").encode())
    except Exception as exc:
        summary.update(ok=False, report_error=f"{type(exc).__name__}: {exc}"[:500])
    print(json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False))
    return 0 if summary["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
