#!/usr/bin/env python3
"""Bridge UTAU arguments to the pinned native Linux ARM64 ESPER resampler.

The source voicebank is never mounted in the guest. ESPER's analysis files live
beside a private copy of each recording, and only a verified WAV is published.
"""
from __future__ import annotations

import argparse
import array
import base64
import contextlib
from collections import OrderedDict
import fcntl
import hashlib
import io
import json
import math
import os
import platform
import re
import select
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import wave
from pathlib import Path


RELEASE = "v2.5.0"
ENGINE_BYTES = 107335629
ENGINE_SHA256 = "e2cb6dc113593cb3b788debb51996f591a5f9d47bbd2a54df57bc1ecd843602f"
CONFIG_BYTES = 762
CONFIG_SHA256 = "21951154b9bafdebde0b3a1d6533bb1fde549113b63b99a573b49ea68fadbe83"
# .NET 8 otherwise reserves at least 256 GiB of virtual address space for
# region GC. Termux/PRoot ARM64 can reject that reservation before synthesis.
# Environment values for GCHeapHardLimit are hexadecimal: 40000000 = 1 GiB.
# This caps the managed heap, not total RSS, and does not preallocate 1 GiB.
GC_ENVIRONMENT = {"DOTNET_GCHeapHardLimit": "40000000", "DOTNET_gcServer": "0"}
# External FRQ zero/unvoiced frames make ESPER 2.5.0 select wrong pitch
# periods. Keep its own analysis and generated FRQ in a new private namespace.
ANALYSIS_CACHE_VERSION = "native-pitch-v1"
PITCH_ANALYSIS_MODE = "esper-native"
# The upstream PCM16 writer wraps over-range samples instead of limiting them.
# Leave nominal 6 dB headroom before that irreversible conversion.
NATIVE_VOLUME_GAIN = 0.5
MAX_NATIVE_VOLUME_PERCENT = 50.0
MAX_SOURCE_BYTES = 64 * 1024 * 1024
MAX_FRQ_BYTES = 16 * 1024 * 1024
MAX_ANALYSIS_BYTES = 256 * 1024 * 1024
MAX_OUTPUT_BYTES = 8 * 1024 * 1024
MAX_OUTPUT_SECONDS = 20.0
MAX_LOG_BYTES = 16384
MAX_SOURCE_SECONDS = 180.0
SCRIPT = Path(__file__).resolve()
RESIDENT_SOURCE_COMMIT = "2ff1a4692fa88fb1b60933408125632d0c5d8356"
RESIDENT_PROTOCOL = 1
# The portable startup hook is embedded at build time. It uses the already
# verified official runtime and adds no SDK or second engine download.
RESIDENT_HOOK_BASE64 = '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c3BsYXlDbGFzczExXzAAPEluaXRpYWxpemU+Yl9fMABJbnQzMgBJbnQ2NABnZXRfVGlja0NvdW50NjQAPE1vZHVsZT4AU3lzdGVtLklPAFZhbGlkSWQAZ2V0X1Byb2Nlc3NJZABSZWFkAFN5c3RlbS5UaHJlYWRpbmcuVGhyZWFkAGVsYXBzZWQAaWQAR2V0RmllbGQAU2VuZABBcHBlbmQAc2V0X0lzQmFja2dyb3VuZABJbnZva2UAR2V0RW52aXJvbm1lbnRWYXJpYWJsZQBSdW50aW1lVHlwZUhhbmRsZQBHZXRUeXBlRnJvbUhhbmRsZQBTeXN0ZW0uQ29uc29sZQBnZXRfTmFtZQBzZXRfTmFtZQBHZXROYW1lAEFzc2VtYmx5TmFtZQBTeXN0ZW0uUnVudGltZQBSZWFkQm91bmRlZExpbmUAV3JpdGVMaW5lAGxpbmUAZ2V0X0ZpZWxkVHlwZQBHZXRUeXBlAE1ldGhvZEJhc2UAUmVzcG9uc2UAQ29tcGlsZXJHZW5lcmF0ZWRBdHRyaWJ1dGUARGVidWdnYWJsZUF0dHJpYnV0ZQBOdWxsYWJsZUF0dHJpYnV0ZQBBc3NlbWJseVRpdGxlQXR0cmlidXRlAFRhcmdldEZyYW1ld29ya0F0dHJpYnV0ZQBBc3NlbWJseUZpbGVWZXJzaW9uQXR0cmlidXRlAEFzc2VtYmx5SW5mb3JtYXRpb25hbFZlcnNpb25BdHRyaWJ1dGUAQXNzZW1ibHlDb25maWd1cmF0aW9uQXR0cmlidXRlAFJlZlNhZmV0eVJ1bGVzQXR0cmlidXRlAENvbXBpbGF0aW9uUmVsYXhhdGlvbnNBdHRyaWJ1dGUAQXNzZW1ibHlQcm9kdWN0QXR0cmlidXRlAE51bGxhYmxlQ29udGV4dEF0dHJpYnV0ZQBBc3NlbWJseUNvbXBhbnlBdHRyaWJ1dGUAUnVudGltZUNvbXBhdGliaWxpdHlBdHRyaWJ1dGUAU2V0VmFsdWUAdmFsdWUASW5pdGlhbGl6ZQBTeXN0ZW0uVGhyZWFkaW5nAHBlbmRpbmcAVVRGOEVuY29kaW5nAFN5c3RlbS5SdW50aW1lLlZlcnNpb25pbmcARnJvbUJhc2U2NFN0cmluZwBUb1N0cmluZwBHZXRTdHJpbmcARmx1c2gATWF0aABnZXRfTGVuZ3RoAFByb3RvY29sTG9jawBSZXF1ZXN0TG9jawBFc3Blci5TdGFydHVwSG9vawBQdWxzZUFsbABFc3Blci5TdGFydHVwSG9vay5kbGwATnVsbABwcm90b2NvbABTeXN0ZW0AZ2V0X0luAFN5c3RlbS5SZWZsZWN0aW9uAEludmFsaWREYXRhRXhjZXB0aW9uAFRhcmdldEludm9jYXRpb25FeGNlcHRpb24ASW52YWxpZE9wZXJhdGlvbkV4Y2VwdGlvbgBnZXRfSW5uZXJFeGNlcHRpb24AQXJndW1lbnRFeGNlcHRpb24ARmllbGRJbmZvAE1ldGhvZEluZm8ATWVtYmVySW5mbwBQYXJhbWV0ZXJJbmZvAFRleHRSZWFkZXIAU3RyaW5nQnVpbGRlcgBUZXh0V3JpdGVyAEVudGVyAFNldEVycm9yAGVycm9yAC5jdG9yAC5jY3RvcgBNb25pdG9yAFN5c3RlbS5EaWFnbm9zdGljcwBTeXN0ZW0uUnVudGltZS5Db21waWxlclNlcnZpY2VzAERlYnVnZ2luZ01vZGVzAE1heGltdW1Bcmd1bWVudEJ5dGVzAEJpbmRpbmdGbGFncwBHZXRDb21tYW5kTGluZUFyZ3MAQ29udGFpbnMAU3lzdGVtLlRleHQuRW5jb2RpbmcuRXh0ZW5zaW9ucwBTdHJpbmdTcGxpdE9wdGlvbnMAZ2V0X0NoYXJzAE1heGltdW1SZXF1ZXN0Q2hhcmFjdGVycwBHZXRQYXJhbWV0ZXJzAERlY29kZUFyZ3VtZW50cwBDb25jYXQAT2JqZWN0AFdhaXQAU3BsaXQAU291cmNlQ29tbWl0AEV4aXQARW52aXJvbm1lbnQAZ2V0X0VudHJ5UG9pbnQAVGhyZWFkU3RhcnQAQ29udmVydABnZXRfT3V0AFNldE91dABpbnB1dABTeXN0ZW0uVGV4dABNYXgAQXJyYXkAR2V0RW50cnlBc3NlbWJseQBDb3B5AG9wX0luZXF1YWxpdHkARW1wdHkAACN0AHIAdQBuAGMAYQB0AGUAZABfAHIAZQBxAHUAZQBzAHQAAB9pAG4AdgBhAGwAaQBkAF8AcgBlAHEAdQBlAHMAdAAAJWEAcgBnAHUAbQBlAG4AdABfAHQAbwBvAF8AbABhAHIAZwBlAAAhaQBuAHYAYQBsAGkA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AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAMAAADAAAAOA5AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=='
RESIDENT_HOOK_SHA256 = '9a7a67253fafdd9e2a25aa265f40ab1a8789cfb7825bfa03b0abaa49ee3d9da4'


