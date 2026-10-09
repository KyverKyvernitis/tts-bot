#!/usr/bin/env python3
"""Read-only WORLDLINE-R/Termux inventory; this never enables the phone worker."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import selectors
from pathlib import Path
import platform
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import types

TOOLKIT_ROOT = Path(__file__).resolve().parent
DEFAULT_CONTAINER = "voicepeak-arm64"  # Reuse the healthy native guest; its name is historical.
DEFAULT_LIBRARY = Path.home() / ".worldline-r" / "lib" / "libworldline.so"
MAX_OUTPUT = 65536
MAX_ENV = 65536
ENV_ALLOWLIST = frozenset({
    "PHONE_WORKER_TETO_ENABLED", "PHONE_WORKER_TETO_BACKEND",
    "PHONE_WORKER_TETO_VOICEBANK_MODE", "PHONE_WORKER_TETO_VOICEBANK_DIR",
    "PHONE_WORKER_TETO_ENGLISH_VOICEBANK_DIR", "PHONE_WORKER_TETO_MIN_ALIASES",
    "PHONE_WORKER_TETO_ENGLISH_MIN_ALIASES", "PHONE_WORKER_TETO_RESAMPLER_COMMAND",
})
REQUIRED_PACKAGES = ("python3", "libc6", "libstdc++6", "libgcc-s1")
SIGNALLED_PROCESS = re.compile(
    r"(?:qemu:\s*uncaught target signal\s+|proot(?:\s+info)?:[^\r\n]*terminated with signal\s+)(\d{1,3})",
    re.IGNORECASE,
)


def bounded(command: list[str], timeout: float = 20.0) -> dict:
    """Cap time and output for a process group; distrust PRoot's false zero."""
    if timeout <= 0:
        return {"ok": False, "error": "tempo esgotado"}
    process = None
    try:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, start_new_session=True)
        assert process.stdout is not None and process.stderr is not None
        chunks = {"output": bytearray(), "errors": bytearray()}
        failure = None
        deadline = time.monotonic() + timeout
        with selectors.DefaultSelector() as selector:
            for stream, name in ((process.stdout, "output"), (process.stderr, "errors")):
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ, name)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    failure = "tempo esgotado"
                    break
                for key, _ in selector.select(min(remaining, 0.1)):
                    block = os.read(key.fd, 4096)
                    if not block:
                        selector.unregister(key.fileobj)
                        continue
                    chunks[key.data].extend(block)
                    if len(chunks[key.data]) > MAX_OUTPUT:
                        failure = "saída excede limite"
                        break
                if failure:
                    break
        if failure:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
            return {"ok": False, "code": process.returncode, "error": failure}
        remaining = deadline - time.monotonic()
        try:
            process.wait(timeout=max(0, remaining))
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
            return {"ok": False, "code": process.returncode, "error": "tempo esgotado"}
        output, errors = bytes(chunks["output"]), bytes(chunks["errors"])
        crash = SIGNALLED_PROCESS.search((output + errors).decode("utf-8", errors="replace"))
        if crash:
            return {"ok": False, "code": process.returncode, "signal": int(crash.group(1)),
                    "error": "PRoot/QEMU reportou término por sinal"}
        if process.returncode:
            return {"ok": False, "code": process.returncode}
        return {"ok": True, "code": 0, "output": output.decode("utf-8-sig", errors="replace")}
    except OSError:
        return {"ok": False, "error": "comando indisponível"}
    finally:
        if process is not None:
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
            for stream in (process.stdout, process.stderr):
                if stream is not None:
                    stream.close()


def outcome(value: dict) -> dict:
    return {key: value[key] for key in ("ok", "code", "error", "signal") if key in value}


def load_bundled_source(path: Path, name: str):
    """Load our source without creating bytecode files during read-only checks."""
    if not path.is_file():
        raise ValueError("kit incompleto: helper ausente")
    module = types.ModuleType(name)
    module.__file__ = str(path)
    module.__package__ = name.rpartition(".")[0]
    sys.modules[name] = module
    try:
        exec(compile(path.read_bytes(), str(path), "exec"), module.__dict__)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    return module


