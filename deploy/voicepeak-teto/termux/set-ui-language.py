#!/usr/bin/env python3
"""Change the validated VOICEPEAK 1.2.22 UI setting with the GUI closed."""
from __future__ import annotations

import argparse
import ctypes
import errno
import fcntl
import importlib.util
import os
from pathlib import Path
import secrets
import stat
import sys
import time
from xml.dom import Node, minidom
from xml.parsers import expat


MAX_XML_BYTES = 1024 * 1024
LANGUAGES = ("english", "japanese")


class LanguageSettingError(ValueError):
    pass


def settings_path() -> Path:
    """Use the launcher's configuration validation, including under python -I."""
    spec = importlib.util.spec_from_file_location(
        "voicepeak_ui_language_launcher", Path(__file__).with_name("launcher.py")
    )
    if spec is None or spec.loader is None:
        raise LanguageSettingError("launcher.py não encontrado")
    launcher = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(launcher)
    previous = os.environ.get("VOICEPEAK_TERMUX_CONFIG")
    if previous is None:
        directory = Path.home() / ".voicepeak-termux"
        corrected = directory / "config-box64-x11fix.json"
        selected = corrected if corrected.exists() or corrected.is_symlink() else directory / "config-box64.json"
        os.environ["VOICEPEAK_TERMUX_CONFIG"] = str(selected)
    try:
        config = launcher.load_config()
    except launcher.ConfigurationError as exc:
        raise LanguageSettingError(str(exc)) from exc
    finally:
        if previous is None:
            os.environ.pop("VOICEPEAK_TERMUX_CONFIG", None)
        else:
            os.environ["VOICEPEAK_TERMUX_CONFIG"] = previous
    engine = config.get("engine_directory", "")
    if not engine or not Path(engine).is_dir():
        raise LanguageSettingError("engine_directory precisa apontar para a pasta existente do VOICEPEAK")
    return Path(engine) / "usersettings/settings/settings.xml"


def _open_parent(path: Path, *, create: bool) -> int:
    """Open each directory without following links; keep writes on this directory."""
    # Android's /data and /data/data allow the Termux UID to traverse them,
    # but not list them. O_PATH only needs traversal on intermediate folders.
    flags = os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW
    current = os.open(path.anchor, flags)
    try:
        for component in path.parent.parts[1:]:
            try:
                following = os.open(component, flags, dir_fd=current)
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(component, mode=0o700, dir_fd=current)
                except FileExistsError:
                    pass
                following = os.open(component, flags, dir_fd=current)
            os.close(current)
            current = following
        final = os.open(".", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current)
        os.close(current)
        return final
    except BaseException:
        os.close(current)
        raise