class EsperError(RuntimeError):
    pass


def regular_info(path: Path, *, maximum: int, minimum: int = 1) -> os.stat_result:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or not minimum <= info.st_size <= maximum:
        raise EsperError(f"arquivo regular ausente, inválido ou grande demais: {path.name}")
    return info


def read_regular(path: Path, *, maximum: int, minimum: int = 1) -> bytes:
    info = regular_info(path, maximum=maximum, minimum=minimum)
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as source:
        opened = os.fstat(source.fileno())
        if _stamp(opened) != _stamp(info):
            raise EsperError("arquivo mudou durante a leitura")
        data = source.read(maximum + 1)
        if _stamp(os.fstat(source.fileno())) != _stamp(info):
            raise EsperError("arquivo mudou durante a leitura")
    if len(data) != info.st_size or len(data) > maximum:
        raise EsperError("arquivo mudou ou excedeu o limite durante a leitura")
    return data


def real_directories(path: Path, *, create: bool = False) -> None:
    for candidate in reversed((path, *path.parents)):
        try:
            mode = candidate.lstat().st_mode
        except FileNotFoundError:
            if create:
                try:
                    candidate.mkdir(mode=0o700)
                except FileExistsError:
                    # Different recordings may start their first analysis at
                    # once. Accept the other bridge's directory, never a link.
                    if not stat.S_ISDIR(candidate.lstat().st_mode):
                        raise EsperError(f"pasta deve ser real, sem link simbólico: {candidate}")
                continue
            raise EsperError(f"pasta ausente: {candidate}")
        if not stat.S_ISDIR(mode):
            raise EsperError(f"pasta deve ser real, sem link simbólico: {candidate}")


def atomic_write(path: Path, data: bytes) -> None:
    real_directories(path.parent)
    if path.exists() or path.is_symlink():
        regular_info(path, minimum=0, maximum=max(MAX_SOURCE_BYTES, MAX_OUTPUT_BYTES))
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as target:
            target.write(data)
            target.flush()
            os.fsync(target.fileno())
        if path.exists() or path.is_symlink():
            regular_info(path, minimum=0, maximum=max(MAX_SOURCE_BYTES, MAX_OUTPUT_BYTES))
        os.replace(temporary, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)


def positive_timeout(value: str | float) -> float:
    try:
        number = float(value)
    except (ValueError, TypeError, OverflowError) as exc:
        raise EsperError("timeout deve ser um número finito entre 1 e 120 segundos") from exc
    if not math.isfinite(number) or not 1 <= number <= 120:
        raise EsperError("timeout deve estar entre 1 e 120 segundos")
    return number


def deadline_for(timeout: float, environment=None) -> float:
    deadline = time.monotonic() + positive_timeout(timeout)
    configured = (os.environ if environment is None else environment).get("PHONE_WORKER_ESPER_DEADLINE")
    if configured:
        try:
            inherited = float(configured)
        except (ValueError, TypeError, OverflowError) as exc:
            raise EsperError("PHONE_WORKER_ESPER_DEADLINE inválido") from exc
        if not math.isfinite(inherited):
            raise EsperError("PHONE_WORKER_ESPER_DEADLINE deve ser finito")
        deadline = min(deadline, inherited - 0.2)
    if deadline <= time.monotonic():
        raise TimeoutError("prazo ESPER esgotado antes de iniciar")
    return deadline