def load_runtime_module():
    """Import only our bundled source; it does not load native libraries."""
    return load_bundled_source(TOOLKIT_ROOT / "runtime-probe.py", "worldline_runtime_probe")


def read_regular_file(path: Path, limit: int) -> bytes:
    """No symlink, FIFO, or unbounded read; Android does not need hard links."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise ValueError("arquivo não regular ou excede limite")
        raw = stream.read(limit + 1)
        if len(raw) > limit:
            raise ValueError("arquivo excede limite")
        return raw


def safe_path(value: str | Path) -> Path:
    text = os.fspath(value)
    if not text or any(character in text for character in ("\x00", "\n", "\r", ":")):
        raise ValueError("caminho vazio ou incompatível com bind PRoot")
    return Path(text).expanduser().absolute()


def validate_container(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", value):
        raise ValueError("nome de container inválido")
    return value


def guest_command(container: str, arguments: list[str], *, library: Path | None = None) -> list[str]:
    validate_container(container)
    command = ["proot-distro", "login", container]
    if library is not None:
        directory = safe_path(library).parent.resolve()
        command.extend(["--bind", f"{safe_path(TOOLKIT_ROOT).resolve()}:/opt/worldline-r-kit",
                        "--bind", f"{directory}:/opt/worldline-r-lib"])
    return [*command, "--", "/usr/bin/env", "LANG=C", "LC_ALL=C", *arguments]


def read_teto_environment(path: Path | None = None) -> tuple[dict[str, str], dict]:
    """Read an allowlist of literal values. Never source shell or expose other keys."""
    path = path or Path.home() / ".phone-worker.env"
    values: dict[str, str] = {}
    metadata = {"present": False, "read_only": True, "accepted_keys": 0, "rejected_teto_values": 0}
    try:
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_size > MAX_ENV:
            metadata["error"] = "arquivo de ambiente não é regular ou excede limite"
            return values, metadata
        raw = read_regular_file(path, MAX_ENV)
        metadata["present"] = True
        text = raw.decode("utf-8")
    except FileNotFoundError:
        return values, metadata
    except (OSError, UnicodeError, ValueError):
        metadata["error"] = "arquivo de ambiente não pôde ser lido"
        return values, metadata
    for line in text.splitlines():
        match = re.match(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$", line)
        if not match or match.group(1) not in ENV_ALLOWLIST:
            continue
        key, encoded = match.groups()
        try:
            # shlex handles quotes/comments only; it never evaluates shell expressions.
            parts = shlex.split(encoded, comments=True, posix=True)
            if len(parts) > 1:
                raise ValueError("valor não literal")
            value = parts[0] if parts else ""
            if "`" in value or "$" in value or "\x00" in value:
                raise ValueError("expansão não permitida")
            values[key] = value
        except ValueError:
            metadata["rejected_teto_values"] += 1
    metadata["accepted_keys"] = len(values)
    return values, metadata


def package_inventory(value: dict) -> dict[str, bool]:
    result = {name: False for name in REQUIRED_PACKAGES}
    if value.get("ok"):
        lines = value.get("output", "").splitlines()
        for line in lines:
            fields = line.split("\t")
            if len(fields) == 2:
                # Multi-Arch: same packages report an explicit :arm64 suffix.
                name, _, architecture = fields[0].partition(":")
                if name in result and architecture in {"", "arm64"}:
                    result[name] = fields[1] in {"install ok installed", "hold ok installed"}
    return result


def library_inventory(path: Path) -> dict:
    result = {"path": str(path), "present": False, "elf_arm64": False, "sha256": None,
              "pinned_release": False}
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_size < 64:
            result["error"] = "biblioteca ausente, não regular ou fora do limite de tamanho"
            return result
        module = load_runtime_module()
        raw = read_regular_file(path, module.MAX_LIBRARY_BYTES)
        header = raw[:64]
        digest = hashlib.sha256(raw)
        result["present"] = True
        # ELF64, little-endian, ET_DYN, EM_AARCH64=183; never execute an x86 DLL.
        result["elf_arm64"] = (header[:6] == b"\x7fELF\x02\x01" and
                               header[16:20] == b"\x03\x00\xb7\x00")
        result["sha256"] = digest.hexdigest()
        expected = getattr(module, "RELEASE_SHA256", None)
        result["pinned_release"] = isinstance(expected, str) and result["sha256"] == expected
    except (OSError, ValueError, ImportError):
        result["error"] = "biblioteca ou pin oficial indisponível"
    return result


def default_worker_directory() -> Path:
    installed = Path.home() / "phone-worker"
    bundled = TOOLKIT_ROOT.parents[1] / "termux" / "phone-worker"
    return installed if (installed / "teto_renderer" / "voicebank.py").is_file() else bundled


def audit_voicebank(root: Path, worker: Path, minimum: int) -> dict:
    """Reuse the worker's validator without importing its renderer or configuration."""
    source = worker / "teto_renderer"
    if not all((source / name).is_file() for name in ("errors.py", "voicebank.py")):
        return {"ok": False, "error": "VoicebankIndex ausente; informe --worker-dir"}
    package_name = "_worldline_voicebank_audit"
    package = types.ModuleType(package_name)
    package.__path__ = [str(source)]
    sys.modules[package_name] = package
    try:
        for name in ("errors", "voicebank"):
            load_bundled_source(source / f"{name}.py", f"{package_name}.{name}")
        index = sys.modules[f"{package_name}.voicebank"].VoicebankIndex.load(root, minimum_aliases=minimum)
        return {"ok": True, "aliases": index.alias_count, "minimum_aliases": minimum,
                "fingerprint": index.fingerprint, "wav_decode_checked": False,
                "note": "índice confirma aliases e presença dos arquivos; não decodifica os WAVs"}
    except Exception as exc:
        # Exception text can include arbitrary voicebank content; publish a known category only.
        return {"ok": False, "error": "voicebank sem aliases/WAV válidos suficientes",
                "error_type": type(exc).__name__[:80], "minimum_aliases": minimum}
    finally:
        for name in (package_name, f"{package_name}.errors", f"{package_name}.voicebank"):
            sys.modules.pop(name, None)


