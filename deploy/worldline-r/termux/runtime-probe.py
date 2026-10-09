#!/usr/bin/env python3
"""Probe the pinned native WORLDLINE-R API inside a Linux glibc guest.

This is a runtime test, not a Teto voicebank or Portuguese speech test. Native
calls run in a separate process so a library crash cannot kill the doctor.
"""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import math
import platform
import struct
import subprocess
import sys
from pathlib import Path
from typing import Any


SOURCE_COMMIT = "a60ca5830b9064556157245d4bf8f5920d93e5f8"
SOURCE_VERSION = "0.1.565"
LIBRARY_SHA256 = {
    "arm64": "80fb77357de4fae608e2d2fa12db867d0c48d0366263e6a280eccd90a1584dfe",
    "x64": "33a4123188bcf7b0b8fec4ae6c5bf882fb16b1aaa5aec6cd3391eccb5a69c490",
}
ELF_MACHINES = {"arm64": 183, "x64": 62}
RELEASE_VERSION = SOURCE_VERSION
RELEASE_URL = f"https://raw.githubusercontent.com/openutau/OpenUtau/{SOURCE_COMMIT}/runtimes/linux-arm64/native/libworldline.so"
RELEASE_SHA256 = LIBRARY_SHA256["arm64"]
LICENSE_URL = f"https://raw.githubusercontent.com/openutau/OpenUtau/{SOURCE_COMMIT}/LICENSE.txt"
REQUIRED_SYMBOLS = (
    "F0", "DecodeMgc", "DecodeBap", "WorldSynthesis", "Resample",
    "PhraseSynthNew", "PhraseSynthDelete", "PhraseSynthAddRequest",
    "PhraseSynthSetCurves", "PhraseSynthSynth",
)
MAX_LIBRARY_BYTES = 8 * 1024 * 1024
DEFAULT_TIMEOUT = 20.0


class ProbeError(ValueError):
    pass


def native_architecture() -> str:
    machine = platform.machine().lower()
    return {"aarch64": "arm64", "arm64": "arm64", "x86_64": "x64", "amd64": "x64"}.get(machine, machine)


def inspect_library(path: Path) -> dict[str, Any]:
    """Reject wrong architectures and incompatible/unpinned APIs before loading."""
    if not path.is_absolute():
        raise ProbeError("--library must be an absolute path inside the Linux guest")
    if sys.platform != "linux":
        raise ProbeError("run inside the Linux glibc guest; the official library cannot load in Android Bionic")
    if struct.calcsize("P") != 8 or sys.byteorder != "little":
        raise ProbeError("the pinned API requires a 64-bit little-endian process")
    arch = native_architecture()
    if arch not in ELF_MACHINES:
        raise ProbeError(f"unsupported native architecture: {arch}")
    if not path.is_file():
        raise ProbeError(f"library not found: {path}")
    if path.stat().st_size > MAX_LIBRARY_BYTES:
        raise ProbeError("library exceeds the supported size limit")
    data = path.read_bytes()
    if len(data) < 64 or data[:4] != b"\x7fELF":
        raise ProbeError("library is not an ELF file")
    if data[4:6] != b"\x02\x01":
        raise ProbeError("library must be ELF64 little-endian")
    if struct.unpack_from("<H", data, 16)[0] != 3:
        raise ProbeError("library must be an ELF shared object")
    elf_machine = struct.unpack_from("<H", data, 18)[0]
    if elf_machine != ELF_MACHINES[arch]:
        raise ProbeError(f"library architecture does not match native {arch}; ELF machine={elf_machine}")
    sha256 = hashlib.sha256(data).hexdigest()
    if sha256 != LIBRARY_SHA256[arch]:
        raise ProbeError("library SHA-256 does not match pinned OpenUtau 0.1.565; current master has a different phrase API")
    return {
        "native_architecture": arch,
        "elf_machine": elf_machine,
        "sha256": sha256,
        "library_sha256": sha256,
        "library_hash_verified": True,
        "abi_verified": True,
        "library_bytes": len(data),
    }


_DoublePointer = ctypes.POINTER(ctypes.c_double)
_FloatPointer = ctypes.POINTER(ctypes.c_float)
_LogCallback = ctypes.CFUNCTYPE(None, ctypes.c_char_p)


