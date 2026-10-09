#!/usr/bin/env python3
"""Generate independent Teto listening samples without changing worker settings."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import sys
import time
import types


SCRIPT_DIR = Path(__file__).resolve().parent
WORKER_DIR = SCRIPT_DIR.parents[1] / "termux" / "phone-worker"
if str(WORKER_DIR) not in sys.path:
    sys.path.insert(0, str(WORKER_DIR))

from teto_renderer.worldline import WorldlineRenderer  # noqa: E402
from teto_renderer.voicebank import VoicebankIndex  # noqa: E402


# Use the same bounded WAV verification and Android-compatible atomic writes as
# the validator; loading source directly does not create helper bytecode files.
_VALIDATOR_PATH = WORKER_DIR / "scripts" / "validate-teto-assets.py"
_VALIDATOR = types.ModuleType("_worldline_comparison_validator")
_VALIDATOR.__file__ = str(_VALIDATOR_PATH)
exec(compile(_VALIDATOR_PATH.read_bytes(), str(_VALIDATOR_PATH), "exec"), _VALIDATOR.__dict__)

MAX_AUDIO_BYTES = 8 * 1024 * 1024
MAX_AUDIO_SECONDS = 20
DEFAULT_TEXT = "Olá. Eu sou a Teto. Como você está?"
LISTENING_NOTE = (
    "Ouça a mesma frase nos arquivos 01 e 02. O 01 conserva os envelopes antigos; "
    "o 02 usa junções complementares quando há uma sobreposição isolada. "
    "O 03, se disponível, usa a voicebank English da própria Teto. "
    "A geração de WAV não comprova naturalidade nem inteligibilidade em português."
)


class LegacyEnvelopeRenderer(WorldlineRenderer):
    """Reproduce phrase-1 envelopes solely for an uncached comparison sample."""

    RENDER_VERSION = "worldline-r-phrase-1-comparison"
    ENVELOPE_MODE = "legacy-native-envelopes"

    def _balance_crossfades(self, requests: list[dict]) -> int:
        return 0


def _cap_native_deadlines(renderer, sample_deadline: float) -> None:
    """Include status's otherwise independent eight-second native probe."""
    native_run = getattr(renderer, "_run", None)
    if not callable(native_run):
        return

    def limited_run(command, *, deadline):
        return native_run(command, deadline=min(deadline, sample_deadline))

    renderer._run = limited_run


@contextmanager
def temporary_settings(settings: dict[str, str]):
    """Only affect this command's process; no .env or runtime file is written."""
    previous = {key: os.environ.get(key) for key in settings}
    try:
        os.environ.update(settings)
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _settings(args, mode: str, bank: Path) -> dict[str, str]:
    return {
        "PHONE_WORKER_TETO_ENABLED": "true",
        "PHONE_WORKER_TETO_BACKEND": "worldline-r",
        "PHONE_WORKER_TETO_VOICEBANK_MODE": mode,
        "PHONE_WORKER_TETO_VOICEBANK_DIR": str(Path(args.voicebank).expanduser().resolve()),
        "PHONE_WORKER_TETO_ENGLISH_VOICEBANK_DIR": str(bank) if mode == "english" else "",
        "PHONE_WORKER_TETO_MIN_ALIASES": "10",
        "PHONE_WORKER_TETO_ENGLISH_MIN_ALIASES": "500",
        "PHONE_WORKER_WORLDLINE_CONTAINER": args.container,
        "PHONE_WORKER_WORLDLINE_LIBRARY": str(Path(args.library).expanduser().resolve()),
        "PHONE_WORKER_TETO_BASE_PITCH": "C4",
        "PHONE_WORKER_TETO_SPEECH_RATE": "1.0",
        "PHONE_WORKER_TETO_TEMPO": "140",
        "PHONE_WORKER_TETO_VELOCITY": "100",
        "PHONE_WORKER_TETO_MODULATION": "15",
        "PHONE_WORKER_TETO_MAX_AUDIO_SECONDS": str(MAX_AUDIO_SECONDS),
        "PHONE_WORKER_TETO_MAX_CHARACTERS": "180",
        "PHONE_WORKER_TETO_MAX_PHONEMES": "240",
    }


