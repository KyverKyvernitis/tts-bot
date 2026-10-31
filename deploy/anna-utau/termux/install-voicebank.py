#!/usr/bin/env python3
"""Install the pinned official Anna Nyui English VCCV DIVA bank for ESPER."""
from __future__ import annotations

import argparse
import ast
import hashlib
from html.parser import HTMLParser
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
import urllib.error
import urllib.parse
import urllib.request
import wave
import zipfile
import zlib

SOURCE_ID = "18DXqyBYLqBnm6VnE5xV0CoyqGThkqfe2"
SOURCE_URL = "https://drive.google.com/file/d/" + SOURCE_ID + "/view"
AUTHOR_URL = "https://aoka45.wixsite.com/annanyui/english?lang=en"
TERMS_URL = "https://aoka45.wixsite.com/annanyui/terms?lang=en"
SOURCE_SHA256 = "e2372c6673c2f32fbdb911162ff41058c5bc476f8186f1d5089c7538cf65ce1f"
SOURCE_BYTES = 447448490
EXPECTED_UNPACKED_BYTES = 600150538
EXPECTED_FILES = 16240
INVENTORY_SHA256 = "ee9958e687dfa31c40e36bd26dad2618b5e1a82a18a4beb25893ec547be20a5c"
PREFIX_SHA256 = "c890a553ca5db1246d136dfb9cf2287d56325767f306b572fbf733d60702877a"
MINIMUM_ALIASES = 5000
MAX_ENTRIES = 20000
MAX_FILE_BYTES = 32 * 1024 * 1024
MAX_UNPACKED_BYTES = 768 * 1024 * 1024
MAX_ENV_BYTES = 256 * 1024
MARKER = ".anna-nyui-diva-install.json"
WORKER_DIR = Path(__file__).resolve().parents[2] / "termux" / "phone-worker"
KEY = re.compile(r"^[ \t]*(?:export[ \t]+)?([A-Za-z_][A-Za-z0-9_]*)[ \t]*=")
ALLOWED_ENV_KEYS = frozenset({
    "PHONE_WORKER_TETO_RESAMPLER_COMMAND", "PHONE_WORKER_ANNA_ENABLED",
    "PHONE_WORKER_ANNA_VOICEBANK_DIR", "PHONE_WORKER_ANNA_ENGLISH_VOICEBANK_DIR",
    "PHONE_WORKER_MIKU_RESAMPLER_COMMAND", "PHONE_WORKER_ANNA_RESAMPLER_COMMAND",
    "PHONE_WORKER_ANNA_RENDER_THREADS", "PHONE_WORKER_ANNA_FRAGMENT_CACHE_DIR",
    "PHONE_WORKER_ESPER_ROOT", "PHONE_WORKER_ESPER_CONTAINER",
    "PHONE_WORKER_ESPER_TIMEOUT", "PHONE_WORKER_ESPER_CACHE_DIR",
})


class InstallError(ValueError):
    pass


def official_download_url(url: str) -> bool:
    parsed = urllib.parse.urlsplit(url)
    return (parsed.scheme == "https" and parsed.hostname in {
        "drive.google.com", "drive.usercontent.google.com"}
        and parsed.port in (None, 443) and not parsed.username and not parsed.password)


class OfficialRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, url):
        if not official_download_url(url):
            raise InstallError("redirecionamento fora do download oficial HTTPS")
        return super().redirect_request(request, response, code, message, headers, url)


class DownloadForm(HTMLParser):
    def __init__(self):
        super().__init__()
        self.action = None
        self.parameters = {}
        self.active = False

    def handle_starttag(self, tag, attributes):
        values = dict(attributes)
        if tag == "form" and values.get("id") == "download-form":
            self.action = values.get("action")
            self.active = True
        elif tag == "input" and self.active and values.get("name"):
            self.parameters[values["name"]] = values.get("value", "")

    def handle_endtag(self, tag):
        if tag == "form":
            self.active = False

    def url(self) -> str:
        if (not self.action or not official_download_url(self.action)
                or urllib.parse.urlsplit(self.action).hostname != "drive.usercontent.google.com"
                or self.parameters.get("id") != SOURCE_ID
                or self.parameters.get("export") != "download"
                or set(self.parameters) - {"id", "export", "confirm", "uuid"}):
            raise InstallError("formulário de download oficial ausente ou inválido")
        return self.action + "?" + urllib.parse.urlencode(self.parameters)