def require_time(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("prazo ESPER esgotado")
    return remaining


def validate_arguments(values: list[str]) -> list[str]:
    if len(values) != 13:
        raise EsperError("ESPER requer os 13 argumentos UTAU")
    args = [str(item) for item in values]
    if not re.fullmatch(r"[A-G](?:#|b)?-?\d{1,2}", args[2]):
        raise EsperError("nota UTAU inválida")
    if len(args[4]) > 256 or not re.fullmatch(r"[A-Za-z0-9+.-]*", args[4]):
        raise EsperError("flags UTAU inválidas ou grandes demais")
    numbers: dict[int, float] = {}
    for index in (3, 5, 6, 7, 8, 9, 10):
        try:
            value = float(args[index])
        except (ValueError, OverflowError) as exc:
            raise EsperError("parâmetro UTAU numérico inválido") from exc
        if not math.isfinite(value):
            raise EsperError("parâmetro UTAU deve ser finito")
        numbers[index] = value
    if not (1 <= numbers[3] <= 200 and 0 <= numbers[5] <= 180000
            and 1 <= numbers[6] <= MAX_OUTPUT_SECONDS * 1000
            and 0 <= numbers[7] <= 180000 and -180000 <= numbers[8] <= 180000
            and 0 < numbers[9] <= 200 and 0 <= numbers[10] <= 100):
        raise EsperError("parâmetro UTAU fora dos limites")
    if not re.fullmatch(r"![0-9]+(?:\.[0-9]+)?", args[11]) or not 60 <= float(args[11][1:]) <= 240:
        raise EsperError("tempo UTAU deve usar !60 a !240")
    if not args[12] or len(args[12]) > 16384:
        raise EsperError("pitchbend UTAU vazio ou grande demais")
    pieces = args[12].split("#")
    expanded = 0
    for index, piece in enumerate(pieces):
        if index % 2 == 0:
            if not piece and index == len(pieces) - 1:
                continue
            if not piece or len(piece) % 2 or not re.fullmatch(r"[A-Za-z0-9+/]+", piece):
                raise EsperError("pitchbend UTAU inválido")
            expanded += len(piece) // 2
        else:
            if not re.fullmatch(r"[0-9]{1,5}", piece):
                raise EsperError("repetição de pitchbend UTAU inválida")
            expanded += int(piece)
    if not 1 <= expanded <= 10000:
        raise EsperError("pitchbend UTAU excedeu o limite de pontos")
    # ESPER ArgParser uses long.Parse for LENGTH, unlike double OTO fields.
    args[6] = str(math.ceil(numbers[6]))
    return args


def pcm_evidence(data: bytes, *, output: bool) -> dict:
    try:
        with wave.open(io.BytesIO(data), "rb") as source:
            rate, width, channels, frames = (source.getframerate(), source.getsampwidth(),
                                            source.getnchannels(), source.getnframes())
            maximum = MAX_OUTPUT_SECONDS if output else MAX_SOURCE_SECONDS
            if channels != 1 or width != 2 or source.getcomptype() != "NONE" or not 8000 <= rate <= 192000:
                raise EsperError("WAV precisa ser PCM16 mono, entre 8000 e 192000 Hz")
            if not 0 < frames <= math.ceil(rate * maximum):
                raise EsperError("WAV vazio ou longo demais")
            samples = source.readframes(frames)
            if len(samples) != frames * width or not any(samples):
                raise EsperError("WAV truncado ou silencioso")
        return {"wav_verified": True, "sample_rate": rate, "channels": channels,
                "sample_width_bytes": width, "sample_frames": frames, "duration_seconds": frames / rate}
    except (wave.Error, EOFError) as exc:
        raise EsperError(f"WAV inválido: {exc}") from exc


def _stamp(info: os.stat_result) -> list[int]:
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns]


def runtime_info(root: Path, cache: Path, *, deadline: float) -> dict:
    if platform.machine().lower() not in {"aarch64", "arm64"}:
        raise EsperError("este wrapper usa somente Linux ARM64 no guest; host ARM64 necessário")
    release = root / "releases" / RELEASE
    real_directories(release)
    engine, config = release / "ESPER-Utau", release / "esper-config.ini"
    info = regular_info(engine, minimum=ENGINE_BYTES, maximum=ENGINE_BYTES)
    if not info.st_mode & stat.S_IXUSR:
        raise EsperError("ESPER-Utau não tem permissão de execução")
    descriptor = os.open(engine, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as source:
        if _stamp(os.fstat(source.fileno())) != _stamp(info):
            raise EsperError("executável ESPER mudou durante a verificação")
        header = source.read(64)
        if not (len(header) == 64 and header[:4] == b"\x7fELF" and header[4:6] == b"\x02\x01"
                and int.from_bytes(header[18:20], "little") == 183):
            raise EsperError("executável ESPER não é ELF Linux ARM64")
        cached_path = cache / f"verified-{ENGINE_SHA256}.json"
        cached = {}
        with contextlib.suppress(OSError, ValueError, EsperError):
            cached = json.loads(read_regular(cached_path, maximum=4096))
        if not isinstance(cached, dict):
            cached = {}
        if cached.get("sha256") != ENGINE_SHA256 or cached.get("stamp") != _stamp(info):
            source.seek(0)
            digest = hashlib.sha256()
            while block := source.read(1024 * 1024):
                require_time(deadline)
                digest.update(block)
            if digest.hexdigest() != ENGINE_SHA256 or _stamp(os.fstat(source.fileno())) != _stamp(info):
                raise EsperError("SHA256 do ESPER-Utau difere da versão oficial fixada")
            atomic_write(cached_path, json.dumps({"sha256": ENGINE_SHA256, "stamp": _stamp(info)}).encode())
    config_bytes = read_regular(config, minimum=CONFIG_BYTES, maximum=CONFIG_BYTES)
    if hashlib.sha256(config_bytes).hexdigest() != CONFIG_SHA256:
        raise EsperError("esper-config.ini difere do arquivo oficial fixado")
    return {"release": release, "engine": engine, "engine_sha256": ENGINE_SHA256,
            "config_sha256": CONFIG_SHA256, "engine_elf_arm64": True}


def source_data(path: Path) -> bytes:
    data = read_regular(path, minimum=45, maximum=MAX_SOURCE_BYTES)
    pcm_evidence(data, output=False)
    # Do not even read the voicebank FRQ. It remains available to other engines;
    # ESPER must infer pitch from this recording rather than import those hints.
    return data


@contextlib.contextmanager
def source_lock(path: Path, deadline: float, *, cancelled=None):
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise EsperError("lock do cache não é arquivo regular")
        while True:
            if _cancelled(cancelled):
                raise EsperError("renderização ESPER interrompida")
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                time.sleep(min(0.05, require_time(deadline)))
        yield
    finally:
        os.close(descriptor)


def private_source(cache: Path, key: str, data: bytes, *, verified=None) -> Path:
    directory = cache / "sources" / key
    real_directories(directory, create=True)
    source = directory / "source.wav"
    if source.exists() or source.is_symlink():
        info = regular_info(source, minimum=45, maximum=MAX_SOURCE_BYTES)
        stamp = _stamp(info)
        if verified is None or verified.get(str(source)) != stamp:
            existing = read_regular(source, minimum=45, maximum=MAX_SOURCE_BYTES)
            if existing != data:
                raise EsperError("fonte privada do cache mudou; use outra pasta de cache")
            if verified is not None:
                verified[str(source)] = stamp
    else:
        atomic_write(source, data)
        if verified is not None:
            verified[str(source)] = _stamp(regular_info(source, minimum=45, maximum=MAX_SOURCE_BYTES))
    cached_frq = directory / "source_wav.frq"
    for auxiliary, maximum in ((directory / "source.esp", MAX_ANALYSIS_BYTES), (cached_frq, MAX_FRQ_BYTES)):
        if auxiliary.exists() or auxiliary.is_symlink():
            regular_info(auxiliary, maximum=maximum)
    return directory


def guest_command(container: str, release: Path, cache: Path, source: Path, job: Path,
                  args: list[str]) -> list[str]:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", container):
        raise EsperError("nome do container proot inválido")
    if not shutil.which("proot-distro"):
        raise EsperError("proot-distro não encontrado")
    if any(":" in str(path) for path in (release, cache, source, job)):
        raise EsperError("pasta ESPER incompatível com bind do proot")
    command = ["proot-distro", "login", container,
               "--bind", f"{release}:/opt/esper-engine", "--bind", f"{cache}:/opt/esper-cache",
               "--bind", f"{source}:/opt/esper-source", "--bind", f"{job}:/opt/esper-job",
               "--", "/usr/bin/env", "LANG=C", "LC_ALL=C",
               "DOTNET_SYSTEM_GLOBALIZATION_INVARIANT=1",
               "DOTNET_BUNDLE_EXTRACT_BASE_DIR=/opt/esper-cache/dotnet",
               *[f"{key}={value}" for key, value in GC_ENVIRONMENT.items()],
               "OMP_NUM_THREADS=2", "OPENBLAS_NUM_THREADS=2",
               "/opt/esper-engine/ESPER-Utau"]
    return command + ["/opt/esper-source/source.wav", "/opt/esper-job/output.wav", *args[2:]]


def _kill_group(process: subprocess.Popen) -> None:
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)
    process.wait()


