#!/usr/bin/env python3
"""Offline A/B of the real native reader, FFmpeg and Music Agent TTS command.

The MP3 fixture and its paced local Unix provider are synthetic. Discord's
network/voice handshake is replaced by a controlled delay and a PCM sink.
This measures local preparation, not audible latency on a production phone.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import asynccontextmanager
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import types
import zipfile

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = "cogs/musica/runtime_telefone/agente/tts.py"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def load_source(source: str, variant: str):
    name = "cogs.musica.runtime_telefone.agente._tts_benchmark_" + variant
    module = types.ModuleType(name)
    module.__package__ = "cogs.musica.runtime_telefone.agente"
    module.__file__ = f"{MODULE_PATH} ({variant})"
    sys.modules[name] = module
    exec(compile(source, module.__file__, "exec"), module.__dict__)
    return module


class Provider:
    """One complete protocol-v2 response, using the real client's socket API."""
    def __init__(self, directory, fixture, args):
        self.path = Path(directory) / "s"
        self.fixture, self.args = fixture, args
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(str(self.path))
        self.listener.listen(1)
        self.listener.settimeout(args.timeout_seconds)
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.error = None
        self.request = None
        self.completed = False

    def run(self):
        try:
            connection, _ = self.listener.accept()
            with connection:
                connection.settimeout(self.args.timeout_seconds)
                raw = bytearray()
                while b"\r\n\r\n" not in raw:
                    chunk = connection.recv(4096)
                    if not chunk or len(raw) > 32768:
                        raise RuntimeError("incomplete benchmark request")
                    raw.extend(chunk)
                header, body = bytes(raw).split(b"\r\n\r\n", 1)
                length = next(int(line.split(b":", 1)[1]) for line in header.split(b"\r\n")
                              if line.lower().startswith(b"content-length:"))
                while len(body) < length:
                    chunk = connection.recv(length - len(body))
                    if not chunk:
                        raise RuntimeError("incomplete benchmark request body")
                    body += chunk
                self.request = json.loads(body)
                time.sleep(self.args.initial_delay_ms / 1000)
                connection.sendall(
                    b"HTTP/1.1 200 OK\r\nConnection: close\r\nTransfer-Encoding: chunked\r\n"
                    b"Content-Type: audio/mpeg\r\nX-Core-Worker-Audio-Format: mp3\r\n"
                    b"X-Core-Worker-Engine: gtts\r\nX-Core-Worker-Stream-Protocol: 2\r\n"
                    b"X-Core-Worker-Cache-Hit: false\r\n\r\n")
                for offset in range(0, len(self.fixture), self.args.chunk_bytes):
                    data = self.fixture[offset:offset + self.args.chunk_bytes]
                    connection.sendall(f"{len(data):x}\r\n".encode() + data + b"\r\n")
                    time.sleep(self.args.chunk_interval_ms / 1000)
                connection.sendall(b"0\r\n\r\n")
                self.completed = True
        except Exception as error:
            self.error = error

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.listener.close()
        self.thread.join(self.args.timeout_seconds + 1)
        if self.thread.is_alive():
            raise RuntimeError("benchmark provider did not stop")


class VoiceSink:
    """Observe real PCM reads without Discord network or playback pacing."""
    def __init__(self):
        self.thread = None
        self.playing = False
        self.closed = threading.Event()
        self.pcm = bytearray()
        self.error = None

    def is_connected(self):
        return True

    def is_playing(self):
        return self.playing

    def is_paused(self):
        return False

    def play(self, source, after):
        self.playing = True
        def drain():
            try:
                while not self.closed.is_set():
                    # A fast benchmark sink must not turn deliberate mixer
                    # underrun silence into fixture bytes or a timing sample.
                    frames = getattr(source, "frames", None)
                    if frames is not None and frames.empty() and not source.finished:
                        time.sleep(.001)
                        continue
                    frame = source.read()
                    if not frame:
                        break
                    self.pcm.extend(frame)
            except Exception as error:
                self.error = error
            finally:
                self.playing = False
                after(self.error)
        self.thread = threading.Thread(target=drain, daemon=True)
        self.thread.start()

    def stop(self):
        self.closed.set()

    def join(self):
        self.stop()
        if self.thread:
            self.thread.join(2)
            if self.thread.is_alive():
                raise RuntimeError("benchmark PCM sink did not stop")