def _remove_previous_sample(path: Path) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("WAV anterior deve ser arquivo regular, sem link simbólico")
    path.unlink()


def _render_sample(renderer: WorldlineRenderer, text: str, destination: Path,
                   timeout: float, expected_profile: str) -> dict:
    rendered = renderer.synthesize(text, timeout_seconds=timeout, max_audio_bytes=MAX_AUDIO_BYTES,
                                   pitch_offset_semitones=0.0)
    audio = rendered.get("audio")
    if not isinstance(audio, (bytes, bytearray)) or not 44 < len(audio) <= MAX_AUDIO_BYTES:
        raise ValueError("renderer não retornou áudio dentro do limite de 8 MiB")
    evidence = _VALIDATOR._wav_evidence(audio)
    if not (evidence["wav_verified"] and rendered.get("backend") == "worldline-r"
            and rendered.get("native_phrase_render_verified") is True):
        raise ValueError("frase nativa WORLDLINE-R e WAV PCM audível não confirmados")
    if evidence["sample_frames"] > MAX_AUDIO_SECONDS * 44100 + 1:
        raise ValueError("WAV excede o limite de 20 segundos")
    if rendered.get("voicebank_profile") != expected_profile:
        raise ValueError("renderer mudou o perfil da voicebank solicitada")
    _VALIDATOR._atomic_write(destination, bytes(audio))
    result = {key: value for key, value in rendered.items() if key != "audio"}
    result.update(evidence)
    result.update({
        "bytes": len(audio), "sha256": hashlib.sha256(audio).hexdigest(),
        "output": str(destination.resolve()), "teto_synthesis_verified": True,
        "portuguese_speech_verified": False, "portuguese_quality_verified": False,
    })
    return result


def _json(path: Path, result: dict) -> None:
    _VALIDATOR._atomic_write(path, (json.dumps(result, ensure_ascii=False, indent=2,
                                              allow_nan=False) + "\n").encode("utf-8"))


