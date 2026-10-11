#!/usr/bin/env python3
"""Install the inspected third-party Miku English CVVC ZIP in its own directory."""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import shutil
import stat
import struct
import sys
import tempfile
import time
import zipfile
import zlib

SOURCE_URL = "https://drive.google.com/file/d/1VoNNANLMEW-TEcqLPWa1hxgATWZPMfwP/view"
AUTHOR_URL = "https://demosvocas.blogspot.com/2025/12/mikuv4xbeta-utau.html"
SOURCE_SHA256 = "e1fc9a37a7e45e0ad4209b2b0f37d90541b2528c85d1b58a81c0b399ecaf79be"
SOURCE_BYTES = 71819802
EXPECTED_UNPACKED_BYTES = 78493362
EXPECTED_FILES = 3384
INVENTORY_SHA256 = "e42332dc346d81e8fa69786841e38bd50f4385b1ab1e1ea33b33250d4c4b209e"
ALIAS_SUFFIX = "_D4"
MINIMUM_ALIASES = 500
MAX_ENTRIES = 10000
MAX_FILE_BYTES = 32 * 1024 * 1024
MAX_UNPACKED_BYTES = 256 * 1024 * 1024
MAX_ENV_BYTES = 256 * 1024
MARKER = ".miku-english-cvvc-install.json"
WORKER_DIR = Path(__file__).resolve().parents[2] / "termux" / "phone-worker"
KEY = re.compile(r"^[ \t]*(?:export[ \t]+)?([A-Za-z_][A-Za-z0-9_]*)[ \t]*=")
ALLOWED_ENV_KEYS = frozenset({
    "PHONE_WORKER_TETO_RESAMPLER_COMMAND", "PHONE_WORKER_MIKU_ENABLED",
    "PHONE_WORKER_MIKU_VOICEBANK_DIR", "PHONE_WORKER_MIKU_ENGLISH_VOICEBANK_DIR",
    "PHONE_WORKER_MIKU_ALIAS_SUFFIX", "PHONE_WORKER_MIKU_RESAMPLER_COMMAND",
    "PHONE_WORKER_MIKU_RENDER_THREADS", "PHONE_WORKER_MIKU_FRAGMENT_CACHE_DIR",
    "PHONE_WORKER_ESPER_ROOT", "PHONE_WORKER_ESPER_CONTAINER",
    "PHONE_WORKER_ESPER_TIMEOUT", "PHONE_WORKER_ESPER_CACHE_DIR",
})


class InstallError(ValueError):
    pass


def split_comment(value: str) -> tuple[str, str]:
    quote, escaped = None, False
    for position, character in enumerate(value):
        if escaped:
            escaped = False
        elif character == "\\" and quote != "'":
            escaped = True
        elif quote:
            if character == quote:
                quote = None
        elif character in "\"'":
            quote = character
        elif character == "#" and (position == 0 or value[position - 1].isspace()):
            return value[:position].rstrip(), value[position:].rstrip("\r\n")
    return value.rstrip(), ""


def real_directories(path: Path) -> None:
    for directory in (path, *path.parents):
        try:
            info = directory.lstat()
        except FileNotFoundError:
            continue
        if not stat.S_ISDIR(info.st_mode):
            raise InstallError("destino e pastas pai devem ser diretórios reais, sem links simbólicos")


