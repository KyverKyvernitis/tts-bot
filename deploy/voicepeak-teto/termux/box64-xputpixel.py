#!/usr/bin/env python3
"""Prepare an isolated XPutPixel compatibility overlay for pinned Box64.

Only open Box64 source is copied. The pristine checkout is never modified.
CMake-owned generated wrappers, x64printer.c and git_head.h may change during
a build; all other source content and the two patched files are checked on reuse.
"""
from __future__ import annotations

import argparse
import ctypes
import errno
import hashlib
import json
import os
from pathlib import Path
import posixpath
import shutil
import stat
import sys
import tempfile


SOURCE_COMMIT = "dae0917c47b4edd8956f314210417a20fd225c4b"
PATCH_ID = "voicepeak-xputpixel-1"
MANIFEST_NAME = ".voicepeak-xputpixel-manifest.json"
PINNED_SHA256 = {
    "src/wrapped/wrappedlibx11.c": "9aeb3b1dc9935c240b8d293d3260a342ab0a9b823c7776613800af56fafff0f9",
    "src/wrapped/wrappedlibx11_private.h": "d3f0d94a22cec31bdf51a2ecfcdbd3613562142a74ce2ac569e32187bb5a5094",
}
# Exact output locations of upstream rebuild_wrappers.py and CMakeLists.txt.
BUILD_GENERATED = ("src/wrapped/generated", "src/emu/x64printer.c", "src/git_head.h")
PATCH_BODY = b"""/* VOICEPEAK XPutPixel compatibility overlay, voicepeak-xputpixel-1.
 * XImage callbacks are bridged for x86 callers; restore native callbacks
 * around Xlib's dispatch, then bridge them again as my_XPutImage does. */
EXPORT int32_t my_XPutPixel(x64emu_t* emu, void* image, int32_t x, int32_t y, uintptr_t pixel)
{
    UnbridgeImageFunc(emu, (XImage*)image);
    int32_t ret = my->XPutPixel(image, x, y, pixel);
    BridgeImageFunc(emu, (XImage*)image);
    return ret;
}

"""


class OverlayError(ValueError):
    """The source or destination cannot be changed without losing integrity."""


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def render_patch(source: Path) -> dict[str, bytes]:
    originals = {}
    for name, expected in PINNED_SHA256.items():
        path = source / name
        if not path.is_file() or path.is_symlink():
            raise OverlayError(f"arquivo original ausente ou inválido: {name}")
        data = path.read_bytes()
        if _digest(data) != expected:
            raise OverlayError(f"fonte Box64 diferente do commit fixado: {name}")
        originals[name] = data
    header = "src/wrapped/wrappedlibx11_private.h"
    old = b"//GO(XPutPixel, \n"
    if originals[header].count(old) != 1:
        raise OverlayError("marcador XPutPixel original não foi encontrado uma vez")
    originals[header] = originals[header].replace(old, b"GOM(XPutPixel, iFEpiiL)\n", 1)
    implementation = "src/wrapped/wrappedlibx11.c"
    marker = b"EXPORT void* my_XGetSubImage(x64emu_t* emu, void* disp, size_t drawable\n"
    if originals[implementation].count(marker) != 1:
        raise OverlayError("ponto de inserção da implementação não é o esperado")
    originals[implementation] = originals[implementation].replace(marker, PATCH_BODY + marker, 1)
    return originals


def _excluded(name: str) -> bool:
    return (
        ".git" in name.split("/")
        or name == MANIFEST_NAME
        or any(name == entry or name.startswith(entry + "/") for entry in BUILD_GENERATED)
    )


def _inventory(root: Path) -> dict[str, dict[str, str | int]]:
    result = {}

    def visit(directory: Path, prefix: str = "") -> None:
        for entry in sorted(os.scandir(directory), key=lambda item: item.name):
            name = prefix + entry.name
            if _excluded(name):
                continue
            info = entry.stat(follow_symlinks=False)
            mode = stat.S_IMODE(info.st_mode)
            path = Path(entry.path)
            if stat.S_ISDIR(info.st_mode):
                result[name] = {"kind": "directory", "mode": mode}
                visit(path, name + "/")
            elif stat.S_ISREG(info.st_mode):
                with path.open("rb") as stream:
                    digest = hashlib.file_digest(stream, "sha256").hexdigest() if hasattr(hashlib, "file_digest") else _digest(stream.read())
                result[name] = {"kind": "file", "mode": mode, "sha256": digest}
            elif stat.S_ISLNK(info.st_mode):
                link = os.readlink(path)
                resolved = posixpath.normpath(posixpath.join(posixpath.dirname(name), link))
                if os.path.isabs(link) or resolved == ".." or resolved.startswith("../"):
                    raise OverlayError(f"link sai da árvore de fonte: {name}")
                result[name] = {"kind": "symlink", "target": link}
            else:
                raise OverlayError(f"arquivo especial não é fonte Box64: {name}")

    visit(root)
    return result


