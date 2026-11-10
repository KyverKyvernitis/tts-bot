#!/usr/bin/env python3
"""Compare Python/native replay with identical local MP3 and FFmpeg inputs.

No provider, Discord gateway, voice connection or remote worker is contacted.
Each backend/scenario runs in a fresh process to separate CPU/RSS accounting.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import resource
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
SCENARIOS = ("replay_ram", "replay_spill", "replay_two_readers", "ffmpeg_progressive", "ffmpeg_native_source")
FRAME_BYTES = 3840


def file_sha256(path: Path) -> str:
    # Debug extensions can exceed 100 MiB. Hash incrementally so provenance
    # collection cannot inflate the process high-water RSS being measured.
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(128 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def percentile(values: list[float], q: float) -> float:
    values = sorted(values)
    position = (len(values) - 1) * q
    lower, upper = math.floor(position), math.ceil(position)
    return values[lower] + (values[upper] - values[lower]) * (position - lower)


def summarize(samples: list[dict[str, object]]) -> dict[str, object]:
    metrics = sorted({key for record in samples for key, value in record.items()
                      if isinstance(value, (float, int)) and key.endswith(("_ms", "_bytes"))})
    output: dict[str, object] = {}
    for metric in metrics:
        values = [float(record[metric]) for record in samples if metric in record]
        output[metric] = {"n": len(values), "p50": round(percentile(values, .5), 4),
                          "p95": round(percentile(values, .95), 4),
                          "min": round(min(values), 4), "max": round(max(values), 4)}
    for key in ("input_sha256", "output_sha256", "output_bytes"):
        values = sorted({record[key] for record in samples if key in record})
        if values:
            output[key] = values
    return output


def make_fixture(ffmpeg: str, duration: float, timeout: float) -> bytes:
    completed = subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=24000", "-t", str(duration),
        "-ac", "1", "-c:a", "libmp3lame", "-b:a", "48k", "-write_xing", "0",
        "-id3v2_version", "0", "-f", "mp3", "pipe:1"],
        check=True, capture_output=True, timeout=timeout)
    if len(completed.stdout) < 2048:
        raise RuntimeError("fixture MP3 too short")
    return completed.stdout


def max_rss_bytes() -> int:
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(value if sys.platform == "darwin" else value * 1024)


def child_cpu_ms() -> float:
    usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    return (usage.ru_utime + usage.ru_stime) * 1000


async def stop_process(process) -> None:
    if process is None or process.returncode is not None:
        return
    with contextlib.suppress(ProcessLookupError):
        process.terminate()
    try:
        await asyncio.wait_for(process.wait(), .5)
    except asyncio.TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        await asyncio.wait_for(process.wait(), 2)


async def trial(args, fixture: bytes, directory: str) -> dict[str, object]:
    from cogs.tts.runtime import MemoryBudget, ReplayBuffer
    limit = args.memory_bytes if args.case != "replay_spill" else min(args.memory_bytes, 512)
    budget = MemoryBudget(limit)
    buffer = ReplayBuffer(directory, budget, memory_limit=limit, max_bytes=len(fixture) + 1)
    if args.backend == "rust" and getattr(buffer, "backend", None) != "rust":
        raise RuntimeError("native backend was selected but not constructed")
    tasks: list[asyncio.Task] = []
    process = None
    native_source = None
    timings: dict[str, object] = {}
    start = time.perf_counter()
    cpu_start, child_start = time.process_time(), child_cpu_ms()

    async def produce():
        if args.producer_delay_ms:
            await asyncio.sleep(args.producer_delay_ms / 1000)
        for index in range(0, len(fixture), args.chunk_bytes):
            if index and args.chunk_interval_ms:
                await asyncio.sleep(args.chunk_interval_ms / 1000)
            await buffer.append(fixture[index:index + args.chunk_bytes])
            if index == 0:
                timings["provider_first_byte_ms"] = (time.perf_counter() - start) * 1000
            await asyncio.sleep(0)
        await buffer.finish()

    async def collect_replay(*, secondary: bool = False):
        digest, size = hashlib.sha256(), 0
        async for chunk in buffer.chunks(chunk_size=args.chunk_bytes):
            if not size and not secondary:
                timings["first_chunk_ms"] = (time.perf_counter() - start) * 1000
            digest.update(chunk)
            size += len(chunk)
            if secondary:
                await asyncio.sleep(0)
        if digest.hexdigest() != hashlib.sha256(fixture).hexdigest() or size != len(fixture):
            raise RuntimeError("replay bytes changed or disappeared")
        return size, digest.hexdigest()

    try:
        if args.case == "ffmpeg_native_source" and args.backend == "rust":
            from cogs.tts.replay_rust import native_module
            native = native_module()
            if getattr(native, "SOURCE_PROTOCOL_VERSION", None) != 1:
                raise RuntimeError("native source protocol 1 is required")
            native_source = await asyncio.to_thread(native.NativeSourceHandle, buffer._native,
                ["-f", "mp3", "-probesize", "512", "-analyzeduration", "0"],
                ["-loglevel", "error"], args.ffmpeg)
            timings["ffmpeg_spawn_ms"] = (time.perf_counter() - start) * 1000

            async def native_pcm():
                digest, size = hashlib.sha256(), 0
                while chunk := await asyncio.to_thread(native_source.read_frame):
                    if len(chunk) != FRAME_BYTES:
                        raise RuntimeError("native source emitted a partial Discord frame")
                    if size == 0:
                        timings["first_pcm_frame_ms"] = (time.perf_counter() - start) * 1000
                    digest.update(chunk)
                    size += len(chunk)
                if size == 0:
                    raise RuntimeError("native source emitted no PCM")
                return size, digest.hexdigest()

            tasks = [asyncio.create_task(produce()), asyncio.create_task(native_pcm())]
            results = await asyncio.wait_for(asyncio.gather(*tasks), args.timeout_seconds)
            size, digest = results[1]
            if not native_source.snapshot()["reaped"]:
                raise RuntimeError("native FFmpeg process was not reaped")
            timings["first_pcm_after_provider_ms"] = max(0.0, float(timings["first_pcm_frame_ms"])
                                                         - float(timings["provider_first_byte_ms"]))
        elif args.case in {"ffmpeg_progressive", "ffmpeg_native_source"}:
            process = await asyncio.create_subprocess_exec(
                args.ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-f", "mp3",
                "-probesize", "512", "-analyzeduration", "0", "-i", "pipe:0", "-vn",
                "-f", "s16le", "-ar", "48000", "-ac", "2", "pipe:1",
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE)
            timings["ffmpeg_spawn_ms"] = (time.perf_counter() - start) * 1000

            async def feed():
                async for chunk in buffer.chunks(chunk_size=args.chunk_bytes):
                    process.stdin.write(chunk)
                    await process.stdin.drain()
                process.stdin.close()
                with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                    await process.stdin.wait_closed()

            async def pcm():
                digest = hashlib.sha256()
                first = await process.stdout.readexactly(FRAME_BYTES)
                timings["first_pcm_frame_ms"] = (time.perf_counter() - start) * 1000
                digest.update(first)
                size = len(first)
                if args.case == "ffmpeg_native_source":
                    while True:
                        try:
                            chunk = await process.stdout.readexactly(FRAME_BYTES)
                        except asyncio.IncompleteReadError as error:
                            if error.partial:
                                chunk = error.partial.ljust(FRAME_BYTES, b"\0")
                                size += len(chunk)
                                digest.update(chunk)
                            break
                        size += len(chunk)
                        digest.update(chunk)
                else:
                    while chunk := await process.stdout.read(65536):
                        size += len(chunk)
                        digest.update(chunk)
                return size, digest.hexdigest()

            tasks = [asyncio.create_task(produce()), asyncio.create_task(feed()),
                     asyncio.create_task(pcm()), asyncio.create_task(process.stderr.read())]
            results = await asyncio.wait_for(asyncio.gather(*tasks), args.timeout_seconds)
            size, digest = results[2]
            if await process.wait():
                raise RuntimeError(f"FFmpeg failed: {results[3][-2048:].decode(errors='replace')}")
            timings["first_pcm_after_provider_ms"] = max(0.0, float(timings["first_pcm_frame_ms"])
                                                         - float(timings["provider_first_byte_ms"]))
        else:
            tasks = [asyncio.create_task(produce()), asyncio.create_task(collect_replay())]
            if args.case == "replay_two_readers":
                tasks.append(asyncio.create_task(collect_replay(secondary=True)))
            results = await asyncio.wait_for(asyncio.gather(*tasks), args.timeout_seconds)
            size, digest = results[1]
        timings.update(total_ms=(time.perf_counter() - start) * 1000,
                       process_cpu_ms=(time.process_time() - cpu_start) * 1000,
                       child_cpu_ms=child_cpu_ms() - child_start,
                       process_peak_rss_bytes=max_rss_bytes(), output_bytes=size,
                       input_sha256=hashlib.sha256(fixture).hexdigest(), output_sha256=digest)
        return timings
    finally:
        await stop_process(process)
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if native_source is not None:
            await asyncio.to_thread(native_source.cancel)
        buffer.close()
        if budget.used != 0:
            raise RuntimeError("memory budget leaked after replay close")


async def worker(args) -> dict[str, object]:
    started = datetime.now(timezone.utc).isoformat()
    sys.path.insert(0, os.fspath(ROOT))
    os.environ["TTS_REPLAY_RUST"] = "1" if args.backend == "rust" else "0"
    os.environ["TTS_BACKEND"] = "python"
    os.environ["TTS_POLICY_RUST"] = "0"
    native_sha256 = None
    native_path = None
    if args.backend == "rust":
        from cogs.tts.replay_rust import native_module
        native = native_module()  # Mandatory: do not benchmark fallback as native.
        native_path = Path(native.__file__).resolve()
        native_sha256 = file_sha256(native_path)
    fixture = make_fixture(args.ffmpeg, args.duration_seconds, args.timeout_seconds)
    with tempfile.TemporaryDirectory(prefix="tts-native-benchmark-") as directory:
        for _ in range(args.warmup):
            await trial(args, fixture, directory)
        baseline_rss = max_rss_bytes()
        samples = [await trial(args, fixture, directory) for _ in range(args.runs)]
    return {"backend": args.backend, "case": args.case, "runs": args.runs, "warmup": args.warmup,
            "started_utc": started, "finished_utc": datetime.now(timezone.utc).isoformat(),
            "native_sha256": native_sha256, "native_path": os.fspath(native_path) if native_path else None,
            "native_artifact_unchanged": file_sha256(native_path) == native_sha256 if native_path else None,
            "baseline_peak_rss_bytes": baseline_rss, "summary": summarize(samples),
            "samples": samples}


def positive(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return parsed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=positive, default=200)
    parser.add_argument("--warmup", type=int, default=3)
    parser.add_argument("--out", type=Path, default=ROOT / "native-dist/benchmark.json")
    parser.add_argument("--ffmpeg", default=shutil.which("ffmpeg") or "ffmpeg")
    parser.add_argument("--duration-seconds", type=float, default=2.0)
    parser.add_argument("--timeout-seconds", type=float, default=10.0)
    parser.add_argument("--memory-bytes", type=positive, default=64 * 1024)
    parser.add_argument("--chunk-bytes", type=positive, default=1024)
    parser.add_argument("--producer-delay-ms", type=float, default=0.0)
    parser.add_argument("--chunk-interval-ms", type=float, default=0.0)
    parser.add_argument("--cases", nargs="+", choices=SCENARIOS, default=list(SCENARIOS))
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--backend", choices=("python", "rust"), help=argparse.SUPPRESS)
    parser.add_argument("--case", choices=SCENARIOS, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.warmup < 0 or not 0 < args.duration_seconds <= 60 or not 0 < args.timeout_seconds <= 3600:
        parser.error("invalid warmup/duration/timeout")
    if args.producer_delay_ms < 0 or args.chunk_interval_ms < 0:
        parser.error("delays cannot be negative")
    if args.worker:
        if args.backend is None or args.case is None:
            parser.error("worker needs backend and case")
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(asyncio.run(worker(args)), indent=2) + "\n", encoding="utf-8")
        return 0
    started = datetime.now(timezone.utc).isoformat()
    results = []
    with tempfile.TemporaryDirectory(prefix="tts-native-results-") as temporary:
        for case in args.cases:
            for backend in ("python", "rust"):
                output = Path(temporary) / f"{case}-{backend}.json"
                command = [sys.executable, os.fspath(Path(__file__).resolve()), "--worker", "--backend", backend,
                           "--case", case, "--runs", str(args.runs), "--warmup", str(args.warmup),
                           "--out", os.fspath(output), "--ffmpeg", args.ffmpeg,
                           "--duration-seconds", str(args.duration_seconds), "--timeout-seconds", str(args.timeout_seconds),
                           "--memory-bytes", str(args.memory_bytes), "--chunk-bytes", str(args.chunk_bytes),
                           "--producer-delay-ms", str(args.producer_delay_ms), "--chunk-interval-ms", str(args.chunk_interval_ms)]
                print(f"Benchmark {case}/{backend}: {args.runs} measured + {args.warmup} warmup", flush=True)
                subprocess.run(command, check=True, cwd=ROOT)
                results.append(json.loads(output.read_text(encoding="utf-8")))
    comparisons = {}
    for case in args.cases:
        pair = {result["backend"]: result["summary"] for result in results if result["case"] == case}
        if pair["python"]["output_sha256"] != pair["rust"]["output_sha256"] or pair["python"]["output_bytes"] != pair["rust"]["output_bytes"]:
            raise RuntimeError(f"PCM/replay output changed across backends: {case}")
        metric = "first_pcm_frame_ms" if case.startswith("ffmpeg_") else "first_chunk_ms"
        old = pair["python"][metric]["p95"]
        new = pair["rust"][metric]["p95"]
        comparisons[case] = {"metric": metric, "python_p95_ms": old, "rust_p95_ms": new,
                             "change_percent": round((new / old - 1) * 100, 3) if old else None,
                             "output_identical": True}
    native_versions = sorted({result["native_sha256"] for result in results if result["native_sha256"] is not None})
    report = {"schema": "tts-native-benchmark-v1", "scope": "offline-replay-and-local-ffmpeg",
              "started_utc": started, "finished_utc": datetime.now(timezone.utc).isoformat(),
              "limitations": ["synthetic audio; no provider network", "no Discord player, gateway, or audible latency",
                              "sequential cases; no production contention", "RSS is process high-water mark including Python and imported native code",
                              "replay-only cases schedule Python thread-pool I/O; ffmpeg_native_source keeps compressed bytes in Rust",
                              "host load and clocks are not controlled by this script"],
              "acceptance_sample_size": args.runs >= 200,
              "single_native_artifact": len(native_versions) == 1,
              "native_artifact_unchanged": all(result["native_artifact_unchanged"] for result in results
                                               if result["backend"] == "rust"),
              "versions": {"python": platform.python_version(), "platform": platform.platform(),
                           "ffmpeg": subprocess.run([args.ffmpeg, "-version"], capture_output=True, text=True, check=True).stdout.splitlines()[0],
                           "native_sha256": native_versions},
              "config": {key: value for key, value in vars(args).items() if key not in {"out", "worker", "backend", "case"}},
              "comparisons": comparisons, "results": results}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(comparisons, indent=2), flush=True)
    print(f"Wrote {args.out}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