def regular_bytes(path: Path, maximum: int, *, allow_empty: bool = False) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or not (0 if allow_empty else 1) <= before.st_size <= maximum:
            raise InstallError("arquivo deve ser regular e respeitar o limite de tamanho")
        value = stream.read(maximum + 1)
        after = os.fstat(stream.fileno())
    identity = lambda info: (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
    if len(value) > maximum or identity(before) != identity(after):
        raise InstallError("arquivo mudou durante a leitura")
    return value


def read_environment(path: Path) -> tuple[bytes, dict[str, str]]:
    real_directories(path.parent)
    original = regular_bytes(path, MAX_ENV_BYTES, allow_empty=True)
    if b"\x00" in original:
        raise InstallError(".env contém caracteres inválidos")
    values = {}
    for line in original.decode("utf-8").splitlines():
        match = KEY.match(line)
        if not match or match.group(1) not in ALLOWED_ENV_KEYS:
            continue
        value, _ = split_comment(line[match.end():].strip())
        if value.startswith('"'):
            try:
                decoded, end = json.JSONDecoder().raw_decode(value.replace("\\$", "$").replace("\\`", "`"))
                tail = value.replace("\\$", "$").replace("\\`", "`")[end:].strip()
                if tail and not tail.startswith("#"):
                    raise ValueError("texto após valor")
                value = decoded
            except (TypeError, ValueError) as exc:
                raise InstallError("valor .env entre aspas inválido") from exc
        elif value.startswith("'"):
            quoted = re.fullmatch(r"'([^']*)'(?:\s*#.*)?", value)
            if not quoted:
                raise InstallError("valor .env entre aspas inválido")
            value = quoted.group(1)
        else:
            value = re.split(r"\s+#", value, maxsplit=1)[0].rstrip()
        if not isinstance(value, str) or any(char in value for char in "\x00\r\n"):
            raise InstallError("valor de configuração inválido")
        values[match.group(1)] = value
    return original, values


def shell_value(value: str) -> str:
    if any(char in value for char in "\x00\r\n"):
        raise InstallError("valor de configuração contém caracteres inválidos")
    return json.dumps(value, ensure_ascii=False).replace("$", "\\$").replace("`", "\\`")


def bridge_options(data: bytes) -> frozenset[str]:
    """Inspect the installed parser without importing or executing the bridge."""
    try:
        tree = ast.parse(data)
    except (SyntaxError, ValueError, UnicodeError) as exc:
        raise InstallError("ponte ESPER instalada não é um script Python válido") from exc
    options = frozenset(argument.value for node in ast.walk(tree)
                        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                        and node.func.attr == "add_argument"
                        for argument in node.args
                        if isinstance(argument, ast.Constant) and isinstance(argument.value, str)
                        and argument.value.startswith("--"))
    if not {"--root", "--cache-dir", "--container", "--timeout"}.issubset(options):
        raise InstallError("ponte ESPER instalada não declara as opções necessárias")
    return options


def configuration(directory: Path, values: dict[str, str], *, container: str,
                  esper_root: Path | None = None) -> dict[str, str]:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", container):
        raise InstallError("nome do container inválido")
    # An explicit runtime root configures the new voice independently. Legacy
    # or stale commands for either voice are never executed or rewritten for
    # Teto; only the five Miku keys are published after bank verification.
    raw = None if esper_root is not None else (values.get("PHONE_WORKER_MIKU_RESAMPLER_COMMAND")
                                              or values.get("PHONE_WORKER_TETO_RESAMPLER_COMMAND"))
    if raw:
        try:
            command = shlex.split(raw)
        except ValueError as exc:
            raise InstallError("comando ESPER configurado inválido") from exc
        bridge_names = {"resampler.py", "esper-utau-resampler"}
        if len(command) >= 2 and Path(command[0]).name.lower().startswith("python") and Path(command[1]).name in bridge_names:
            pass
        elif command and Path(command[0]).name in bridge_names:
            # ESPER setup publishes the same Python bridge under both names.
            # Run direct launchers through this Python, avoiding dependence on
            # their executable bit or Android's handling of the env shebang.
            direct = str(Path(command[0]).expanduser())
            if Path(direct).name == direct:
                direct = shutil.which(direct) or direct
            command = [sys.executable, direct, *command[1:]]
        else:
            raise InstallError("comando ESPER não reconhecido; use a ponte resampler.py ou esper-utau-resampler")
        wrapper = Path(command[1]).expanduser().absolute()
        wrapper_bytes = regular_bytes(wrapper, MAX_ENV_BYTES)
        if shutil.which(command[0]) is None and not Path(command[0]).is_file():
            raise InstallError("Python da ponte ESPER não encontrado")
        command[1] = str(wrapper)
    else:
        root = Path(esper_root if esper_root is not None else
                    values.get("PHONE_WORKER_ESPER_ROOT") or Path.home() / ".esper-utau").expanduser().absolute()
        real_directories(root / "bin")
        wrapper = root / "bin" / "resampler.py"
        if not (wrapper.exists() or wrapper.is_symlink()):
            wrapper = root / "bin" / "esper-utau-resampler"
        wrapper_bytes = regular_bytes(wrapper, MAX_ENV_BYTES)
        command = [sys.executable, str(wrapper), "--root", str(root), "--timeout", "60"]
    # Old ESPER bridges reject --implementation-id. Keep the bridge digest in
    # the accepted cache-directory argument instead so both phrase and fragment
    # fingerprints still change after a bridge update. Teto's argv stays intact.
    digest = hashlib.sha256(wrapper_bytes).hexdigest()
    supports_identity = "--implementation-id" in bridge_options(wrapper_bytes)
    replacements = {"--container": container,
                    "--implementation-id": digest if supports_identity else None}
    if not supports_identity:
        cache = Path(values.get("PHONE_WORKER_ESPER_CACHE_DIR") or wrapper.parent.parent / "cache").expanduser().absolute()
        replacements["--cache-dir"] = str(cache / "miku-bridge" / digest)
    clean, position = command[:2], 2
    while position < len(command):
        argument = command[position]
        if argument in replacements:
            if position + 1 >= len(command) or command[position + 1].startswith("--"):
                raise InstallError("opção ESPER sem valor no comando existente")
            position += 2
        elif argument.partition("=")[0] in replacements:
            if not argument.partition("=")[2]:
                raise InstallError("opção ESPER sem valor no comando existente")
            position += 1
        else:
            clean.append(argument)
            position += 1
    command = clean
    for option, value in replacements.items():
        if value is not None:
            command.extend((option, value))
    return {
        "PHONE_WORKER_MIKU_ENABLED": "true",
        "PHONE_WORKER_MIKU_VOICEBANK_DIR": str(directory),
        "PHONE_WORKER_MIKU_ENGLISH_VOICEBANK_DIR": str(directory),
        "PHONE_WORKER_MIKU_ALIAS_SUFFIX": ALIAS_SUFFIX,
        "PHONE_WORKER_MIKU_RESAMPLER_COMMAND": shlex.join(command),
    }


def updated_environment(original: bytes, values: dict[str, str]) -> bytes:
    lines, seen = [], set()
    for line in original.decode("utf-8").splitlines(keepends=True):
        match = KEY.match(line)
        key = match.group(1) if match else None
        if key in values:
            if key not in seen:
                # Preserve comments on their own line so both the shell and
                # Python worker read the replacement as one literal value.
                _, comment = split_comment(line[match.end():])
                if comment:
                    lines.append(comment + "\n")
                lines.append(f"{key}={shell_value(values[key])}\n")
                seen.add(key)
            else:
                _, comment = split_comment(line[match.end():])
                if comment:
                    lines.append(comment + "\n")
        else:
            lines.append(line)
    if lines and not lines[-1].endswith(("\n", "\r")):
        lines[-1] += "\n"
    lines.extend(f"{key}={shell_value(value)}\n" for key, value in values.items() if key not in seen)
    updated = "".join(lines).encode("utf-8")
    if len(updated) > MAX_ENV_BYTES:
        raise InstallError(".env atualizado excederia o limite de 256 KiB")
    return updated


def atomic_write(path: Path, data: bytes) -> None:
    real_directories(path.parent)
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise InstallError("destino de gravação deve ser arquivo regular")
    descriptor, temporary = tempfile.mkstemp(prefix="." + path.name + "-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as target:
            target.write(data)
            target.flush()
            os.fsync(target.fileno())
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise InstallError("destino mudou antes da gravação")
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def publish_environment(path: Path, original: bytes, updated: bytes) -> dict:
    if regular_bytes(path, MAX_ENV_BYTES, allow_empty=True) != original:
        raise InstallError(".env mudou durante a instalação; configuração local preservada")
    if original == updated:
        return {"changed": False}
    backup = path.with_name(path.name + ".bak-miku-" + str(time.time_ns()))
    descriptor = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(descriptor, "wb") as target:
        target.write(original)
        target.flush()
        os.fsync(target.fileno())
    if regular_bytes(path, MAX_ENV_BYTES, allow_empty=True) != original:
        raise InstallError(".env mudou antes da gravação; configuração local preservada")
    atomic_write(path, updated)
    return {"changed": True, "backup": str(backup)}


def decoded_name(info: zipfile.ZipInfo) -> str:
    if "\x00" in info.orig_filename or "\x00" in info.filename:
        raise InstallError("nome de arquivo contém byte nulo")
    if info.flag_bits & 0x800:
        return info.filename
    try:
        original = info.orig_filename.encode("cp437")
    except UnicodeEncodeError:
        return info.filename
    extra = info.extra
    while len(extra) >= 4:
        kind, size = struct.unpack("<HH", extra[:4])
        value, extra = extra[4:4 + size], extra[4 + size:]
        if len(value) != size:
            raise InstallError("metadados ZIP truncados")
        if kind == 0x7075 and size >= 5 and value[0] == 1 and struct.unpack("<I", value[1:5])[0] == zlib.crc32(original):
            return value[5:].decode("utf-8")
    try:
        return original.decode("cp932")
    except UnicodeDecodeError:
        return info.filename


def checked_members(archive: zipfile.ZipFile) -> tuple[list, int]:
    entries = archive.infolist()
    if not entries or len(entries) > MAX_ENTRIES:
        raise InstallError("ZIP vazio ou com entradas acima do limite")
    members, seen, total = [], {}, 0
    for info in entries:
        name = decoded_name(info).replace("\\", "/")
        parts = name.rstrip("/").split("/")
        if (not name or name.startswith("/") or any(part in ("", ".", "..") for part in parts)
                or any(":" in part or any(ord(char) < 32 for char in part) for part in parts)
                or len(parts) > 20 or len(name.encode("utf-8")) > 1024):
            raise InstallError("caminho inseguro no ZIP")
        relative = PurePosixPath(*parts)
        mode = (info.external_attr >> 16) & 0xFFFF
        kind = stat.S_IFMT(mode)
        directory = info.is_dir() or kind == stat.S_IFDIR
        if kind not in (0, stat.S_IFREG, stat.S_IFDIR) or info.flag_bits & 1:
            raise InstallError("ZIP contém link, arquivo especial ou conteúdo criptografado")
        if info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
            raise InstallError("compressão ZIP não suportada")
        if relative in seen or (directory and info.file_size) or not 0 <= info.file_size <= MAX_FILE_BYTES:
            raise InstallError("entrada ZIP duplicada, inválida ou acima do limite")
        seen[relative] = directory
        total += info.file_size
        if total > MAX_UNPACKED_BYTES:
            raise InstallError("ZIP expandido acima do limite")
        members.append((info, relative, directory))
    for relative in seen:
        if any(parent in seen and not seen[parent] for parent in relative.parents):
            raise InstallError("arquivo e diretório disputam o mesmo caminho")
    return members, total


def snapshot_archive(source: Path, target: Path) -> dict:
    descriptor = os.open(source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    digest, count = hashlib.sha256(), 0
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size != SOURCE_BYTES:
            raise InstallError("ZIP não é arquivo regular com o tamanho da bank inspecionada")
        with target.open("xb") as output:
            while block := stream.read(1024 * 1024):
                count += len(block)
                if count > SOURCE_BYTES:
                    raise InstallError("ZIP mudou durante a leitura")
                digest.update(block)
                output.write(block)
    if count != SOURCE_BYTES or digest.hexdigest() != SOURCE_SHA256:
        raise InstallError("SHA-256 do ZIP diverge da English CVVC inspecionada; nada foi instalado")
    return {"bytes": count, "sha256": digest.hexdigest()}


def extract_archive(source: Path, destination: Path) -> dict:
    count = 0
    with zipfile.ZipFile(source) as archive:
        members, total = checked_members(archive)
        if total != EXPECTED_UNPACKED_BYTES or sum(not item[2] for item in members) != EXPECTED_FILES:
            raise InstallError("estrutura ZIP diverge da bank inspecionada")
        for info, relative, directory in members:
            output = destination.joinpath(*relative.parts)
            if directory:
                output.mkdir(parents=True, exist_ok=True, mode=0o700)
                continue
            output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
            written = 0
            with archive.open(info) as stream, os.fdopen(descriptor, "wb") as target:
                while block := stream.read(65536):
                    written += len(block)
                    count += len(block)
                    if written > info.file_size or count > total:
                        raise InstallError("extração excedeu o tamanho declarado")
                    target.write(block)
            if written != info.file_size:
                raise InstallError("arquivo ZIP truncado")
    if count != total:
        raise InstallError("volume extraído diverge do ZIP")
    return {"files": EXPECTED_FILES, "unpacked_bytes": count, "crc_verified": True}


def inventory(directory: Path) -> tuple[list, str]:
    entries = []
    for path in sorted(directory.rglob("*")):
        if path == directory / MARKER:
            continue
        info = path.lstat()
        if stat.S_ISDIR(info.st_mode):
            continue
        if not stat.S_ISREG(info.st_mode):
            raise InstallError("bank instalada contém link ou arquivo especial")
        data = regular_bytes(path, MAX_FILE_BYTES, allow_empty=True)
        entries.append([path.relative_to(directory).as_posix(), len(data), hashlib.sha256(data).hexdigest()])
    encoded = json.dumps(entries, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return entries, hashlib.sha256(encoded).hexdigest()


def inspect_bank(directory: Path) -> dict:
    if str(WORKER_DIR) not in sys.path:
        sys.path.insert(0, str(WORKER_DIR))
    from teto_renderer.voicebank import VoicebankIndex
    index = VoicebankIndex.load(directory, minimum_aliases=MINIMUM_ALIASES)
    prefixes = regular_bytes(directory / "prefix.map", 16384).decode("utf-8-sig").splitlines()
    if not prefixes or any(line.split("\t")[-1] != ALIAS_SUFFIX for line in prefixes if line.strip()):
        raise InstallError("prefix.map diverge do sufixo _D4 esperado")
    if not all(alias.endswith(ALIAS_SUFFIX) for alias in index.entries):
        raise InstallError("aliases da bank divergem do perfil English CVVC inspecionado")
    return {"aliases": index.alias_count, "name": index.name, "alias_suffix": ALIAS_SUFFIX,
            "wav_decode_checked": False}


def check_existing(directory: Path) -> dict | None:
    real_directories(directory)
    if not directory.exists() or not any(directory.iterdir()):
        return None
    try:
        marker = json.loads(regular_bytes(directory / MARKER, 4096))
    except (OSError, ValueError) as exc:
        raise InstallError("destino já contém arquivos sem instalação Miku confirmada; bank existente preservada") from exc
    _, digest = inventory(directory)
    if marker.get("source_sha256") != SOURCE_SHA256 or marker.get("inventory_sha256") != INVENTORY_SHA256 or digest != INVENTORY_SHA256:
        raise InstallError("bank instalada foi alterada ou não corresponde ao ZIP fixado; arquivos preservados")
    return inspect_bank(directory)


def install_voicebank(*, archive: Path, directory: Path, env: Path, container: str = "voicepeak-arm64",
                      esper_root: Path | None = None) -> dict:
    report = {"ok": False, "installed": False, "voicebank_ready": False, "voice_id": "hatsune-miku",
              "voicebank_profile": "english-cvvc", "source_url": SOURCE_URL, "author_url": AUTHOR_URL,
              "source_sha256": SOURCE_SHA256, "worker_configuration_changed": False,
              "production_voice_changed": False, "miku_synthesis_verified": False,
              "portuguese_quality_verified": False, "checks": {},
              "note": "Port de terceiros; licença oficial da Crypton não confirmada. Instala em pasta própria e mantém a Teto selecionada."}
    lock, published = None, None
    try:
        directory = Path(os.path.abspath(directory.expanduser()))
        env = Path(os.path.abspath(env.expanduser()))
        archive = Path(os.path.abspath(archive.expanduser()))
        report["directory"] = str(directory)
        original, settings = read_environment(env)
        configured = configuration(directory, settings, container=container, esper_root=esper_root)
        updated = updated_environment(original, configured)
        real_directories(directory)
        existing = check_existing(directory)
        directory.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        real_directories(directory.parent)
        lock_path = directory.parent / ("." + directory.name + ".miku-install.lock")
        try:
            lock = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        except FileExistsError as exc:
            raise InstallError("há uma instalação Miku em andamento ou um lock antigo; arquivos preservados") from exc
        existing = check_existing(directory)
        if existing is None:
            if shutil.disk_usage(directory.parent).free < SOURCE_BYTES + EXPECTED_UNPACKED_BYTES + 32 * 1024 * 1024:
                raise InstallError("espaço livre insuficiente para preparar a bank")
            with tempfile.TemporaryDirectory(prefix=".miku-stage-", dir=directory.parent) as temporary:
                stage = Path(temporary)
                report["checks"]["archive_pin"] = {"ok": True, **snapshot_archive(archive, stage / "source.zip")}
                bank = stage / "bank"
                bank.mkdir(mode=0o700)
                report["checks"]["extraction"] = {"ok": True, **extract_archive(stage / "source.zip", bank)}
                existing = inspect_bank(bank)
                _, digest = inventory(bank)
                if digest != INVENTORY_SHA256:
                    raise InstallError("inventário extraído diverge do ZIP fixado")
                atomic_write(bank / MARKER, (json.dumps({"source_sha256": SOURCE_SHA256,
                    "inventory_sha256": digest, "voice_id": "hatsune-miku"}, indent=2) + "\n").encode("utf-8"))
                if directory.exists():
                    # Never remove files that another process created in the empty destination.
                    directory.rmdir()
                bank.rename(directory)
                published = directory.stat().st_ino
                report["installed"] = True
                report["checks"]["publication"] = {"ok": True, "atomic": True}
        else:
            report["checks"]["existing_installation"] = {"ok": True, "inventory_verified": True}
        change = publish_environment(env, original, updated)
        report["worker_configuration_changed"] = change["changed"]
        if "backup" in change:
            report["configuration_backup"] = change["backup"]
        report.update(ok=True, voicebank_ready=True, voicebank=existing, restart_required=change["changed"])
    except (OSError, ValueError, UnicodeError, zipfile.BadZipFile, NotImplementedError) as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
        if published is not None:
            try:
                if stat.S_ISDIR(directory.lstat().st_mode) and directory.stat().st_ino == published:
                    shutil.rmtree(directory)
                    report["installed"] = False
                    report["checks"]["rollback"] = {"ok": True}
            except OSError:
                report["checks"]["rollback"] = {"ok": False, "note": "bank preservada; configuração não publicada"}
    finally:
        if lock is not None:
            os.close(lock)
            lock_path.unlink(missing_ok=True)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, default=Path.home() / "storage/downloads/mukki english cvvc.zip")
    parser.add_argument("--directory", type=Path, default=Path.home() / "voicebanks/hatsune-miku-english")
    parser.add_argument("--env", type=Path, default=Path(os.getenv("PHONE_WORKER_ENV") or Path.home() / ".phone-worker.env"))
    parser.add_argument("--container", default="voicepeak-arm64")
    parser.add_argument("--esper-root", type=Path,
                        help="pasta do ESPER instalado; cria o comando da Miku sem reutilizar o comando salvo da Teto")
    options = parser.parse_args(argv)
    report = install_voicebank(archive=options.archive, directory=options.directory, env=options.env,
                              container=options.container, esper_root=options.esper_root)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