async def trial(module, fixture, args, voice_delay_ms):
    voice = VoiceSink()
    class Owner(module.TTSMixin):
        ffmpeg_executable = args.ffmpeg
        def __init__(self):
            self.states = {}
            self._tts_direct_locks, self._tts_direct_lock_users = {}, {}
        @asynccontextmanager
        async def _registry_lock(self, *_):
            yield
        async def _ensure_direct_voice_client(self, guild):
            await asyncio.sleep(voice_delay_ms / 1000)
            return voice, True
        def _tts_cache_enabled(self):
            return False
        def _update_auto_leave_from_body(self, *_):
            pass
        def _cancel_idle_disconnect(self, *_):
            pass
        def _cancel_voice_presence_disconnect(self, *_):
            pass
        def _set_voice_session_mode(self, state, mode, **_):
            state.voice_session_mode = mode
        def _set_status(self, state, status, **_):
            state.status = status
        async def _finish_tts_voice_session(self, *_args, **_kwargs):
            pass
        def log(self, *_args, **_kwargs):
            pass
    with tempfile.TemporaryDirectory(prefix="tts-first-") as directory:
        with Provider(directory, fixture, args) as provider:
            os.environ["TTS_NATIVE_WORKER_SOCKET"] = str(provider.path)
            try:
                result = await asyncio.wait_for(Owner().cmd_voice_tts({
                    "guild_id": 1, "voice_channel_id": 2, "engine": "gtts", "language": "pt",
                    "text": "Fixture sintética de preparação local.", "timeout_seconds": args.timeout_seconds,
                    "advanced_reverb_level": args.reverb_level,
                }), args.timeout_seconds)
            finally:
                voice.join()
        if provider.error:
            raise provider.error
        if not provider.completed or not result["first_frame_observed"] or not voice.pcm:
            raise RuntimeError("benchmark did not observe a complete native stream and PCM")
        return {"first_pcm_ms": result["first_frame_ms"], "elapsed_ms": result["elapsed_ms"],
                "pcm_bytes": len(voice.pcm), "pcm_sha256": hashlib.sha256(voice.pcm).hexdigest(),
                "provider_eof_confirmed": True}


def percentile(values, quantile):
    values = sorted(values)
    index = (len(values) - 1) * quantile
    lower = int(index)
    return values[lower] + (values[min(lower + 1, len(values) - 1)] - values[lower]) * (index - lower)


