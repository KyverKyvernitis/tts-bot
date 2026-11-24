"""Piper CLI local for Termux: one process per request, PCM WAV output.

This adapter intentionally does not keep a model resident or stream playback.
Compatible with the proven gyroing Android/Termux CLI (-m/-f/-- text).
"""
from __future__ import annotations

import array
import json
import math
import os
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


def synthesize_to_wav(text: str, model_path: str, config_path: str, destination: Path, *, timeout_seconds: float = 20.0) -> None:
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