def decode_probe(value: dict) -> dict:
    """Require JSON evidence, not the wrapper's exit code, and publish only safe fields."""
    if not value.get("ok"):
        return {**outcome(value), "runtime_verified": False}
    try:
        data = json.loads(value.get("output", ""))
        if not isinstance(data, dict):
            raise ValueError("objeto esperado")
    except (ValueError, TypeError):
        return {"ok": False, "runtime_verified": False, "error": "guest não retornou JSON válido"}
    # Match the bundled runtime-probe.py schema. A zero exit or ELF header alone
    # does not establish C API compatibility or a successful native invocation.
    evidence = ("runtime_ok", "api_verified", "abi_verified", "library_hash_verified", "synthetic_render_verified")
    module = load_runtime_module()
    metadata_ok = (data.get("source_commit") == module.SOURCE_COMMIT and
                   data.get("source_version") == module.SOURCE_VERSION and
                   data.get("native_architecture") == "arm64" and
                   data.get("library_sha256") == module.RELEASE_SHA256)
    verified = metadata_ok and all(data.get(key) is True for key in evidence)
    summary = {key: data.get(key) is True for key in (
        *evidence, "teto_synthesis_verified", "portuguese_speech_verified",
    )}
    for key in ("source_version", "source_commit", "native_architecture", "library_sha256"):
        value = data.get(key)
        if isinstance(value, str) and len(value) <= 128 and not any(char in value for char in "\r\n\x00"):
            summary[key] = value
    return {"ok": verified, "runtime_verified": verified, "probe": summary,
            **({} if verified else {"error": "C API nativa não confirmou execução"})}


def _minimum(values: dict[str, str], key: str, default: int) -> int:
    try:
        return max(1, min(100000, int(values.get(key, str(default)))))
    except ValueError:
        return default


