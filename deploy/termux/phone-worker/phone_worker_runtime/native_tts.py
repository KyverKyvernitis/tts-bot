"""Platform-only supervision and opaque HTTP relay for the native TTS child.

No providers, cache decisions, text processing or audio decoding live here.
Importing the module neither reads private settings nor starts a process.
"""
from __future__ import annotations

import atexit
import contextlib
import http.client
import hashlib
import json
import os
from pathlib import Path
import select
import signal
import socket
import stat
import subprocess
import threading
import time


def enabled():
    return os.getenv("TTS_NATIVE_WORKER", "0").strip().lower() in {"1", "true", "yes", "on"}


_HEAVY_LOCK = None


def configure_heavy_lock(lock):
    """Share the platform lease already used by builds and maintenance."""
    global _HEAVY_LOCK
    _HEAVY_LOCK = lock


class _ResourceBroker:
    def __init__(self, path, lock):
        self.path, self.lock = Path(path), lock
        self.closed = threading.Event()
        self.connections = set()
        self.connections_lock = threading.Lock()
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            self.listener.bind(str(path))
            os.chmod(path, 0o600)
            self.listener.listen(4)
            self.listener.settimeout(0.5)
        except BaseException:
            self.listener.close()
            raise
        self.thread = threading.Thread(target=self._accept, name="native-platform-leases", daemon=True)
        self.thread.start()

    def _accept(self):
        while not self.closed.is_set():
            try:
                connection, _ = self.listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            with self.connections_lock:
                if self.closed.is_set() or len(self.connections) >= 4:
                    connection.close()
                    continue
                self.connections.add(connection)
            threading.Thread(target=self._lease, args=(connection,), daemon=True).start()

    def _lease(self, connection):
        acquired = False
        try:
            connection.settimeout(2)
            request = bytearray()
            while b"\n" not in request and len(request) < 32:
                part = connection.recv(32 - len(request))
                if not part:
                    return
                request.extend(part)
            if request != b"acquire\n":
                return
            acquired = self.lock.acquire(blocking=False)
            connection.sendall(b"ok\n" if acquired else b"busy\n")
            if acquired:
                # The native physical job owns this connection. EOF is its
                # release receipt, including after cancellation or process death.
                connection.settimeout(None)
                release = connection.recv(1)
                self.lock.release()
                acquired = False
                if release:
                    connection.sendall(b"released\n")
        except OSError:
            pass
        finally:
            if acquired:
                self.lock.release()
            connection.close()
            with self.connections_lock:
                self.connections.discard(connection)

    def close(self):
        self.closed.set()
        self.listener.close()
        with self.connections_lock:
            for connection in self.connections:
                with contextlib.suppress(OSError):
                    connection.shutdown(socket.SHUT_RDWR)
        self.thread.join(timeout=1)
        with contextlib.suppress(OSError):
            self.path.unlink()


class _UnixHTTP(http.client.HTTPConnection):
    def __init__(self, path, timeout):
        super().__init__("native-tts", timeout=timeout)
        self.path = str(path)

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        try:
            self.sock.connect(self.path)
        except BaseException:
            self.sock.close()
            self.sock = None
            raise


