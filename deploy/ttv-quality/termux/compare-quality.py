#!/usr/bin/env python3
"""Compare a pronúncia anterior e atual da Teto/Miku, sem alterar o worker."""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import shlex
import shutil
import sys
import tempfile
import time

TOOLKIT_DIR = Path(__file__).resolve().parent
DEPLOY_DIR = TOOLKIT_DIR.parents[1]
BASELINE_WORKER = TOOLKIT_DIR.parent / "baseline" / "phone-worker"
CURRENT_WORKER = DEPLOY_DIR / "termux" / "phone-worker"
MAX_REPORT_BYTES = 256 * 1024
VOICES = {"teto": ("kasane-teto", "Teto", "TETO", 2),
          "miku": ("hatsune-miku", "Miku", "MIKU", 4)}
PROFILE_KEYS = frozenset({"VOICEBANK_DIR", "ENGLISH_VOICEBANK_DIR", "ALIAS_SUFFIX", "RESAMPLER_COMMAND",
                          "FLAGS", "TEMPO", "VELOCITY", "MODULATION", "LENGTH_MODE", "FRAGMENT_CACHE_DIR"})
ALLOWED_KEYS = frozenset("PHONE_WORKER_" + voice + "_" + key
                         for voice in ("TETO", "MIKU") for key in PROFILE_KEYS) | frozenset({
    "PHONE_WORKER_ESPER_ROOT", "PHONE_WORKER_ESPER_CONTAINER", "PHONE_WORKER_ESPER_TIMEOUT",
    "PHONE_WORKER_ESPER_CACHE_DIR"})
LIMITATION = ("Ouça anterior e atual da mesma voz e frase. Timbre, tom e velocidade são mantidos iguais; "
              "o texto em português é adaptado aos fonemas da bank inglesa. Cobertura e telemetria não "
              "comprovam inteligibilidade nem naturalidade. Este teste não altera as preferências do bot.")


def load_helper(name: str, filename: str):
    path = DEPLOY_DIR / "miku-utau" / "termux" / filename
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def installer():
    return load_helper("_ttv_quality_installer", "install-voicebank.py")


def audio_helper():
    return load_helper("_ttv_quality_audio", "validate-miku.py")


def read_environment(helper, path: Path) -> dict[str, str]:
    """Read only audio settings, as literals; never source or evaluate the .env."""
    helper.real_directories(path.parent)
    original = helper.regular_bytes(path, helper.MAX_ENV_BYTES, allow_empty=True)
    if b"\x00" in original:
        raise ValueError(".env contém caracteres inválidos")
    values = {}
    for line in original.decode("utf-8").splitlines():
        match = helper.KEY.match(line)
        if not match or match.group(1) not in ALLOWED_KEYS:
            continue
        value, _ = helper.split_comment(line[match.end():].strip())
        if value.startswith('"'):
            value = json.loads(value.replace("\\$", "$").replace("\\`", "`"))
        elif value.startswith("'"):
            quoted = re.fullmatch(r"'([^']*)'", value)
            if not quoted:
                raise ValueError("valor .env entre aspas inválido")
            value = quoted.group(1)
        if not isinstance(value, str) or any(character in value for character in "\x00\r\n"):
            raise ValueError("valor de configuração inválido")
        values[match.group(1)] = value
    return values


def temporary_command(helper, values: dict[str, str], *, voice: str, directory: Path,
                      container: str, analysis: Path, esper_root: Path | None) -> str:
    _, _, prefix, _ = VOICES[voice]
    settings = dict(values)
    # Select the command for this voice; configuration() normalizes either
    # direct launcher or Python bridge and detects legacy parser capabilities.
    settings["PHONE_WORKER_MIKU_RESAMPLER_COMMAND"] = values.get("PHONE_WORKER_" + prefix + "_RESAMPLER_COMMAND", "")
    if voice == "miku":
        settings.pop("PHONE_WORKER_TETO_RESAMPLER_COMMAND", None)
    settings["PHONE_WORKER_ESPER_CACHE_DIR"] = str(analysis)
    configured = helper.configuration(directory, settings, container=container, esper_root=esper_root)
    command = shlex.split(configured["PHONE_WORKER_MIKU_RESAMPLER_COMMAND"])
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
    return shlex.join(clean + ["--cache-dir", str(analysis)])


