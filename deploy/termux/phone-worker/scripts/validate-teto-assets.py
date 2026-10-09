#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import io
import json
import math
import os
import re
import stat
import sys
import tempfile
import wave
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
WORKER_DIR = SCRIPT_DIR.parent
if str(WORKER_DIR) not in sys.path:
    sys.path.insert(0, str(WORKER_DIR))

from teto_renderer import TetoRenderer  # noqa: E402


RENDER_TIMEOUT_SECONDS = 30
MAX_AUDIO_BYTES = 8 * 1024 * 1024
AUDIT_PHRASES = (
    ("nasais-palatais", "Minha filha encontrou pão quente."),
    ("roticos", "O carro virou à direita."),
    ("encontros", "Brasil, trabalho e problema."),
    ("fricativas", "A chave azul caiu no chão."),
    ("ditongos-hiatos", "Hoje eu vou ao teatro."),
    ("tonicidade", "Ela pediu três pratos."),
    ("palavras-funcionais", "Quem pode abrir a porta?"),
    ("ritmo", "Amanhã nós chegaremos cedo."),
    ("pergunta", "Você terminou o relatório?"),
    ("exclamacao", "Cuidado! O cachorro está correndo."),
)
LISTENING_NOTE = (
    "Cobertura de aliases e telemetria não medem inteligibilidade nem naturalidade. "
    "Compare os WAVs por audição; a adaptação PT-BR usa aproximações dos sons "
    "ausentes na voicebank."
)


