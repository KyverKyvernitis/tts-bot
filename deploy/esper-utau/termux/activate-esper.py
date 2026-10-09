#!/usr/bin/env python3
"""Validate ESPER with the Teto English bank, then activate it in the worker .env."""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import wave

TOOLKIT_ROOT = Path(__file__).resolve().parent
WORKER_DIR = TOOLKIT_ROOT.parents[1] / "termux" / "phone-worker"
MAX_ENV_BYTES = 256 * 1024
MAX_AUDIO_BYTES = 8 * 1024 * 1024
MAX_REPORT_BYTES = 128 * 1024
PHRASE = "Olá. Eu sou a Teto."
TOTAL_TIMEOUT = 120
ENGINE_SHA256 = "e2cb6dc113593cb3b788debb51996f591a5f9d47bbd2a54df57bc1ecd843602f"
CONFIG_SHA256 = "21951154b9bafdebde0b3a1d6533bb1fde549113b63b99a573b49ea68fadbe83"
KEY = re.compile(r"^[ \t]*(?:export[ \t]+)?([A-Za-z_][A-Za-z0-9_]*)[ \t]*=")
REMOVE_KEYS = frozenset({"PHONE_WORKER_ESPER_DEADLINE"})


class ActivationError(ValueError):
    pass


def real_directories(path: Path) -> None:
    for directory in (*reversed(path.parents), path):
        if not stat.S_ISDIR(directory.lstat().st_mode):
            raise ActivationError("pasta deve ser real, sem link simbólico")


def regular_bytes(path: Path, maximum: int, *, allow_empty: bool = False) -> tuple[bytes, tuple]:
    real_directories(path.parent)
    original = path.lstat()
    if not stat.S_ISREG(original.st_mode):
        raise ActivationError("arquivo deve ser regular, sem link simbólico")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or (info.st_dev, info.st_ino) != (original.st_dev, original.st_ino)
                or not (0 if allow_empty else 1) <= info.st_size <= maximum):
            raise ActivationError("arquivo vazio, alterado durante a leitura ou acima do limite")
        data = stream.read(maximum + 1)
        final = os.fstat(stream.fileno())
    fingerprint = lambda item: (item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns, item.st_ctime_ns)
    if len(data) > maximum or fingerprint(info) != fingerprint(final):
        raise ActivationError("arquivo mudou durante a leitura")
    return data, fingerprint(final)


def shell_value(value: str) -> str:
    # Both the shell launcher and the Python worker understand these escapes.
    # json.dumps alone does not prevent shell substitution of $ or backticks.
    if any(character in value for character in ("\x00", "\n", "\r")):
        raise ActivationError("valor de configuração contém caracteres inválidos")
    return json.dumps(value, ensure_ascii=False).replace("$", "\\$").replace("`", "\\`")


def validate_wrapper(root: Path) -> dict:
    supplied, _ = regular_bytes(TOOLKIT_ROOT / "resampler.py", MAX_ENV_BYTES)
    installed, _ = regular_bytes(root / "bin" / "resampler.py", MAX_ENV_BYTES)
    if installed != supplied:
        raise ActivationError("wrapper instalado difere do kit; execute setup.py antes de ativar. Arquivo preservado")
    digest = hashlib.sha256(installed).hexdigest()
    return {"ok": True, "wrapper_sha256": digest, "wrapper_matches_kit": True}