def _fingerprint(info: os.stat_result) -> tuple[int, ...]:
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _read_source(parent: int, name: str) -> tuple[bytes, tuple[int, ...]] | None:
    try:
        info = os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode):
        raise LanguageSettingError("settings.xml precisa ser um arquivo regular, sem links simbólicos")
    descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or _fingerprint(before) != _fingerprint(info):
            raise LanguageSettingError("settings.xml mudou durante a leitura; feche o VOICEPEAK e tente novamente")
        if before.st_size > MAX_XML_BYTES:
            raise LanguageSettingError("settings.xml excede 1 MiB")
        chunks = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(65536, MAX_XML_BYTES + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > MAX_XML_BYTES:
                raise LanguageSettingError("settings.xml excede 1 MiB")
        if _fingerprint(os.fstat(descriptor)) != _fingerprint(before):
            raise LanguageSettingError("settings.xml mudou durante a leitura; feche o VOICEPEAK e tente novamente")
        return b"".join(chunks), _fingerprint(before)
    finally:
        os.close(descriptor)


def _reject_declaration(*_arguments: object) -> None:
    raise LanguageSettingError("DOCTYPE e ENTITY não são permitidos em settings.xml")


def _prepare_xml(source: bytes | None, language: str) -> bytes | None:
    if language not in LANGUAGES:
        raise LanguageSettingError("idioma inválido; use english ou japanese")
    if source is None:
        document = minidom.Document()
        root = document.createElement("ApplicationSettings")
        document.appendChild(root)
    else:
        # Expat rejects DTDs before expanding entities, including UTF-16 XML.
        parser = expat.ParserCreate()
        parser.StartDoctypeDeclHandler = _reject_declaration
        parser.EntityDeclHandler = _reject_declaration
        try:
            parser.Parse(source, True)
            document = minidom.parseString(source)
        except expat.ExpatError as exc:
            raise LanguageSettingError("settings.xml contém XML inválido; o arquivo foi preservado") from exc
        root = document.documentElement
    try:
        if root.tagName != "ApplicationSettings":
            raise LanguageSettingError("raiz XML inesperada; esperado ApplicationSettings")
        interfaces = [child for child in root.childNodes if child.nodeType == Node.ELEMENT_NODE and child.tagName == "Interface"]
        if len(interfaces) > 1:
            raise LanguageSettingError("settings.xml contém mais de um elemento Interface")
        if interfaces:
            interface = interfaces[0]
            if interface.getAttribute("language") == language:
                return None
        else:
            interface = document.createElement("Interface")
            root.appendChild(interface)
        interface.setAttribute("language", language)
        try:
            result = document.toxml(encoding="utf-8")
        except (RecursionError, UnicodeError) as exc:
            raise LanguageSettingError("não foi possível serializar settings.xml; o arquivo foi preservado") from exc
        if len(result) > MAX_XML_BYTES:
            raise LanguageSettingError("o XML atualizado excederia 1 MiB; o arquivo foi preservado")
        return result
    finally:
        document.unlink()


def _verify_source(parent: int, path: Path, source: tuple[bytes, tuple[int, ...]] | None) -> None:
    check_parent = _open_parent(path, create=False)
    try:
        original = os.fstat(parent)
        current = os.fstat(check_parent)
        if (original.st_dev, original.st_ino) != (current.st_dev, current.st_ino):
            raise LanguageSettingError("a pasta de settings.xml mudou; nenhuma configuração foi substituída")
    finally:
        os.close(check_parent)
    if _read_source(parent, path.name) != source:
        raise LanguageSettingError("settings.xml mudou durante a operação; nenhuma configuração foi substituída")


def _write_exclusive(parent: int, name: str, contents: bytes, mode: int = 0o600) -> None:
    descriptor = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
    try:
        view = memoryview(contents)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("não foi possível gravar o arquivo inteiro")
            view = view[written:]
        os.fchmod(descriptor, mode)
        os.fsync(descriptor)
    except BaseException:
        os.unlink(name, dir_fd=parent)
        raise
    finally:
        os.close(descriptor)


def _rename_new_no_replace(parent: int, source: str, destination: str) -> None:
    """Publish a new file using Linux/Bionic without replacing another writer."""
    if not (sys.platform.startswith("linux") or sys.platform == "android" or hasattr(sys, "getandroidapilevel")):
        raise LanguageSettingError("a criação atômica deste arquivo exige Android/Linux")
    libraries = []
    names = (None, "libc.so") if sys.platform == "android" or hasattr(sys, "getandroidapilevel") else (None,)
    for name in names:
        try:
            libraries.append(ctypes.CDLL(name, use_errno=True))
        except OSError:
            continue
    rename = next((function for library in libraries if (function := getattr(library, "renameat2", None)) is not None), None)
    arguments = (parent, os.fsencode(source), parent, os.fsencode(destination), 1)
    ctypes.set_errno(0)
    if rename is not None:
        rename.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
        rename.restype = ctypes.c_int
        result = rename(*arguments)
    else:
        native_abi = (os.uname().machine.lower(), ctypes.sizeof(ctypes.c_void_p), ctypes.sizeof(ctypes.c_long))
        syscall_number = {
            ("aarch64", 8, 8): 276,
            ("arm64", 8, 8): 276,
            ("x86_64", 8, 8): 316,
            ("amd64", 8, 8): 316,
            ("riscv64", 8, 8): 276,
        }.get(native_abi)
        syscall = next((function for library in libraries if (function := getattr(library, "syscall", None)) is not None), None)
        if syscall_number is None or syscall is None:
            raise LanguageSettingError("o sistema não oferece criação atômica sem substituir um arquivo existente")
        syscall.restype = ctypes.c_long
        # Bionic may lack the renameat2 symbol despite kernel support. The
        # variadic syscall needs explicit pointer and integer types.
        result = syscall(
            ctypes.c_long(syscall_number), ctypes.c_int(arguments[0]), ctypes.c_char_p(arguments[1]),
            ctypes.c_int(arguments[2]), ctypes.c_char_p(arguments[3]), ctypes.c_uint(arguments[4]),
        )
    if result != 0:
        failure = ctypes.get_errno() or errno.EIO
        if failure in (errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP):
            raise LanguageSettingError("o sistema de arquivos não oferece criação atômica sem substituir um arquivo existente")
        raise OSError(failure, os.strerror(failure), destination)


def _publish_new(parent: int, source: str, destination: str) -> None:
    link = getattr(os, "link", None)
    if callable(link):
        try:
            link(source, destination, src_dir_fd=parent, dst_dir_fd=parent, follow_symlinks=False)
            return
        except NotImplementedError:
            pass
    _rename_new_no_replace(parent, source, destination)


def set_ui_language(path: Path, language: str = "english") -> tuple[bool, Path | None]:
    """Change only the UI language, backing up exact existing bytes before replacement."""
    path = Path(os.path.abspath(path))
    if not path.name:
        raise LanguageSettingError("especifique o caminho de settings.xml")
    if language not in LANGUAGES:
        raise LanguageSettingError("idioma inválido; use english ou japanese")
    parent = _open_parent(path, create=True)
    temporary = None
    backup = None
    committed = False
    try:
        # Serialize this helper without creating persistent lock files.
        fcntl.flock(parent, fcntl.LOCK_EX | fcntl.LOCK_NB)
        source = _read_source(parent, path.name)
        updated = _prepare_xml(None if source is None else source[0], language)
        if updated is None:
            _verify_source(parent, path, source)
            return False, None
        candidate = f".{path.name}.ui-language-{secrets.token_hex(12)}.tmp"
        mode = 0o600 if source is None else source[1][2] & 0o777
        _write_exclusive(parent, candidate, updated, mode)
        temporary = candidate
        _verify_source(parent, path, source)
        if source is not None:
            timestamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
            candidate = f"{path.name}.backup-{timestamp}-{secrets.token_hex(12)}"
            _write_exclusive(parent, candidate, source[0])
            backup = candidate
            _verify_source(parent, path, source)
            os.replace(temporary, path.name, src_dir_fd=parent, dst_dir_fd=parent)
        else:
            # Android's Python may omit os.link. Both publication methods
            # preserve a file created by another writer after our absence check.
            _publish_new(parent, temporary, path.name)
        committed = True
        return True, None if backup is None else path.with_name(backup)
    finally:
        for name in (temporary, backup if not committed else None):
            if name is not None:
                try:
                    os.unlink(name, dir_fd=parent)
                except FileNotFoundError:
                    pass
        os.close(parent)


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Altera o idioma da interface do VOICEPEAK 1.2.22. Feche o VOICEPEAK antes de executar e abra novamente depois."
    )
    parser.add_argument("--language", choices=LANGUAGES, default="english", help="idioma da interface (padrão: english)")
    parser.add_argument("--settings", type=Path, help="caminho explícito de settings.xml; padrão: pasta do engine na configuração Termux")
    options = parser.parse_args(arguments)
    try:
        path = options.settings if options.settings is not None else settings_path()
        changed, backup = set_ui_language(path, options.language)
    except (LanguageSettingError, OSError, RecursionError) as exc:
        print(f"VOICEPEAK: {exc}", file=sys.stderr)
        return 2
    if changed:
        print(f"Interface configurada: {options.language}. Abra o VOICEPEAK novamente.")
        if backup is not None:
            print(f"Backup: {backup}")
    else:
        print(f"A interface já está configurada: {options.language}. Nenhum arquivo foi alterado.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
