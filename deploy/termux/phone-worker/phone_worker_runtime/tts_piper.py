"""Piper in Termux: isolated resident C engine with progressive PCM and CLI recovery."""
from __future__ import annotations

import array
import atexit
import ctypes
import json
import math
import os
import select
import struct
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import wave
from pathlib import Path

# One CPU-intensive model at a time; jobs rejected if the budget is exhausted.
_SYNTH_SLOT = threading.BoundedSemaphore(1)


def _binary() -> Path:
    configured = os.environ.get("PHONE_WORKER_PIPER_BIN", "").strip()
    if configured:
        path = Path(configured).expanduser()
        if not path.is_file() or not os.access(path, os.X_OK):
            raise RuntimeError(f"binário Piper inválido: {path}")
        return path

    # Prefer the exact Termux ARM64 build already tested on this device.
    package = Path.home() / "piper-termux-teste" / "pkg"
    matches = sorted(path for path in package.rglob("piper") if path.is_file() and os.access(path, os.X_OK)) if package.is_dir() else []
    if matches:
        return matches[0]

    resolved = shutil.which("piper")
    if resolved:
        return Path(resolved)
    raise RuntimeError("Piper CLI não encontrado; preserve ~/piper-termux-teste/pkg ou defina PHONE_WORKER_PIPER_BIN")


