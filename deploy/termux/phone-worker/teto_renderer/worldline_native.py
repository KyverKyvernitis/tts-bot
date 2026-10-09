#!/usr/bin/env python3
"""Isolated, pinned WORLDLINE-R phrase synthesis for a Linux ARM64 guest.

The ctypes ABI and timing rules follow OpenUtau 0.1.565, commit
a60ca5830b9064556157245d4bf8f5920d93e5f8, Copyright (c) 2014 StAkira,
MIT licensed. This module uses PhraseSynth directly; it never substitutes
the standalone Resample function.
All input WAVs must be decoded PCM16 mono 44100 Hz files in the job directory.

The MIT License (MIT)

Copyright (c) 2014 StAkira

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""
from __future__ import annotations

import argparse
from array import array
import ctypes
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import struct
import subprocess
import sys
import tempfile
from typing import Any
import wave

SOURCE_COMMIT = "a60ca5830b9064556157245d4bf8f5920d93e5f8"
SOURCE_VERSION = "0.1.565"
LIBRARY_SHA256 = {
    "arm64": "80fb77357de4fae608e2d2fa12db867d0c48d0366263e6a280eccd90a1584dfe",
    "x64": "33a4123188bcf7b0b8fec4ae6c5bf882fb16b1aaa5aec6cd3391eccb5a69c490",
}
ELF_MACHINES = {"arm64": 183, "x64": 62}
SAMPLE_RATE = 44100
FRAME_MS = 10
SAMPLES_PER_FRAME = 441
MAX_OUTPUT_SECONDS = 120
MAX_REQUESTS = 256
MAX_SAMPLE_SECONDS = 30
MAX_TOTAL_SAMPLE_FRAMES = SAMPLE_RATE * 180
MAX_JOB_BYTES = 4 * 1024 * 1024
DEFAULT_TIMEOUT = 20.0
CURVE_NAMES = ("f0", "gender", "tension", "breathiness", "voicing")
REQUIRED_SYMBOLS = ("PhraseSynthNew", "PhraseSynthDelete", "PhraseSynthAddRequest", "PhraseSynthSetCurves", "PhraseSynthSynth")
_DoublePointer = ctypes.POINTER(ctypes.c_double)
_FloatPointer = ctypes.POINTER(ctypes.c_float)
_LogCallback = ctypes.CFUNCTYPE(None, ctypes.c_char_p)


class WorldlineNativeError(ValueError):
    pass


class SynthRequest(ctypes.Structure):
    """Natural 64-bit alignment of the pinned OpenUtau SynthRequest."""
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


def native_architecture() -> str:
    machine = platform.machine().lower()
    return {"aarch64": "arm64", "arm64": "arm64", "x86_64": "x64", "amd64": "x64"}.get(machine, machine)


def inspect_library(path: Path) -> dict[str, Any]:
    if not path.is_absolute():
        raise WorldlineNativeError("--library must be an absolute Linux guest path")
    if sys.platform != "linux":
        raise WorldlineNativeError("WORLDLINE-R requires the Linux glibc guest, not Android Bionic")
    architecture = native_architecture()
    if architecture not in ELF_MACHINES or struct.calcsize("P") != 8 or sys.byteorder != "little":
        raise WorldlineNativeError("unsupported native architecture; use a 64-bit little-endian ARM64 Linux guest")
    if not path.is_file() or path.stat().st_size > 8 * 1024 * 1024:
        raise WorldlineNativeError("pinned WORLDLINE-R library missing or oversized")
    data = path.read_bytes()
    if (len(data) < 64 or data[:6] != b"\x7fELF\x02\x01"
            or struct.unpack_from("<H", data, 16)[0] != 3
            or struct.unpack_from("<H", data, 18)[0] != ELF_MACHINES[architecture]):
        raise WorldlineNativeError("library must be a native ELF64 little-endian shared object")
    digest = hashlib.sha256(data).hexdigest()
    if digest != LIBRARY_SHA256[architecture]:
        raise WorldlineNativeError("library SHA-256 does not match pinned OpenUtau 0.1.565")
    return {"native_architecture": architecture, "library_sha256": digest,
            "library_hash_verified": True, "source_commit": SOURCE_COMMIT,
            "source_version": SOURCE_VERSION}


def bind_api(library: Any) -> None:
    if ctypes.sizeof(SynthRequest) != 144 or SynthRequest.sample.offset != 8 or SynthRequest.pitch_bend.offset != 112:
        raise WorldlineNativeError("SynthRequest does not match the pinned 64-bit ABI")
    missing = [name for name in REQUIRED_SYMBOLS if not hasattr(library, name)]
    if missing:
        raise WorldlineNativeError("missing phrase API symbols: " + ", ".join(missing))
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


def _number(value: Any, name: str, lower: float, upper: float) -> float:
    try:
        converted = float(value) if not isinstance(value, bool) and isinstance(value, (int, float)) else float("nan")
    except (OverflowError, ValueError):
        converted = float("nan")
    if not math.isfinite(converted) or not lower <= converted <= upper:
        raise WorldlineNativeError(f"{name} must be a finite number in [{lower}, {upper}]")
    return converted


def _integer(value: Any, name: str, lower: int, upper: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not lower <= value <= upper:
        raise WorldlineNativeError(f"{name} must be an integer in [{lower}, {upper}]")
    return value


def _round_frame(milliseconds: float) -> int:
    # std::round, rather than Python's ties-to-even round. All arguments >= 0.
    return int(math.floor(milliseconds / FRAME_MS + 0.5))


def _load_wav(path: Path) -> tuple[Any, int]:
    if not path.is_file() or path.is_symlink() or path.stat().st_size > SAMPLE_RATE * MAX_SAMPLE_SECONDS * 2 + 65536:
        raise WorldlineNativeError("source WAV missing, symlinked or exceeds 30 seconds")
    try:
        with wave.open(str(path), "rb") as source:
            if (source.getnchannels(), source.getsampwidth(), source.getframerate(), source.getcomptype()) != (1, 2, SAMPLE_RATE, "NONE"):
                raise WorldlineNativeError("source WAV must be decoded PCM16 mono 44100 Hz")
            frames = source.getnframes()
            if not SAMPLE_RATE // 10 <= frames <= SAMPLE_RATE * MAX_SAMPLE_SECONDS:
                raise WorldlineNativeError("source WAV must contain between 100 ms and 30 seconds")
            data = source.readframes(frames + 1)
    except (wave.Error, EOFError) as error:
        raise WorldlineNativeError("invalid source WAV") from error
    if len(data) != frames * 2:
        raise WorldlineNativeError("source WAV sample data is truncated")
    pcm = array("h")
    pcm.frombytes(data)
    if max(abs(value) for value in pcm) == 0:
        raise WorldlineNativeError("source WAV is entirely silent")
    samples = (ctypes.c_double * frames)(*(value / 32768.0 for value in pcm))
    return samples, frames


def _source_path(directory: Path, name: Any) -> Path:
    if not isinstance(name, str) or not name or len(name) > 256 or "\x00" in name:
        raise WorldlineNativeError("sample_file must name a relative WAV in the job directory")
    relative = Path(name)
    if relative.is_absolute() or any(part in ("..", ".") for part in relative.parts):
        raise WorldlineNativeError("sample_file must stay inside the job directory")
    source = directory / relative
    # A dedicated flat job folder also avoids following intermediate symlinks.
    if len(relative.parts) != 1 or source.is_symlink() or source.resolve().parent != directory:
        raise WorldlineNativeError("sample_file must be a regular file directly in the job directory")
    return source


def _request(item: Any, directory: Path, cache: dict[str, tuple[Any, int]], maximum: float) -> dict[str, Any]:
    if not isinstance(item, dict):
        raise WorldlineNativeError("each phrase request must be an object")
    name = item.get("sample_file")
    source = _source_path(directory, name)
    if name not in cache:
        cache[name] = _load_wav(source)
        if sum(frames for _, frames in cache.values()) > MAX_TOTAL_SAMPLE_FRAMES:
            raise WorldlineNativeError("total unique source WAVs exceed 180 seconds")
    samples, sample_frames = cache[name]
    duration_ms = sample_frames * 1000.0 / SAMPLE_RATE
    fields: dict[str, Any] = {"samples": samples, "sample_frames": sample_frames, "sample_file": name}
    fields["tone"] = _integer(item.get("tone"), "tone", 0, 127)
    defaults = {"con_vel": 100, "volume": 100, "modulation": 0, "tempo": 120,
                "skip_ms": 0, "fade_in_ms": 10, "fade_out_ms": 10}
    bounds = {"con_vel": (20, 300), "volume": (0, 200), "modulation": (0, 100),
              "tempo": (20, 300), "offset_ms": (0, duration_ms),
              "consonant_ms": (0, duration_ms), "cutoff_ms": (-duration_ms, duration_ms),
              "required_length_ms": (FRAME_MS, maximum * 1000),
              "position_ms": (0, maximum * 1000), "length_ms": (FRAME_MS, maximum * 1000),
              "skip_ms": (0, maximum * 1000), "fade_in_ms": (0, maximum * 1000), "fade_out_ms": (0, maximum * 1000)}
    for key, (lower, upper) in bounds.items():
        fields[key] = _number(item.get(key, defaults.get(key)), key, lower, upper)
    offset, cutoff = fields["offset_ms"], fields["cutoff_ms"]
    region_ms = -cutoff if cutoff < 0 else duration_ms - offset - cutoff
    if region_ms <= 0 or offset + region_ms > duration_ms + 1e-6:
        raise WorldlineNativeError("OTO region is empty or exceeds the source WAV duration")
    # Match the C++ Trim arithmetic exactly: int(ceil(end_ms) / 10).
    start = int(offset / FRAME_MS)
    trimmed_frames = int(math.ceil(offset + region_ms) / FRAME_MS) - start
    trimmed_samples = trimmed_frames * SAMPLES_PER_FRAME
    if trimmed_frames < 2 or start * SAMPLES_PER_FRAME + trimmed_samples > sample_frames:
        raise WorldlineNativeError("OTO region must contain at least two complete 10 ms frames within the WAV")
    if fields["consonant_ms"] > region_ms:
        raise WorldlineNativeError("OTO consonant exceeds the available input region")
    con_speed = 0.5 ** (1.0 - fields["con_vel"] / 100.0)
    out_con = max(1.0, fields["consonant_ms"]) / con_speed
    mapping_frames = math.floor(max(out_con, fields["required_length_ms"]) / FRAME_MS) + 1
    p0 = _round_frame(fields["position_ms"])
    p4 = _round_frame(fields["position_ms"] + fields["length_ms"])
    skip = _round_frame(fields["skip_ms"])
    if p4 <= p0 or fields["position_ms"] + fields["length_ms"] > maximum * 1000:
        raise WorldlineNativeError("phrase request duration exceeds the output limit or has no 10 ms frame")
    if skip + p4 - p0 > mapping_frames:
        raise WorldlineNativeError("required_length_ms does not cover skip_ms plus the request's 10 ms model frames")
    if fields["required_length_ms"] < fields["skip_ms"] + fields["length_ms"]:
        raise WorldlineNativeError("required_length_ms must cover skip_ms plus length_ms")
    if fields["fade_in_ms"] > fields["length_ms"] or fields["fade_out_ms"] > fields["length_ms"]:
        raise WorldlineNativeError("fade durations must not exceed request length")
    fields["p0"], fields["p4"] = p0, p4
    fields["mapping_frames"] = mapping_frames + 4
    return fields


def load_job(path: Path, output: Path) -> dict[str, Any]:
    if not path.is_absolute() or path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_JOB_BYTES:
        raise WorldlineNativeError("--job must be an absolute regular JSON file of at most 4 MiB")
    directory = path.resolve().parent
    if (not output.is_absolute() or output.parent.resolve() != directory or output.is_symlink()
            or output.name == path.name or output.suffix.lower() != ".wav"):
        raise WorldlineNativeError("--output must be a WAV directly inside the job directory")
    try:
        job = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, UnicodeError) as error:
        raise WorldlineNativeError("job must contain valid UTF-8 JSON") from error
    if not isinstance(job, dict) or type(job.get("schema_version")) is not int or job["schema_version"] != 1:
        raise WorldlineNativeError("unsupported phrase job schema_version; expected 1")
    if type(job.get("sample_rate")) is not int or job["sample_rate"] != SAMPLE_RATE:
        raise WorldlineNativeError("phrase job sample_rate must be 44100")
    maximum = _number(job.get("max_output_seconds", MAX_OUTPUT_SECONDS), "max_output_seconds", 0.01, MAX_OUTPUT_SECONDS)
    requests = job.get("requests")
    if not isinstance(requests, list) or not 1 <= len(requests) <= MAX_REQUESTS:
        raise WorldlineNativeError("phrase job must contain between 1 and 256 requests")
    cache: dict[str, tuple[Any, int]] = {}
    prepared = [_request(item, directory, cache, maximum) for item in requests]
    # The pinned C++ implementation resizes its final vectors for each request.
    # Sorting by p4 prevents a later overlapping request from truncating earlier audio.
    prepared.sort(key=lambda item: item["p4"])
    if sum(item["mapping_frames"] for item in prepared) > 25280:
        raise WorldlineNativeError("aggregate phrase models exceed the bounded native memory budget")
    end_frame = prepared[-1]["p4"]
    maximum_frames = math.floor(maximum * SAMPLE_RATE) + 1
    duration_ms = _number(job.get("duration_ms", end_frame * FRAME_MS), "duration_ms", end_frame * FRAME_MS, maximum * 1000)
    if abs(duration_ms / FRAME_MS - round(duration_ms / FRAME_MS)) > 1e-6:
        raise WorldlineNativeError("duration_ms must be aligned to the 10 ms phrase grid")
    output_frames = _round_frame(duration_ms) * SAMPLES_PER_FRAME + 1
    if output_frames > maximum_frames:
        raise WorldlineNativeError("rounded native output exceeds max_output_seconds")
    raw_curves = job.get("curves")
    if not isinstance(raw_curves, dict):
        raise WorldlineNativeError("curves must provide all five 10 ms WORLDLINE-R controls")
    curves = []
    curve_length = None
    for key in CURVE_NAMES:
        values = raw_curves.get(key)
        if not isinstance(values, list) or not _round_frame(duration_ms) + 1 <= len(values) <= MAX_OUTPUT_SECONDS * 100 + 1:
            raise WorldlineNativeError(f"curve {key} must cover every 10 ms output frame plus its final frame")
        if curve_length is not None and len(values) != curve_length:
            raise WorldlineNativeError("all WORLDLINE-R curves must have the same length")
        curve_length = len(values)
        upper = 2000 if key == "f0" else 1
        checked = [_number(value, f"curve {key}", 0, upper) for value in values]
        curves.append((ctypes.c_double * len(checked))(*checked))
    intervals = job.get("silence_intervals_ms", [])
    if not isinstance(intervals, list) or len(intervals) > MAX_REQUESTS + 1:
        raise WorldlineNativeError("silence_intervals_ms must be a bounded list of [start, end]")
    silence = []
    for interval in intervals:
        if not isinstance(interval, list) or len(interval) != 2:
            raise WorldlineNativeError("silence intervals must contain [start, end]")
        begin = _number(interval[0], "silence start", 0, maximum * 1000)
        end = _number(interval[1], "silence end", begin, maximum * 1000)
        silence.append((int(begin * SAMPLE_RATE / 1000), int(end * SAMPLE_RATE / 1000)))
    if output.name in cache:
        raise WorldlineNativeError("output must not overwrite an input WAV")
    return {"requests": prepared, "curves": curves, "curve_length": curve_length,
            "maximum_frames": maximum_frames, "expected_frames": end_frame * SAMPLES_PER_FRAME + 1,
            "output_frames": output_frames,
            "silence_intervals": silence, "directory": directory}


def _write_output(path: Path, values: Any, silence_intervals: list[tuple[int, int]],
                  *, pad_to_frames: int | None = None) -> dict[str, Any]:
    floats = list(values)
    if not floats or not all(math.isfinite(value) for value in floats):
        raise WorldlineNativeError("phrase render produced empty or non-finite audio")
    if pad_to_frames is not None:
        if not len(floats) <= pad_to_frames <= MAX_OUTPUT_SECONDS * SAMPLE_RATE + 1:
            raise WorldlineNativeError("declared phrase duration cannot truncate native audio or exceed the output limit")
        floats.extend([0.0] * (pad_to_frames - len(floats)))
    for begin, end in silence_intervals:
        floats[begin:min(end, len(floats))] = [0.0] * max(0, min(end, len(floats)) - begin)
    peak = max(abs(value) for value in floats)
    rms = math.sqrt(sum(value * value for value in floats) / len(floats))
    if peak <= 1e-8 or rms <= 1e-8:
        raise WorldlineNativeError("phrase render produced silence")
    # Preserve low-level speech; normalize only to prevent PCM clipping.
    gain = min(1.0, 0.98 / peak)
    pcm = array("h", (max(-32768, min(32767, round(value * gain * 32767))) for value in floats))
    if sys.byteorder != "little":
        pcm.byteswap()
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(prefix=".worldline-", suffix=".wav", dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            with wave.open(stream, "wb") as target:
                target.setnchannels(1)
                target.setsampwidth(2)
                target.setframerate(SAMPLE_RATE)
                target.writeframes(pcm.tobytes())
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return {"frames": len(floats), "sample_rate": SAMPLE_RATE, "channels": 1,
            "duration_seconds": len(floats) / SAMPLE_RATE, "peak": peak * gain,
            "rms": rms * gain, "gain": gain, "outputfilename": path.name}


def child_run(library_path: Path, job_path: Path | None, output_path: Path | None) -> dict[str, Any]:
    metadata = inspect_library(library_path)
    prepared = load_job(job_path, output_path) if job_path is not None and output_path is not None else None
    library = ctypes.CDLL(str(library_path))
    bind_api(library)
    phrase = library.PhraseSynthNew()
    if not phrase:
        raise WorldlineNativeError("PhraseSynthNew returned NULL")
    report = {"ok": True, "backend": "worldline-r", "api_verified": True,
              "abi_verified": True, "phrase_adapter_available": True, **metadata}
    output = _FloatPointer()
    delete_array = None
    try:
        if prepared is None:
            return {**report, "phrase_render_verified": False}
        cpp = ctypes.CDLL("libstdc++.so.6")
        delete_array = cpp._ZdaPv
        delete_array.argtypes = [ctypes.c_void_p]
        delete_array.restype = None
        callback = _LogCallback(lambda message: None)
        for item in prepared["requests"]:
            request = SynthRequest(
                sample_fs=SAMPLE_RATE, sample_length=item["sample_frames"], sample=item["samples"],
                tone=item["tone"], con_vel=item["con_vel"], offset=item["offset_ms"],
                required_length=item["required_length_ms"], consonant=item["consonant_ms"],
                cut_off=item["cutoff_ms"], volume=item["volume"], modulation=item["modulation"], tempo=item["tempo"],
                flag_g=0, flag_O=0, flag_P=86, flag_Mt=0, flag_Mb=0, flag_Mv=100,
            )
            library.PhraseSynthAddRequest(phrase, ctypes.byref(request), item["position_ms"], item["skip_ms"],
                                        item["length_ms"], item["fade_in_ms"], item["fade_out_ms"], callback)
        library.PhraseSynthSetCurves(phrase, *prepared["curves"], prepared["curve_length"], callback)
        count = library.PhraseSynthSynth(phrase, ctypes.byref(output), callback)
        if not output or count <= 0 or count > prepared["maximum_frames"] or count != prepared["expected_frames"]:
            raise WorldlineNativeError(f"phrase render returned an invalid sample count: {count}")
        wav_report = _write_output(output_path, output[:count], prepared["silence_intervals"],
                                   pad_to_frames=prepared["output_frames"])
        return {**report, **wav_report, "phrase_render_verified": True, "request_count": len(prepared["requests"]),
                "portuguese_speech_verified": False, "voice_quality_verified": False}
    finally:
        if output and delete_array is not None:
            delete_array(output)
        library.PhraseSynthDelete(phrase)


def _tail(value: Any) -> str:
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    return str(value or "")[-4096:]


def run_isolated(library: Path, job: Path | None = None, output: Path | None = None,
                 *, timeout: float = DEFAULT_TIMEOUT) -> dict[str, Any]:
    report: dict[str, Any] = {"ok": False, "backend": "worldline-r", "phrase_render_verified": False,
                              "portuguese_speech_verified": False, "voice_quality_verified": False}
    try:
        _number(timeout, "timeout", 0.001, 120)
        metadata = inspect_library(library)
        command = [sys.executable, str(Path(__file__).resolve()), "--library", str(library), "--_child"]
        if job is None and output is None:
            command.append("--probe")
        elif job is not None and output is not None:
            command.extend(["--job", str(job), "--output", str(output)])
        else:
            raise WorldlineNativeError("--job and --output must be provided together")
        try:
            completed = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
        except subprocess.TimeoutExpired as error:
            report.update(timed_out=True, timeout_seconds=timeout, stderr=_tail(error.stderr))
            raise WorldlineNativeError("native phrase child exceeded its timeout") from error
        if completed.returncode:
            report.update(code=completed.returncode, stderr=_tail(completed.stderr))
            if completed.returncode < 0:
                report["signal"] = -completed.returncode
            try:
                failed = json.loads(completed.stdout.strip().splitlines()[-1])
                detail = _tail(failed.get("error"))
            except (ValueError, IndexError, AttributeError):
                detail = _tail(completed.stdout)
            raise WorldlineNativeError("native phrase child failed" + (": " + detail if detail else ""))
        try:
            child = json.loads(completed.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError) as error:
            raise WorldlineNativeError("native phrase child returned no valid JSON report") from error
        if (not isinstance(child, dict) or child.get("ok") is not True or child.get("api_verified") is not True
                or child.get("library_sha256") != metadata["library_sha256"]
                or (job is not None and child.get("phrase_render_verified") is not True)):
            raise WorldlineNativeError("native phrase child did not verify the requested operation")
        report.update(child)
        if completed.stderr:
            report["stderr"] = _tail(completed.stderr)
    except (WorldlineNativeError, OSError) as error:
        report["error"] = _tail(str(error))
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--library", type=Path, required=True)
    parser.add_argument("--job", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--probe", action="store_true")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    parser.add_argument("--_child", action="store_true", help=argparse.SUPPRESS)
    options = parser.parse_args(argv)
    if options.probe and (options.job is not None or options.output is not None):
        parser.error("--probe cannot be combined with --job or --output")
    if not options.probe and (options.job is None or options.output is None):
        parser.error("provide --probe or both --job and --output")
    if options._child:
        try:
            result = child_run(options.library, options.job, options.output)
        except (WorldlineNativeError, OSError) as error:
            result = {"ok": False, "backend": "worldline-r", "error": _tail(str(error))}
    else:
        result = run_isolated(options.library, options.job, options.output, timeout=options.timeout)
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0 if result.get("ok") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