async def benchmark(args, before, after, fixture):
    results = {}
    for case, delay in (("warm_voice", 0), ("cold_voice", args.voice_delay_ms)):
        pair = {"before": [], "after": []}
        for variant, module in (("before", before), ("after", after)):
            for _ in range(args.warmup):
                await trial(module, fixture, args, delay)
        # Alternate A/B order to reduce drift from temperature or machine load.
        for index in range(args.runs):
            order = (("before", before), ("after", after))
            for variant, module in (order if index % 2 == 0 else reversed(order)):
                pair[variant].append(await trial(module, fixture, args, delay))
        all_samples = pair["before"] + pair["after"]
        if len({(sample["pcm_bytes"], sample["pcm_sha256"]) for sample in all_samples}) != 1:
            raise RuntimeError("decoded PCM differs across A/B variants")
        summary = {variant: {"samples": len(samples),
                             "first_pcm_p50_ms": round(percentile([s["first_pcm_ms"] for s in samples], .5), 3),
                             "first_pcm_p95_ms": round(percentile([s["first_pcm_ms"] for s in samples], .95), 3)}
                   for variant, samples in pair.items()}
        summary["first_pcm_p95_change_percent"] = round(
            100 * (summary["after"]["first_pcm_p95_ms"] / summary["before"]["first_pcm_p95_ms"] - 1), 2)
        results[case] = {"simulated_voice_delay_ms": delay, "pcm_identical": True,
                         "summary": summary, "samples": pair}
        print(f"{case}: before={summary['before']['first_pcm_p95_ms']}ms after={summary['after']['first_pcm_p95_ms']}ms", flush=True)
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-zip", type=Path, required=True, help="original repository ZIP before this change")
    parser.add_argument("--runs", type=int, default=30)
    parser.add_argument("--warmup", type=int, default=2)
    parser.add_argument("--voice-delay-ms", type=float, default=160)
    parser.add_argument("--initial-delay-ms", type=float, default=80)
    parser.add_argument("--chunk-interval-ms", type=float, default=8)
    parser.add_argument("--chunk-bytes", type=int, default=512)
    parser.add_argument("--duration-seconds", type=float, default=3)
    parser.add_argument("--timeout-seconds", type=float, default=5)
    parser.add_argument("--reverb-level", type=int, choices=range(4), default=0)
    parser.add_argument("--ffmpeg", default=shutil.which("ffmpeg") or "ffmpeg")
    parser.add_argument("--out", type=Path, default=ROOT / "native-dist/benchmark-worker-first-audio.json")
    args = parser.parse_args()
    if not 1 <= args.runs <= 1000 or not 0 <= args.warmup <= 20 or not 1 <= args.chunk_bytes <= 65536:
        parser.error("invalid run, warmup or chunk limits")
    for key in ("voice_delay_ms", "initial_delay_ms", "chunk_interval_ms", "duration_seconds", "timeout_seconds"):
        value = getattr(args, key)
        if not 0 <= value <= (120 if key.endswith("seconds") else 10000) or key.endswith("seconds") and value == 0:
            parser.error("invalid timing limit: " + key)
    with zipfile.ZipFile(args.baseline_zip) as archive:
        names = [name for name in archive.namelist() if name.endswith("/" + MODULE_PATH) or name == MODULE_PATH]
        if len(names) != 1:
            raise RuntimeError("baseline ZIP must contain exactly one Music Agent TTS module")
        baseline_source = archive.read(names[0]).decode()
    current_source = (ROOT / MODULE_PATH).read_text()
    before, after = load_source(baseline_source, "before"), load_source(current_source, "after")
    fixture = subprocess.run([
        args.ffmpeg, "-v", "error", "-f", "lavfi", "-i",
        f"sine=frequency=440:sample_rate=24000:duration={args.duration_seconds}",
        "-c:a", "libmp3lame", "-b:a", "48k", "-f", "mp3", "pipe:1"
    ], capture_output=True, check=True, timeout=10).stdout
    controlled = ("TTS_NATIVE_WORKER", "TTS_NATIVE_WORKER_SOCKET", "TTS_STREAM_FFMPEG_PROBESIZE_BYTES")
    previous = {key: os.environ.get(key) for key in controlled}
    os.environ["TTS_NATIVE_WORKER"] = "1"
    os.environ.pop("TTS_STREAM_FFMPEG_PROBESIZE_BYTES", None)
    try:
        results = asyncio.run(benchmark(args, before, after, fixture))
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    report = {"schema": "tts-worker-first-audio-benchmark-v1", "results": results,
              "environment": {"python": sys.version, "platform": platform.platform(),
                              "ffmpeg": subprocess.run([args.ffmpeg, "-version"], capture_output=True, text=True, check=True).stdout.splitlines()[0]},
              "settings": {key: value for key, value in vars(args).items() if key not in ("baseline_zip", "out")},
              "provenance": {"baseline_source_sha256": hashlib.sha256(baseline_source.encode()).hexdigest(),
                             "current_source_sha256": hashlib.sha256(current_source.encode()).hexdigest(),
                             "fixture_sha256": hashlib.sha256(fixture).hexdigest(), "fixture_mp3_bytes": len(fixture)},
              "limitations": ["Synthetic MP3 and delayed local Unix provider; no upstream synthesis or Rust CPU measured.",
                              "Voice connection delay is simulated; PCM sink has no Discord network or playback pacing.",
                              "First PCM observed by Music Agent source, not audio heard by another Discord user.",
                              "Local exploratory sample; production Android latencies require separate A/B measurement."]}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2) + "\n")
    print(args.out)


if __name__ == "__main__":
    main()
