#!/usr/bin/env python3
"""Benchmark offline de primeiro PCM: FFmpeg real e gTTS local simulado.

Exemplo: .venv/bin/python scripts/benchmark-tts-local.py --runs 10
Não acessa Edge, Google ou Discord. A leitura de AudioSource usa discord.py
real, mas não mede pacotes de voz enviados nem o áudio ouvido por um usuário.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
PCM_FRAME_BYTES = 3840  # 20 ms, s16le, estéreo, 48 kHz, como discord.py.
EFFECTS = (("none", 0, 0, 0), ("nightcore3_reverb3", 3, 0, 3),
           ("slowed3_reverb3", 0, 3, 3))


def percentile(values, fraction):
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def summarize(records):
    names = sorted({name for record in records for name, value in record.items()
                    if isinstance(value, (float, int)) and name.endswith("_ms")})
    return {name: {"n": len(values), "p50_ms": round(percentile(values, .5), 3),
                   "p95_ms": round(percentile(values, .95), 3)}
            for name in names
            if (values := [float(record[name]) for record in records if name in record])}


def load_effect_filters(repo):
    spec = importlib.util.spec_from_file_location(
        "tts_benchmark_effects", repo / "cogs/musica/runtime_telefone/agente/efeitos.py")
    if spec is None or spec.loader is None:
        raise RuntimeError("não foi possível carregar os filtros de efeitos do repositório")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return {label: module.filtros_tts(engine="gtts", nightcore_level=night,
                                    slowed_level=slow, reverb_level=reverb)
            for label, night, slow, reverb in EFFECTS}


def make_fixture(ffmpeg, duration, timeout):
    # MP3 mono/24 kHz lembra a entrada dos provedores, sem dados aleatórios.
    result = subprocess.run(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-f", "lavfi",
         "-i", "sine=frequency=440:sample_rate=24000", "-t", str(duration),
         "-ac", "1", "-c:a", "libmp3lame", "-b:a", "48k", "-write_xing", "0",
         "-id3v2_version", "0", "-f", "mp3", "pipe:1"],
        check=True, capture_output=True, timeout=timeout)
    if len(result.stdout) < 2048:
        raise RuntimeError("fixture MP3 insuficiente para comparar probesizes")
    return result.stdout


async def stop_process(process):
    if process.returncode is None:
        with contextlib.suppress(ProcessLookupError):
            process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=.5)
        except asyncio.TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            await asyncio.wait_for(process.wait(), timeout=1)


async def ffmpeg_trial(args, mp3, effect_filter, *, progressive, probesize):
    command = [args.ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin",
               "-f", "mp3", "-probesize", str(probesize), "-analyzeduration", "0",
               "-i", "pipe:0", "-vn"]
    if effect_filter:
        command += ["-af", effect_filter]
    command += ["-f", "s16le", "-ar", "48000", "-ac", "2", "pipe:1"]
    start = time.monotonic()
    process = await asyncio.wait_for(asyncio.create_subprocess_exec(
        *command, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE), timeout=args.timeout_seconds)
    timings = {"ffmpeg_spawn_ms": (time.monotonic() - start) * 1000}
    tasks = []

    async def feed():
        if progressive:
            await asyncio.sleep(args.initial_delay_ms / 1000)
        chunks = range(0, len(mp3), args.chunk_bytes) if progressive else (0,)
        for index in chunks:
            if index:
                await asyncio.sleep(args.chunk_interval_ms / 1000)
            if not index:
                timings["input_first_chunk_ms"] = (time.monotonic() - start) * 1000
            process.stdin.write(mp3[index:index + args.chunk_bytes] if progressive else mp3)
            await process.stdin.drain()
        timings["input_complete_ms"] = (time.monotonic() - start) * 1000
        process.stdin.close()

    async def drain_pcm():
        digest = hashlib.sha256()
        first = await process.stdout.readexactly(PCM_FRAME_BYTES)
        timings["first_pcm_frame_ms"] = (time.monotonic() - start) * 1000
        digest.update(first)
        count = len(first)
        while chunk := await process.stdout.read(65536):
            count += len(chunk)
            digest.update(chunk)
        return count, digest.hexdigest()

    async def run():
        tasks.extend((asyncio.create_task(feed()), asyncio.create_task(drain_pcm()),
                      asyncio.create_task(process.stderr.read())))
        _, (byte_count, digest), stderr = await asyncio.gather(*tasks)
        if await process.wait():
            raise RuntimeError(f"FFmpeg falhou: {stderr[-2048:].decode(errors='replace')}")
        if byte_count < PCM_FRAME_BYTES * 10:
            raise RuntimeError("FFmpeg produziu menos de 200 ms de PCM")
        timings["decode_complete_ms"] = (time.monotonic() - start) * 1000
        timings["first_pcm_after_input_ms"] = max(
            0, timings["first_pcm_frame_ms"] - timings["input_first_chunk_ms"])
        return {**timings, "pcm_bytes": byte_count, "pcm_sha256": digest}

    try:
        remaining = max(.001, args.timeout_seconds - (time.monotonic() - start))
        return await asyncio.wait_for(run(), timeout=remaining)
    finally:
        await stop_process(process)
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def make_runtime_probe(audio, mp3, args):
    class Probe(audio.TTSAudioMixin):
        def __init__(self):
            self.guild_states = {}
            self.edge_voice_names = set()
            self.bot = SimpleNamespace(audio_router=None, get_guild=lambda _: None)
            self.sources = []
            self.provider_stops = []
            self.started_at = time.monotonic()
            self.timings = {}

        def _get_db(self):
            return None

        def _tts_agent_route_available(self):
            return False

        def _tts_agent_should_try_worker(self, item):
            return False, "benchmark_offline"

        def _worker_stream_available_for(self, item):
            return False

        def _try_get_cached_path(self, state, item):
            return None

        def _schedule_persistent_synt_success(self, *args):
            return None

        async def _shared_job_file(self, state, item, *, store_in_cache):
            return await super()._shared_job_file(state, item, store_in_cache=False)

        async def _prepare_gtts_stream(self, state, item, *, store_in_cache, tld="com"):
            return await super()._prepare_gtts_stream(state, item, store_in_cache=False, tld=tld)

        def _make_discord_tts_source(self, path, *, item=None):
            source, kind = super()._make_discord_tts_source(path, item=item)
            self.sources.append(source)
            self.timings.setdefault("ffmpeg_source_created_ms", self.elapsed())
            return source, kind

        def elapsed(self):
            return (time.monotonic() - self.started_at) * 1000

        def _gtts_stream_blocking(self, handle, loop, language, tld):
            # gTTS curto entrega um MP3 completo após a única resposta HTTP.
            # Simulamos exatamente essa fronteira: nenhum objeto gTTS/rede.
            self.provider_stops.append(handle.stop_requested)
            if handle.stop_requested.wait(args.runtime_provider_delay_ms / 1000):
                return
            self.timings["provider_first_chunk_ms"] = self.elapsed()
            self.timings["provider_complete_ms"] = self.elapsed()
            bridge = asyncio.run_coroutine_threadsafe(
                self._accept_gtts_stream_chunk(handle, mp3), loop)
            bridge.result(timeout=args.timeout_seconds)

    return Probe()


async def runtime_trial(audio, args, mp3, effect, min_chars):
    probe = make_runtime_probe(audio, mp3, args)
    _, night, slow, reverb = effect
    item = audio.QueueItem(guild_id=1, channel_id=2, author_id=3,
                          text="Uma frase curta para medir a preparação local do áudio.",
                          engine="gtts", voice="", language="pt", rate="+0%", pitch="+0Hz",
                          advanced_nightcore_level=night, advanced_slowed_level=slow,
                          advanced_reverb_level=reverb)
    prepared = None
    path = None
    observed = None

    async def run():
        nonlocal prepared, path, observed
        audio_task = asyncio.create_task(probe._resolve_audio_path(
            probe._get_state(1), item, allow_edge_stream=True))
        path, _, prepared = await probe._resolve_and_prime_audio(audio_task, item)
        if prepared is None:
            raise RuntimeError("AudioSource não pôde ser preparado")
        if prepared.source.is_opus():
            raise RuntimeError("benchmark de PCM exige libopus acessível ao discord.py")
        probe.timings["source_primed_ms"] = probe.elapsed()

        def on_frame(frame_at, read_ms):
            probe.timings["audio_source_first_frame_read_ms"] = (
                frame_at - probe.started_at) * 1000
            probe.timings["audio_source_first_read_duration_ms"] = read_ms

        observed = audio._FirstFrameAudioSource(prepared.source, on_frame)

        def consume():
            digest = hashlib.sha256()
            count = 0
            while chunk := observed.read():
                count += len(chunk)
                digest.update(chunk)
            return count, digest.hexdigest()

        count, digest = await asyncio.to_thread(consume)
        if count < PCM_FRAME_BYTES * 10:
            raise RuntimeError("AudioSource produziu menos de 200 ms de PCM")
        probe.timings["source_first_frame_after_provider_ms"] = max(
            0, probe.timings["audio_source_first_frame_read_ms"] -
            probe.timings["provider_first_chunk_ms"])
        return {**probe.timings, "pcm_bytes": count, "pcm_sha256": digest,
                "audio_route": getattr(item, "_tts_audio_origin", "unknown"),
                "source_kind": prepared.source_kind}

    with patch.object(audio, "TTS_GTTS_STREAM_MIN_CHARS", min_chars):
        try:
            return await asyncio.wait_for(run(), timeout=args.timeout_seconds)
        finally:
            for stop in probe.provider_stops:
                stop.set()
            # Cleanup também encerra o FFmpeg se um .read() exceder o prazo.
            if observed is not None:
                observed.cleanup()
            for source in probe.sources:
                with contextlib.suppress(Exception):
                    source.cleanup()
            if path:
                await probe._discard_edge_stream_path(path)
            executor = getattr(probe, "_tts_gtts_executor", None)
            jobs = list(probe._shared_synthesis_jobs().values())
            probe._release_item_audio(item)
            probe._shutdown_tts_runtime()
            await asyncio.gather(*(job.task for job in jobs if job.task), return_exceptions=True)
            if executor is not None:
                await asyncio.to_thread(executor.shutdown, wait=True, cancel_futures=True)


async def benchmark(args, mp3, filters, audio):
    cases = []
    for effect in EFFECTS:
        label = effect[0]
        for size in (2048, 1024, 512):
            cases.append((f"progressive_mp3/{label}/probesize{size}", "ffmpeg",
                          dict(effect_filter=filters[label], progressive=True, probesize=size)))
        cases.append((f"complete_mp3/{label}/probesize2048", "ffmpeg",
                      dict(effect_filter=filters[label], progressive=False, probesize=2048)))
        if audio is not None:
            for min_chars in (101, 1):
                cases.append((f"gtts_short_runtime/{label}/min_chars{min_chars}", "runtime",
                              dict(effect=effect, min_chars=min_chars)))
    records = {name: [] for name, _, _ in cases}
    failures = []
    # Alternar a ordem de forma determinística reduz o viés da ordem de execução.
    for iteration in range(args.warmup + args.runs):
        offset = iteration % len(cases)
        for name, kind, options in cases[offset:] + cases[:offset]:
            try:
                record = (await ffmpeg_trial(args, mp3, **options) if kind == "ffmpeg"
                          else await runtime_trial(audio, args, mp3, **options))
                if iteration >= args.warmup:
                    records[name].append(record)
            except Exception as error:
                failures.append({"case": name, "iteration": iteration,
                                 "warmup": iteration < args.warmup,
                                 "error": f"{type(error).__name__}: {error}"})
    result = {}
    for name, samples in records.items():
        if not samples:
            result[name] = {"n": 0}
            continue
        hashes = {sample["pcm_sha256"] for sample in samples}
        if len(hashes) != 1:
            failures.append({"case": name, "error": "PCM variou entre repetições"})
        result[name] = {"n": len(samples), "metrics": summarize(samples),
                        "pcm_bytes": sorted({sample["pcm_bytes"] for sample in samples}),
                        "pcm_sha256": sorted(hashes)}
        for key in ("audio_route", "source_kind"):
            if key in samples[0]:
                result[name][key] = sorted({sample[key] for sample in samples})
    # Decodificar progressivamente não deve alterar o PCM dos mesmos filtros.
    for effect in EFFECTS:
        reference = result[f"complete_mp3/{effect[0]}/probesize2048"].get("pcm_sha256")
        if reference is None:
            continue
        for size in (2048, 1024, 512):
            name = f"progressive_mp3/{effect[0]}/probesize{size}"
            if result[name].get("pcm_sha256") != reference:
                failures.append({"case": name, "error": "PCM difere do arquivo completo"})
        if audio is not None:
            file_name = f"gtts_short_runtime/{effect[0]}/min_chars101"
            stream_name = f"gtts_short_runtime/{effect[0]}/min_chars1"
            if result[file_name].get("pcm_sha256") != result[stream_name].get("pcm_sha256"):
                failures.append({"case": stream_name, "error": "PCM difere do caminho gTTS arquivo"})
    return result, failures


def positive_int(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("deve ser maior que zero")
    return number


def nonnegative_float(value):
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise argparse.ArgumentTypeError("deve ser finito e não negativo")
    return number


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=positive_int, default=7)
    parser.add_argument("--warmup", type=int, default=1)
    parser.add_argument("--repo", type=Path, default=ROOT)
    parser.add_argument("--ffmpeg", default=shutil.which("ffmpeg") or "ffmpeg")
    parser.add_argument("--skip-runtime", action="store_true",
                        help="mede apenas FFmpeg, sem dependências Python do bot")
    parser.add_argument("--timeout-seconds", type=nonnegative_float, default=10)
    parser.add_argument("--duration-seconds", type=nonnegative_float, default=2)
    parser.add_argument("--initial-delay-ms", type=nonnegative_float, default=80)
    parser.add_argument("--chunk-interval-ms", type=nonnegative_float, default=8)
    parser.add_argument("--chunk-bytes", type=positive_int, default=512)
    parser.add_argument("--runtime-provider-delay-ms", type=nonnegative_float, default=120)
    args = parser.parse_args()
    if args.warmup < 0 or args.timeout_seconds < 1 or args.duration_seconds < .5:
        parser.error("warmup deve ser >= 0, timeout >= 1 e duração >= 0.5")
    args.repo = args.repo.resolve()
    version = subprocess.run([args.ffmpeg, "-version"], check=True,
                             capture_output=True, text=True, timeout=args.timeout_seconds)
    mp3 = make_fixture(args.ffmpeg, args.duration_seconds, args.timeout_seconds)
    filters = load_effect_filters(args.repo)
    temp_parent = "/dev/shm" if os.path.isdir("/dev/shm") and os.access("/dev/shm", os.W_OK) else None
    with tempfile.TemporaryDirectory(prefix="tts-local-benchmark-", dir=temp_parent) as tmp:
        # O import do bot cria diretórios temporários; configure-o antes de importar.
        with patch.dict(os.environ, {"TTS_TEMP_DIR": tmp}):
            audio = None
            versions = {"python": platform.python_version(), "ffmpeg": version.stdout.splitlines()[0]}
            runtime_settings = None
            if not args.skip_runtime:
                if Path(shutil.which(args.ffmpeg) or args.ffmpeg).resolve() != Path(
                        shutil.which("ffmpeg") or "ffmpeg").resolve():
                    parser.error("--ffmpeg customizado exige --skip-runtime; discord.py usa FFmpeg do PATH")
                sys.path.insert(0, str(args.repo))
                import cogs.tts.audio as audio
                import discord
                import edge_tts
                import gtts
                versions.update(discord_py=discord.__version__, gtts=gtts.__version__,
                                edge_tts=edge_tts.__version__)
                runtime_settings = {"stream_probesize_bytes": getattr(
                    audio, "TTS_STREAM_FFMPEG_PROBESIZE_BYTES", 2048),
                    "provider": "simulated_blocking_single_complete_mp3_chunk",
                    "cold_source": True, "cache": False, "overlap": True}
            # Fixar flags evita que .env local contamine esta comparação offline.
            with contextlib.ExitStack() as stack:
                if audio is not None:
                    for name, value in {"TTS_GTTS_STREAMING_ENABLED": True,
                                        "TTS_FFMPEG_PRIME_ENABLED": True,
                                        "TTS_EDGE_FFMPEG_MP3_INPUT_HINT_ENABLED": True,
                                        "TTS_FFMPEG_BEFORE_OPTIONS": "-nostdin",
                                        "TTS_FFMPEG_OPTIONS": "-vn -loglevel error",
                                        "TTS_FFMPEG_PRIME_TIMEOUT_SECONDS": args.timeout_seconds}.items():
                        stack.enter_context(patch.object(audio, name, value))
                    stack.enter_context(patch.object(audio.config, "TTS_FFMPEG_OVERLAP_ENABLED", True, create=True))
                    stack.enter_context(patch.object(audio.config, "TTS_PREPARED_OPUS_CACHE_ENABLED", False, create=True))
                cases, failures = asyncio.run(benchmark(args, mp3, filters, audio))
        report = {"schema": 1, "scope": "offline_local_first_pcm_and_audio_source_read",
                  "metric_origin": "trial_start_monotonic_except_explicit_durations_or_after_input",
                  "discord_voice_first_frame_ms": None,
                  "limitations": ["synthetic_sine_mp3_and_provider_delay",
                                  "no_network_provider_voice_player_or_user_audio",
                                  "sequential_cold_sources_without_production_queue_or_cpu_contention",
                                  "p95_interpolated_from_small_local_sample"],
                  "versions": versions, "runs": args.runs, "warmup_per_case": args.warmup,
                  "runtime_settings": runtime_settings,
                  "timeout_seconds_per_trial": args.timeout_seconds,
                  "temp_storage": "ram" if temp_parent else "system_temp",
                  "fixture": {"mp3_bytes": len(mp3), "mp3_sha256": hashlib.sha256(mp3).hexdigest(),
                              "duration_seconds": args.duration_seconds, "sample_rate": 24000,
                              "bitrate_kbps": 48, "chunk_bytes": args.chunk_bytes,
                              "initial_delay_ms": args.initial_delay_ms,
                              "chunk_interval_ms": args.chunk_interval_ms,
                              "runtime_provider_delay_ms": args.runtime_provider_delay_ms},
                  "filters": filters, "cases": cases, "failures": failures}
        print(json.dumps(report, ensure_ascii=False, separators=(",", ":")))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