def supervise(path: Path) -> int:
    """Keep a guest alive only while the bridge's control pipe stays open.

    The legacy UTAU renderer can SIGKILL its immediate subprocess on timeout.
    EOF still reaches this supervisor, which kills the guest process group.
    """
    process = None
    try:
        job = json.loads(read_regular(path, maximum=65536))
        command = job["command"]
        deadline = float(job["deadline"])
        if (not isinstance(command, list) or not command or len(command) > 128
                or any(not isinstance(item, str) or len(item) > 16384 for item in command)
                or not math.isfinite(deadline)):
            raise EsperError("job de supervisão inválido")
        require_time(deadline)
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, start_new_session=True)
        while process.poll() is None:
            remaining = require_time(deadline)
            ready, _, _ = select.select([sys.stdin.fileno()], [], [], min(0.1, remaining))
            if ready and not os.read(sys.stdin.fileno(), 1):
                raise EsperError("launcher pai terminou; grupo ESPER interrompido")
        return process.returncode if process.returncode >= 0 else 128 - process.returncode
    except (EsperError, OSError, KeyError, TypeError, ValueError, TimeoutError) as exc:
        print(f"ESPER: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 124 if isinstance(exc, TimeoutError) else 125
    finally:
        if process is not None:
            _kill_group(process)


def run_guest(command: list[str], job: Path, deadline: float, *, environment=None,
              cancelled=None) -> tuple[str, str]:
    request = job / "supervision.json"
    atomic_write(request, json.dumps({"command": command, "deadline": deadline}).encode())
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        process = subprocess.Popen([sys.executable, str(SCRIPT), "--_supervise", str(request)],
                                   stdin=subprocess.PIPE, stdout=stdout, stderr=stderr, start_new_session=True,
                                   env=environment)
        try:
            if cancelled is None:
                process.wait(timeout=require_time(deadline) + 0.5)
            else:
                while process.poll() is None:
                    if _cancelled(cancelled):
                        raise EsperError("renderização ESPER interrompida")
                    try:
                        process.wait(timeout=min(.025, require_time(deadline)))
                    except subprocess.TimeoutExpired:
                        pass
        except BaseException:
            # Closing the pipe first lets the supervisor stop its separate group.
            if process.stdin is not None:
                process.stdin.close()
            try:
                process.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                _kill_group(process)
            raise
        finally:
            if process.stdin is not None:
                process.stdin.close()
        tails = []
        for stream in (stdout, stderr):
            stream.seek(0, os.SEEK_END)
            stream.seek(max(0, stream.tell() - MAX_LOG_BYTES))
            tails.append(stream.read().decode("utf-8", errors="replace"))
        output, error = tails
    # Some proot versions return zero after reporting a signalled vpid.
    combined = output + "\n" + error
    if process.returncode or re.search(r"terminated with signal|Segmentation fault|uncaught target signal", combined, re.I):
        raise EsperError(f"guest ESPER falhou (code {process.returncode}): {(error or output)[-2000:]}")
    require_time(deadline)
    return output, error


def _source_key(data: bytes) -> str:
    identity = hashlib.sha256()
    for part in (ANALYSIS_CACHE_VERSION.encode(), ENGINE_SHA256.encode(), CONFIG_SHA256.encode(), data):
        identity.update(len(part).to_bytes(8, "little"))
        identity.update(part)
    return identity.hexdigest()


class _NativeSession:
    """One real .NET process, accessed by a single leased Python thread."""

    def __init__(self, command, environment, deadline, cancelled, identity):
        self.identity = identity
        self._next_id = 0
        self._buffer = bytearray()
        self._stderr = tempfile.TemporaryFile()
        self.process = None
        try:
            self.process = subprocess.Popen(command, stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=self._stderr, start_new_session=True,
                env=environment, bufsize=0)
            os.set_blocking(self.process.stdin.fileno(), False)
            os.set_blocking(self.process.stdout.fileno(), False)
            ready = self._receive(deadline, cancelled)
            if (ready.get('protocol') != RESIDENT_PROTOCOL or ready.get('ready') is not True
                    or ready.get('native_resident') is not True
                    or ready.get('source_commit') != RESIDENT_SOURCE_COMMIT):
                raise EsperError('handshake do host ESPER residente inválido')
            self._transport = ready.get('transport',
                'base64-tsv-v1' if ready.get('request_encoding') == 'tsv-base64-v1' else 'json-v1')
            if self._transport not in {'json-v1', 'base64-tsv-v1'}:
                raise EsperError('transporte do host ESPER residente inválido')
        except BaseException:
            self.close()
            raise

    def _check(self, deadline, cancelled):
        if _cancelled(cancelled):
            raise EsperError('renderização ESPER interrompida')
        require_time(deadline)
        if self.process is None or self.process.poll() is not None:
            raise EsperError('host ESPER residente terminou')

    def _receive(self, deadline, cancelled):
        while True:
            self._check(deadline, cancelled)
            separator = self._buffer.find(b'\n')
            if separator >= 0:
                raw = bytes(self._buffer[:separator])
                del self._buffer[:separator + 1]
                try:
                    value = json.loads(raw)
                except (UnicodeError, ValueError) as exc:
                    raise EsperError('resposta do host ESPER residente inválida') from exc
                if not isinstance(value, dict):
                    raise EsperError('resposta do host ESPER residente inválida')
                return value
            if len(self._buffer) > 65536:
                raise EsperError('resposta do host ESPER residente excedeu limite')
            ready, _, _ = select.select([self.process.stdout], [], [], min(.025, require_time(deadline)))
            if ready:
                block = os.read(self.process.stdout.fileno(), 65537 - len(self._buffer))
                if not block:
                    raise EsperError('host ESPER residente fechou a resposta')
                self._buffer.extend(block)

    def render(self, args, *, deadline, cancelled):
        self._next_id += 1
        identity = str(self._next_id)
        if self._transport == 'base64-tsv-v1':
            data = (identity + '\t' + '\t'.join(base64.b64encode(value.encode('utf-8')).decode('ascii')
                                               for value in args)).encode('ascii') + b'\n'
        else:
            data = json.dumps({'id': identity, 'args': args}, ensure_ascii=False,
                              allow_nan=False, separators=(',', ':')).encode() + b'\n'
        if len(data) > 65536:
            raise EsperError('pedido do host ESPER residente excedeu limite')
        offset = 0
        while offset < len(data):
            self._check(deadline, cancelled)
            _, ready, _ = select.select([], [self.process.stdin], [], min(.025, require_time(deadline)))
            if ready:
                offset += os.write(self.process.stdin.fileno(), data[offset:])
        response = self._receive(deadline, cancelled)
        if str(response.get('id')) != identity or response.get('ok') is not True:
            raise EsperError('host ESPER residente não confirmou a síntese')
        # Diagnostics cannot grow for the lifetime of the resident process.
        self._stderr.seek(0)
        self._stderr.truncate()

    def close(self):
        if self.process is not None:
            process, self.process = self.process, None
            if process.stdin is not None:
                with contextlib.suppress(OSError):
                    process.stdin.close()
            _kill_group(process)
            if process.stdout is not None:
                process.stdout.close()
        self._stderr.close()


class InProcessEsper:
    """Keep the verified Python bridge and immutable recording cache resident.

    The portable startup hook keeps the verified official .NET engine alive.
    Unsupported hosts retain the original CLI and parent-death supervisor.
    Each source keeps one physical writer and every cancellation drains its
    native process before releasing the source lock. Native inverse transforms
    still complete a whole fragment before returning its waveform.
    """

    def __init__(self, *, root: Path, cache: Path, container: str, timeout=60,
                 max_source_bytes=128 * 1024 * 1024):
        self.root = Path(root)
        self.cache = Path(cache)
        self.container = str(container)
        self.timeout = positive_timeout(timeout)
        if type(max_source_bytes) is not int or max_source_bytes < 0:
            raise EsperError("limite do cache de fontes inválido")
        self.max_source_bytes = max_source_bytes
        self._sources = OrderedDict()
        self._source_bytes = 0
        self._verified_private = OrderedDict()
        self._lock = threading.RLock()
        self._native_condition = threading.Condition()
        self._native_slots = []
        self._native_verified = None
        self._native_failed = None
        self._closed = False
        self.source_cache_hits = self.source_cache_misses = 0

    def close(self):
        with self._native_condition:
            self._closed = True
            sessions = [slot['session'] for slot in self._native_slots if slot['session'] is not None]
            self._native_condition.notify_all()
        for session in sessions:
            session.close()
        with self._lock:
            self._sources.clear()
            self._source_bytes = 0
            self._verified_private.clear()
        source_directory = getattr(self, '_bundled_source_directory', None)
        if source_directory is not None:
            source_directory.cleanup()

    def _resident_host(self, root, deadline):
        if RESIDENT_HOOK_BASE64:
            require_time(deadline)
            payload = base64.b64decode(RESIDENT_HOOK_BASE64, validate=True)
            if (not 1024 <= len(payload) <= 65536
                    or hashlib.sha256(payload).hexdigest() != RESIDENT_HOOK_SHA256):
                raise EsperError('startup hook ESPER embutido inválido')
            directory = self.cache.expanduser().absolute() / 'resident-hook' / RESIDENT_HOOK_SHA256
            real_directories(directory, create=True)
            target = directory / 'Esper.StartupHook.dll'
            if target.exists() or target.is_symlink():
                info = regular_info(target, maximum=65536)
                identity = ('startup-hook', RESIDENT_HOOK_SHA256, tuple(_stamp(info)))
                with self._lock:
                    verified = self._native_verified == identity
                    failed = self._native_failed == identity
                if failed:
                    return None
                if not verified and hashlib.sha256(read_regular(target, maximum=65536)).hexdigest() != RESIDENT_HOOK_SHA256:
                    raise EsperError('startup hook ESPER instalado mudou')
            else:
                atomic_write(target, payload)
                identity = ('startup-hook', RESIDENT_HOOK_SHA256,
                            tuple(_stamp(regular_info(target, maximum=65536))))
            with self._lock:
                self._native_verified = identity
            return directory, identity
        host = root / 'resident' / 'v1-linux-arm64'
        manifest_path = host / 'manifest.json'
        if not manifest_path.exists():
            return None
        real_directories(host)
        info = regular_info(manifest_path, maximum=131072)
        manifest = json.loads(read_regular(manifest_path, maximum=131072))
        if (not isinstance(manifest, dict) or manifest.get('protocol') != RESIDENT_PROTOCOL
                or manifest.get('source_commit') != RESIDENT_SOURCE_COMMIT
                or manifest.get('engine_sha256') != ENGINE_SHA256
                or manifest.get('config_sha256') != CONFIG_SHA256):
            raise EsperError('manifesto do host ESPER residente inválido')
        files = manifest.get('files')
        if not isinstance(files, dict) or not 1 <= len(files) <= 256 or 'Esper.Resident' not in files:
            raise EsperError('arquivos do host ESPER residente inválidos')
        records, total = [], 0
        for name, digest in sorted(files.items()):
            if (not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9_.+/-]{1,240}', name)
                    or name.startswith('/') or any(part in ('', '.', '..') for part in name.split('/'))
                    or not isinstance(digest, str) or not re.fullmatch(r'[a-f0-9]{64}', digest)):
                raise EsperError('arquivo do host ESPER residente inválido')
            target = host / name
            real_directories(target.parent)
            stamp = regular_info(target, maximum=128 * 1024 * 1024)
            total += stamp.st_size
            if total > 512 * 1024 * 1024:
                raise EsperError('host ESPER residente excedeu limite')
            records.append((name, digest, tuple(_stamp(stamp))))
        identity = (tuple(_stamp(info)), tuple(records))
        with self._lock:
            if self._native_failed == identity:
                return None
            if self._native_verified == identity:
                return host, identity
        for name, digest, stamp in records:
            require_time(deadline)
            data = read_regular(host / name, maximum=128 * 1024 * 1024)
            if hashlib.sha256(data).hexdigest() != digest:
                raise EsperError('SHA256 do host ESPER residente inválido')
            if tuple(_stamp(regular_info(host / name, maximum=128 * 1024 * 1024))) != stamp:
                raise EsperError('host ESPER residente mudou durante verificação')
        if not (host / 'Esper.Resident').stat().st_mode & stat.S_IXUSR:
            raise EsperError('host ESPER residente não executável')
        if hashlib.sha256(read_regular(host / 'esper-config.ini', maximum=CONFIG_BYTES)).hexdigest() != CONFIG_SHA256:
            raise EsperError('configuração do host ESPER residente inválida')
        with self._lock:
            self._native_verified = identity
        return host, identity

    def _run_resident(self, root, cache, directory, workdir, args, deadline, cancelled, environment):
        if str(environment.get('PHONE_WORKER_ESPER_RESIDENT', 'auto')).lower() in {'0', 'false', 'no', 'off'}:
            return False
        try:
            installed = self._resident_host(root, deadline)
        except (EsperError, OSError, ValueError):
            # An unverified optional host is never executed. The separately
            # pinned official CLI remains the compatibility path.
            return False
        if installed is None:
            return False
        host, identity = installed
        try:
            workers = max(1, min(4, int(environment.get('PHONE_WORKER_TETO_RENDER_THREADS', 2))))
        except (ValueError, TypeError):
            workers = 2
        with self._native_condition:
            while True:
                if self._closed or _cancelled(cancelled):
                    raise EsperError('renderização ESPER interrompida')
                require_time(deadline)
                slot = next((slot for slot in self._native_slots if not slot['busy']), None)
                if slot is None and len(self._native_slots) < workers:
                    slot = {'busy': False, 'session': None}
                    self._native_slots.append(slot)
                if slot is not None:
                    slot['busy'] = True
                    break
                self._native_condition.wait(min(.025, require_time(deadline)))
        try:
            session = slot['session']
            if session is not None and session.identity != identity:
                session.close()
                session = slot['session'] = None
            if session is None:
                hook = isinstance(identity, tuple) and identity[0] == 'startup-hook'
                command = ['proot-distro', 'login', self.container,
                    '--bind', f'{host}:/opt/esper-resident', '--bind', f'{cache}:/opt/esper-cache',
                    *(['--bind', f'{root / "releases" / RELEASE}:/opt/esper-engine'] if hook else []),
                    '--', '/usr/bin/env', 'LANG=C', 'LC_ALL=C',
                    'DOTNET_SYSTEM_GLOBALIZATION_INVARIANT=1',
                    'DOTNET_BUNDLE_EXTRACT_BASE_DIR=/opt/esper-cache/dotnet',
                    *[f'{key}={value}' for key, value in GC_ENVIRONMENT.items()],
                    'OMP_NUM_THREADS=2', 'OPENBLAS_NUM_THREADS=2',
                    *(['DOTNET_STARTUP_HOOKS=/opt/esper-resident/Esper.StartupHook.dll',
                       'PHONE_WORKER_ESPER_HOOK_PROTOCOL=1',
                       '/opt/esper-engine/ESPER-Utau', '--_resident'] if hook else
                      ['/opt/esper-resident/Esper.Resident', '--server'])]
                session = slot['session'] = _NativeSession(command, environment, deadline, cancelled, identity)
            request = list(args)
            request[0] = '/opt/esper-cache/' + (directory / 'source.wav').relative_to(cache).as_posix()
            request[1] = '/opt/esper-cache/' + (workdir / 'output.wav').relative_to(cache).as_posix()
            session.render(request, deadline=deadline, cancelled=cancelled)
            return True
        except BaseException as error:
            if slot['session'] is not None:
                slot['session'].close()
                slot['session'] = None
            if self._closed or _cancelled(cancelled):
                raise EsperError('renderização ESPER interrompida') from error
            if isinstance(error, TimeoutError) or not isinstance(error, (EsperError, OSError)):
                raise
            require_time(deadline)
            with self._lock:
                self._native_failed = identity
            # A failed native analysis must not poison a subsequent CLI retry.
            for auxiliary in (directory / 'source.esp', directory / 'source_wav.frq'):
                with contextlib.suppress(OSError):
                    auxiliary.unlink()
            return False
        finally:
            with self._native_condition:
                slot['busy'] = False
                self._native_condition.notify()

    def _recording(self, path):
        # Include ctime, inode and device: an edit that preserves mtime must
        # still invalidate this cache. Recheck after a cached access too.
        info = regular_info(path, minimum=45, maximum=MAX_SOURCE_BYTES)
        key = (str(path), tuple(_stamp(info)))
        with self._lock:
            saved = self._sources.get(key)
            if saved is not None:
                if _stamp(regular_info(path, minimum=45, maximum=MAX_SOURCE_BYTES)) != list(key[1]):
                    raise EsperError("fonte mudou durante consulta ao cache")
                self._sources.move_to_end(key)
                self.source_cache_hits += 1
                return saved
        data = source_data(path)
        if _stamp(regular_info(path, minimum=45, maximum=MAX_SOURCE_BYTES)) != list(key[1]):
            raise EsperError("fonte mudou durante leitura")
        value = (data, _source_key(data))
        with self._lock:
            self.source_cache_misses += 1
            if len(data) <= self.max_source_bytes:
                old = self._sources.pop(key, None)
                if old is not None:
                    self._source_bytes -= len(old[0])
                self._sources[key] = value
                self._source_bytes += len(data)
                while self._source_bytes > self.max_source_bytes:
                    _, removed = self._sources.popitem(last=False)
                    self._source_bytes -= len(removed[0])
        return value

    def synthesize(self, values, *, deadline, cancelled=None, environment=None):
        if self._closed:
            raise EsperError("bridge ESPER encerrado")
        if type(deadline) not in (int, float) or not math.isfinite(deadline):
            raise EsperError("prazo absoluto ESPER inválido")
        require_time(deadline)
        context = {"bridge": self, "deadline": deadline, "cancelled": cancelled,
                   "environment": dict(os.environ if environment is None else environment)}
        return synthesize(values, root=self.root, cache=self.cache,
                          container=self.container, timeout=self.timeout, _context=context)

    def _private_source(self, cache, key, data):
        with self._lock:
            directory = private_source(cache, key, data, verified=self._verified_private)
            name = str(directory / 'source.wav')
            self._verified_private.move_to_end(name)
            while len(self._verified_private) > 2048:
                self._verified_private.popitem(last=False)
            return directory