class SynthRequest(ctypes.Structure):
    """OpenUtau 0.1.565 SynthRequest, natural 64-bit C structure alignment."""
    _fields_ = [
        ("sample_fs", ctypes.c_int32), ("sample_length", ctypes.c_int32),
        ("sample", _DoublePointer), ("frq_length", ctypes.c_int32),
        ("frq", ctypes.c_void_p), ("tone", ctypes.c_int32),
        ("con_vel", ctypes.c_double), ("offset", ctypes.c_double),
        ("required_length", ctypes.c_double), ("consonant", ctypes.c_double),
        ("cut_off", ctypes.c_double), ("volume", ctypes.c_double),
        ("modulation", ctypes.c_double), ("tempo", ctypes.c_double),
        ("pitch_bend_length", ctypes.c_int32),
        ("pitch_bend", ctypes.POINTER(ctypes.c_int32)),
        ("flag_g", ctypes.c_int), ("flag_O", ctypes.c_int),
        ("flag_P", ctypes.c_int), ("flag_Mt", ctypes.c_int),
        ("flag_Mb", ctypes.c_int), ("flag_Mv", ctypes.c_int),
    ]


def bind_api(library: Any) -> None:
    missing = [name for name in REQUIRED_SYMBOLS if not hasattr(library, name)]
    if missing:
        raise ProbeError("missing pinned C API symbols: " + ", ".join(missing))
    if ctypes.sizeof(SynthRequest) != 144 or SynthRequest.sample.offset != 8 or SynthRequest.pitch_bend.offset != 112:
        raise ProbeError("SynthRequest layout does not match the pinned 64-bit ABI")
    library.PhraseSynthNew.argtypes = []
    library.PhraseSynthNew.restype = ctypes.c_void_p
    library.PhraseSynthDelete.argtypes = [ctypes.c_void_p]
    library.PhraseSynthDelete.restype = None
    library.PhraseSynthAddRequest.argtypes = [ctypes.c_void_p, ctypes.POINTER(SynthRequest)] + [ctypes.c_double] * 5 + [_LogCallback]
    library.PhraseSynthAddRequest.restype = None
    library.PhraseSynthSetCurves.argtypes = [ctypes.c_void_p] + [_DoublePointer] * 5 + [ctypes.c_int, _LogCallback]
    library.PhraseSynthSetCurves.restype = None
    library.PhraseSynthSynth.argtypes = [ctypes.c_void_p, ctypes.POINTER(_FloatPointer), _LogCallback]
    library.PhraseSynthSynth.restype = ctypes.c_int


def render_synthetic_probe(library: Any) -> dict[str, Any]:
    """Render a generated harmonic tone through the phrase API; no voicebank."""
    fs = 44100
    samples = (ctypes.c_double * fs)(*(
        0.25 * math.sin(2 * math.pi * 220 * i / fs)
        + 0.12 * math.sin(2 * math.pi * 440 * i / fs)
        + 0.05 * math.sin(2 * math.pi * 660 * i / fs)
        for i in range(fs)
    ))
    request = SynthRequest(
        sample_fs=fs, sample_length=fs, sample=samples,
        tone=60, con_vel=100, offset=100, required_length=300,
        consonant=80, cut_off=-500, volume=100, modulation=0, tempo=120,
        flag_g=0, flag_O=0, flag_P=86, flag_Mt=0, flag_Mb=0, flag_Mv=100,
    )
    # Missing FRQ data uses the library's built-in pYIN estimator.
    curves = [(ctypes.c_double * 31)(*([value] * 31)) for value in (260.0, 0.5, 0.5, 0.5, 1.0)]
    callback = _LogCallback(lambda message: None)
    # The C API returns new float[]. Pair it with the same C++ delete[].
    cpp = ctypes.CDLL("libstdc++.so.6")
    delete_array = cpp._ZdaPv
    delete_array.argtypes = [ctypes.c_void_p]
    delete_array.restype = None
    phrase = library.PhraseSynthNew()
    if not phrase:
        raise ProbeError("PhraseSynthNew returned NULL during synthetic render")
    output = _FloatPointer()
    try:
        library.PhraseSynthAddRequest(phrase, ctypes.byref(request), 0, 0, 300, 10, 10, callback)
        library.PhraseSynthSetCurves(phrase, *curves, 31, callback)
        count = library.PhraseSynthSynth(phrase, ctypes.byref(output), callback)
        if count <= 0 or count > fs * 2 or not output:
            raise ProbeError(f"synthetic render returned invalid sample count: {count}")
        values = output[:count]
        if not all(math.isfinite(value) for value in values):
            raise ProbeError("synthetic render returned non-finite samples")
        rms = math.sqrt(sum(value * value for value in values) / count)
        if rms <= 1e-8:
            raise ProbeError("synthetic render returned silence")
        return {
            "ok": True, "source": "generated harmonic tone, not a voicebank",
            "sample_rate": fs, "channels": 1, "sample_frames": count,
            "duration_seconds": count / fs, "rms": rms,
            "peak": max(abs(value) for value in values), "all_samples_finite": True,
            "wav_written": False,
        }
    finally:
        if output:
            delete_array(output)
        library.PhraseSynthDelete(phrase)


