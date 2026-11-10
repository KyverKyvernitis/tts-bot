#!/usr/bin/env python3
"""Install already-built native artifacts, separately from the legacy worker ZIP.

The installer is platform plumbing. It never builds code or touches voicebanks,
pairing, Python dependencies, music services, or the running Phone Worker.
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import http.client
import json
import os
from pathlib import Path
import platform
import re
import shutil
import socket
import stat
import struct
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request
import uuid

SCHEMA = "tts-native-artifact-v1"
MAX_MANIFEST_BYTES = 256 * 1024
MAX_ARTIFACT_BYTES = 128 * 1024 * 1024
MAX_TOTAL_BYTES = 256 * 1024 * 1024
TARGET_MACHINES = {"x86_64-unknown-linux-gnu": 62, "aarch64-unknown-linux-gnu": 183,
                   "aarch64-linux-android": 183}
ARTIFACT_NAMES = {"tts-worker", "tts-worldline-native"}


def current_target():
    arch = platform.machine().lower()
    if arch in {"aarch64", "arm64"}:
        android = hasattr(__import__("sys"), "getandroidapilevel") or os.getenv("PREFIX", "").startswith("/data/data/com.termux/")
        return "aarch64-linux-android" if android else "aarch64-unknown-linux-gnu"
    if arch in {"amd64", "x86_64"}:
        return "x86_64-unknown-linux-gnu"
    raise ValueError(f"unsupported native target: {arch}")


def origin(url):
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise ValueError("native artifact URLs require HTTPS without userinfo/fragment")
    return parsed.scheme, parsed.hostname.lower(), parsed.port or 443


class SameOriginRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        if origin(request.full_url) != origin(newurl):
            raise ValueError("cross-origin native artifact redirect rejected")
        return super().redirect_request(request, fp, code, message, headers, newurl)


def fetch(url, limit, *, destination=None, trusted_origin=None):
    if origin(url) != (trusted_origin or origin(url)):
        raise ValueError("native artifact must share the manifest origin")
    headers = {"Accept": "application/octet-stream,application/json", "User-Agent": "CoreTTS-Native-Installer/1"}
    token = os.getenv("TTS_NATIVE_ARTIFACT_TOKEN")
    if token:
        headers["Authorization"] = "Bearer " + token
    request = urllib.request.Request(url, headers=headers)
    opener = urllib.request.build_opener(SameOriginRedirect())
    data = bytearray()
    count = 0
    with opener.open(request, timeout=20) as response:
        if origin(response.url) != origin(url):
            raise ValueError("native artifact origin changed")
        while True:
            chunk = response.read(min(65536, limit - count + 1))
            if not chunk:
                break
            count += len(chunk)
            if count > limit:
                raise ValueError("native artifact exceeds declared byte budget")
            if destination is None:
                data.extend(chunk)
            else:
                destination.write(chunk)
    return bytes(data) if destination is None else count


def load_manifest(source, *, manifest_sha256=None):
    source = str(source)
    remote = source.startswith("https://")
    if remote:
        raw = fetch(source, MAX_MANIFEST_BYTES)
    else:
        path = Path(source).expanduser()
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_MANIFEST_BYTES:
            raise ValueError("invalid local artifact manifest")
        raw = path.read_bytes()
        if len(raw) > MAX_MANIFEST_BYTES:
            raise ValueError("native manifest exceeds byte budget")
    if manifest_sha256 and hashlib.sha256(raw).hexdigest() != manifest_sha256.lower():
        raise ValueError("native manifest SHA-256 mismatch")
    manifest = json.loads(raw)
    validate_manifest(manifest)
    return manifest, source, remote


def validate_manifest(manifest, target=None):
    if not isinstance(manifest, dict) or manifest.get("schema") != SCHEMA:
        raise ValueError("unsupported native artifact manifest")
    if manifest.get("target") not in TARGET_MACHINES or target and manifest["target"] != target:
        raise ValueError("native artifact target mismatch")
    if not isinstance(manifest.get("version"), str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,63}", manifest["version"]):
        raise ValueError("invalid native version")
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list) or not 1 <= len(artifacts) <= len(ARTIFACT_NAMES):
        raise ValueError("invalid native artifact list")
    names, total = set(), 0
    for artifact in artifacts:
        if not isinstance(artifact, dict) or artifact.get("name") not in ARTIFACT_NAMES or artifact["name"] in names:
            raise ValueError("invalid/duplicate native artifact name")
        names.add(artifact["name"])
        size = artifact.get("bytes")
        if type(size) is not int or not 64 <= size <= MAX_ARTIFACT_BYTES:
            raise ValueError("invalid native artifact size")
        if not isinstance(artifact.get("sha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", artifact["sha256"]):
            raise ValueError("invalid native artifact SHA-256")
        if bool(artifact.get("url")) == bool(artifact.get("file")):
            raise ValueError("specify exactly one native artifact source")
        if artifact.get("url"):
            origin(artifact["url"])
        elif not isinstance(artifact["file"], str) or Path(artifact["file"]).name != artifact["file"] or artifact["file"] in {"", ".", ".."}:
            raise ValueError("native local artifact must use a basename")
        total += size
    if total > MAX_TOTAL_BYTES:
        raise ValueError("native release exceeds total byte budget")


def verify_elf(path, target):
    with path.open("rb") as handle:
        header = handle.read(64)
    if len(header) < 64 or header[:6] != b"\x7fELF\x02\x01" or struct.unpack_from("<H", header, 16)[0] not in {2, 3}:
        raise ValueError("native artifact is not a little-endian ELF64 executable")
    if struct.unpack_from("<H", header, 18)[0] != TARGET_MACHINES[target]:
        raise ValueError("native ELF architecture mismatch")


def verify_release(directory, manifest):
    for artifact in manifest["artifacts"]:
        path = directory / artifact["name"]
        if path.is_symlink() or not path.is_file() or path.stat().st_size != artifact["bytes"]:
            raise ValueError("native artifact size/type mismatch")
        if hashlib.sha256(path.read_bytes()).hexdigest() != artifact["sha256"]:
            raise ValueError("native artifact SHA-256 mismatch")
        verify_elf(path, manifest["target"])


def sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_json(path, value):
    temporary = path.with_name("." + path.name + "." + uuid.uuid4().hex)
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            os.chmod(temporary, 0o600)
            json.dump(value, handle, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def replace_link(path, value):
    if value is None:
        path.unlink(missing_ok=True)
    else:
        temporary = path.with_name("." + path.name + "." + uuid.uuid4().hex)
        try:
            temporary.symlink_to(value)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
    sync_directory(path.parent)


def read_pointer(path, root):
    if not path.is_symlink():
        if path.exists():
            raise ValueError("native release pointer must be a symlink")
        return None
    value = os.readlink(path)
    resolved = path.resolve(strict=True)
    releases = root / "releases"
    if resolved.parent != releases.resolve() or not re.fullmatch(r"[0-9a-f]{64}", resolved.name):
        raise ValueError("native release pointer escapes release root")
    return value


class UnixHTTP(http.client.HTTPConnection):
    def __init__(self, path):
        super().__init__("native", timeout=2)
        self.path = str(path)

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(2)
        self.sock.connect(self.path)


def health_check(directory, manifest):
    worker = directory / "tts-worker"
    if not worker.is_file():
        # WORLDLINE helper needs the installed external native library. Its
        # real synthesis smoke belongs to the guest/device validation gate.
        return {"elf_verified": True, "worker_health": "not_applicable"}
    # A fresh, short /tmp path also works when the installation root is longer
    # than the Unix socket address limit. No persistent cache is modified.
    with tempfile.TemporaryDirectory(prefix="tts-native-check-") as temporary:
        state = Path(temporary)
        command = [str(worker), "--socket", str(state / "s"), "--cache-dir", str(state / "cache"), "--owner-stdin"]
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, close_fds=True, start_new_session=True)
        try:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise RuntimeError("candidate native worker exited")
                connection = UnixHTTP(state / "s")
                try:
                    connection.request("GET", "/health")
                    response = connection.getresponse()
                    result = json.loads(response.read(65537))
                    if response.status == 200 and result.get("native") is True and result.get("protocol") == 1 and result.get("pid") == process.pid:
                        return {"elf_verified": True, "worker_health": "ready", "native_version": result.get("native_version")}
                except (OSError, ValueError, http.client.HTTPException):
                    time.sleep(0.025)
                finally:
                    connection.close()
            raise RuntimeError("candidate native worker health timed out")
        finally:
            if process.stdin is not None:
                process.stdin.close()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, __import__("signal").SIGKILL)
                process.wait(timeout=3)


def recover(root):
    journal = root / "transaction.json"
    if not journal.exists():
        return
    state = json.loads(journal.read_text("utf-8"))
    if state.get("schema") != SCHEMA:
        raise ValueError("unknown interrupted native installation")
    for key in ("current", "previous"):
        value = state.get(key)
        if value is not None:
            candidate = (root / value).resolve(strict=True)
            if candidate.parent != (root / "releases").resolve() or not re.fullmatch(r"[0-9a-f]{64}", candidate.name):
                raise ValueError("invalid interrupted native release pointer")
    replace_link(root / "current", state.get("current"))
    replace_link(root / "previous", state.get("previous"))
    journal.unlink()
    sync_directory(root)


def install(source, root, *, target=None, manifest_sha256=None, check=health_check):
    manifest, source, remote = load_manifest(source, manifest_sha256=manifest_sha256)
    validate_manifest(manifest, target or current_target())
    root = Path(root).expanduser()
    if root.is_symlink():
        raise ValueError("native runtime root cannot be a symlink")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root = root.resolve()
    releases = root / "releases"
    releases.mkdir(exist_ok=True, mode=0o700)
    if releases.is_symlink():
        raise ValueError("native releases root cannot be a symlink")
    identity = hashlib.sha256(json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    final = releases / identity
    with (root / "install.lock").open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        recover(root)
        old_current = read_pointer(root / "current", root)
        old_previous = read_pointer(root / "previous", root)
        if final.is_symlink() or final.exists() and not final.is_dir():
            raise ValueError("native release directory must not be a symlink/file")
        if not final.exists():
            staging = Path(tempfile.mkdtemp(prefix=".staging-", dir=releases))
            try:
                for artifact in manifest["artifacts"]:
                    destination = staging / artifact["name"]
                    with destination.open("xb") as output:
                        if remote:
                            if not artifact.get("url"):
                                raise ValueError("remote manifest requires artifact URLs")
                            fetch(artifact["url"], artifact["bytes"], destination=output, trusted_origin=origin(source))
                        else:
                            if not artifact.get("file"):
                                raise ValueError("local manifest requires local artifacts")
                            source_file = Path(source).expanduser().resolve().parent / artifact["file"]
                            if source_file.is_symlink() or not source_file.is_file() or source_file.stat().st_size != artifact["bytes"]:
                                raise ValueError("invalid local native artifact")
                            with source_file.open("rb") as incoming:
                                shutil.copyfileobj(incoming, output, 65536)
                        output.flush()
                        os.fsync(output.fileno())
                    destination.chmod(0o755)
                verify_release(staging, manifest)
                atomic_json(staging / "manifest.json", manifest)
                check(staging, manifest)
                os.replace(staging, final)
                sync_directory(releases)
            finally:
                if staging.exists():
                    shutil.rmtree(staging)
        verify_release(final, manifest)
        atomic_json(root / "transaction.json", {"schema": SCHEMA, "current": old_current, "previous": old_previous})
        transaction_closed = False
        try:
            relative = str(Path("releases") / identity)
            if old_current and old_current != relative:
                replace_link(root / "previous", old_current)
            replace_link(root / "current", relative)
            evidence = check(root / "current", manifest)
            transaction_closed = True
        except BaseException:
            replace_link(root / "current", old_current)
            replace_link(root / "previous", old_previous)
            transaction_closed = True
            raise
        finally:
            # If even pointer restoration failed, retain the durable journal;
            # the next installation must recover it before touching anything.
            if transaction_closed:
                (root / "transaction.json").unlink(missing_ok=True)
                sync_directory(root)
        result = {"ok": True, "schema": SCHEMA, "release": identity, "target": manifest["target"],
                  "version": manifest["version"], "health": evidence, "restart_required": True}
        atomic_json(root / "status.json", result)
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", help="local manifest or HTTPS URL")
    parser.add_argument("--runtime-root", default=str(Path.home() / ".local/share/tts-native"))
    parser.add_argument("--target", choices=sorted(TARGET_MACHINES))
    parser.add_argument("--manifest-sha256", help="trusted manifest digest supplied by the release channel")
    args = parser.parse_args()
    result = install(args.manifest, args.runtime_root, target=args.target, manifest_sha256=args.manifest_sha256)
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
