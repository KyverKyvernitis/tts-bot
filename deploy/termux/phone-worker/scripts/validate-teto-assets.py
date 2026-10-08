#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
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


def _render(renderer: TetoRenderer, text: str, output: Path | None = None) -> dict:
    rendered = renderer.synthesize(
        text, timeout_seconds=RENDER_TIMEOUT_SECONDS, max_audio_bytes=MAX_AUDIO_BYTES,
    )
    audio = rendered.get("audio") or b""
    if not isinstance(audio, (bytes, bytearray)) or not 0 < len(audio) <= MAX_AUDIO_BYTES:
        raise ValueError("renderer não retornou áudio válido dentro do limite de 8 MiB")
    result = {key: value for key, value in rendered.items() if key != "audio"}
    result["bytes"] = len(audio)
    if output is not None:
        output.write_bytes(audio)
        result["output"] = str(output.resolve())
    return result


def _audit(renderer: TetoRenderer, directory: Path, status: dict) -> dict:
    directory.mkdir(parents=True, exist_ok=True)
    records = []
    for number, (focus, text) in enumerate(AUDIT_PHRASES, 1):
        stem = f"{number:02d}-{focus}"
        record = {"id": stem, "focus": focus, "text": text, "status": status}
        try:
            output = directory / f"{stem}.wav"
            # A failed rerun must not leave an older WAV looking like its output.
            output.unlink(missing_ok=True)
            record["render"] = _render(renderer, text, output)
            record["ok"] = True
        except Exception as exc:
            record.update(ok=False, error=f"{type(exc).__name__}: {exc}")
        (directory / f"{stem}.json").write_text(
            json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
        )
        records.append(record)
    summary = {
        "directory": str(directory.resolve()),
        "status": status,
        "listening_note": LISTENING_NOTE,
        "limits": {
            "phrases": len(AUDIT_PHRASES),
            "timeout_seconds_per_phrase": RENDER_TIMEOUT_SECONDS,
            "max_audio_bytes_per_phrase": MAX_AUDIO_BYTES,
        },
        "rendered": sum(record["ok"] for record in records),
        "failed": sum(not record["ok"] for record in records),
        "phrases": records,
    }
    (directory / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Valida voicebank e resampler da Kasane Teto no phone worker.")
    parser.add_argument("--voicebank", required=True, help="Pasta da voicebank UTAU instalada pelo operador.")
    parser.add_argument(
        "--mode",
        choices=("standard", "english", "auto"),
        default="standard",
        help="Perfil da voicebank a validar. O padrão mantém compatibilidade e valida exatamente --voicebank.",
    )
    parser.add_argument("--resampler", required=True, help="Comando do resampler, por exemplo: python ~/bin/straycat.py")
    parser.add_argument("--render-test", action="store_true", help="Também sintetiza uma frase curta de teste.")
    parser.add_argument("--text", default="teto", help="Texto usado no teste opcional.")
    parser.add_argument("--output", help="Salva o WAV de --render-test neste arquivo; a pasta deve existir.")
    parser.add_argument("--audit-dir", help="Renderiza dez frases PT-BR nesta pasta, com WAVs, JSONs e summary.json.")
    args = parser.parse_args(argv)
    if args.output and not args.render_test:
        parser.error("--output requer --render-test")

    os.environ["PHONE_WORKER_TETO_ENABLED"] = "true"
    voicebank = str(Path(args.voicebank).expanduser())
    os.environ["PHONE_WORKER_TETO_VOICEBANK_MODE"] = args.mode
    if args.mode == "english":
        os.environ["PHONE_WORKER_TETO_ENGLISH_VOICEBANK_DIR"] = voicebank
    elif args.mode == "auto":
        # In auto mode the supplied bank is treated as the English candidate,
        # while the normal configured standard bank remains available as fallback.
        os.environ["PHONE_WORKER_TETO_ENGLISH_VOICEBANK_DIR"] = voicebank
    else:
        os.environ["PHONE_WORKER_TETO_VOICEBANK_DIR"] = voicebank
    os.environ["PHONE_WORKER_TETO_RESAMPLER_COMMAND"] = args.resampler
    renderer = TetoRenderer(resource_guard=lambda: {"ok": True, "reason": "validator"})
    result = {"status": renderer.status(force=True), "listening_note": LISTENING_NOTE}
    if not result["status"].get("ready"):
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 2
    exit_code = 0
    try:
        if args.render_test:
            output = Path(args.output).expanduser() if args.output else None
            result["render_test"] = _render(renderer, args.text, output)
        if args.audit_dir:
            result["audit"] = _audit(
                renderer, Path(args.audit_dir).expanduser(), result["status"],
            )
            if result["audit"]["failed"]:
                exit_code = 3
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        exit_code = 3
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