def controls(helper, values: dict[str, str], *, voice: str, directory: Path, container: str,
             analysis: Path, fragments: Path, timeout: float, esper_root: Path | None) -> dict[str, str]:
    _, _, prefix, workers = VOICES[voice]
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith(("PHONE_WORKER_TETO_", "PHONE_WORKER_MIKU_", "PHONE_WORKER_ESPER_"))}
    environment.update({
        "PHONE_WORKER_TETO_ENABLED": "true", "PHONE_WORKER_TETO_BACKEND": "utau",
        "PHONE_WORKER_TETO_VOICEBANK_MODE": "english",
        "PHONE_WORKER_TETO_VOICEBANK_DIR": str(directory),
        "PHONE_WORKER_TETO_ENGLISH_VOICEBANK_DIR": str(directory),
        "PHONE_WORKER_TETO_MIN_ALIASES": "500", "PHONE_WORKER_TETO_ENGLISH_MIN_ALIASES": "500",
        "PHONE_WORKER_TETO_ALIAS_SUFFIX": values.get("PHONE_WORKER_" + prefix + "_ALIAS_SUFFIX", "_D4" if voice == "miku" else ""),
        "PHONE_WORKER_TETO_RESAMPLER_COMMAND": temporary_command(helper, values, voice=voice, directory=directory,
            container=container, analysis=analysis, esper_root=esper_root),
        "PHONE_WORKER_TETO_BASE_PITCH": "C4", "PHONE_WORKER_TETO_SPEECH_RATE": "1.0",
        "PHONE_WORKER_TETO_RENDER_THREADS": str(workers),
        "PHONE_WORKER_TETO_FRAGMENT_CACHE_DIR": str(fragments),
        "PHONE_WORKER_TETO_MAX_CHARACTERS": "180", "PHONE_WORKER_TETO_MAX_PHONEMES": "240",
        "PHONE_WORKER_TETO_MAX_AUDIO_SECONDS": "20", "PHONE_WORKER_ESPER_CONTAINER": container,
        "PHONE_WORKER_ESPER_CACHE_DIR": str(analysis),
        "PHONE_WORKER_ESPER_TIMEOUT": str(min(timeout, 60.0)),
        "PHONE_WORKER_ESPER_DEADLINE": str(time.monotonic() + timeout),
    })
    for key, default in {"FLAGS": "", "TEMPO": "140", "VELOCITY": "100", "MODULATION": "15", "LENGTH_MODE": "total"}.items():
        environment["PHONE_WORKER_TETO_" + key] = values.get("PHONE_WORKER_" + prefix + "_" + key, default)
    if esper_root is not None:
        environment["PHONE_WORKER_ESPER_ROOT"] = str(esper_root)
    elif values.get("PHONE_WORKER_ESPER_ROOT"):
        environment["PHONE_WORKER_ESPER_ROOT"] = values["PHONE_WORKER_ESPER_ROOT"]
    return environment