def child_probe(path: Path, render_probe: bool) -> dict[str, Any]:
    metadata = inspect_library(path)
    library = ctypes.CDLL(str(path))
    bind_api(library)
    phrase = library.PhraseSynthNew()
    if not phrase:
        raise ProbeError("PhraseSynthNew returned NULL")
    library.PhraseSynthDelete(phrase)
    result = {
        **metadata, "ok": True, "api_verified": True,
        "required_symbols": list(REQUIRED_SYMBOLS), "synth_request_bytes": 144,
        "phrase_create_delete_ok": True,
    }
    if render_probe:
        result["synthetic_render"] = render_synthetic_probe(library)
    return result


def _text_tail(value: Any, limit: int = 4096) -> str:
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    return str(value or "")[-limit:]


def probe(path: Path, *, render_probe: bool = False, timeout: float = DEFAULT_TIMEOUT) -> dict[str, Any]:
    result: dict[str, Any] = {
        "runtime_ok": False, "api_verified": False, "abi_verified": False,
        "synthetic_render_verified": False, "teto_synthesis_verified": False,
        "portuguese_speech_verified": False, "library_hash_verified": False,
        "runtime_backend": "native Linux glibc; Box64 is not required",
        "source_version": SOURCE_VERSION, "source_commit": SOURCE_COMMIT,
        "library": str(path), "checks": {},
        "note": "checks only the WORLDLINE-R runtime; no Teto voicebank or speech quality verification",
    }
    try:
        if not math.isfinite(timeout) or not 0 < timeout <= 120:
            raise ProbeError("timeout must be greater than zero and at most 120 seconds")
        result.update(inspect_library(path))
        result["checks"]["library"] = {"ok": True}
        command = [sys.executable, str(Path(__file__).resolve()), "--library", str(path), "--_child"]
        if render_probe:
            command.append("--render-probe")
        try:
            completed = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
        except subprocess.TimeoutExpired as error:
            result["checks"]["native_child"] = {"ok": False, "timed_out": True, "timeout_seconds": timeout, "stderr": _text_tail(error.stderr)}
            raise ProbeError("native runtime probe exceeded its timeout") from error
        child_check = {"ok": False, "code": completed.returncode}
        result["checks"]["native_child"] = child_check
        if completed.returncode:
            child_check["stderr"] = _text_tail(completed.stderr)
            child_check["output"] = _text_tail(completed.stdout)
            if completed.returncode < 0:
                child_check["signal"] = -completed.returncode
            raise ProbeError("native runtime probe failed; see native_child output")
        lines = completed.stdout.strip().splitlines()
        try:
            child = json.loads(lines[-1])
        except (ValueError, IndexError) as error:
            child_check["output"] = _text_tail(completed.stdout)
            raise ProbeError("native child did not return a valid JSON report") from error
        if not isinstance(child, dict) or child.get("ok") is not True or child.get("api_verified") is not True:
            raise ProbeError("native child did not verify the pinned API")
        if render_probe and child.get("synthetic_render", {}).get("ok") is not True:
            raise ProbeError("native child did not verify the requested synthetic render")
        child_check.update(child)
        if completed.stderr:
            child_check["stderr"] = _text_tail(completed.stderr)
        result["runtime_ok"] = True
        result["api_verified"] = True
        result["synthetic_render_verified"] = child.get("synthetic_render", {}).get("ok") is True
    except (ProbeError, OSError) as error:
        result["error"] = str(error)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--library", required=True, type=Path, help="absolute path to pinned libworldline.so inside the Linux guest")
    parser.add_argument("--render-probe", action="store_true", help="also render a generated tone; does not test Teto or Portuguese")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="native child timeout in seconds (maximum 120)")
    parser.add_argument("--_child", action="store_true", help=argparse.SUPPRESS)
    options = parser.parse_args(argv)
    if options._child:
        try:
            report = child_probe(options.library, options.render_probe)
        except Exception as error:
            print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False))
            return 1
    else:
        report = probe(options.library, render_probe=options.render_probe, timeout=options.timeout)
    print(json.dumps(report, ensure_ascii=False, allow_nan=False))
    return 0 if report.get("ok", report.get("runtime_ok", False)) else 1


if __name__ == "__main__":
    raise SystemExit(main())