class Supervisor:
    def __init__(self, *, binary=None, socket_path=None, cache=None, worker_version="", heavy_lock=None):
        self._binary = binary
        self._socket_path = socket_path
        self._cache = cache
        self.worker_version = worker_version
        self.process = None
        self._lock = threading.RLock()
        self._owned_socket = None
        self._closed = False
        self.heavy_lock = _HEAVY_LOCK if heavy_lock is None else heavy_lock
        self._broker = None

    def binary(self):
        root = Path(os.getenv("TTS_NATIVE_RUNTIME_ROOT") or Path.home() / ".local/share/tts-native")
        return Path(self._binary or os.getenv("TTS_NATIVE_WORKER_BINARY") or root / "current/tts-worker").expanduser().resolve(strict=True)

    def _verify_binary(self, binary):
        expected = os.getenv("TTS_NATIVE_WORKER_SHA256", "").strip().lower()
        if not self._binary and not os.getenv("TTS_NATIVE_WORKER_BINARY"):
            manifest_path = binary.parent / "manifest.json"
            if manifest_path.is_symlink() or not manifest_path.is_file() or manifest_path.stat().st_size > 256 * 1024:
                raise RuntimeError("manifesto do serviço TTS nativo ausente/inválido")
            manifest = json.loads(manifest_path.read_text("utf-8"))
            if manifest.get("schema") != "tts-native-artifact-v1":
                raise RuntimeError("manifesto TTS nativo incompatível")
            artifact = next((item for item in manifest.get("artifacts", []) if item.get("name") == "tts-worker"), None)
            if not artifact or binary.stat().st_size != artifact.get("bytes"):
                raise RuntimeError("tamanho do serviço TTS nativo diverge do manifesto")
            expected = str(artifact.get("sha256") or "")
        if expected:
            digest = hashlib.sha256()
            with binary.open("rb") as source:
                for chunk in iter(lambda: source.read(65536), b""):
                    digest.update(chunk)
            if len(expected) != 64 or digest.hexdigest() != expected:
                raise RuntimeError("SHA-256 do serviço TTS nativo diverge do manifesto")

    def socket_path(self):
        if self._socket_path:
            return Path(self._socket_path)
        state = Path(os.getenv("PHONE_WORKER_STATE_DIR") or Path.home() / ".local/state/core-worker-phone-worker")
        path = Path(os.getenv("TTS_NATIVE_WORKER_SOCKET") or state / f"tts-native-{os.getpid()}.sock").expanduser()
        if len(os.fsencode(path)) > 103:
            raise RuntimeError("caminho do socket TTS nativo excede limite Unix")
        return path

    def _health(self, timeout=0.5):
        connection = _UnixHTTP(self.socket_path(), timeout)
        try:
            connection.request("GET", "/health")
            response = connection.getresponse()
            raw = response.read(65537)
            if response.status != 200 or len(raw) > 65536:
                raise RuntimeError("health TTS nativo inválido")
            result = json.loads(raw)
            if not isinstance(result, dict) or result.get("protocol") != 1 or result.get("native") is not True:
                raise RuntimeError("protocolo TTS nativo incompatível")
            if self.process is not None and result.get("pid") != self.process.pid:
                raise RuntimeError("socket TTS pertence a outro processo")
            return result
        finally:
            connection.close()

    def ensure(self):
        with self._lock:
            if self._closed:
                raise RuntimeError("supervisor TTS nativo encerrado")
            if self.process is not None and self.process.poll() is None:
                return self.socket_path()
            self._stop_locked()
            binary = self.binary()
            if not binary.is_file() or not os.access(binary, os.X_OK):
                raise RuntimeError("binário TTS nativo ausente ou não executável")
            self._verify_binary(binary)
            path = self.socket_path()
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            # Never unlink a socket which was not created by this supervisor.
            if path.exists() or path.is_symlink():
                raise RuntimeError("socket TTS nativo já existe; use caminho privado livre")
            cache = Path(self._cache or os.getenv("PHONE_WORKER_TTS_CACHE_DIR") or Path.home() / "phone-worker/cache/tts").expanduser()
            child_environment = os.environ.copy()
            if self.heavy_lock is not None:
                if self._broker is None:
                    broker_path = path.with_name(f"tts-lease-{os.getpid()}.sock")
                    self._broker = _ResourceBroker(broker_path, self.heavy_lock)
                child_environment["TTS_NATIVE_HEAVY_LEASE_SOCKET"] = str(self._broker.path)
            command = [str(binary), "--socket", str(path), "--cache-dir", str(cache), "--owner-stdin"]
            if self.worker_version:
                command += ["--worker-version", self.worker_version]
            self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, close_fds=True, start_new_session=True, env=child_environment)
            self._owned_socket = path
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if self.process.poll() is not None:
                    break
                try:
                    self._health()
                    if self._socket_path is None:
                        os.environ["TTS_NATIVE_WORKER_SOCKET"] = str(path)
                    return path
                except (OSError, ValueError, RuntimeError, http.client.HTTPException):
                    time.sleep(0.025)
            self._stop_locked()
            raise RuntimeError("serviço TTS nativo não confirmou saúde")

    def _stop_locked(self):
        process, self.process = self.process, None
        if process is not None:
            if process.stdin is not None:
                with contextlib.suppress(OSError):
                    process.stdin.close()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=2)
        if self._owned_socket is not None:
            with contextlib.suppress(OSError):
                if stat.S_ISSOCK(self._owned_socket.lstat().st_mode):
                    self._owned_socket.unlink()
            self._owned_socket = None

    def close(self):
        with self._lock:
            self._closed = True
            self._stop_locked()
            if self._broker is not None:
                self._broker.close()
                self._broker = None


_SUPERVISOR = None
_SUPERVISOR_LOCK = threading.Lock()


def supervisor(*, worker_version=""):
    global _SUPERVISOR
    with _SUPERVISOR_LOCK:
        if _SUPERVISOR is None:
            _SUPERVISOR = Supervisor(worker_version=worker_version)
            atexit.register(_SUPERVISOR.close)
        return _SUPERVISOR