def download_archive(output: Path, *, timeout: float = 900) -> dict:
    """Fetch only the author's pinned public archive, never a mirrored bank."""
    if not 1 <= timeout <= 3600:
        raise InstallError("timeout do download deve ficar entre 1 e 3600 segundos")
    deadline = time.monotonic() + timeout
    opener = urllib.request.build_opener(OfficialRedirects())
    url = "https://drive.google.com/uc?export=download&id=" + SOURCE_ID
    response = None
    try:
        # Google first returns its public large-file confirmation form. Do not
        # execute page scripts or accept an arbitrary form destination.
        for _ in range(2):
            if not official_download_url(url) or time.monotonic() >= deadline:
                raise InstallError("download oficial excedeu o prazo")
            response = opener.open(urllib.request.Request(url, headers={
                "User-Agent": "Anna-Nyui-ESPER-installer/1", "Accept-Encoding": "identity"}),
                timeout=min(60, max(1, deadline - time.monotonic())))
            if not official_download_url(response.geturl()):
                raise InstallError("download saiu do site oficial")
            if "text/html" not in response.headers.get("Content-Type", "").lower():
                break
            page = response.read(65537)
            response.close()
            response = None
            if len(page) > 65536:
                raise InstallError("resposta de confirmação do download grande demais")
            form = DownloadForm()
            form.feed(page.decode("utf-8"))
            url = form.url()
        if response is None or "text/html" in response.headers.get("Content-Type", "").lower():
            raise InstallError("o Google Drive não entregou o ZIP oficial")
        length = response.headers.get("Content-Length")
        if length is not None and (not length.isdigit() or int(length) != SOURCE_BYTES):
            raise InstallError("o tamanho do download oficial mudou")
        descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        count, digest = 0, hashlib.sha256()
        try:
            with os.fdopen(descriptor, "wb") as stream:
                while True:
                    if time.monotonic() >= deadline:
                        raise InstallError("download oficial excedeu o prazo")
                    block = response.read(65536)
                    if not block:
                        break
                    count += len(block)
                    if count > SOURCE_BYTES:
                        raise InstallError("download excedeu o tamanho oficial")
                    stream.write(block)
                    digest.update(block)
                stream.flush()
                os.fsync(stream.fileno())
            if count != SOURCE_BYTES or digest.hexdigest() != SOURCE_SHA256:
                raise InstallError("tamanho ou SHA-256 do download diverge do banco oficial fixado")
        except BaseException:
            output.unlink(missing_ok=True)
            raise
        return {"bytes": count, "sha256": digest.hexdigest()}
    finally:
        if response is not None:
            response.close()


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