def configuration(*, root: Path, container: str, voicebank: Path, flags: str, wrapper_hash: str) -> dict[str, str]:
    if not re.fullmatch(r"[a-f0-9]{64}", wrapper_hash):
        raise ActivationError("SHA-256 da implementação ESPER inválido")
    # The renderer stamps command[0] (Python), so the wrapper digest must also
    # be in the command to invalidate phrase/VPS caches after a wrapper update.
    command = shlex.join([sys.executable, str(root / "bin" / "resampler.py"),
                          "--root", str(root), "--container", container, "--timeout", "60",
                          "--implementation-id", wrapper_hash])
    return {
        "PHONE_WORKER_TETO_ENABLED": "true",
        "PHONE_WORKER_TETO_BACKEND": "utau",
        "PHONE_WORKER_TETO_VOICEBANK_MODE": "english",
        "PHONE_WORKER_TETO_ENGLISH_VOICEBANK_DIR": str(voicebank),
        "PHONE_WORKER_TETO_ENGLISH_MIN_ALIASES": "500",
        "PHONE_WORKER_TETO_RESAMPLER_COMMAND": command,
        "PHONE_WORKER_TETO_LENGTH_MODE": "total",
        "PHONE_WORKER_TETO_FRAGMENT_CACHE_DIR": str(root / "cache" / "fragments" / wrapper_hash),
        "PHONE_WORKER_TETO_BASE_PITCH": "C4",
        "PHONE_WORKER_TETO_SPEECH_RATE": "1.0",
        "PHONE_WORKER_TETO_TEMPO": "140",
        "PHONE_WORKER_TETO_VELOCITY": "100",
        "PHONE_WORKER_TETO_MODULATION": "15",
        "PHONE_WORKER_TETO_FLAGS": flags,
        "PHONE_WORKER_TETO_RENDER_THREADS": "2",
        "PHONE_WORKER_TETO_MAX_CHARACTERS": "180",
        "PHONE_WORKER_TETO_MAX_PHONEMES": "240",
        "PHONE_WORKER_TETO_MAX_AUDIO_SECONDS": "20",
        "PHONE_WORKER_ESPER_ROOT": str(root),
        "PHONE_WORKER_ESPER_CONTAINER": container,
        "PHONE_WORKER_ESPER_CACHE_DIR": str(root / "cache"),
        "PHONE_WORKER_ESPER_TIMEOUT": "60",
        "PHONE_WORKER_TTS_AGENT_TIMEOUT_SECONDS": str(TOTAL_TIMEOUT),
        "PHONE_WORKER_JOB_TIMEOUT_SECONDS": str(TOTAL_TIMEOUT),
    }


def bounded(command: list[str], timeout: float, *, environment: dict[str, str] | None = None) -> str:
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
                                   env=environment, start_new_session=True)
        try:
            process.wait(timeout=timeout)
        except BaseException:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            process.wait()
            raise
        output = []
        for handle in (stdout, stderr):
            handle.seek(0, os.SEEK_END)
            handle.seek(max(0, handle.tell() - MAX_REPORT_BYTES))
            output.append(handle.read().decode("utf-8", errors="replace"))
    if process.returncode or re.search(r"terminated with signal|segmentation fault|uncaught target signal", "\n".join(output), re.I):
        raise ActivationError(f"validação falhou ({process.returncode}): {(output[1] or output[0])[-1200:]}")
    return output[0]


def wav_evidence(audio: bytes) -> dict:
    if not 44 < len(audio) <= MAX_AUDIO_BYTES:
        raise ActivationError("áudio do teste vazio ou acima do limite")
    try:
        with wave.open(io.BytesIO(audio), "rb") as wav:
            channels, width, rate, frames = wav.getnchannels(), wav.getsampwidth(), wav.getframerate(), wav.getnframes()
            if (channels != 1 or width != 2 or rate != 44100 or wav.getcomptype() != "NONE"
                    or not 0 < frames <= 20 * rate + 1):
                raise ActivationError("formato PCM do teste inválido")
            pcm = wav.readframes(frames)
            if len(pcm) != frames * width or not any(pcm):
                raise ActivationError("áudio do teste silencioso ou truncado")
    except (wave.Error, EOFError) as exc:
        raise ActivationError("teste não retornou WAV PCM") from exc
    return {"wav_verified": True, "sample_rate": rate, "channels": channels,
            "sample_width_bytes": width, "sample_frames": frames,
            "duration_seconds": frames / rate, "sha256": hashlib.sha256(audio).hexdigest()}