def _environment(binary: Path, model: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["PIPER_VOICE_PATH"] = str(model.parent)
    if not env.get("ESPEAK_DATA_PATH"):
        prefix = Path(os.environ.get("PREFIX", "/data/data/com.termux/files/usr"))
        candidate = prefix / "share" / "espeak-ng-data"
        if candidate.is_dir():
            env["ESPEAK_DATA_PATH"] = str(candidate)

    package = Path.home() / "piper-termux-teste" / "pkg"
    if package.is_dir() and package in binary.parents:
        folders = sorted({str(path.parent) for path in package.rglob("*.so*") if path.is_file()})
        if folders:
            env["LD_LIBRARY_PATH"] = ":".join(folders + [str(Path(os.environ.get('PREFIX', '/data/data/com.termux/files/usr')) / 'lib'), env.get("LD_LIBRARY_PATH", "")]).rstrip(":")
    return env


def _synthesize_cli_to_wav(text: str, model_path: str, config_path: str, destination: Path, *, timeout_seconds: float = 20.0) -> None:
    """Generate and validate a 16-bit mono WAV from the Termux Piper float32 output."""
    model = Path(model_path).expanduser()
    config = Path(config_path).expanduser() if config_path else Path(str(model) + ".json")
    if not model.is_file() or model.stat().st_size == 0 or not config.is_file():
        raise RuntimeError("modelo ou configuração Piper ausente")
    metadata = json.loads(config.read_text(encoding="utf-8"))
    sample_rate = int(metadata["audio"]["sample_rate"])
    if not (8000 <= sample_rate <= 96000):
        raise RuntimeError("taxa de amostragem Piper inválida")
    text = str(text).strip()
    if not text:
        raise ValueError("texto Piper vazio")
    if not (1 <= len(text) <= 1600):
        raise ValueError("texto excede limite Piper")
    try:
        timeout = float(timeout_seconds)
    except (ValueError, TypeError):
        raise ValueError("timeout Piper inválido") from None
    if not math.isfinite(timeout) or timeout < 1 or timeout > 120:
        raise ValueError("timeout Piper fora do intervalo")
    # Concurrency stays bounded to protect the phone; wait for a brief queue
    # slot without exhausting the full request budget.
    started = time.monotonic()
    if not _SYNTH_SLOT.acquire(timeout=max(0.0, timeout - 2.0)):
        raise RuntimeError("Piper ocupado; tempo de espera na fila excedido")
    try:
        remaining = timeout - (time.monotonic() - started)
        if remaining < 1:
            raise RuntimeError("Piper sem tempo restante após aguardar a fila")
        binary = _binary()
        destination = Path(destination)
        with tempfile.TemporaryDirectory(prefix="piper-local-") as tmp:
            raw_path = Path(tmp) / "output.raw"
            command = [str(binary), "-m", model.stem, "-f", str(raw_path), "--", text]
            try:
                result = subprocess.run(command, env=_environment(binary, model), stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, timeout=remaining, check=False)
            except subprocess.TimeoutExpired as exc:
                raise RuntimeError(f"Piper excedeu {timeout:g}s") from exc
            if result.returncode:
                stderr = (result.stderr or b"").decode("utf-8", "replace")[-400:]
                raise RuntimeError(f"Piper retornou código {result.returncode}: {stderr}")
            if not raw_path.is_file():
                raise RuntimeError("Piper não gerou o arquivo de áudio")
            if raw_path.stat().st_size > 16 * 1024 * 1024:
                raise RuntimeError("áudio bruto Piper excedeu 16 MiB")
            data = raw_path.read_bytes()
            if data.startswith(b"RIFF") and data[8:12] == b"WAVE":
                # Some builds produce WAV directly. Check basic structure.
                with wave.open(str(raw_path), "rb") as handle:
                    if handle.getnchannels() != 1 or handle.getnframes() < 1:
                        raise RuntimeError("WAV Piper inválido")
                shutil.copyfile(raw_path, destination)
                return
            if len(data) < 4000 or len(data) % 4:
                raise RuntimeError("PCM float32 do Piper está vazio ou truncado")
            floats = array.array("f")
            floats.frombytes(data)
            if sys.byteorder != "little":
                floats.byteswap()
            pcm = array.array("h", (
                int(max(-1.0, min(1.0, v if math.isfinite(v) else 0.0)) * 32767)
                for v in floats
            ))
            if sys.byteorder != "little":
                pcm.byteswap()
            with wave.open(str(destination), "wb") as handle:
                handle.setnchannels(1)
                handle.setsampwidth(2)
                handle.setframerate(sample_rate)
                handle.writeframes(pcm.tobytes())
        if destination.stat().st_size < 44:
            raise RuntimeError("Piper gerou WAV vazio")
    finally:
        _SYNTH_SLOT.release()


# Piper's public C API (piper.h) exposes streaming via piper_synthesize_next.
# Keep that native library in a separate Python process: a native crash cannot
# bring down the HTTP/control-plane process. The child retains the voice model
# across all requests. The framing is private to the local stdin/stdout pipes.
_FRAME_MAX = 8 * 1024 * 1024
_RESIDENT = None
_RESIDENT_LOCK = threading.Lock()


def _enabled(name: str, default: bool = True) -> bool:
    value = os.getenv(name, "true" if default else "false")
    return str(value).strip().lower() not in {"0", "no", "false", "off"}


def _shared_library() -> Path:
    value = os.getenv("PHONE_WORKER_PIPER_LIB", "").strip()
    if value:
        target = Path(value).expanduser()
        if not target.is_file():
            raise RuntimeError("PHONE_WORKER_PIPER_LIB aponta para arquivo inexistente")
        return target
    root = Path.home() / "piper-termux-teste" / "pkg"
    for target in sorted(root.rglob("libpiper.so")) if root.is_dir() else ():
        if target.is_file():
            return target
    raise RuntimeError("libpiper.so não encontrada na instalação Termux")


def _validated(model_path: str, config_path: str, text: str, timeout_seconds: float):
    model = Path(model_path).expanduser()
    config = Path(config_path).expanduser() if config_path else Path(str(model) + ".json")
    if not model.is_file() or model.stat().st_size == 0 or not config.is_file():
        raise RuntimeError("modelo ou configuração Piper ausente")
    rate = int(json.loads(config.read_text(encoding="utf-8"))["audio"]["sample_rate"])
    if rate < 8000 or rate > 96000:
        raise RuntimeError("sample_rate inválido")
    text = str(text).strip()
    if not 1 <= len(text) <= 1600:
        raise ValueError("texto Piper vazio ou acima de 1600 caracteres")
    timeout = float(timeout_seconds)
    if not math.isfinite(timeout) or not 1 <= timeout <= 120:
        raise ValueError("timeout Piper inválido")
    return model, config, rate, text, timeout


def _send_frame(stream, tag: bytes, payload: bytes = b"") -> None:
    if len(payload) > _FRAME_MAX:
        raise RuntimeError("bloco Piper excedeu o limite")
    stream.write(tag + struct.pack("!I", len(payload)) + payload)
    stream.flush()


def _read_frame(stream, deadline: float) -> tuple[bytes, bytes]:
    fd = stream.fileno()

    def read_exact(amount):
        result = bytearray()
        while len(result) < amount:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([fd], [], [], remaining)[0]:
                raise TimeoutError("Piper residente excedeu o prazo")
            chunk = os.read(fd, amount - len(result))
            if not chunk:
                raise RuntimeError("Piper residente encerrou o pipe")
            result.extend(chunk)
        return bytes(result)

    header = read_exact(5)
    length = struct.unpack("!I", header[1:])[0]
    if length > _FRAME_MAX:
        raise RuntimeError("quadro Piper excedeu limite")
    return header[:1], read_exact(length)


class _PiperResident:
    def __init__(self, model: Path, config: Path, rate: int):
        self.model = model
        self.config = config
        self.rate = rate
        self.proc = None

    def start(self, deadline: float) -> None:
        if self.proc is not None and self.proc.poll() is None:
            return
        lib = _shared_library()
        env = _environment(_binary(), self.model)
        env["PHONE_WORKER_PIPER_LIB"] = str(lib)
        self.proc = subprocess.Popen(
            [sys.executable, "-u", str(Path(__file__).resolve()), "--resident", str(self.model), str(self.config)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            env=env, bufsize=0,
        )
        try:
            tag, data = _read_frame(self.proc.stdout, deadline)
            if tag != b"R" or len(data) != 4 or struct.unpack("!I", data)[0] != self.rate:
                raise RuntimeError("Piper residente não inicializou: " + data.decode("utf-8", "replace")[:160])
        except BaseException:
            self.stop()
            raise

    def stop(self) -> None:
        proc, self.proc = self.proc, None
        if proc is None:
            return
        if proc.poll() is None:
            proc.kill()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass
        for stream in (proc.stdin, proc.stdout):
            if stream is not None:
                stream.close()

    def chunks(self, text: str, deadline: float, max_audio_bytes: int):
        proc = self.proc
        if proc is None or proc.poll() is not None:
            raise RuntimeError("Piper residente não iniciou")
        payload = text.encode("utf-8")
        try:
            proc.stdin.write(struct.pack("!I", len(payload)) + payload)
            proc.stdin.flush()
            total = 0
            while True:
                tag, chunk = _read_frame(proc.stdout, deadline)
                if tag == b"E":
                    if total < 2:
                        raise RuntimeError("Piper residente gerou áudio vazio")
                    return
                if tag == b"X":
                    raise RuntimeError("Piper residente: " + chunk.decode("utf-8", "replace")[:240])
                if tag != b"D" or not chunk or len(chunk) % 2:
                    raise RuntimeError("quadro de áudio Piper inválido")
                total += len(chunk)
                if total > max_audio_bytes:
                    raise RuntimeError("Piper excedeu limite de áudio")
                yield chunk
        except BaseException:
            self.stop()  # Never reuse a child with an incomplete frame sequence.
            raise


def _resident_for(model: Path, config: Path, rate: int, deadline: float):
    global _RESIDENT
    with _RESIDENT_LOCK:
        if _RESIDENT is not None and (_RESIDENT.model != model or _RESIDENT.config != config or _RESIDENT.rate != rate):
            _RESIDENT.stop()
            _RESIDENT = None
        if _RESIDENT is None:
            _RESIDENT = _PiperResident(model, config, rate)
        _RESIDENT.start(deadline)
        return _RESIDENT


def stop_resident() -> None:
    global _RESIDENT
    with _RESIDENT_LOCK:
        if _RESIDENT is not None:
            _RESIDENT.stop()
            _RESIDENT = None


atexit.register(stop_resident)


def warmup() -> bool:
    """Load the default model once, without synthesizing text or touching Discord."""
    if not _enabled("PHONE_WORKER_PIPER_RESIDENT_ENABLED"):
        return False
    model = Path(os.getenv("PHONE_WORKER_PIPER_MODEL", "") or
                 str(Path.home() / "piper-termux-teste/voices/pt_BR-faber-medium.onnx")).expanduser()
    config = Path(os.getenv("PHONE_WORKER_PIPER_CONFIG", "") or str(model) + ".json").expanduser()
    try:
        model, config, rate, _, _ = _validated(str(model), str(config), "teste", 30)
        if not _SYNTH_SLOT.acquire(timeout=0.5):
            return False
        try:
            _resident_for(model, config, rate, time.monotonic() + 30)
            return True
        finally:
            _SYNTH_SLOT.release()
    except Exception:
        # Optional engine; keep the control plane up on a missing native ABI.
        return False


def stream_pcm(text: str, model_path: str, config_path: str, *, timeout_seconds: float = 20.0,
               max_audio_bytes: int = 8 * 1024 * 1024):
    """Yield (sample_rate, signed-16-bit mono PCM chunk) from the resident model.

    The model remains resident after successful completion. Caller must close
    this iterator on disconnect so its serialization slot is released.
    """
    model, config, rate, text, timeout = _validated(model_path, config_path, text, timeout_seconds)
    if not _enabled("PHONE_WORKER_PIPER_RESIDENT_ENABLED"):
        raise RuntimeError("Piper residente desativado")
    deadline = time.monotonic() + timeout
    if not _SYNTH_SLOT.acquire(timeout=max(0.0, timeout - 1.0)):
        raise RuntimeError("Piper ocupado; fila excedeu o prazo")
    completed = False
    resident = None
    try:
        resident = _resident_for(model, config, rate, deadline)
        for chunk in resident.chunks(text, deadline, max_audio_bytes):
            yield rate, chunk
        completed = True
    finally:
        if not completed and resident is not None:
            resident.stop()
        _SYNTH_SLOT.release()


def synthesize_to_wav(text: str, model_path: str, config_path: str, destination: Path, *, timeout_seconds: float = 20.0) -> None:
    """Prefer retained native model; retain the proven CLI as safe fallback."""
    if _enabled("PHONE_WORKER_PIPER_RESIDENT_ENABLED"):
        try:
            source = stream_pcm(text, model_path, config_path, timeout_seconds=timeout_seconds)
            try:
                sample_rate, first = next(source)
                with wave.open(str(destination), "wb") as output:
                    output.setnchannels(1)
                    output.setsampwidth(2)
                    output.setframerate(sample_rate)
                    output.writeframesraw(first)
                    for rate, pcm in source:
                        if rate != sample_rate:
                            raise RuntimeError("Piper mudou a taxa de amostragem")
                        output.writeframesraw(pcm)
            finally:
                source.close()
            if destination.stat().st_size >= 44:
                return
            raise RuntimeError("Piper residente gerou WAV vazio")
        except (RuntimeError, OSError, TimeoutError, StopIteration):
            Path(destination).unlink(missing_ok=True)
            if not _enabled("PHONE_WORKER_PIPER_CLI_FALLBACK", True):
                raise
    _synthesize_cli_to_wav(text, model_path, config_path, destination, timeout_seconds=timeout_seconds)


class _AudioChunk(ctypes.Structure):
    _fields_ = [("samples", ctypes.POINTER(ctypes.c_float)), ("num_samples", ctypes.c_size_t),
                ("sample_rate", ctypes.c_int), ("is_last", ctypes.c_bool)]


def _resident_entry(model_path: str, config_path: str):
    lib = ctypes.CDLL(str(_shared_library()))
    lib.piper_create.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p]
    lib.piper_create.restype = ctypes.c_void_p
    lib.piper_free.argtypes = [ctypes.c_void_p]
    lib.piper_synthesize_start.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_void_p]
    lib.piper_synthesize_start.restype = ctypes.c_int
    lib.piper_synthesize_next.argtypes = [ctypes.c_void_p, ctypes.POINTER(_AudioChunk)]
    lib.piper_synthesize_next.restype = ctypes.c_int
    config = json.loads(Path(config_path).read_text(encoding="utf-8"))
    sample_rate = int(config["audio"]["sample_rate"])
    espeak = os.getenv("ESPEAK_DATA_PATH", "").encode("utf-8") or None
    voice = lib.piper_create(os.fsencode(model_path), os.fsencode(config_path), espeak)
    if not voice:
        raise RuntimeError("piper_create falhou")
    reader = sys.stdin.buffer
    writer = sys.stdout.buffer
    _send_frame(writer, b"R", struct.pack("!I", sample_rate))
    try:
        while True:
            header = reader.read(4)
            if not header:
                return
            if len(header) != 4:
                return
            size = struct.unpack("!I", header)[0]
            if size < 1 or size > 1600 * 4:
                _send_frame(writer, b"X", b"tamanho de texto invalido")
                continue
            text = reader.read(size)
            if len(text) != size:
                return
            try:
                if lib.piper_synthesize_start(voice, text, None) != 0:
                    raise RuntimeError("piper_synthesize_start falhou")
                for _ in range(100000):
                    chunk = _AudioChunk()
                    result = lib.piper_synthesize_next(voice, ctypes.byref(chunk))
                    if result == 1:
                        break
                    if result != 0:
                        raise RuntimeError(f"piper_synthesize_next={result}")
                    if chunk.sample_rate != sample_rate or chunk.num_samples > 2_000_000:
                        raise RuntimeError("bloco nativo invalido")
                    if chunk.num_samples:
                        raw = ctypes.string_at(chunk.samples, chunk.num_samples * 4)
                        floats = array.array("f")
                        floats.frombytes(raw)
                        if sys.byteorder != "little":
                            floats.byteswap()
                        pcm = array.array("h", (int(max(-1.0, min(1.0, value if math.isfinite(value) else 0.0)) * 32767) for value in floats))
                        if sys.byteorder != "little":
                            pcm.byteswap()
                        data = pcm.tobytes()
                        for i in range(0, len(data), 65536):
                            _send_frame(writer, b"D", data[i:i + 65536])
                    if chunk.is_last:
                        break
                else:
                    raise RuntimeError("limite de blocos Piper excedido")
                _send_frame(writer, b"E")
            except Exception as exc:
                _send_frame(writer, b"X", str(exc).encode("utf-8")[:500])
    finally:
        lib.piper_free(voice)


if __name__ == "__main__" and len(sys.argv) == 4 and sys.argv[1] == "--resident":
    try:
        _resident_entry(sys.argv[2], sys.argv[3])
    except Exception as exc:
        try:
            _send_frame(sys.stdout.buffer, b"X", str(exc).encode("utf-8")[:500])
        except Exception:
            pass
        raise SystemExit(1)