def renderer_digest(directory: Path) -> str:
    source = directory / "teto_renderer"
    files = sorted(source.glob("*.py"))
    if not files or not (source / "renderer.py").is_file():
        raise ValueError("renderer de comparação ausente: " + str(source))
    digest = hashlib.sha256()
    for path in files:
        digest.update(path.name.encode() + b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest()


def render_sample(job: dict) -> dict:
    # A fresh child imports one revision only. Never swap sys.modules while a
    # renderer is live or import the before/after trees in a shared process.
    directory = Path(job["worker_dir"])
    actual_digest = renderer_digest(directory)
    if actual_digest != job["renderer_digest"]:
        raise ValueError("renderer mudou durante a comparação")
    sys.path.insert(0, str(directory))
    from teto_renderer import TetoRenderer
    helper = installer()
    environment = controls(helper, job["settings"], voice=job["voice"], directory=Path(job["voicebank"]),
        container=job["container"], analysis=Path(job["analysis"]), fragments=Path(job["fragments"]),
        timeout=job["timeout"], esper_root=Path(job["esper_root"]) if job.get("esper_root") else None)
    voice_id = VOICES[job["voice"]][0]
    renderer = TetoRenderer(environment=environment, voice_id=voice_id,
                           resource_guard=lambda: {"ok": True, "reason": "ttv-quality-independent-sample"})
    status = renderer.status(force=True)
    if not status.get("ready"):
        raise ValueError(status.get("last_error") or "voicebank ou ESPER indisponível")
    rendered = renderer.synthesize(job["text"], timeout_seconds=job["timeout"], max_audio_bytes=audio_helper().MAX_AUDIO_BYTES,
        pitch_offset_semitones=0.0, speech_rate=1.0, reading_language=job["language"])
    audio = rendered.pop("audio", None)
    if not isinstance(audio, (bytes, bytearray)):
        raise ValueError("renderer não retornou WAV")
    if (rendered.get("voice_id") != voice_id or rendered.get("reading_language") != job["language"]
            or rendered.get("pitch_offset_semitones") != 0.0 or rendered.get("speech_rate") != 1.0):
        raise ValueError("renderer retornou voz, idioma, tom ou velocidade diferentes dos solicitados")
    evidence = audio_helper().wav_evidence(bytes(audio))
    helper.atomic_write(Path(job["output"]), bytes(audio))
    return {"ok": True, "id": job["id"], "revision": job["revision"], "voice_id": voice_id,
        "text": job["text"], "reading_language": job["language"], "renderer_source_sha256": actual_digest,
        "status": status, "render": {**rendered, **evidence, "bytes": len(audio),
        "sha256": hashlib.sha256(audio).hexdigest(), "output": job["output"]},
        "synthesis_verified": True, "portuguese_quality_verified": False, "english_quality_verified": False}


def run_sample(job: dict) -> dict:
    helper = installer()
    with tempfile.TemporaryDirectory(prefix="ttv-quality-job-") as temporary:
        source, result = Path(temporary) / "job.json", Path(temporary) / "result.json"
        source.write_text(json.dumps(job, ensure_ascii=False, allow_nan=False), encoding="utf-8")
        audio_helper().bounded([sys.executable, "-B", str(Path(__file__).resolve()), "--_render", str(source), str(result)],
                               timeout=job["timeout"] + 5)
        report = json.loads(helper.regular_bytes(result, MAX_REPORT_BYTES))
        if not isinstance(report, dict) or report.get("ok") is not True:
            raise ValueError(report.get("error", "teste não retornou resultado válido") if isinstance(report, dict)
                             else "teste não retornou resultado válido")
        return report


def phrases(name: str, *, english: bool, extended: bool) -> list[tuple[str, str, str]]:
    samples = [("pt-BR", "01-apresentacao", f"Olá, eu sou a {name}. Nossa voz pode falar.")]
    if extended:
        samples.append(("pt-BR", "02-articulacao", "Meu pai tem pão. Amanhã trago três flores."))
    if english:
        samples.append(("en-US", "03-english", f"Hello. I am {name}. How are you?"))
        if extended:
            samples.append(("en-US", "04-articulation", "Three bright stars shine through the night."))
    return samples


def safe_output(helper, output: Path, *, env: Path, banks: list[Path], values: dict[str, str],
                esper_root: Path | None) -> Path:
    output = output.expanduser().absolute()
    if output.is_symlink() or (output.exists() and not output.is_dir()):
        raise ValueError("pasta de amostras deve ser um diretório real")
    # Termux Downloads commonly has a parent Android-storage symlink.
    output = output.resolve()
    protected = [bank.resolve() for bank in banks] + [DEPLOY_DIR.resolve(), env.resolve(),
        (Path.home() / ".esper-utau").resolve(), (Path.home() / "phone-worker/cache").resolve()]
    protected.extend(Path(values[key]).expanduser().resolve() for key in
        ("PHONE_WORKER_ESPER_ROOT", "PHONE_WORKER_ESPER_CACHE_DIR", "PHONE_WORKER_TETO_FRAGMENT_CACHE_DIR",
         "PHONE_WORKER_MIKU_FRAGMENT_CACHE_DIR") if values.get(key))
    if esper_root is not None:
        protected.append(esper_root.resolve())
    if any(output == root or output.is_relative_to(root) for root in protected):
        raise ValueError("amostras devem ficar fora da voicebank, configuração, runtime e caches de produção")
    helper.real_directories(output)
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    destination = output / ("comparacao-v1-" + time.strftime("%Y%m%d-%H%M%S") + "-" + str(time.time_ns() % 1_000_000_000))
    destination.mkdir(mode=0o700)
    return destination


def compare(*, env: Path, output: Path, container: str, timeout: float, voice: str = "both",
            english: bool = False, extended: bool = False, teto_voicebank: Path | None = None,
            miku_voicebank: Path | None = None, esper_root: Path | None = None) -> dict:
    report = {"ok": False, "backend": "esper-utau", "samples": [], "skipped": [],
        "worker_configuration_changed": False, "production_voice_changed": False, "original_voicebanks_changed": False,
        "production_caches_changed": False, "portuguese_quality_verified": False, "english_quality_verified": False,
        "controls": {"base_pitch": "C4", "pitch_offset_semitones": 0.0, "speech_rate": 1.0}, "note": LIMITATION}
    destination = temporary_root = None
    try:
        if isinstance(timeout, bool) or not math.isfinite(timeout) or not 1 <= timeout <= 120:
            raise ValueError("--timeout deve estar entre 1 e 120 segundos")
        if voice not in {"both", "teto", "miku"}:
            raise ValueError("--voice deve ser both, teto ou miku")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", container):
            raise ValueError("nome do container inválido")
        helper = installer()
        env = env.expanduser().absolute()
        values = read_environment(helper, env)
        before, after = renderer_digest(BASELINE_WORKER), renderer_digest(CURRENT_WORKER)
        report["renderer_sources"] = {"anterior": before, "atual": after}
        if before == after:
            raise ValueError("renderers anterior e atual são idênticos; baixe o kit de comparação completo")
        requested = list(VOICES) if voice == "both" else [voice]
        banks = {"teto": Path(teto_voicebank or values.get("PHONE_WORKER_TETO_ENGLISH_VOICEBANK_DIR")
                   or Path.home() / "voicebanks/kasane-teto-english").expanduser().absolute(),
                 "miku": Path(miku_voicebank or values.get("PHONE_WORKER_MIKU_ENGLISH_VOICEBANK_DIR")
                   or values.get("PHONE_WORKER_MIKU_VOICEBANK_DIR")
                   or Path.home() / "voicebanks/hatsune-miku-english").expanduser().absolute()}
        esper_root = esper_root.expanduser().absolute() if esper_root is not None else None
        destination = safe_output(helper, output, env=env, banks=list(banks.values()), values=values, esper_root=esper_root)
        report["directory"] = str(destination)
        with tempfile.TemporaryDirectory(prefix="ttv-quality-cache-") as temporary:
            temporary_root = Path(temporary)
            analysis = temporary_root / "esper-analysis"
            for selected in requested:
                directory = banks[selected]
                if not directory.is_dir():
                    report["skipped"].append({"voice": selected, "reason": "voicebank inglesa ausente",
                        "directory": str(directory), "action": "informe --" + selected + "-voicebank com a pasta já instalada"})
                    continue
                helper.real_directories(directory)
                identity, name, prefix, workers = VOICES[selected]
                report.setdefault("voice_controls", {})[identity] = {"render_workers": workers,
                    "flags": values.get("PHONE_WORKER_" + prefix + "_FLAGS", ""), "voicebank": str(directory)}
                for language, case, text in phrases(name, english=english, extended=extended):
                    for revision, worker, digest in (("anterior", BASELINE_WORKER, before), ("atual", CURRENT_WORKER, after)):
                        sample_id = selected + "-" + language.lower() + "-" + case + "-" + revision
                        wav, mp3 = destination / (sample_id + ".wav"), destination / (sample_id + ".mp3")
                        record = {"ok": False, "id": sample_id, "voice_id": identity, "revision": revision,
                                  "reading_language": language, "text": text}
                        started = time.monotonic()
                        try:
                            record = run_sample({"id": sample_id, "revision": revision, "voice": selected,
                                "language": language, "text": text, "voicebank": str(directory), "settings": values,
                                "container": container, "timeout": timeout, "output": str(wav), "worker_dir": str(worker),
                                "renderer_digest": digest, "analysis": str(analysis),
                                "fragments": str(temporary_root / "fragments" / selected / revision),
                                "esper_root": str(esper_root) if esper_root else None})
                            record["mp3"] = audio_helper().encode_mp3(wav, mp3)
                            if shutil.which("termux-media-scan"):
                                with contextlib.suppress(Exception):
                                    audio_helper().bounded(["termux-media-scan", str(mp3)], timeout=10)
                        except Exception as exc:
                            record.update(ok=False, error=f"{type(exc).__name__}: {exc}")
                        record["elapsed_seconds"] = round(time.monotonic() - started, 3)
                        report["samples"].append(record)
        report["rendered"] = sum(sample["ok"] for sample in report["samples"])
        report["ok"] = bool(report["samples"]) and not report["skipped"] and all(sample["ok"] for sample in report["samples"])
        report["synthesis_verified"] = any(sample.get("synthesis_verified") for sample in report["samples"])
        report["temporary_cache_only"] = True
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
    if temporary_root is not None:
        report["temporary_files_removed"] = not temporary_root.exists()
    if destination is not None:
        try:
            report["report"] = str(destination / "ttv-qualidade.json")
            installer().atomic_write(Path(report["report"]), (json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode())
        except Exception as exc:
            report.update(ok=False, error=f"não foi possível salvar relatório: {type(exc).__name__}: {exc}")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", type=Path, default=Path(os.getenv("PHONE_WORKER_ENV") or Path.home() / ".phone-worker.env"), help="Configuração do worker; somente leitura.")
    parser.add_argument("--output-dir", type=Path, default=Path.home() / "storage/downloads/ttv-qualidade", help="Pasta para WAV, MP3 e relatório; cada execução cria uma subpasta.")
    parser.add_argument("--container", default="voicepeak-arm64", help="Container Linux ARM64 já instalado.")
    parser.add_argument("--timeout", type=float, default=120.0, help="Limite por amostra: 1 a 120 segundos.")
    parser.add_argument("--voice", choices=("both", "teto", "miku"), default="both", help="Vozes da comparação; padrão ambas.")
    parser.add_argument("--english", action="store_true", help="Inclui amostras em inglês.")
    parser.add_argument("--extended", action="store_true", help="Inclui frases extras com nasais e encontros consonantais.")
    parser.add_argument("--teto-voicebank", type=Path, help="Pasta da voicebank inglesa da Teto já instalada.")
    parser.add_argument("--miku-voicebank", type=Path, help="Pasta da voicebank inglesa da Miku já instalada.")
    parser.add_argument("--esper-root", type=Path, help="Runtime ESPER já instalado; opcional, sem download ou atualização.")
    parser.add_argument("--_render", nargs=2, metavar=("JOB", "RESULT"), help=argparse.SUPPRESS)
    options = parser.parse_args(argv)
    if options._render:
        helper = installer()
        try:
            job = json.loads(helper.regular_bytes(Path(options._render[0]), MAX_REPORT_BYTES))
            report = render_sample(job)
        except Exception as exc:
            report = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        helper.atomic_write(Path(options._render[1]), (json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode())
        return 0
    report = compare(env=options.env, output=options.output_dir, container=options.container, timeout=options.timeout,
        voice=options.voice, english=options.english, extended=options.extended, teto_voicebank=options.teto_voicebank,
        miku_voicebank=options.miku_voicebank, esper_root=options.esper_root)
    print(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