def build_report(*, container: str = DEFAULT_CONTAINER, library: Path | None = None,
                 voicebank: Path | None = None, english_voicebank: Path | None = None,
                 worker_dir: Path | None = None, timeout: float = 25.0) -> dict:
    container = validate_container(container)
    library = safe_path(library or DEFAULT_LIBRARY)
    values, env_info = read_teto_environment()
    selected = (("standard", voicebank, "PHONE_WORKER_TETO_VOICEBANK_DIR", "PHONE_WORKER_TETO_MIN_ALIASES", 10),
                ("english", english_voicebank, "PHONE_WORKER_TETO_ENGLISH_VOICEBANK_DIR", "PHONE_WORKER_TETO_ENGLISH_MIN_ALIASES", 500))
    worker = safe_path(worker_dir or default_worker_directory())
    deadline = time.monotonic() + timeout
    def probe(command: list[str]) -> dict:
        remaining = deadline - time.monotonic()
        return bounded(command, remaining) if remaining > 0 else {"ok": False, "error": "tempo total esgotado"}
    memory = {}
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            name, value = line.split(":", 1)
            if name in {"MemTotal", "MemAvailable"}:
                memory[name] = int(value.strip().split()[0]) * 1024
    except (OSError, ValueError):
        pass
    architecture = platform.machine().lower()
    report = {"read_only": True, "native_architecture": architecture, "python_platform": sys.platform,
              "python_version": platform.python_version(), "memory_bytes": memory,
              "container": container, "environment": env_info, "native_arm64": architecture in {"aarch64", "arm64"},
              "box64_required": False, "voicepeak_required": False, "voicebanks": {}, "requirements": {},
              "phrase_adapter_available": False, "runtime_ready": False, "tts_ready": False,
              "note": "WORLDLINE-R é uma biblioteca. O adaptador de frases do worker ainda não foi implementado; este diagnóstico não sintetiza a Teto."}
    try:
        report["free_disk_bytes"] = shutil.disk_usage(Path.home()).free
    except OSError:
        report["free_disk_bytes"] = None
    requirements = report["requirements"]
    requirements["host_arm64"] = report["native_arm64"]
    requirements["host_python"] = sys.version_info >= (3, 10)
    ffmpeg = shutil.which("ffmpeg")
    result = probe([ffmpeg, "-version"]) if ffmpeg else {"ok": False, "error": "ffmpeg ausente"}
    ffmpeg_ok = bool(result.get("ok") and re.match(r"ffmpeg version [^\r\n]+", result.get("output", "")))
    report["ffmpeg"] = {**outcome(result), "version_confirmed": ffmpeg_ok}
    requirements["ffmpeg"] = ffmpeg_ok
    proot = shutil.which("proot-distro")
    requirements["proot_distro"] = bool(proot)
    guest = {"read_only": True, "architecture": None, "packages": {name: False for name in REQUIRED_PACKAGES}}
    report["guest"] = guest
    requirements["guest_arm64"] = False
    requirements["guest_packages"] = False
    if proot:
        result = probe(guest_command(container, ["/usr/bin/dpkg", "--print-architecture"]))
        guest["architecture_probe"] = outcome(result)
        text = result.get("output", "").strip()
        guest["architecture"] = text if result.get("ok") and text in {"arm64", "amd64", "armhf", "i386"} else None
        requirements["guest_arm64"] = guest["architecture"] == "arm64"
        if requirements["guest_arm64"]:
            result = probe(guest_command(container, ["/usr/bin/dpkg-query", "-W", "-f=${binary:Package}\t${Status}\\n", *REQUIRED_PACKAGES]))
            guest["packages_probe"] = outcome(result)
            guest["packages"] = package_inventory(result)
            requirements["guest_packages"] = all(guest["packages"].values())
    inventory = library_inventory(library)
    report["library"] = inventory
    requirements["library_arm64"] = inventory["elf_arm64"]
    requirements["library_pinned"] = inventory["pinned_release"]
    requirements["native_c_api"] = False
    if requirements["guest_arm64"] and requirements["guest_packages"] and inventory["pinned_release"] and inventory["elf_arm64"]:
        result = probe(guest_command(container, ["/usr/bin/python3", "/opt/worldline-r-kit/runtime-probe.py",
                                               "--library", f"/opt/worldline-r-lib/{library.name}", "--render-probe"], library=library))
        runtime = decode_probe(result)
        report["runtime_probe"] = runtime
        requirements["native_c_api"] = runtime["runtime_verified"]
    for name, explicit, env_key, min_key, default_min in selected:
        root_value = explicit or values.get(env_key)
        bank = {"configured": bool(root_value), "ok": False}
        if root_value:
            try:
                root = safe_path(root_value)
                result = probe([sys.executable, "-B", str(TOOLKIT_ROOT / "diagnostic.py"), "--_voicebank-audit", str(root),
                                "--worker-dir", str(worker), "--_minimum", str(_minimum(values, min_key, default_min))])
                data = json.loads(result.get("output", "")) if result.get("ok") else None
                bank = {"configured": True, "path": str(root), **(data if isinstance(data, dict) else outcome(result))}
                if "ok" not in bank:
                    bank["ok"] = False
            except (ValueError, TypeError):
                bank["error"] = "caminho ou relatório da voicebank inválido"
        report["voicebanks"][name] = bank
    requirements["valid_voicebank"] = any(bank.get("ok") for bank in report["voicebanks"].values())
    requirements["phrase_adapter"] = False
    runtime_keys = ("host_arm64", "host_python", "proot_distro", "guest_arm64", "guest_packages",
                    "library_arm64", "library_pinned", "native_c_api")
    report["runtime_ready"] = all(requirements[key] for key in runtime_keys)
    report["blockers"] = [key for key, satisfied in requirements.items() if not satisfied]
    return report


