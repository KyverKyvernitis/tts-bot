#!/usr/bin/env python3
"""Install the pinned official Pururu Púrpura native Brazilian Portuguese CVVC bank for ESPER."""
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
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import wave
import contextlib
import ctypes
import ctypes.util

SOURCE_ID = "1x1LuuUMhjl7X_5wmDqiZvPU6NaW_uo-_"
SOURCE_URL = "https://drive.google.com/file/d/" + SOURCE_ID + "/view"
AUTHOR_URL = "https://utaupururu.wixsite.com/home/outono-inverno"
TERMS_URL = "https://utaupururu.wixsite.com/home/outono-inverno"
SOURCE_SHA256 = "04b61325cddbb5defef5452ec8eb15f2649a7f405263dba7e56e3e880ba4773d"
SOURCE_BYTES = 91989388
EXPECTED_UNPACKED_BYTES = 168356281
EXPECTED_FILES = 494
INVENTORY_SHA256 = "f1154a0d967493f532acf935bd2a46042490baf0a040be12810e6273a20e33d3"
MINIMUM_ALIASES = 1000
MAX_ENTRIES = 2048
MAX_FILE_BYTES = 32 * 1024 * 1024
MAX_UNPACKED_BYTES = 256 * 1024 * 1024
MAX_ENV_BYTES = 256 * 1024
MARKER = ".pururu-purpura-install.json"
WORKER_DIR = Path(__file__).resolve().parents[2] / "termux" / "phone-worker"
BUNDLED_BRIDGE = Path(__file__).resolve().parents[2] / "esper-utau" / "termux" / "resampler.py"
BRIDGE_SHA256 = "75a256a704afca3b835fd2fc8e273f3a6f488fd37c1f180291992aa2289a5698"
KEY = re.compile(r"^[ \t]*(?:export[ \t]+)?([A-Za-z_][A-Za-z0-9_]*)[ \t]*=")
ALLOWED_ENV_KEYS = frozenset({
    "PHONE_WORKER_TETO_RESAMPLER_COMMAND", "PHONE_WORKER_PURURU_ENABLED",
    "PHONE_WORKER_PURURU_VOICEBANK_DIR",
    "PHONE_WORKER_MIKU_RESAMPLER_COMMAND", "PHONE_WORKER_PURURU_RESAMPLER_COMMAND",
    "PHONE_WORKER_PURURU_RENDER_THREADS", "PHONE_WORKER_PURURU_FRAGMENT_CACHE_DIR",
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
                "User-Agent": "Pururu-Purpura-ESPER-installer/1", "Accept-Encoding": "identity"}),
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
            raise InstallError("o Google Drive não entregou o RAR oficial")
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
    # Teto; only Pururu Púrpura keys are published after bank verification.
    configured_raw = (values.get("PHONE_WORKER_PURURU_RESAMPLER_COMMAND")
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
        if not (wrapper.parent.name == BRIDGE_SHA256
                and wrapper.parent.parent.name == "pururu"
                and wrapper.parent.parent.parent.name == "bridges"
                and not wrapper.exists() and not wrapper.is_symlink()):
            regular_bytes(wrapper, MAX_ENV_BYTES)
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
    # A native-only voice must not silently inherit an older CLI bridge that
    # the renderer cannot recognize. Publish the shipped, pinned resident bridge
    # in a voice-owned path; the configured bridges of other voices stay intact.
    bundled_bridge_bytes()
    options = {}
    position = 2
    while position < len(command):
        name, equal, value = command[position].partition("=")
        if name not in {"--root", "--cache-dir", "--container", "--timeout", "--implementation-id"} or name in options:
            raise InstallError("opção ESPER desconhecida ou duplicada no comando existente")
        position += 1
        if not equal:
            if position >= len(command) or command[position].startswith("--"):
                raise InstallError("opção ESPER sem valor no comando existente")
            value = command[position]
            position += 1
        if not value:
            raise InstallError("opção ESPER sem valor no comando existente")
        options[name] = value
    root = Path(esper_root or options.get("--root") or values.get("PHONE_WORKER_ESPER_ROOT")
                or wrapper.parent.parent).expanduser().absolute()
    cache = Path(options.get("--cache-dir") or values.get("PHONE_WORKER_ESPER_CACHE_DIR")
                 or root / "cache").expanduser().absolute()
    timeout = options.get("--timeout") or values.get("PHONE_WORKER_ESPER_TIMEOUT") or "60"
    try:
        if not 1 <= float(timeout) <= 120:
            raise ValueError()
    except (ValueError, OverflowError):
        raise InstallError("timeout ESPER deve ficar entre 1 e 120 segundos")
    owned_bridge = root / "bridges" / "pururu" / BRIDGE_SHA256 / "resampler.py"
    real_directories(owned_bridge.parent)
    command = [command[0], str(owned_bridge), "--root", str(root), "--cache-dir", str(cache),
               "--container", container, "--timeout", timeout, "--implementation-id", BRIDGE_SHA256]
    threads = values.get("PHONE_WORKER_PURURU_RENDER_THREADS") or "2"
    if not threads.isdigit() or not 1 <= int(threads) <= 4:
        raise InstallError("Pururu Púrpura exige entre 1 e 4 threads de renderização")
    return {
        "PHONE_WORKER_PURURU_ENABLED": "true",
        "PHONE_WORKER_PURURU_VOICEBANK_DIR": str(directory),
        "PHONE_WORKER_PURURU_FRAGMENT_CACHE_DIR": (values.get("PHONE_WORKER_PURURU_FRAGMENT_CACHE_DIR")
                                                or str(Path.home() / "phone-worker/cache/pururu-fragments")),
        "PHONE_WORKER_PURURU_RENDER_THREADS": threads,
        "PHONE_WORKER_PURURU_RESAMPLER_COMMAND": shlex.join(command),
    }


def bundled_bridge_bytes() -> bytes:
    data = regular_bytes(BUNDLED_BRIDGE, 128 * 1024)
    if hashlib.sha256(data).hexdigest() != BRIDGE_SHA256:
        raise InstallError("ponte ESPER do kit não corresponde à versão fixada")
    if "--implementation-id" not in bridge_options(data):
        raise InstallError("ponte ESPER do kit não oferece identidade verificável")
    return data


def publish_runtime_bridge(configured: dict[str, str]) -> tuple[Path, int | None]:
    command = shlex.split(configured["PHONE_WORKER_PURURU_RESAMPLER_COMMAND"])
    path = Path(command[1])
    data = bundled_bridge_bytes()
    real_directories(path.parent)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    real_directories(path.parent)
    if path.exists() or path.is_symlink():
        if regular_bytes(path, 128 * 1024) != data:
            raise InstallError("ponte Pururu existente foi alterada; arquivo preservado")
        return path, None
    atomic_write(path, data)
    return path, path.stat().st_ino


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
    backup = path.with_name(path.name + ".bak-pururu-" + str(time.time_ns()))
    descriptor = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(descriptor, "wb") as target:
        target.write(original)
        target.flush()
        os.fsync(target.fileno())
    if regular_bytes(path, MAX_ENV_BYTES, allow_empty=True) != original:
        raise InstallError(".env mudou antes da gravação; configuração local preservada")
    atomic_write(path, updated)
    return {"changed": True, "backup": str(backup)}


_LIBARCHIVE = None


def archive_library():
    """Use libarchive's built-in RAR decoder without executing an unpacker."""
    global _LIBARCHIVE
    if _LIBARCHIVE is not None:
        return _LIBARCHIVE
    name = ctypes.util.find_library("archive")
    if not name:
        candidate = Path(os.getenv("PREFIX") or "/data/data/com.termux/files/usr") / "lib/libarchive.so"
        if candidate.is_file():
            name = str(candidate)
    if not name:
        raise InstallError("libarchive ausente; no Termux execute pkg install libarchive")
    try:
        library = ctypes.CDLL(name)
        signatures = {
            "archive_read_new": (ctypes.c_void_p, []),
            "archive_read_support_filter_all": (ctypes.c_int, [ctypes.c_void_p]),
            "archive_read_support_format_all": (ctypes.c_int, [ctypes.c_void_p]),
            "archive_read_open_filename": (ctypes.c_int, [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_size_t]),
            "archive_read_next_header": (ctypes.c_int, [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]),
            "archive_read_data": (ctypes.c_ssize_t, [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t]),
            "archive_read_free": (ctypes.c_int, [ctypes.c_void_p]),
            "archive_error_string": (ctypes.c_char_p, [ctypes.c_void_p]),
            "archive_entry_pathname_utf8": (ctypes.c_char_p, [ctypes.c_void_p]),
            "archive_entry_size": (ctypes.c_longlong, [ctypes.c_void_p]),
            "archive_entry_filetype": (ctypes.c_uint, [ctypes.c_void_p]),
            "archive_entry_symlink": (ctypes.c_char_p, [ctypes.c_void_p]),
            "archive_entry_hardlink": (ctypes.c_char_p, [ctypes.c_void_p]),
            "archive_entry_is_encrypted": (ctypes.c_int, [ctypes.c_void_p]),
        }
        for symbol, (result, arguments) in signatures.items():
            function = getattr(library, symbol)
            function.restype, function.argtypes = result, arguments
    except (OSError, AttributeError) as exc:
        raise InstallError("libarchive não possui o leitor RAR necessário; atualize o pacote libarchive") from exc
    _LIBARCHIVE = library
    return library


class ArchiveReader:
    def __init__(self, source: Path):
        self.library = archive_library()
        self.handle = self.library.archive_read_new()
        if not self.handle:
            raise InstallError("não foi possível iniciar o leitor RAR")
        self.buffer = ctypes.create_string_buffer(65536)
        try:
            self._check(self.library.archive_read_support_filter_all(self.handle))
            self._check(self.library.archive_read_support_format_all(self.handle))
            self._check(self.library.archive_read_open_filename(self.handle, os.fsencode(source), len(self.buffer)))
        except BaseException:
            self.close()
            raise

    def _check(self, status):
        if status < 0:
            message = self.library.archive_error_string(self.handle)
            detail = message.decode("utf-8", errors="replace")[:240] if message else "erro desconhecido"
            raise InstallError("RAR inválido ou corrompido: " + detail)

    def __iter__(self):
        while True:
            entry = ctypes.c_void_p()
            status = self.library.archive_read_next_header(self.handle, ctypes.byref(entry))
            if status == 1:
                return
            self._check(status)
            if not entry:
                raise InstallError("RAR contém cabeçalho inválido")
            raw_name = self.library.archive_entry_pathname_utf8(entry)
            if raw_name is None:
                raise InstallError("RAR contém nome de arquivo não Unicode")
            name = raw_name.decode("utf-8")
            size = self.library.archive_entry_size(entry)
            kind = self.library.archive_entry_filetype(entry)
            linked = (self.library.archive_entry_symlink(entry) is not None
                      or self.library.archive_entry_hardlink(entry) is not None)
            encrypted = self.library.archive_entry_is_encrypted(entry) != 0
            yield name, size, kind, linked, encrypted

    def blocks(self):
        while True:
            count = self.library.archive_read_data(self.handle, self.buffer, len(self.buffer))
            self._check(count)
            if not count:
                return
            yield self.buffer.raw[:count]

    def close(self):
        if self.handle:
            self.library.archive_read_free(self.handle)
            self.handle = None


@contextlib.contextmanager
def open_archive(source: Path):
    archive = ArchiveReader(source)
    try:
        yield archive
    finally:
        archive.close()


def checked_members(archive) -> tuple[list, int]:
    members, seen, total = [], {}, 0
    for name, size, kind, linked, encrypted in archive:
        if len(members) >= MAX_ENTRIES:
            raise InstallError("RAR com entradas acima do limite")
        name = name.replace("\\", "/")
        parts = name.rstrip("/").split("/")
        if (not name or name.startswith("/") or any(part in ("", ".", "..") for part in parts)
                or any(":" in part or any(ord(char) < 32 for char in part) for part in parts)
                or len(parts) > 20 or len(name.encode("utf-8")) > 1024):
            raise InstallError("caminho inseguro no RAR")
        relative = PurePosixPath(*parts)
        directory = kind == stat.S_IFDIR
        if linked or encrypted or kind not in (stat.S_IFREG, stat.S_IFDIR):
            raise InstallError("RAR contém link, arquivo especial ou conteúdo criptografado")
        if relative in seen or (directory and size) or not 0 <= size <= MAX_FILE_BYTES:
            raise InstallError("entrada RAR duplicada, inválida ou acima do limite")
        seen[relative] = directory
        total += size
        if total > MAX_UNPACKED_BYTES:
            raise InstallError("RAR expandido acima do limite")
        members.append((relative, directory, size))
    if not members:
        raise InstallError("RAR vazio")
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
            raise InstallError("RAR não é arquivo regular com o tamanho da bank inspecionada")
        with target.open("xb") as output:
            while block := stream.read(1024 * 1024):
                count += len(block)
                if count > SOURCE_BYTES:
                    raise InstallError("RAR mudou durante a leitura")
                digest.update(block)
                output.write(block)
    if count != SOURCE_BYTES or digest.hexdigest() != SOURCE_SHA256:
        raise InstallError("SHA-256 do RAR diverge da Púrpura oficial inspecionada; nada foi instalado")
    return {"bytes": count, "sha256": digest.hexdigest()}


def extract_archive(source: Path, destination: Path) -> dict:
    with open_archive(source) as archive:
        members, total = checked_members(archive)
    if total != EXPECTED_UNPACKED_BYTES or sum(not directory for _, directory, _ in members) != EXPECTED_FILES:
        raise InstallError("estrutura RAR diverge do banco inspecionado")
    roots = {relative.parts[0] for relative, _, _ in members}
    if len(roots) != 1 or any(len(relative.parts) < 2 and not directory
                             for relative, directory, _ in members):
        raise InstallError("RAR deve conter uma única pasta de banco de voz")
    count = 0
    with open_archive(source) as archive:
        actual = iter(archive)
        for relative, directory, size in members:
            header = next(actual, None)
            if (header is None or header[0].replace("\\", "/").rstrip("/") != relative.as_posix()
                    or header[1] != size or header[2] != (stat.S_IFDIR if directory else stat.S_IFREG)
                    or header[3] or header[4]):
                raise InstallError("RAR mudou antes da extração")
            if len(relative.parts) == 1:
                continue
            output = destination.joinpath(*relative.parts[1:])
            if directory:
                output.mkdir(parents=True, exist_ok=True, mode=0o700)
                continue
            output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
            written = 0
            with os.fdopen(descriptor, "wb") as target:
                for block in archive.blocks():
                    written += len(block)
                    count += len(block)
                    if written > size or count > total:
                        raise InstallError("extração excedeu o tamanho declarado")
                    target.write(block)
            if written != size:
                raise InstallError("arquivo RAR truncado")
        if next(actual, None) is not None:
            raise InstallError("RAR mudou durante a extração")
    if count != total:
        raise InstallError("volume extraído diverge do RAR")
    # Exact archive and extracted-file SHA-256 pins are checked separately.
    # libarchive versions differ in whether stored RAR entries enforce CRCs.
    return {"files": EXPECTED_FILES, "unpacked_bytes": count, "data_stream_checked": True}

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
    index = VoicebankIndex.load(directory, minimum_aliases=MINIMUM_ALIASES, case_sensitive=True)
    required = ("-a", "*a", "*e", "*E", "*i", "*o", "*O", "*u", "*6~")
    if any(alias not in index.entries for alias in required):
        raise InstallError("faltam vogais orais ou nasais do banco português oficial")
    wavs = sorted({entry.wav_path for entry in index.entries.values()})
    for path in wavs:
        with wave.open(str(path), "rb") as audio:
            if (audio.getnchannels() != 1 or audio.getsampwidth() != 2
                    or audio.getframerate() not in (44100, 48000) or audio.getnframes() <= 0
                    or audio.getcomptype() != "NONE"):
                raise InstallError("banco contém WAV incompatível com o ESPER")
            if len(audio.readframes(audio.getnframes())) != audio.getnframes() * 2:
                raise InstallError("WAV do banco está truncado")
    return {"aliases": index.alias_count, "name": index.name, "reading_language": "pt-BR",
            "wav_decode_checked": True, "wav_files": len(wavs)}


def check_existing(directory: Path) -> dict | None:
    real_directories(directory)
    if not directory.exists() or not any(directory.iterdir()):
        return None
    try:
        marker = json.loads(regular_bytes(directory / MARKER, 4096))
    except (OSError, ValueError) as exc:
        raise InstallError("destino já contém arquivos sem instalação Pururu Púrpura confirmada; bank existente preservada") from exc
    _, digest = inventory(directory)
    if marker.get("source_sha256") != SOURCE_SHA256 or marker.get("inventory_sha256") != INVENTORY_SHA256 or digest != INVENTORY_SHA256:
        raise InstallError("bank instalada foi alterada ou não corresponde ao RAR fixado; arquivos preservados")
    return inspect_bank(directory)


def install_voicebank(*, archive: Path | None, directory: Path, env: Path, container: str | None = None,
                      esper_root: Path | None = None, dry_run: bool = False, repair_runtime: bool = False) -> dict:
    report = {"ok": False, "installed": False, "voicebank_ready": False, "voice_id": "pururu-purpura",
              "voicebank_profile": "pururu-purpura", "source_url": SOURCE_URL, "author_url": AUTHOR_URL,
              "terms_url": TERMS_URL, "dry_run": dry_run,
              "source_sha256": SOURCE_SHA256, "worker_configuration_changed": False,
              "production_voice_changed": False, "pururu_synthesis_verified": False,
              "portuguese_quality_verified": False, "checks": {},
              "note": "Banco oficial de Juliana Plu. Instala em pasta própria, sem alterar a voz selecionada; não redistribua os arquivos do banco."}
    lock, published, runtime_bridge = None, None, None
    try:
        directory = Path(os.path.abspath(directory.expanduser()))
        env = Path(os.path.abspath(env.expanduser()))
        archive = Path(os.path.abspath(archive.expanduser())) if archive is not None else None
        report["directory"] = str(directory)
        original, settings = read_environment(env)
        configured = configuration(directory, settings, container=container, esper_root=esper_root)
        updated = updated_environment(original, configured)
        real_directories(directory)
        existing = check_existing(directory)
        if repair_runtime and existing is None:
            raise InstallError("Pururu Púrpura ainda não está instalada; instale o banco antes de reparar o runtime")
        if existing is None and archive is None:
            raise InstallError("RAR oficial ausente para instalar o banco")
        report["runtime_repair"] = repair_runtime
        report["bank_reused"] = existing is not None
        report["bank_downloaded"] = False
        if dry_run:
            if existing is None:
                with tempfile.TemporaryDirectory(prefix="pururu-check-") as temporary:
                    stage = Path(temporary)
                    report["checks"]["archive_pin"] = {"ok": True, **snapshot_archive(archive, stage / "source.rar")}
                    bank = stage / "bank"
                    bank.mkdir(mode=0o700)
                    report["checks"]["extraction"] = {"ok": True, **extract_archive(stage / "source.rar", bank)}
                    existing = inspect_bank(bank)
                    _, digest = inventory(bank)
                    if digest != INVENTORY_SHA256:
                        raise InstallError("inventário extraído diverge do RAR fixado")
            report.update(ok=True, voicebank_ready=True, voicebank=existing,
                          restart_required=False, would_change_configuration=original != updated)
            return report
        directory.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        real_directories(directory.parent)
        lock_path = directory.parent / ("." + directory.name + ".pururu-install.lock")
        try:
            lock = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        except FileExistsError as exc:
            raise InstallError("há uma instalação Pururu Púrpura em andamento ou um lock antigo; arquivos preservados") from exc
        existing = check_existing(directory)
        if existing is None:
            if shutil.disk_usage(directory.parent).free < SOURCE_BYTES + EXPECTED_UNPACKED_BYTES + 32 * 1024 * 1024:
                raise InstallError("espaço livre insuficiente para preparar a bank")
            with tempfile.TemporaryDirectory(prefix=".pururu-stage-", dir=directory.parent) as temporary:
                stage = Path(temporary)
                report["checks"]["archive_pin"] = {"ok": True, **snapshot_archive(archive, stage / "source.rar")}
                bank = stage / "bank"
                bank.mkdir(mode=0o700)
                report["checks"]["extraction"] = {"ok": True, **extract_archive(stage / "source.rar", bank)}
                existing = inspect_bank(bank)
                _, digest = inventory(bank)
                if digest != INVENTORY_SHA256:
                    raise InstallError("inventário extraído diverge do RAR fixado")
                atomic_write(bank / MARKER, (json.dumps({"source_sha256": SOURCE_SHA256,
                    "inventory_sha256": digest, "voice_id": "pururu-purpura"}, indent=2) + "\n").encode("utf-8"))
                if directory.exists():
                    # Never remove files that another process created in the empty destination.
                    directory.rmdir()
                bank.rename(directory)
                published = directory.stat().st_ino
                report["installed"] = True
                report["checks"]["publication"] = {"ok": True, "atomic": True}
        else:
            report["checks"]["existing_installation"] = {"ok": True, "inventory_verified": True}
        runtime_bridge = publish_runtime_bridge(configured)
        report["checks"]["runtime_bridge"] = {"ok": True, "sha256": BRIDGE_SHA256,
            "path": str(runtime_bridge[0]), "published": runtime_bridge[1] is not None,
            "resident_bridge_compatible": True}
        change = publish_environment(env, original, updated)
        report["worker_configuration_changed"] = change["changed"]
        if "backup" in change:
            report["configuration_backup"] = change["backup"]
        report.update(ok=True, voicebank_ready=True, voicebank=existing,
                      restart_required=change["changed"] or runtime_bridge[1] is not None)
    except (OSError, ValueError, UnicodeError, NotImplementedError, wave.Error, EOFError) as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
        if runtime_bridge is not None and runtime_bridge[1] is not None:
            try:
                path, inode = runtime_bridge
                if (stat.S_ISREG(path.lstat().st_mode) and path.stat().st_ino == inode
                        and regular_bytes(path, 128 * 1024) == bundled_bridge_bytes()):
                    path.unlink()
                    report["checks"]["runtime_rollback"] = {"ok": True}
            except OSError:
                report["checks"]["runtime_rollback"] = {"ok": False}
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
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv == ["--check-archive-library"]:
        try:
            archive_library()
        except InstallError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        return 0
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--source", "--archive", dest="archive", type=Path, help="RAR oficial já baixado")
    source.add_argument("--download", action="store_true", help="baixar os 92 MB diretamente do link oficial de Juliana Plu")
    source.add_argument("--repair-runtime", action="store_true", help="reparar a ponte ESPER do banco já instalado sem baixar o banco novamente")
    parser.add_argument("--timeout", type=float, default=900, help="prazo total do download em segundos")
    parser.add_argument("--dry-run", action="store_true", help="validar o banco sem instalar nem alterar a configuração")
    parser.add_argument("--directory", type=Path, default=Path.home() / "voicebanks/pururu-purpura")
    parser.add_argument("--env", type=Path, default=Path(os.getenv("PHONE_WORKER_ENV") or Path.home() / ".phone-worker.env"))
    parser.add_argument("--container", help="nome do container; por padrão mantém o ESPER configurado")
    parser.add_argument("--esper-root", type=Path,
                        help="pasta do ESPER instalado; cria o comando da Pururu Púrpura sem reutilizar o comando salvo da Teto")
    options = parser.parse_args(argv)
    try:
        with tempfile.TemporaryDirectory(prefix="pururu-source-") as temporary:
            archive = options.archive
            if options.download:
                archive = Path(temporary) / "pururu-purpura.rar"
                download_archive(archive, timeout=options.timeout)
            report = install_voicebank(archive=archive, directory=options.directory, env=options.env,
                                      container=options.container, esper_root=options.esper_root,
                                      dry_run=options.dry_run, repair_runtime=options.repair_runtime)
            report["bank_downloaded"] = bool(options.download)
    except (OSError, ValueError, urllib.error.URLError) as exc:
        report = {"ok": False, "installed": False, "voice_id": "pururu-purpura", "error": str(exc)}
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
