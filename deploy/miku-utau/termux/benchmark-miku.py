#!/usr/bin/env python3
"""Compare 2 e 4 renderizações paralelas da Miku sem alterar o worker."""
from __future__ import annotations

import argparse
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import shlex
import statistics
import tempfile
import time

TOOLKIT_DIR = Path(__file__).resolve().parent
TELEMETRY_KEYS = ("render_workers", "fragment_cache_hits", "fragment_cache_misses",
                  "resampler_calls", "resample_ms", "rendered_phonemes", "worker_synth_ms")
LIMITATION = (
    "Uma renderização inicial aquece a análise ESPER e o .NET em cache temporário, fora da comparação. "
    "Cada caso começa com fragmentos vazios e repete a frase com esses fragmentos prontos. "
    "Não mede o cache de frases do bot nem a primeira análise da voicebank. "
    "O tempo total inclui iniciar Python, verificar a bank e consultar o status; worker_synth_ms mede a síntese. "
    "Temperatura e outros aplicativos podem influenciar os tempos."
)


def validator():
    spec = importlib.util.spec_from_file_location("_miku_benchmark_validator", TOOLKIT_DIR / "validate-miku.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def isolated_settings(helper, values: dict[str, str], *, directory: Path, container: str,
                      analysis: Path, fragments: Path, threads: int) -> dict[str, str]:
    settings = dict(values)
    settings.update(PHONE_WORKER_ESPER_CACHE_DIR=str(analysis),
                    PHONE_WORKER_MIKU_FRAGMENT_CACHE_DIR=str(fragments),
                    PHONE_WORKER_MIKU_RENDER_THREADS=str(threads))
    configured = helper.configuration(directory, settings, container=container)
    command = shlex.split(configured["PHONE_WORKER_MIKU_RESAMPLER_COMMAND"])
    # A saved explicit --cache-dir takes precedence over the environment in
    # both old and current bridges. Replace it only in this temporary profile.
    clean, position = command[:2], 2
    while position < len(command):
        option = command[position]
        if option == "--cache-dir":
            if position + 1 >= len(command) or command[position + 1].startswith("--"):
                raise ValueError("opção ESPER --cache-dir sem valor")
            position += 2
        elif option.startswith("--cache-dir="):
            if not option.partition("=")[2]:
                raise ValueError("opção ESPER --cache-dir sem valor")
            position += 1
        else:
            clean.append(option)
            position += 1
    settings["PHONE_WORKER_MIKU_RESAMPLER_COMMAND"] = shlex.join(clean + ["--cache-dir", str(analysis)])
    return settings


def measure(module, job: dict, *, threads: int, round_number: int, phase: str) -> dict:
    started = time.monotonic()
    record = {"ok": False, "threads_requested": threads, "round": round_number, "phase": phase}
    try:
        result = module.run_sample(job)
        render = result["render"]
        digest = render.get("sha256")
        synth_ms = render.get("worker_synth_ms")
        if (not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)
                or isinstance(synth_ms, bool) or not isinstance(synth_ms, (int, float))
                or not math.isfinite(synth_ms) or synth_ms < 0):
            raise ValueError("resultado sem SHA-256 ou tempo de síntese válido")
        record.update(ok=True, wav_sha256=digest, worker_synth_ms=synth_ms,
                      duration_seconds=render["duration_seconds"],
                      telemetry={key: render[key] for key in TELEMETRY_KEYS if key in render})
    except Exception as exc:
        record["error"] = f"{type(exc).__name__}: {exc}"
    elapsed_ms = (time.monotonic() - started) * 1000
    record["elapsed_ms"] = round(elapsed_ms, 2)
    if record["ok"]:
        record["overhead_ms"] = round(max(0.0, elapsed_ms - record["worker_synth_ms"]), 2)
    return record


def summary(records: list[dict]) -> list[dict]:
    results = []
    for threads in (2, 4):
        for phase in ("fragmentos_novos", "fragmentos_prontos"):
            selected = [record for record in records
                        if record["threads_requested"] == threads and record["phase"] == phase and record["ok"]]
            item = {"threads_requested": threads, "phase": phase, "successful_runs": len(selected)}
            for field in ("worker_synth_ms", "elapsed_ms", "overhead_ms"):
                values = [record[field] for record in selected]
                item[field + "_mean"] = round(statistics.mean(values), 2) if values else None
                item[field + "_median"] = round(statistics.median(values), 2) if values else None
            results.append(item)
    return results