def _cancelled(callback):
    if callback is None:
        return False
    return bool(callback() if callable(callback) else callback.is_set())


def run_guest_in_process(command, job, deadline, *, cancelled=None, environment=None):
    """Retain the parent-death guardian when using the official native CLI."""
    if _cancelled(cancelled):
        raise EsperError("renderização ESPER interrompida")
    require_time(deadline)
    return run_guest(command, job, deadline, cancelled=cancelled, environment=environment)


def synthesize(values: list[str], *, root: Path, cache: Path, container: str, timeout: float,
               _context=None) -> dict:
    started = time.monotonic()
    environment = os.environ if _context is None else _context["environment"]
    deadline = deadline_for(timeout, environment)
    bridge = None if _context is None else _context["bridge"]
    if _context is not None:
        deadline = min(deadline, _context["deadline"])
        if _cancelled(_context["cancelled"]):
            raise EsperError("renderização ESPER interrompida")
    args = validate_arguments(values)
    requested_volume = float(args[9])
    native_volume = min(requested_volume * NATIVE_VOLUME_GAIN, MAX_NATIVE_VOLUME_PERCENT)
    args[9] = format(native_volume, ".12g")
    root, cache = root.expanduser().absolute(), cache.expanduser().absolute()
    source, target = Path(args[0]).expanduser().absolute(), Path(args[1]).expanduser().absolute()
    real_directories(source.parent)
    real_directories(target.parent)
    if source.resolve() == target.resolve() or source.parent.resolve() == target.parent.resolve():
        raise EsperError("saída deve ficar em outra pasta, fora da pasta do WAV original")
    for name in ("PHONE_WORKER_TETO_VOICEBANK_DIR", "PHONE_WORKER_TETO_ENGLISH_VOICEBANK_DIR"):
        configured = environment.get(name)
        if configured:
            try:
                target.resolve().relative_to(Path(configured).expanduser().resolve())
            except ValueError:
                pass
            else:
                raise EsperError("saída não pode alterar a voicebank original")
    if target.exists() or target.is_symlink():
        regular_info(target, minimum=0, maximum=MAX_OUTPUT_BYTES)
    real_directories(cache, create=True)
    real_directories(cache / "sources", create=True)
    real_directories(cache / "jobs", create=True)
    real_directories(cache / "dotnet", create=True)
    runtime = runtime_info(root, cache, deadline=deadline)
    data, key = ((source_data(source), None) if bridge is None else bridge._recording(source))
    require_time(deadline)
    key = key or _source_key(data)
    native_resident = False
    lock_options = ({"cancelled": _context["cancelled"]}
                    if _context is not None and _context["cancelled"] is not None else {})
    with source_lock(cache / "sources" / f"{key}.lock", deadline, **lock_options):
        directory = (private_source(cache, key, data) if bridge is None
                     else bridge._private_source(cache, key, data))
        try:
            with tempfile.TemporaryDirectory(prefix="esper-job-", dir=cache / "jobs") as temporary:
                workdir = Path(temporary)
                command = guest_command(container, runtime["release"], cache, directory, workdir, args)
                if bridge is None:
                    run_guest(command, workdir, deadline)
                else:
                    native_resident = bridge._run_resident(root, cache, directory, workdir,
                        args, deadline, _context["cancelled"], environment)
                    if not native_resident:
                        run_guest_in_process(command, workdir, deadline,
                            cancelled=_context["cancelled"], environment=environment)
                rendered = read_regular(workdir / "output.wav", minimum=45, maximum=MAX_OUTPUT_BYTES)
                evidence = pcm_evidence(rendered, output=True)
                require_time(deadline)
                for auxiliary, maximum in ((directory / "source.esp", MAX_ANALYSIS_BYTES),
                                           (directory / "source_wav.frq", MAX_FRQ_BYTES)):
                    if auxiliary.exists() or auxiliary.is_symlink():
                        regular_info(auxiliary, maximum=maximum)
                atomic_write(target, rendered)
        except BaseException:
            # Interrupted analysis can leave a partial file that looks reusable.
            # Discard only the bridge's generated auxiliaries while still owning
            # the source lock; the next request regenerates them natively.
            for auxiliary in (directory / "source.esp", directory / "source_wav.frq"):
                with contextlib.suppress(OSError):
                    auxiliary.unlink()
            raise
    return {"ok": True, "resampler": "ESPER-Utau", "source_version": RELEASE,
            "implementation_id": hashlib.sha256(read_regular(SCRIPT, maximum=128 * 1024)).hexdigest(),
            "engine_sha256": runtime["engine_sha256"], "config_sha256": runtime["config_sha256"],
            "engine_elf_arm64": runtime["engine_elf_arm64"], "native_architecture": "arm64",
            "gc_settings_requested": GC_ENVIRONMENT,
            "bridge_in_process": bridge is not None, "native_resident": native_resident,
            "native_streaming": False,
            **({"source_cache_hits": bridge.source_cache_hits,
                "source_cache_misses": bridge.source_cache_misses,
                "source_cache_bytes": bridge._source_bytes} if bridge is not None else {}),
            "container": container, "original_voicebank_changed": False,
            "source_cache_key": key, "original_frq_copied": False,
            "pitch_analysis_mode": PITCH_ANALYSIS_MODE, "analysis_cache_version": ANALYSIS_CACHE_VERSION,
            "requested_volume_percent": requested_volume, "native_volume_percent": native_volume,
            "analysis_directory": str(directory), "output": str(target),
            "sha256": hashlib.sha256(rendered).hexdigest(), "bytes": len(rendered),
            "worker_synth_ms": round((time.monotonic() - started) * 1000, 2), **evidence}