def _expected_manifest(source: Path, rendered: dict[str, bytes]) -> dict:
    inventory = _inventory(source)
    for name, data in rendered.items():
        inventory[name] = {**inventory[name], "sha256": _digest(data)}
    return {
        "version": 1,
        "patch_id": PATCH_ID,
        "source_commit": SOURCE_COMMIT,
        "pinned_source_sha256": PINNED_SHA256,
        "patched_sha256": {name: _digest(data) for name, data in rendered.items()},
        "build_generated_exclusions": list(BUILD_GENERATED),
        "protected_entries": inventory,
    }


def _manifest_bytes(manifest: dict) -> bytes:
    return (json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def _verify_target(target: Path, manifest: dict) -> None:
    marker = target / MANIFEST_NAME
    if target.is_symlink() or not target.is_dir() or marker.is_symlink() or not marker.is_file():
        raise OverlayError("pasta de destino já existe sem manifesto válido; preservada")
    if marker.read_bytes() != _manifest_bytes(manifest):
        raise OverlayError("manifesto do destino difere da fonte esperada; pasta preservada")
    if _inventory(target) != manifest["protected_entries"]:
        raise OverlayError("fonte de destino foi alterada; pasta preservada")


def _publish_new(staging: Path, target: Path) -> None:
    """Linux renameat2 prevents overwriting a concurrent destination."""
    libc = ctypes.CDLL(None, use_errno=True)
    rename = getattr(libc, "renameat2", None)
    if rename is None:
        raise OverlayError("renameat2 é necessário para publicar a fonte sem sobrescrever")
    rename.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
    rename.restype = ctypes.c_int
    if rename(-100, os.fsencode(staging), -100, os.fsencode(target), 1) != 0:
        failure = ctypes.get_errno() or errno.EIO
        if failure == errno.EEXIST:
            raise OverlayError("pasta de destino apareceu durante a cópia; preservada")
        raise OSError(failure, os.strerror(failure))


def prepare_overlay(source: Path, target: Path) -> dict:
    source = Path(source).expanduser()
    target = Path(target).expanduser()
    if source.is_symlink() or not source.is_dir():
        raise OverlayError("--source deve ser a pasta original, sem link simbólico")
    if target.is_symlink():
        raise OverlayError("--target não pode ser um link simbólico")
    source = source.resolve()
    target = target.resolve()
    if source == target or source in target.parents or target in source.parents:
        raise OverlayError("--target deve ficar separado da árvore original")
    if os.path.lexists(source / MANIFEST_NAME):
        raise OverlayError("--source já contém overlay; use o checkout original")
    rendered = render_patch(source)
    manifest = _expected_manifest(source, rendered)
    reused = os.path.lexists(target)
    if reused:
        _verify_target(target, manifest)
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".voicepeak-xputpixel-", dir=target.parent) as temporary:
            staging = Path(temporary) / "source"
            shutil.copytree(source, staging, symlinks=True, ignore=shutil.ignore_patterns(".git"))
            for name, data in rendered.items():
                (staging / name).write_bytes(data)
            (staging / MANIFEST_NAME).write_bytes(_manifest_bytes(manifest))
            _verify_target(staging, manifest)
            # Detect source changes while copying before any target is published.
            if _expected_manifest(source, render_patch(source)) != manifest:
                raise OverlayError("fonte original mudou durante a cópia; destino não foi publicado")
            _publish_new(staging, target)
    return {
        "patch_id": PATCH_ID,
        "source_commit": SOURCE_COMMIT,
        "reused": reused,
        "target": str(target),
        "manifest_path": str(target / MANIFEST_NAME),
        "patched_sha256": manifest["patched_sha256"],
        "build_generated_exclusions": list(BUILD_GENERATED),
        "note": "overlay de fonte verificado; compilação, interface e voz Teto não são comprovadas",
    }


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--target", type=Path, required=True)
    args = parser.parse_args(arguments)
    try:
        result = prepare_overlay(args.source, args.target)
    except (OverlayError, OSError) as exc:
        print(f"Overlay Box64 XPutPixel: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