def phrase_child(values: dict[str, str]) -> dict:
    # This only executes the bundled renderer and our already verified wrapper.
    if str(WORKER_DIR) not in sys.path:
        sys.path.insert(0, str(WORKER_DIR))
    from teto_renderer import TetoRenderer
    previous = {key: os.environ.get(key) for key in (*values, "PHONE_WORKER_ESPER_DEADLINE")}
    try:
        os.environ.update(values)
        os.environ["PHONE_WORKER_ESPER_DEADLINE"] = str(time.monotonic() + TOTAL_TIMEOUT)
        renderer = TetoRenderer(resource_guard=lambda: {"ok": True, "reason": "explicit-esper-activation"})
        status = renderer.status(force=True)
        if status.get("ready") is not True or status.get("voicebank_profile") != "english-cvvc":
            raise ActivationError(status.get("last_error") or "voicebank English indisponível")
        result = renderer.synthesize(PHRASE, timeout_seconds=TOTAL_TIMEOUT,
                                     max_audio_bytes=MAX_AUDIO_BYTES, pitch_offset_semitones=0.0)
        if result.get("voicebank_profile") != "english-cvvc":
            raise ActivationError("renderer trocou o perfil da voicebank")
        audio = result.get("audio")
        if not isinstance(audio, (bytes, bytearray)):
            raise ActivationError("renderer não retornou bytes de áudio")
        evidence = wav_evidence(bytes(audio))
        return {"ok": True, "teto_synthesis_verified": True, "text": PHRASE,
                "voicebank_profile": "english-cvvc", "rendered_phonemes": result.get("rendered_phonemes"),
                "evidence": evidence, "portuguese_speech_verified": False, "portuguese_quality_verified": False}
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def verify_runtime(values: dict[str, str]) -> dict:
    for executable in ("ffmpeg", "proot-distro"):
        if not shutil.which(executable):
            raise ActivationError(f"{executable} ausente")
    bounded(["ffmpeg", "-version"], 10)
    architecture = bounded(["proot-distro", "login", values["PHONE_WORKER_ESPER_CONTAINER"], "--",
                            "/usr/bin/dpkg", "--print-architecture"], 15)
    if architecture.strip() != "arm64":
        raise ActivationError("container precisa ser ARM64")
    environment = dict(os.environ)
    environment.update(values)
    environment.pop("PHONE_WORKER_ESPER_DEADLINE", None)
    command = shlex.split(values["PHONE_WORKER_TETO_RESAMPLER_COMMAND"]) + ["--probe"]
    try:
        implementation_id = command[command.index("--implementation-id") + 1]
    except (ValueError, IndexError) as exc:
        raise ActivationError("comando ESPER sem identificação da implementação verificada") from exc
    output = bounded(command, 65, environment=environment)
    try:
        native = json.loads(output)
    except (TypeError, ValueError) as exc:
        raise ActivationError("probe ESPER não retornou JSON válido") from exc
    if (not isinstance(native, dict) or native.get("ok") is not True
            or native.get("synthetic_render_verified") is not True
            or native.get("wav_verified") is not True or native.get("native_architecture") != "arm64"
            or native.get("engine_sha256") != ENGINE_SHA256 or native.get("config_sha256") != CONFIG_SHA256
            or native.get("implementation_id") != implementation_id):
        raise ActivationError("síntese nativa ESPER não confirmada")
    with tempfile.TemporaryDirectory(prefix="teto-esper-activation-") as temporary:
        path = Path(temporary) / "settings.json"
        path.write_text(json.dumps(values, ensure_ascii=False), encoding="utf-8")
        output = bounded([sys.executable, "-B", str(Path(__file__).resolve()), "--_validate", str(path)],
                         TOTAL_TIMEOUT + 5, environment=environment)
    try:
        phrase = json.loads(output)
    except (TypeError, ValueError) as exc:
        raise ActivationError("teste da Teto não retornou JSON válido") from exc
    if not isinstance(phrase, dict) or phrase.get("ok") is not True or phrase.get("teto_synthesis_verified") is not True:
        raise ActivationError("síntese da Teto English não confirmada")
    return {"native_probe": native, "phrase_probe": phrase}


def updated_environment(original: bytes, values: dict[str, str]) -> bytes:
    if b"\x00" in original:
        raise ActivationError(".env contém caracteres inválidos")
    text = original.decode("utf-8")
    result, seen = [], set()
    for line in text.splitlines(keepends=True):
        match = KEY.match(line)
        key = match.group(1) if match else None
        if key in REMOVE_KEYS:
            continue
        if key in values:
            if key not in seen:
                result.append(f"{key}={shell_value(values[key])}\n")
                seen.add(key)
        else:
            result.append(line)
    if result and not result[-1].endswith(("\n", "\r")):
        result[-1] += "\n"
    result.extend(f"{key}={shell_value(value)}\n" for key, value in values.items() if key not in seen)
    updated = "".join(result).encode("utf-8")
    if len(updated) > MAX_ENV_BYTES:
        raise ActivationError(".env atualizado excederia 256 KiB; configuração preservada")
    return updated