def probe(*, root: Path, cache: Path, container: str, timeout: float) -> dict:
    real_directories(cache, create=True)
    with tempfile.TemporaryDirectory(prefix="esper-probe-", dir=cache) as temporary:
        path = Path(temporary)
        original, output = path / "input", path / "output"
        original.mkdir()
        output.mkdir()
        samples = array.array("h", (int(9000 * math.sin(2 * math.pi * 220 * index / 44100)
                                         + 3500 * math.sin(2 * math.pi * 440 * index / 44100))
                                   for index in range(round(44100 * 0.45))))
        if sys.byteorder != "little":
            samples.byteswap()
        source = original / "source.wav"
        with wave.open(str(source), "wb") as wav:
            wav.setparams((1, 2, 44100, 0, "NONE", "not compressed"))
            wav.writeframes(samples.tobytes())
        result = synthesize([str(source), str(output / "result.wav"), "C4", "100", "", "0",
                             "240", "30", "-400", "100", "0", "!140", "AA"],
                            root=root, cache=cache, container=container, timeout=timeout)
    result.pop("output", None)
    result.update({"synthetic_render_verified": True, "teto_synthesis_verified": False,
                   "portuguese_speech_verified": False, "portuguese_quality_verified": False,
                   "box64_required": False, "voicepeak_required": False,
                   "note": "Teste real do resampler com fonte sintética; não usa voicebank nem comprova fala da Teto."})
    return result