def write_report(path: Path, data: dict) -> None:
    """Explicit report output only; no hard links, including on Android Python 3.14."""
    path = safe_path(path)
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValueError("relatório existente não é arquivo regular")
    encoded = (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verifica WORLDLINE-R no Termux sem instalar pacotes nem alterar o worker.")
    parser.add_argument("--container", default=DEFAULT_CONTAINER, help="guest ARM64 existente (padrão: voicepeak-arm64)")
    parser.add_argument("--library", type=Path, help="libworldline.so ARM64 (padrão: ~/.worldline-r/lib/libworldline.so)")
    parser.add_argument("--voicebank", type=Path, help="voicebank Teto padrão; senão lê apenas a chave Teto de ~/.phone-worker.env")
    parser.add_argument("--english-voicebank", type=Path, help="voicebank Teto English")
    parser.add_argument("--worker-dir", type=Path, help="pasta do phone-worker contendo teto_renderer/voicebank.py")
    parser.add_argument("--report", type=Path, help="salva explicitamente este JSON; nenhum segredo do worker é publicado")
    parser.add_argument("--timeout", type=float, default=25.0, help="limite total dos probes em segundos (1 a 120)")
    parser.add_argument("--_voicebank-audit", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--_minimum", type=int, default=10, help=argparse.SUPPRESS)
    options = parser.parse_args(arguments)
    if not 1 <= options.timeout <= 120:
        parser.error("--timeout deve estar entre 1 e 120")
    try:
        if options._voicebank_audit is not None:
            data = audit_voicebank(safe_path(options._voicebank_audit), safe_path(options.worker_dir or default_worker_directory()),
                                  max(1, min(100000, options._minimum)))
            print(json.dumps(data, ensure_ascii=False))
            return 0  # The parent's JSON validation distinguishes an invalid bank from a child failure.
        data = build_report(container=options.container, library=options.library, voicebank=options.voicebank,
                            english_voicebank=options.english_voicebank, worker_dir=options.worker_dir, timeout=options.timeout)
        if options.report:
            write_report(options.report, data)
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0  # Inventory is useful even with blockers; inspect tts_ready/runtime_ready explicitly.
    except (OSError, ValueError) as exc:
        print(json.dumps({"read_only": True, "tts_ready": False, "error": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