def configuration(directory: Path, values: dict[str, str], *, container: str | None = None,
                  esper_root: Path | None = None) -> dict[str, str]:
    # An explicit runtime root configures the new voice independently. Legacy
    # or stale commands for either voice are never executed or rewritten for
    # Teto; only Anna Nyui keys are published after bank verification.
    configured_raw = (values.get("PHONE_WORKER_ANNA_RESAMPLER_COMMAND")
                      or values.get("PHONE_WORKER_MIKU_RESAMPLER_COMMAND")
                      or values.get("PHONE_WORKER_TETO_RESAMPLER_COMMAND"))
    raw = None if esper_root is not None else configured_raw
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
    inherited_command = command
    if esper_root is not None and configured_raw:
        # Replacing a stale runtime root does not imply replacing a valid
        # custom container name. Inspect a recognizable old bridge's argv
        # without executing it or requiring that its old path still exists.
        try:
            previous = shlex.split(configured_raw)
        except ValueError:
            previous = []
        names = {"resampler.py", "esper-utau-resampler"}
        if ((len(previous) >= 2 and Path(previous[0]).name.lower().startswith("python")
             and Path(previous[1]).name in names)
                or (previous and Path(previous[0]).name in names)):
            inherited_command = previous
    inherited_container = None
    for position, argument in enumerate(inherited_command):
        if argument == "--container":
            if (position + 1 >= len(inherited_command)
                    or inherited_command[position + 1].startswith("--")):
                raise InstallError("opção ESPER --container sem valor no comando existente")
            inherited_container = inherited_command[position + 1]
        elif argument.startswith("--container="):
            inherited_container = argument.partition("=")[2]
            if not inherited_container:
                raise InstallError("opção ESPER --container sem valor no comando existente")
    container = (container if container is not None else inherited_container
                 or values.get("PHONE_WORKER_ESPER_CONTAINER") or "voicepeak-arm64")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", container):
        raise InstallError("nome do container inválido")
    # Old ESPER bridges reject --implementation-id. Keep the bridge digest in
    # the accepted cache-directory argument instead so both phrase and fragment
    # fingerprints still change after a bridge update. Teto's argv stays intact.
    digest = hashlib.sha256(wrapper_bytes).hexdigest()
    supports_identity = "--implementation-id" in bridge_options(wrapper_bytes)
    replacements = {"--container": container,
                    "--implementation-id": digest if supports_identity else None}
    if not supports_identity:
        cache = Path(values.get("PHONE_WORKER_ESPER_CACHE_DIR") or wrapper.parent.parent / "cache").expanduser().absolute()
        replacements["--cache-dir"] = str(cache / "anna-bridge" / digest)
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
    threads = values.get("PHONE_WORKER_ANNA_RENDER_THREADS") or "2"
    if not threads.isdigit() or not 1 <= int(threads) <= 4:
        raise InstallError("Anna Nyui exige entre 1 e 4 threads de renderização")
    return {
        "PHONE_WORKER_ANNA_ENABLED": "true",
        "PHONE_WORKER_ANNA_VOICEBANK_DIR": str(directory),
        "PHONE_WORKER_ANNA_ENGLISH_VOICEBANK_DIR": str(directory),
        "PHONE_WORKER_ANNA_FRAGMENT_CACHE_DIR": (values.get("PHONE_WORKER_ANNA_FRAGMENT_CACHE_DIR")
                                                or str(Path.home() / "phone-worker/cache/anna-fragments")),
        "PHONE_WORKER_ANNA_RENDER_THREADS": threads,
        "PHONE_WORKER_ANNA_RESAMPLER_COMMAND": shlex.join(command),
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
    backup = path.with_name(path.name + ".bak-anna-" + str(time.time_ns()))
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
        raise InstallError("SHA-256 do ZIP diverge da DIVA oficial inspecionada; nada foi instalado")
    return {"bytes": count, "sha256": digest.hexdigest()}


def extract_archive(source: Path, destination: Path) -> dict:
    count = 0
    with zipfile.ZipFile(source) as archive:
        members, total = checked_members(archive)
        if total != EXPECTED_UNPACKED_BYTES or sum(not item[2] for item in members) != EXPECTED_FILES:
            raise InstallError("estrutura ZIP diverge da bank inspecionada")
        roots = {relative.parts[0] for _, relative, _ in members}
        if len(roots) != 1 or any(len(relative.parts) < 2 and not directory
                                 for _, relative, directory in members):
            raise InstallError("ZIP deve conter uma única pasta de banco de voz")
        for info, relative, directory in members:
            if len(relative.parts) == 1:
                continue
            output = destination.joinpath(*relative.parts[1:])
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
    index = VoicebankIndex.load(directory, minimum_aliases=MINIMUM_ALIASES,
                                pitch_map=True, case_sensitive=True)
    prefix = regular_bytes(directory / "prefix.map", 16384)
    if hashlib.sha256(prefix).hexdigest() != PREFIX_SHA256:
        raise InstallError("prefix.map diverge do mapa de alturas do banco oficial")
    for suffix in ("", "B3", "A4"):
        if not all(vowel + suffix in index.entries for vowel in ("a", "e", "i", "o", "u")):
            raise InstallError("faltam as vogais das três alturas oficiais de gravação")
    wavs = sorted({entry.wav_path for entry in index.entries.values()})
    for path in wavs:
        with wave.open(str(path), "rb") as audio:
            if (audio.getnchannels() != 1 or audio.getsampwidth() != 2
                    or audio.getframerate() not in (44100, 48000) or audio.getnframes() <= 0
                    or audio.getcomptype() != "NONE"):
                raise InstallError("banco contém WAV incompatível com o ESPER")
            if len(audio.readframes(audio.getnframes())) != audio.getnframes() * 2:
                raise InstallError("WAV do banco está truncado")
    return {"aliases": index.alias_count, "name": index.name, "recorded_pitches": ["B3", "E4", "A4"],
            "wav_decode_checked": True, "wav_files": len(wavs)}


def check_existing(directory: Path) -> dict | None:
    real_directories(directory)
    if not directory.exists() or not any(directory.iterdir()):
        return None
    try:
        marker = json.loads(regular_bytes(directory / MARKER, 4096))
    except (OSError, ValueError) as exc:
        raise InstallError("destino já contém arquivos sem instalação Anna Nyui confirmada; bank existente preservada") from exc
    _, digest = inventory(directory)
    if marker.get("source_sha256") != SOURCE_SHA256 or marker.get("inventory_sha256") != INVENTORY_SHA256 or digest != INVENTORY_SHA256:
        raise InstallError("bank instalada foi alterada ou não corresponde ao ZIP fixado; arquivos preservados")
    return inspect_bank(directory)


def install_voicebank(*, archive: Path, directory: Path, env: Path, container: str | None = None,
                      esper_root: Path | None = None, dry_run: bool = False) -> dict:
    report = {"ok": False, "installed": False, "voicebank_ready": False, "voice_id": "anna-nyui",
              "voicebank_profile": "anna-nyui-diva", "source_url": SOURCE_URL, "author_url": AUTHOR_URL,
              "terms_url": TERMS_URL, "dry_run": dry_run,
              "source_sha256": SOURCE_SHA256, "worker_configuration_changed": False,
              "production_voice_changed": False, "anna_synthesis_verified": False,
              "portuguese_quality_verified": False, "checks": {},
              "note": "Banco oficial de Aoka. Instala em pasta própria, sem alterar a voz selecionada; não redistribua os arquivos do banco."}
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
        if dry_run:
            if existing is None:
                with tempfile.TemporaryDirectory(prefix="anna-check-") as temporary:
                    stage = Path(temporary)
                    report["checks"]["archive_pin"] = {"ok": True, **snapshot_archive(archive, stage / "source.zip")}
                    bank = stage / "bank"
                    bank.mkdir(mode=0o700)
                    report["checks"]["extraction"] = {"ok": True, **extract_archive(stage / "source.zip", bank)}
                    existing = inspect_bank(bank)
                    _, digest = inventory(bank)
                    if digest != INVENTORY_SHA256:
                        raise InstallError("inventário extraído diverge do ZIP fixado")
            report.update(ok=True, voicebank_ready=True, voicebank=existing,
                          restart_required=False, would_change_configuration=original != updated)
            return report
        directory.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        real_directories(directory.parent)
        lock_path = directory.parent / ("." + directory.name + ".anna-install.lock")
        try:
            lock = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        except FileExistsError as exc:
            raise InstallError("há uma instalação Anna Nyui em andamento ou um lock antigo; arquivos preservados") from exc
        existing = check_existing(directory)
        if existing is None:
            if shutil.disk_usage(directory.parent).free < SOURCE_BYTES + EXPECTED_UNPACKED_BYTES + 32 * 1024 * 1024:
                raise InstallError("espaço livre insuficiente para preparar a bank")
            with tempfile.TemporaryDirectory(prefix=".anna-stage-", dir=directory.parent) as temporary:
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
                    "inventory_sha256": digest, "voice_id": "anna-nyui"}, indent=2) + "\n").encode("utf-8"))
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
    except (OSError, ValueError, UnicodeError, zipfile.BadZipFile, NotImplementedError, wave.Error, EOFError) as exc:
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
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--source", "--archive", dest="archive", type=Path, help="ZIP oficial já baixado")
    source.add_argument("--download", action="store_true", help="baixar os 447 MB diretamente do link oficial de Aoka")
    parser.add_argument("--timeout", type=float, default=900, help="prazo total do download em segundos")
    parser.add_argument("--dry-run", action="store_true", help="validar o banco sem instalar nem alterar a configuração")
    parser.add_argument("--directory", type=Path, default=Path.home() / "voicebanks/anna-nyui-diva")
    parser.add_argument("--env", type=Path, default=Path(os.getenv("PHONE_WORKER_ENV") or Path.home() / ".phone-worker.env"))
    parser.add_argument("--container", help="nome do container; por padrão mantém o ESPER configurado")
    parser.add_argument("--esper-root", type=Path,
                        help="pasta do ESPER instalado; cria o comando da Anna Nyui sem reutilizar o comando salvo da Teto")
    options = parser.parse_args(argv)
    try:
        with tempfile.TemporaryDirectory(prefix="anna-source-") as temporary:
            archive = options.archive
            if options.download:
                archive = Path(temporary) / "anna-nyui-diva.zip"
                download_archive(archive, timeout=options.timeout)
            report = install_voicebank(archive=archive, directory=options.directory, env=options.env,
                                      container=options.container, esper_root=options.esper_root,
                                      dry_run=options.dry_run)
    except (OSError, ValueError, urllib.error.URLError) as exc:
        report = {"ok": False, "installed": False, "voice_id": "anna-nyui", "error": str(exc)}
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