def main(argv: list[str] | None = None) -> int:
    # A background/nohup parent can pass SIG_IGN through exec. Restore Python's
    # cooperative interruption so run_guest closes and joins its supervisor.
    signal.signal(signal.SIGINT, signal.default_int_handler)
    parser = argparse.ArgumentParser(description="Executa ESPER-Utau Linux ARM64 com cópia privada da fonte UTAU.")
    parser.add_argument("--root", default=os.getenv("PHONE_WORKER_ESPER_ROOT") or str(Path.home() / ".esper-utau"))
    parser.add_argument("--cache-dir", default=os.getenv("PHONE_WORKER_ESPER_CACHE_DIR"))
    parser.add_argument("--container", default=os.getenv("PHONE_WORKER_ESPER_CONTAINER") or "voicepeak-arm64")
    parser.add_argument("--timeout", default=os.getenv("PHONE_WORKER_ESPER_TIMEOUT") or "60")
    parser.add_argument("--implementation-id", help="SHA256 esperado deste bridge; também identifica o cache do worker.")
    parser.add_argument("--probe", action="store_true", help="Sintetiza fonte artificial e verifica o WAV, sem a Teto.")
    parser.add_argument("--_supervise", help=argparse.SUPPRESS)
    parser.add_argument("utau", nargs="*", help="Os 13 argumentos UTAU: input, output, nota, velocity, flags, OTO, etc.")
    options = parser.parse_args(argv)
    if options._supervise:
        return supervise(Path(options._supervise))
    root = Path(options.root).expanduser().absolute()
    cache = Path(options.cache_dir).expanduser().absolute() if options.cache_dir else root / "cache"
    try:
        if options.implementation_id is not None:
            expected = options.implementation_id
            if not re.fullmatch(r"[0-9a-fA-F]{64}", expected):
                raise EsperError("--implementation-id deve ser um SHA256 de 64 caracteres hexadecimais")
            actual = hashlib.sha256(read_regular(SCRIPT, maximum=128 * 1024)).hexdigest()
            if actual != expected.lower():
                raise EsperError("SHA256 do bridge difere de --implementation-id; atualize a configuração do ESPER")
        timeout = positive_timeout(options.timeout)
        if options.probe:
            if options.utau:
                raise EsperError("--probe não aceita argumentos UTAU")
            result = probe(root=root, cache=cache, container=options.container, timeout=timeout)
        else:
            result = synthesize(options.utau, root=root, cache=cache, container=options.container, timeout=timeout)
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        return 0
    except (EsperError, OSError, ValueError, TimeoutError, subprocess.TimeoutExpired) as exc:
        result = {"ok": False, "resampler": "ESPER-Utau", "source_version": RELEASE,
                  "gc_settings_requested": GC_ENVIRONMENT,
                  "synthetic_render_verified": False, "teto_synthesis_verified": False,
                  "portuguese_speech_verified": False, "original_voicebank_changed": False,
                  "error": f"{type(exc).__name__}: {exc}"}
        print(json.dumps(result, ensure_ascii=False, allow_nan=False), file=sys.stderr)
        return 124 if isinstance(exc, (TimeoutError, subprocess.TimeoutExpired)) else 1


if __name__ == "__main__":
    raise SystemExit(main())