def request_json(body=None, *, path="/task", method="POST", timeout=125, max_bytes=48 * 1024 * 1024, worker_version=""):
    owner = supervisor(worker_version=worker_version)
    connection = _UnixHTTP(owner.ensure(), timeout)
    try:
        raw = None if body is None else json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        connection.request(method, path, body=raw, headers={"Content-Type": "application/json"})
        response = connection.getresponse()
        raw_response = response.read(max_bytes + 1)
        if len(raw_response) > max_bytes:
            raise RuntimeError("resposta TTS nativa excedeu limite")
        result = json.loads(raw_response)
        if not isinstance(result, dict):
            raise RuntimeError("resposta TTS nativa precisa ser objeto")
        if response.status >= 400:
            raise RuntimeError(str(result.get("error") or f"TTS nativo HTTP {response.status}"))
        return result
    finally:
        connection.close()


def proxy_http(handler, *, method, path, body=None, worker_version=""):
    """Relay HTTP bytes unchanged; never parse/reframe a single audio chunk.

    A native disconnect remains an incomplete chunked response. A client
    disconnect closes the Unix connection, cancelling its physical provider.
    """
    owner = supervisor(worker_version=worker_version)
    target = owner.ensure()
    if body is not None:
        request_body = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        length = len(request_body)
    else:
        request_body = None
        if handler.headers.get("Transfer-Encoding"):
            raise ValueError("use Content-Length para TTS nativo")
        values = handler.headers.get_all("Content-Length", [])
        if len(values) > 1:
            raise ValueError("Content-Length duplicado")
        length = int(values[0]) if values else 0
    if not 0 <= length <= handler.max_body_bytes:
        raise ValueError("tamanho de requisição TTS inválido")
    headers = [f"{method} {path} HTTP/1.1", "Host: native-tts", "Connection: close", f"Content-Length: {length}"]
    for name, value in handler.headers.items():
        if name.lower() == "content-type" or name.lower().startswith("x-core-"):
            if any(character in name + value for character in "\r\n"):
                raise ValueError("header TTS inválido")
            headers.append(f"{name}: {value}")
    upstream = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    upstream.settimeout(10)
    emitted = False
    try:
        upstream.connect(str(target))
        upstream.sendall(("\r\n".join(headers) + "\r\n\r\n").encode("ascii"))
        if request_body is not None:
            upstream.sendall(request_body)
        else:
            remaining = length
            deadline = time.monotonic() + 20
            handler.connection.settimeout(10)
            while remaining:
                if time.monotonic() >= deadline:
                    raise TimeoutError("upload TTS nativo excedeu prazo")
                chunk = handler.rfile.read(min(65536, remaining))
                if not chunk:
                    raise ValueError("requisição TTS incompleta")
                upstream.sendall(chunk)
                remaining -= len(chunk)
            handler._request_body_consumed = True
        # Validate only the HTTP envelope. The body remains opaque and bounded
        # by socket backpressure, irrespective of audio size or frame count.
        head = bytearray()
        while b"\r\n\r\n" not in head:
            chunk = upstream.recv(4096)
            if not chunk:
                raise RuntimeError("serviço TTS fechou antes dos headers")
            head.extend(chunk)
            if len(head) > 20 * 1024:
                raise RuntimeError("headers TTS grandes demais")
        prefix, tail = bytes(head).split(b"\r\n\r\n", 1)
        status = prefix.split(b"\r\n", 1)[0].split()
        if len(status) < 2 or status[0] != b"HTTP/1.1" or not status[1].isdigit():
            raise RuntimeError("resposta HTTP nativa inválida")
        handler.close_connection = True
        handler.wfile.flush()
        handler.connection.settimeout(10)
        handler.connection.sendall(prefix + b"\r\n\r\n" + tail)
        emitted = True
        deadline = time.monotonic() + max(125, handler.job_timeout + 5)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("resposta TTS nativa excedeu prazo")
            readable, _, _ = select.select([upstream, handler.connection], [], [], min(10, remaining))
            if handler.connection in readable:
                # HTTP keep-alive/pipelining is unsupported on this endpoint;
                # either EOF or a new request releases the native stream.
                return
            if upstream in readable:
                chunk = upstream.recv(65536)
                if not chunk:
                    return
                handler.connection.sendall(chunk)
    except (OSError, ValueError, RuntimeError, http.client.HTTPException):
        if not emitted:
            raise
        handler.close_connection = True
    finally:
        upstream.close()