def _atomic_write(path: Path, data: bytes) -> None:
    """Publish in the original directory; Android Python need not expose os.link."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        info = None
    if info is not None and not stat.S_ISREG(info.st_mode):
        raise ValueError("destino existente deve ser arquivo regular, sem link simbólico")
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        # Recheck before replacing an existing destination, to reject symlink swaps.
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise ValueError("destino mudou durante a gravação")
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _wav_evidence(audio: bytes | bytearray) -> dict:
    """Check actual PCM frames; byte count and renderer metadata alone are not proof."""
    try:
        with wave.open(io.BytesIO(audio), "rb") as wav:
            channels, width, rate, frames = (wav.getnchannels(), wav.getsampwidth(), wav.getframerate(), wav.getnframes())
            if channels != 1 or width != 2 or rate != 44100 or wav.getcomptype() != "NONE" or frames <= 0:
                raise ValueError("formato PCM esperado: mono, 16 bits, 44100 Hz, com frames")
            pcm = wav.readframes(frames)
            if len(pcm) != frames * channels * width or not any(pcm):
                raise ValueError("WAV truncado ou silencioso")
        return {"wav_verified": True, "sample_rate": rate, "channels": channels,
                "sample_width_bytes": width, "sample_frames": frames, "duration_seconds": frames / rate}
    except (wave.Error, EOFError, ValueError):
        return {"wav_verified": False}


def _render(renderer: TetoRenderer, text: str, output: Path | None = None,
            timeout_seconds: float = RENDER_TIMEOUT_SECONDS) -> dict:
    rendered = renderer.synthesize(
        text, timeout_seconds=timeout_seconds, max_audio_bytes=MAX_AUDIO_BYTES,
    )
    audio = rendered.get("audio") or b""
    if not isinstance(audio, (bytes, bytearray)) or not 0 < len(audio) <= MAX_AUDIO_BYTES:
        raise ValueError("renderer não retornou áudio válido dentro do limite de 8 MiB")
    backend = str(os.environ.get("PHONE_WORKER_TETO_BACKEND") or rendered.get("backend") or "utau")
    evidence = _wav_evidence(audio)
    # Preserve the legacy UTAU byte-return contract. WORLDLINE-R must produce
    # a verified native phrase and a real non-silent WAV before it can be saved.
    if backend == "worldline-r" and (not evidence["wav_verified"] or
            rendered.get("backend") != "worldline-r" or rendered.get("native_phrase_render_verified") is not True):
        raise ValueError("WORLDLINE-R não retornou frase nativa confirmada em WAV PCM válido e audível")
    result = {key: value for key, value in rendered.items() if key != "audio"}
    result.update(evidence)
    result.update({"bytes": len(audio), "backend": backend, "sha256": hashlib.sha256(audio).hexdigest(),
                   "teto_synthesis_verified": evidence["wav_verified"],
                   "portuguese_speech_verified": False, "portuguese_quality_verified": False})
    if output is not None:
        _atomic_write(output, audio)
        result["output"] = str(output.resolve())
    return result


def _audit(renderer: TetoRenderer, directory: Path, status: dict,
           timeout_seconds: float = RENDER_TIMEOUT_SECONDS) -> dict:
    directory.mkdir(parents=True, exist_ok=True)
    records = []
    for number, (focus, text) in enumerate(AUDIT_PHRASES, 1):
        stem = f"{number:02d}-{focus}"
        record = {"id": stem, "focus": focus, "text": text, "status": status}
        try:
            output = directory / f"{stem}.wav"
            # A failed rerun must not leave an older WAV looking like its output.
            output.unlink(missing_ok=True)
            record["render"] = _render(renderer, text, output, timeout_seconds=timeout_seconds)
            record["ok"] = True
        except Exception as exc:
            record.update(ok=False, error=f"{type(exc).__name__}: {exc}")
        _atomic_write(directory / f"{stem}.json", (json.dumps(record, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
        records.append(record)
    summary = {
        "directory": str(directory.resolve()),
        "status": status,
        "listening_note": LISTENING_NOTE,
        "limits": {
            "phrases": len(AUDIT_PHRASES),
            "timeout_seconds_per_phrase": timeout_seconds,
            "max_audio_bytes_per_phrase": MAX_AUDIO_BYTES,
        },
        "rendered": sum(record["ok"] for record in records),
        "failed": sum(not record["ok"] for record in records),
        "phrases": records,
        "backend": str(os.environ.get("PHONE_WORKER_TETO_BACKEND") or "utau"),
        "teto_synthesis_verified": any(record.get("render", {}).get("teto_synthesis_verified") is True for record in records),
        "portuguese_speech_verified": False, "portuguese_quality_verified": False,
    }
    _atomic_write(directory / "summary.json", (json.dumps(summary, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    return summary


def _emit(result: dict, report: str | None, exit_code: int) -> int:
    if report:
        try:
            encoded = (json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
            _atomic_write(Path(report).expanduser(), encoded)
        except Exception as exc:
            result["report_error"] = f"{type(exc).__name__}: {exc}"
            exit_code = 3
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    return exit_code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Valida voicebank e renderizador da Kasane Teto no phone worker.")
    parser.add_argument("--voicebank", required=True, help="Pasta da voicebank UTAU instalada pelo operador.")
    parser.add_argument(
        "--mode", choices=("standard", "english", "auto"), default="standard",
        help="Perfil da voicebank a validar. O padrão valida exatamente --voicebank.",
    )
    parser.add_argument("--backend", choices=("utau", "worldline-r"), default="utau", help="Renderizador a validar (padrão: utau).")
    parser.add_argument("--resampler", help="Obrigatório para --backend utau; exemplo: python ~/bin/straycat.py.")
    parser.add_argument("--container", default="voicepeak-arm64", help="Guest Linux ARM64 existente para WORLDLINE-R.")
    parser.add_argument("--library", default=str(Path.home() / ".worldline-r/lib/libworldline.so"), help="Biblioteca ARM64 instalada pelo setup WORLDLINE-R.")
    parser.add_argument("--timeout", type=float, default=RENDER_TIMEOUT_SECONDS, help="Limite por frase em segundos (1 a 120; padrão: 30).")
    parser.add_argument("--render-test", action="store_true", help="Também sintetiza uma frase curta de teste.")
    parser.add_argument("--text", default="teto", help="Texto usado no teste opcional.")
    parser.add_argument("--output", help="Salva o WAV de --render-test neste arquivo; a pasta deve existir.")
    parser.add_argument("--audit-dir", help="Renderiza dez frases PT-BR nesta pasta, com WAVs, JSONs e summary.json.")
    parser.add_argument("--report", help="Salva este relatório JSON; a pasta deve existir.")
    args = parser.parse_args(argv)
    if args.output and not args.render_test:
        parser.error("--output requer --render-test")
    if args.backend == "utau" and not args.resampler:
        parser.error("--resampler é obrigatório para --backend utau")
    if not math.isfinite(args.timeout) or not 1 <= args.timeout <= 120:
        parser.error("--timeout deve estar entre 1 e 120 segundos")
    if args.backend == "worldline-r" and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", args.container):
        parser.error("--container inválido")
    if args.output and args.report and Path(args.output).expanduser().resolve() == Path(args.report).expanduser().resolve():
        parser.error("--output e --report devem apontar para arquivos diferentes")

    os.environ["PHONE_WORKER_TETO_ENABLED"] = "true"
    os.environ["PHONE_WORKER_TETO_BACKEND"] = args.backend
    voicebank = str(Path(args.voicebank).expanduser())
    os.environ["PHONE_WORKER_TETO_VOICEBANK_MODE"] = args.mode
    if args.mode in {"english", "auto"}:
        # Auto uses the supplied bank as English candidate and keeps the existing
        # standard bank available as fallback, matching the original validator.
        os.environ["PHONE_WORKER_TETO_ENGLISH_VOICEBANK_DIR"] = voicebank
    else:
        os.environ["PHONE_WORKER_TETO_VOICEBANK_DIR"] = voicebank
    if args.backend == "worldline-r":
        os.environ["PHONE_WORKER_WORLDLINE_CONTAINER"] = args.container
        os.environ["PHONE_WORKER_WORLDLINE_LIBRARY"] = str(Path(args.library).expanduser().absolute())
    else:
        os.environ["PHONE_WORKER_TETO_RESAMPLER_COMMAND"] = args.resampler
    result = {"backend": args.backend, "listening_note": LISTENING_NOTE,
              "teto_synthesis_verified": False, "portuguese_speech_verified": False,
              "portuguese_quality_verified": False, "timeout_seconds": args.timeout}
    exit_code = 0
    try:
        if args.backend == "worldline-r":
            from teto_renderer.worldline import WorldlineRenderer
            renderer = WorldlineRenderer(resource_guard=lambda: {"ok": True, "reason": "validator"})
        else:
            renderer = TetoRenderer(resource_guard=lambda: {"ok": True, "reason": "validator"})
        result["status"] = renderer.status(force=True)
        if not result["status"].get("ready"):
            return _emit(result, args.report, 2)
        if args.render_test:
            output = Path(args.output).expanduser() if args.output else None
            result["render_test"] = _render(renderer, args.text, output, timeout_seconds=args.timeout)
            result["teto_synthesis_verified"] = result["render_test"]["teto_synthesis_verified"]
        if args.audit_dir:
            result["audit"] = _audit(renderer, Path(args.audit_dir).expanduser(), result["status"], timeout_seconds=args.timeout)
            result["teto_synthesis_verified"] = result["teto_synthesis_verified"] or result["audit"]["teto_synthesis_verified"]
            if result["audit"]["failed"]:
                exit_code = 3
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        exit_code = 3
    return _emit(result, args.report, exit_code)


if __name__ == "__main__":
    raise SystemExit(main())
