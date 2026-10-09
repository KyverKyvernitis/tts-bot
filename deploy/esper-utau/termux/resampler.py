#!/usr/bin/env python3
"""Bridge UTAU arguments to the pinned native Linux ARM64 ESPER resampler.

The source voicebank is never mounted in the guest. ESPER's analysis files live
beside a private copy of each recording, and only a verified WAV is published.
"""
from __future__ import annotations

import argparse
import array
import contextlib
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
MAX_SOURCE_BYTES = 64 * 1024 * 1024
MAX_FRQ_BYTES = 16 * 1024 * 1024
MAX_ANALYSIS_BYTES = 256 * 1024 * 1024
MAX_OUTPUT_BYTES = 8 * 1024 * 1024
MAX_OUTPUT_SECONDS = 20.0
MAX_LOG_BYTES = 16384
MAX_SOURCE_SECONDS = 180.0
SCRIPT = Path(__file__).resolve()


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


def deadline_for(timeout: float) -> float:
    deadline = time.monotonic() + positive_timeout(timeout)
    configured = os.environ.get("PHONE_WORKER_ESPER_DEADLINE")
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


def source_data(path: Path) -> tuple[bytes, bytes | None]:
    data = read_regular(path, minimum=45, maximum=MAX_SOURCE_BYTES)
    pcm_evidence(data, output=False)
    frq = path.with_name(path.stem + "_wav.frq")
    if frq.exists() or frq.is_symlink():
        frq_data = read_regular(frq, minimum=40, maximum=MAX_FRQ_BYTES)
        if frq_data[:8] != b"FREQ0003":
            raise EsperError("FRQ original não possui formato FREQ0003")
    else:
        frq_data = None
    return data, frq_data


@contextlib.contextmanager
def source_lock(path: Path, deadline: float):
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise EsperError("lock do cache não é arquivo regular")
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                time.sleep(min(0.05, require_time(deadline)))
        yield
    finally:
        os.close(descriptor)


def private_source(cache: Path, key: str, data: bytes, frq: bytes | None) -> Path:
    directory = cache / "sources" / key
    real_directories(directory, create=True)
    source = directory / "source.wav"
    if source.exists() or source.is_symlink():
        existing = read_regular(source, minimum=45, maximum=MAX_SOURCE_BYTES)
        if existing != data:
            raise EsperError("fonte privada do cache mudou; use outra pasta de cache")
    else:
        atomic_write(source, data)
    cached_frq = directory / "source_wav.frq"
    if frq is not None and not cached_frq.exists() and not cached_frq.is_symlink():
        atomic_write(cached_frq, frq)
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


def run_guest(command: list[str], job: Path, deadline: float) -> tuple[str, str]:
    request = job / "supervision.json"
    atomic_write(request, json.dumps({"command": command, "deadline": deadline}).encode())
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        process = subprocess.Popen([sys.executable, str(SCRIPT), "--_supervise", str(request)],
                                   stdin=subprocess.PIPE, stdout=stdout, stderr=stderr, start_new_session=True)
        try:
            process.wait(timeout=require_time(deadline) + 0.5)
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


def synthesize(values: list[str], *, root: Path, cache: Path, container: str, timeout: float) -> dict:
    started = time.monotonic()
    deadline = deadline_for(timeout)
    args = validate_arguments(values)
    root, cache = root.expanduser().absolute(), cache.expanduser().absolute()
    source, target = Path(args[0]).expanduser().absolute(), Path(args[1]).expanduser().absolute()
    real_directories(source.parent)
    real_directories(target.parent)
    if source.resolve() == target.resolve() or source.parent.resolve() == target.parent.resolve():
        raise EsperError("saída deve ficar em outra pasta, fora da pasta do WAV original")
    for name in ("PHONE_WORKER_TETO_VOICEBANK_DIR", "PHONE_WORKER_TETO_ENGLISH_VOICEBANK_DIR"):
        configured = os.environ.get(name)
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
    data, frq = source_data(source)
    require_time(deadline)
    identity = hashlib.sha256()
    for part in (ENGINE_SHA256.encode(), CONFIG_SHA256.encode(), data, frq or b"no-original-frq"):
        identity.update(len(part).to_bytes(8, "little"))
        identity.update(part)
    key = identity.hexdigest()
    with source_lock(cache / "sources" / f"{key}.lock", deadline):
        directory = private_source(cache, key, data, frq)
        with tempfile.TemporaryDirectory(prefix="esper-job-", dir=cache / "jobs") as temporary:
            workdir = Path(temporary)
            command = guest_command(container, runtime["release"], cache, directory, workdir, args)
            run_guest(command, workdir, deadline)
            rendered = read_regular(workdir / "output.wav", minimum=45, maximum=MAX_OUTPUT_BYTES)
            evidence = pcm_evidence(rendered, output=True)
            require_time(deadline)
            for auxiliary, maximum in ((directory / "source.esp", MAX_ANALYSIS_BYTES),
                                       (directory / "source_wav.frq", MAX_FRQ_BYTES)):
                if auxiliary.exists() or auxiliary.is_symlink():
                    regular_info(auxiliary, maximum=maximum)
            atomic_write(target, rendered)
    return {"ok": True, "resampler": "ESPER-Utau", "source_version": RELEASE,
            "engine_sha256": runtime["engine_sha256"], "config_sha256": runtime["config_sha256"],
            "engine_elf_arm64": runtime["engine_elf_arm64"], "native_architecture": "arm64",
            "gc_settings_requested": GC_ENVIRONMENT,
            "container": container, "original_voicebank_changed": False,
            "source_cache_key": key, "original_frq_copied": frq is not None,
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
    parser = argparse.ArgumentParser(description="Executa ESPER-Utau Linux ARM64 com cópia privada da fonte UTAU.")
    parser.add_argument("--root", default=os.getenv("PHONE_WORKER_ESPER_ROOT") or str(Path.home() / ".esper-utau"))
    parser.add_argument("--cache-dir", default=os.getenv("PHONE_WORKER_ESPER_CACHE_DIR"))
    parser.add_argument("--container", default=os.getenv("PHONE_WORKER_ESPER_CONTAINER") or "voicepeak-arm64")
    parser.add_argument("--timeout", default=os.getenv("PHONE_WORKER_ESPER_TIMEOUT") or "60")
    parser.add_argument("--probe", action="store_true", help="Sintetiza fonte artificial e verifica o WAV, sem a Teto.")
    parser.add_argument("--_supervise", help=argparse.SUPPRESS)
    parser.add_argument("utau", nargs="*", help="Os 13 argumentos UTAU: input, output, nota, velocity, flags, OTO, etc.")
    options = parser.parse_args(argv)
    if options._supervise:
        return supervise(Path(options._supervise))
    root = Path(options.root).expanduser().absolute()
    cache = Path(options.cache_dir).expanduser().absolute() if options.cache_dir else root / "cache"
    try:
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