def _english_candidate(args) -> tuple[Path | None, dict]:
    candidate = Path(args.english_voicebank).expanduser().resolve() if args.english_voicebank else (
        Path.home() / "voicebanks" / "kasane-teto-english").resolve()
    info = {"path": str(candidate), "explicit": bool(args.english_voicebank), "available": False}
    # An explicitly supplied English bank gets its own failure report if it is
    # invalid. Automatic discovery includes only a valid 500-alias bank.
    if args.english_voicebank:
        return candidate, info
    if not candidate.is_dir():
        info["reason"] = "voicebank English da Teto não instalada; nenhuma será baixada"
        return None, info
    try:
        index = VoicebankIndex.load(candidate, minimum_aliases=500)
        info.update(available=True, aliases=index.alias_count, fingerprint=index.fingerprint)
        return candidate, info
    except Exception as exc:
        info["reason"] = f"{type(exc).__name__}: {exc}"[:500]
        return None, info


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Gera uma comparação WAV da Teto sem alterar o bot ou o .env.")
    parser.add_argument("--voicebank", default=str(Path.home() / "voicebanks" / "kasane-teto"))
    parser.add_argument("--english-voicebank", help="Voicebank English da própria Teto; opcional, mínimo de 500 aliases.")
    parser.add_argument("--container", default="voicepeak-arm64")
    parser.add_argument("--library", default=str(Path.home() / ".worldline-r" / "lib" / "libworldline.so"))
    parser.add_argument("--text", default=DEFAULT_TEXT)
    parser.add_argument("--output-dir", default=str(Path.home() / "storage" / "downloads" / "teto-comparacao"))
    parser.add_argument("--timeout", type=float, default=90, help="Limite por amostra, incluindo diagnóstico (1 a 120 segundos).")
    args = parser.parse_args(argv)
    if not math.isfinite(args.timeout) or not 1 <= args.timeout <= 120:
        parser.error("--timeout deve estar entre 1 e 120 segundos")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", args.container):
        parser.error("--container inválido")
    args.text = " ".join(args.text.strip().split())
    if not args.text or len(args.text) > 180:
        parser.error("--text deve conter de 1 a 180 caracteres")
    directory = Path(args.output_dir).expanduser().resolve()
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(json.dumps({"ok": False, "error": f"não foi possível criar a pasta: {exc}"}, ensure_ascii=False))
        return 3
    standard = Path(args.voicebank).expanduser().resolve()
    english, english_info = _english_candidate(args)
    if english is None:
        try:
            # A previously generated optional sample must not look current.
            _remove_previous_sample(directory / "03-teto-english.wav")
            _json(directory / "03-teto-english.json", {"id": "03-teto-english", "skipped": True,
                                                        "text": args.text, **english_info})
        except Exception as exc:
            english_info["cleanup_error"] = f"{type(exc).__name__}: {exc}"[:500]
    presets = [
        ("01-anterior", "Envelopes anteriores, para referência", "standard", standard, LegacyEnvelopeRenderer),
        ("02-juncoes-corrigidas", "Junções complementares, mesma voicebank", "standard", standard, WorldlineRenderer),
    ]
    if english is not None:
        presets.append(("03-teto-english", "Junções complementares, voicebank English da Teto", "english", english, WorldlineRenderer))
    records = []
    for stem, label, mode, bank, renderer_class in presets:
        record = {"id": stem, "label": label, "text": args.text, "backend": "worldline-r",
                  "voicebank_mode": mode, "voicebank_path": str(bank), "ok": False,
                  "portuguese_speech_verified": False, "portuguese_quality_verified": False}
        started = time.monotonic()
        deadline = started + args.timeout
        destination = directory / f"{stem}.wav"
        try:
            _remove_previous_sample(destination)
            with temporary_settings(_settings(args, mode, bank)):
                renderer = renderer_class(resource_guard=lambda: {"ok": True, "reason": "isolated-comparison"})
                _cap_native_deadlines(renderer, deadline)
                status = renderer.status(force=True)
                record["status"] = status
                expected_profile = "english-cvvc" if mode == "english" else "standard"
                if status.get("ready") is not True or status.get("backend") != "worldline-r":
                    raise ValueError(str(status.get("last_error") or "WORLDLINE-R indisponível"))
                if status.get("voicebank_profile") != expected_profile:
                    raise ValueError("diagnóstico mudou o perfil da voicebank solicitada")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("tempo da amostra esgotado durante o diagnóstico")
                record["render"] = _render_sample(renderer, args.text, destination, remaining, expected_profile)
                record["ok"] = True
                if mode == "english":
                    english_info.update(available=True, aliases=status.get("aliases"),
                                        fingerprint=status.get("voicebank_fingerprint"))
        except Exception as exc:
            record["error"] = f"{type(exc).__name__}: {exc}"[:800]
        record["elapsed_seconds"] = round(time.monotonic() - started, 3)
        try:
            _json(directory / f"{stem}.json", record)
        except Exception as exc:
            record["report_error"] = f"{type(exc).__name__}: {exc}"[:500]
            record["ok"] = False
        records.append(record)
    summary = {
        "ok": all(record["ok"] for record in records), "backend": "worldline-r",
        "text": args.text, "directory": str(directory), "listening_note": LISTENING_NOTE,
        "worker_configuration_changed": False, "production_backend_changed": False,
        "controls": {"base_pitch": "C4", "pitch_offset_semitones": 0.0, "speech_rate": 1.0,
                     "tempo": 140, "velocity": 100, "modulation": 15,
                     "modulation_effect": "unused-by-native-phrase-engine"},
        "limits": {"timeout_seconds_per_sample": args.timeout, "max_audio_seconds": MAX_AUDIO_SECONDS,
                   "max_audio_bytes": MAX_AUDIO_BYTES},
        "english_candidate": english_info, "rendered": sum(record["ok"] for record in records),
        "failed": sum(not record["ok"] for record in records), "samples": records,
        "teto_synthesis_verified": any(record.get("render", {}).get("teto_synthesis_verified") is True for record in records),
        "portuguese_speech_verified": False, "portuguese_quality_verified": False,
    }
    try:
        _json(directory / "summary.json", summary)
    except Exception as exc:
        summary.update(ok=False, report_error=f"{type(exc).__name__}: {exc}"[:500])
    print(json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False))
    return 0 if summary["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