def report_path(helper, path: Path, *, directory: Path, env: Path, values: dict[str, str]) -> Path:
    path = path.expanduser().absolute()
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValueError("relatório deve ser arquivo regular, sem link simbólico")
    # Android exposes Downloads through a parent symlink. Resolve that parent,
    # then reject output inside the bank, runtime, cache, or configuration.
    path = path.parent.resolve() / path.name
    protected = [directory.resolve(), TOOLKIT_DIR.resolve(),
                 (Path.home() / ".esper-utau").resolve(), (Path.home() / "phone-worker/cache").resolve()]
    protected.extend(Path(values[key]).expanduser().resolve() for key in
                     ("PHONE_WORKER_ESPER_ROOT", "PHONE_WORKER_ESPER_CACHE_DIR", "PHONE_WORKER_MIKU_FRAGMENT_CACHE_DIR")
                     if values.get(key))
    if path == env.resolve() or any(path.is_relative_to(root) for root in protected):
        raise ValueError("relatório deve ficar fora da voicebank, configuração e caches de produção")
    helper.real_directories(path.parent)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def benchmark(*, env: Path, directory: Path, container: str, timeout: float,
              rounds: int, language: str, text: str | None = None, report: Path | None = None) -> dict:
    result = {"ok": False, "voice_id": "hatsune-miku", "backend": "esper-utau", "samples": [],
              "worker_configuration_changed": False, "production_voice_changed": False,
              "production_caches_changed": False, "phrase_cache_measured": False,
              "controls": {"base_pitch": "C4", "pitch_offset_semitones": 0.0, "speech_rate": 1.0},
              "note": LIMITATION}
    target = None
    base = None
    try:
        if isinstance(timeout, bool) or not math.isfinite(timeout) or not 1 <= timeout <= 120:
            raise ValueError("--timeout deve estar entre 1 e 120 segundos")
        if isinstance(rounds, bool) or not isinstance(rounds, int) or not 1 <= rounds <= 3:
            raise ValueError("--rounds deve estar entre 1 e 3")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", container):
            raise ValueError("nome do container inválido")
        if language not in {"pt-BR", "en-US"}:
            raise ValueError("idioma deve ser pt-BR ou en-US")
        module = validator()
        helper = module.installer()
        text = next(sample[2] for sample in module.SAMPLES if sample[1] == language) if text is None else text
        text = " ".join(str(text).split())
        if not 1 <= len(text) <= 180 or "\x00" in text:
            raise ValueError("--text deve conter de 1 a 180 caracteres, sem NUL")
        _, values = helper.read_environment(env.expanduser().absolute())
        directory = directory.expanduser().absolute()
        helper.real_directories(directory)
        if helper.check_existing(directory) is None:
            raise ValueError("bank Miku não instalada pelo kit")
        if report is not None:
            target = report_path(helper, report, directory=directory, env=env, values=values)
        result.update(text=text, reading_language=language, rounds=rounds, timeout_seconds=timeout)
        with tempfile.TemporaryDirectory(prefix="miku-benchmark-") as temporary:
            base = Path(temporary)
            analysis = base / "esper-analysis"

            def job(threads, fragments, identity):
                settings = isolated_settings(helper, values, directory=directory, container=container,
                                             analysis=analysis, fragments=fragments, threads=threads)
                return {"id": identity, "language": language, "text": text,
                        "voicebank": str(directory), "settings": settings, "container": container,
                        "timeout": timeout, "output": str(base / (identity + ".wav"))}

            warmup = measure(module, job(2, base / "warmup-fragments", "warmup"),
                             threads=2, round_number=0, phase="aquecimento")
            result["warmup"] = warmup
            if not warmup["ok"]:
                raise ValueError("aquecimento falhou: " + warmup["error"])
            reference = warmup["wav_sha256"]
            # Alternate order to reduce the systematic advantage of going first.
            for number in range(1, rounds + 1):
                for threads in ((2, 4) if number % 2 else (4, 2)):
                    fragments = base / f"round-{number}-threads-{threads}"
                    for phase in ("fragmentos_novos", "fragmentos_prontos"):
                        identity = f"{number}-{threads}-{phase}"
                        record = measure(module, job(threads, fragments, identity),
                                         threads=threads, round_number=number, phase=phase)
                        record["wav_matches_reference"] = record.get("wav_sha256") == reference
                        result["samples"].append(record)
            result["wav_sha256"] = reference
            result["wav_identical"] = all(record["wav_matches_reference"] for record in result["samples"])
            result["summary"] = summary(result["samples"])
            result["ok"] = result["wav_identical"] and all(record["ok"] for record in result["samples"])
            result["temporary_cache_only"] = True
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
    if base is not None:
        result["temporary_files_removed"] = not base.exists()
    if target is not None:
        try:
            helper.atomic_write(target, (json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode())
            result["report"] = str(target)
        except Exception as exc:
            result.update(ok=False, error=f"não foi possível salvar relatório: {type(exc).__name__}: {exc}")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", type=Path, default=Path(os.getenv("PHONE_WORKER_ENV") or Path.home() / ".phone-worker.env"),
                        help="Arquivo de configuração do worker; somente leitura.")
    parser.add_argument("--voicebank", type=Path, default=Path.home() / "voicebanks/hatsune-miku-english",
                        help="Bank Miku já instalada pelo kit.")
    parser.add_argument("--container", default="voicepeak-arm64", help="Container Linux ARM64 já instalado.")
    parser.add_argument("--timeout", type=float, default=120.0, help="Limite por renderização: 1 a 120 segundos.")
    parser.add_argument("--rounds", type=int, default=2, help="Rodadas por configuração: 1 a 3; padrão 2.")
    parser.add_argument("--language", choices=("pt-BR", "en-US"), default="pt-BR", help="Idioma da frase; padrão português adaptado.")
    parser.add_argument("--text", help="Frase opcional de até 180 caracteres; o padrão acompanha o idioma escolhido.")
    parser.add_argument("--report", type=Path, default=Path.home() / "storage/downloads/miku-desempenho.json",
                        help="Destino do relatório JSON; nenhum WAV será mantido.")
    options = parser.parse_args(argv)
    result = benchmark(env=options.env, directory=options.voicebank, container=options.container,
                       timeout=options.timeout, rounds=options.rounds, language=options.language,
                       text=options.text, report=options.report)
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