def publish_environment(path: Path, original: bytes, snapshot: tuple, updated: bytes) -> dict:
    current, fingerprint = regular_bytes(path, MAX_ENV_BYTES, allow_empty=True)
    if current != original or fingerprint != snapshot:
        raise ActivationError(".env mudou durante a validação; alterações locais foram preservadas")
    if updated == original:
        return {"changed": False}
    backup = path.with_name(path.name + ".bak-esper-" + str(time.time_ns()))
    descriptor = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(descriptor, "wb") as target:
        target.write(original)
        target.flush()
        os.fsync(target.fileno())
    descriptor, temporary = tempfile.mkstemp(prefix="." + path.name + ".esper-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as target:
            target.write(updated)
            target.flush()
            os.fsync(target.fileno())
        os.chmod(temporary, 0o600)
        current, fingerprint = regular_bytes(path, MAX_ENV_BYTES, allow_empty=True)
        if current != original or fingerprint != snapshot:
            raise ActivationError(".env mudou antes da gravação; alterações locais foram preservadas")
        os.replace(temporary, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)
    return {"changed": True, "backup": str(backup)}


def activate(*, env: Path, root: Path, container: str, voicebank: Path, flags: str = "B0") -> dict:
    report = {"ok": False, "worker_configuration_changed": False, "production_backend_changed": False,
              "restart_required": False, "engine": "teto", "resampler": "ESPER-Utau",
              "backend": "utau", "flags": flags, "flags_experimental": str(flags).startswith("B-"),
              "checks": {}, "teto_synthesis_verified": False,
              "portuguese_speech_verified": False, "portuguese_quality_verified": False}
    try:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", container):
            raise ActivationError("nome do container inválido")
        if not re.fullmatch(r"B(?:0|-?(?:25|50|75|100))", flags):
            raise ActivationError("flags deve ser B0 ou B25/B50/B75/B100, com negativos experimentais permitidos")
        env, root, voicebank = (path.expanduser().absolute() for path in (env, root, voicebank))
        original, snapshot = regular_bytes(env, MAX_ENV_BYTES, allow_empty=True)
        # Decode before any heavy checks so malformed configuration remains untouched.
        original.decode("utf-8")
        if b"\x00" in original:
            raise ActivationError(".env contém caracteres inválidos")
        wrapper = validate_wrapper(root)
        report["checks"]["wrapper"] = wrapper
        if not voicebank.is_dir():
            raise ActivationError("voicebank English ausente")
        values = configuration(root=root, container=container, voicebank=voicebank,
                               flags=flags, wrapper_hash=wrapper["wrapper_sha256"])
        report["checks"].update(verify_runtime(values))
        # Unknown wrapper edits or updates during the long probe must not be selected.
        if validate_wrapper(root)["wrapper_sha256"] != wrapper["wrapper_sha256"]:
            raise ActivationError("wrapper mudou durante a validação; execute novamente")
        publication = publish_environment(env, original, snapshot, updated_environment(original, values))
        report.update(ok=True, worker_configuration_changed=publication["changed"],
                      production_backend_changed=publication["changed"], restart_required=True,
                      teto_synthesis_verified=True, env=str(env), publication=publication,
                      required_bot_pitch_semitones=0.0,
                      note="ESPER configurado após teste de síntese. Reinicie o worker; o tom personalizado no Discord continua valendo.")
    except (ActivationError, OSError, UnicodeError, ValueError, TimeoutError, subprocess.TimeoutExpired) as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"[:1800]
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", type=Path, default=Path(os.getenv("PHONE_WORKER_ENV") or Path.home() / ".phone-worker.env"))
    parser.add_argument("--root", type=Path, default=Path.home() / ".esper-utau")
    parser.add_argument("--container", default="voicepeak-arm64")
    parser.add_argument("--voicebank", type=Path, default=Path.home() / "voicebanks" / "kasane-teto-english")
    parser.add_argument("--flags", default="B0", help="Soprosidade B0 ou B25/B50/B75/B100; negativos experimentais permitidos. Padrão B0.")
    parser.add_argument("--_validate", type=Path, help=argparse.SUPPRESS)
    options = parser.parse_args(argv)
    if options._validate:
        try:
            payload, _ = regular_bytes(options._validate, MAX_REPORT_BYTES)
            values = json.loads(payload)
            if not isinstance(values, dict) or not all(isinstance(key, str) and isinstance(value, str) for key, value in values.items()):
                raise ActivationError("configuração interna inválida")
            result = phrase_child(values)
        except Exception as exc:
            result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:1800]}
    else:
        result = activate(env=options.env, root=options.root, container=options.container,
                          voicebank=options.voicebank, flags=options.flags)
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
