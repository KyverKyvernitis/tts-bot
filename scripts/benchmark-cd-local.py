#!/usr/bin/env python3
"""Measure real local CD extraction/publication/PCM composition in isolation.

No YouTube, Discord, VPS connection or live UTAU voicebank is used. Results
describe this machine's filesystem/FFmpeg/tone fixture only, not phone singing
latency. Run from either VPS or Termux after installing Pillow and FFmpeg.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import argparse
import array
import hashlib
import io
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
import wave
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "deploy/ttv-cd"))


def percentile(values, fraction):
    values = sorted(values)
    position = (len(values) - 1) * fraction
    low, high = int(position), min(int(position) + 1, len(values) - 1)
    return values[low] + (values[high] - values[low]) * (position - low)


def summary(values):
    return {"runs": len(values), "p50_ms": round(percentile(values, .5), 3),
            "p95_ms": round(percentile(values, .95), 3), "min_ms": round(min(values), 3),
            "max_ms": round(max(values), 3)}


def measure(function):
    started, cpu = time.perf_counter(), time.process_time()
    child = resource.getrusage(resource.RUSAGE_CHILDREN)
    result = function()
    ended = resource.getrusage(resource.RUSAGE_CHILDREN)
    return result, {"wall_ms": (time.perf_counter() - started) * 1000,
                    "python_cpu_ms": (time.process_time() - cpu) * 1000,
                    "subprocess_cpu_ms": (ended.ru_utime + ended.ru_stime - child.ru_utime - child.ru_stime) * 1000}


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def fixture_package(path, identity, audio, cover):
    from package_validation import score_sha256
    video_id = digest(identity.encode())[:11]
    score = {"id": identity, "name": identity, "artist": "Benchmark", "excerpt": "Isolated tone fixture",
             "source_url": "", "source_sha256": "a" * 64, "revision": 1, "duration_ms": 500,
             "notes": [{"midi_note": 60, "start_ms": 0, "duration_ms": 500, "velocity": 100}]}
    score["score_sha256"] = score_sha256(score)
    preview = {"manifest_version": 2, "id": identity, "revision": 1, "source_sha256": "a" * 64,
               "score_sha256": score["score_sha256"], "voice_id": "kasane-teto", "language": "pt-BR",
               "pitch_semitones": 0, "speech_rate": 1.0, "mp3_sha256": digest(audio)}
    image_path, audio_path = f"cogs/tts/cd/thumbnails/{video_id}.jpg", f"cogs/tts/cd/previews/{identity}.mp3"
    package = {"manifest_version": 1, "id": identity, "revision": 1, "source_sha256": "a" * 64,
               "score_sha256": score["score_sha256"], "artifacts": {
                   "score": {"status": "ready", "sha256": score["score_sha256"]},
                   "thumbnail": {"status": "ready", "path": image_path, "sha256": digest(cover)},
                   "sample": {"status": "ready", "path": audio_path, "sha256": digest(audio)}}}
    members = {"cogs/tts/cd_song_scores.json": canonical([score]),
               "cogs/tts/cd_song_videos.json": canonical({"version": 1, "songs": {
                   identity: {"video_id": video_id, "thumbnail_sha256": digest(cover)}}}),
               "cogs/tts/cd/previews/index.json": canonical({identity: preview}),
               f"cogs/tts/cd/packages/{identity}.json": canonical(package),
               image_path: cover, audio_path: audio}
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return path


def fixture_arrangement():
    return {"score_schema": 2, "duration_ms": 1000, "parts": [
        {"id": "lead", "role": "lead", "voice_id": "kasane-teto", "lyrics_source": "vowel",
         "notes": [{"midi_note": 60, "start_ms": 0, "duration_ms": 1000, "velocity": 100}], "gain": .85},
        {"id": "harmony", "role": "harmony", "voice_id": "kasane-teto", "lyrics_source": "vowel",
         "notes": [{"midi_note": 64, "start_ms": 0, "duration_ms": 1000, "velocity": 100}], "gain": .5}]}


def pcm_renderer(**request):
    rate, preroll = 48000, 100
    frames = round((request["score"]["duration_ms"] + preroll) * rate / 1000)
    samples = array.array("h", [0]) * frames
    for note in request["score"]["notes"]:
        frequency = 440 * 2 ** ((note["midi_note"] - 69) / 12)
        start = round((note["start_ms"] + preroll) * rate / 1000)
        end = min(frames, start + round(note["duration_ms"] * rate / 1000))
        for index in range(start, end):
            samples[index] = round(8000 * math.sin(2 * math.pi * frequency * (index - start) / rate))
    if sys.byteorder != "little":
        samples.byteswap()
    result = io.BytesIO()
    with wave.open(result, "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(rate)
        target.writeframes(samples.tobytes())
    return result.getvalue(), {"midi_preroll_ms": preroll}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, default=10)
    parser.add_argument("--midi", type=Path, help="Optional real local MIDI; remains unmodified")
    parser.add_argument("--output", type=Path, help="Write report JSON in addition to stdout")
    args = parser.parse_args()
    if not 1 <= args.runs <= 100:
        parser.error("--runs must be between 1 and 100")
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        parser.error("FFmpeg and ffprobe are required")
    from PIL import Image
    from osaka_cd.store import RuntimeStore
    from osaka_cd.render import render_arrangement
    records = {label: [] for label in ("publish_validate_commit", "hot_catalog_read", "composition_cold",
                                       "composition_cached", "mix_change_cached_stems", "publish_concurrent_four")}
    detail = {"physical_utau_rendered": False, "network_called": False, "discord_called": False,
              "note": "Local tone/PCM/FFmpeg benchmark; no claim about phone voicebank latency.",
              "python": platform.python_version(), "platform": platform.platform(), "runs": args.runs}
    configured = os.environ.get("OSAKA_CD_DATA_ROOT")
    with tempfile.TemporaryDirectory(prefix="osaka-cd-benchmark-") as temporary:
        root = Path(temporary)
        legacy = root / "legacy"
        (legacy / "cogs/tts/cd/previews").mkdir(parents=True)
        (legacy / "cogs/tts/cd_song_scores.json").write_text("[]")
        (legacy / "cogs/tts/cd_song_videos.json").write_text('{"songs":{}}')
        (legacy / "cogs/tts/cd/previews/index.json").write_text("{}")
        store = RuntimeStore(root / "store", legacy)
        os.environ["OSAKA_CD_DATA_ROOT"] = str(store.root)
        try:
            mp3 = root / "tone.mp3"
            subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-f", "lavfi", "-i",
                            "sine=frequency=261.625565:duration=0.5:sample_rate=48000", "-c:a", "libmp3lame",
                            "-b:a", "128k", "-y", str(mp3)], check=True, capture_output=True, timeout=20)
            audio = mp3.read_bytes()
            image = io.BytesIO()
            Image.new("RGB", (64, 64), "red").save(image, format="JPEG")
            cover = image.getvalue()
            for index in range(args.runs):
                package = fixture_package(root / f"single-{index}.zip", f"bench-{index}", audio, cover)
                def publish():
                    job = store.receive(package)
                    result = store.process(job["job_id"])
                    if not result["ok"] or result["state"] != "applied":
                        raise RuntimeError("isolated publication failed: " + str(result))
                    return result
                _, metrics = measure(publish)
                records["publish_validate_commit"].append(metrics)
            from osaka_cd import runtime
            from cogs.tts import melodies, cd_previews
            runtime.invalidate()
            preferences = {"ttv_voice_id": "kasane-teto", "ttv_language": "pt-BR"}
            with runtime.pin_snapshot():
                melodies.get_melody("bench-0")
                cd_previews.prepared_sample("bench-0", preferences)
                for _ in range(max(30, args.runs)):
                    def read():
                        melody = melodies.get_melody("bench-0")
                        ready = cd_previews.prepared_sample("bench-0", preferences)
                        if melody.id != "bench-0" or ready != audio:
                            raise RuntimeError("hot reader lost a valid artifact")
                    _, metrics = measure(read)
                    records["hot_catalog_read"].append(metrics)
            for index in range(args.runs):
                cache = root / f"composition-{index}"
                value = fixture_arrangement()
                def compose():
                    return render_arrangement(value, "a", pcm_renderer, cache_dir=cache,
                                              render_profile_fingerprint=digest(b"isolated-tone-fixture"), output_format="mp3")
                cold, metrics = measure(compose)
                records["composition_cold"].append(metrics)
                warm, metrics = measure(compose)
                records["composition_cached"].append(metrics)
                if cold["sha256"] != warm["sha256"] or not all(stem["cache_hit"] for stem in warm["stems"]):
                    raise RuntimeError("composition cache did not reuse the correct stems")
                value["parts"][1]["gain"] = .2
                changed, metrics = measure(compose)
                records["mix_change_cached_stems"].append(metrics)
                if not all(stem["cache_hit"] for stem in changed["stems"]) or changed["sha256"] == cold["sha256"]:
                    raise RuntimeError("mix controls did not preserve stem reuse")
            for index in range(args.runs):
                packages = [fixture_package(root / f"parallel-{index}-{part}.zip",
                                             f"parallel-{index}-{part}", audio, cover) for part in range(4)]
                jobs = [store.receive(package) for package in packages]
                def concurrent():
                    with ThreadPoolExecutor(max_workers=4) as pool:
                        result = list(pool.map(lambda job: store.process(job["job_id"]), jobs))
                    if any(not job["ok"] for job in result):
                        raise RuntimeError("concurrent publication failed")
                _, metrics = measure(concurrent)
                records["publish_concurrent_four"].append(metrics)
            if args.midi:
                from midi_extract import extract_arrangement
                before = digest(args.midi.read_bytes())
                measures = []
                for _ in range(args.runs):
                    extracted, metrics = measure(lambda: extract_arrangement(args.midi))
                    measures.append(metrics)
                if digest(args.midi.read_bytes()) != before:
                    raise RuntimeError("benchmark unexpectedly changed the MIDI")
                records["midi_extract_arrangement"] = measures
                value = extracted["arrangement"]
                detail["midi"] = {"sha256": before, "duration_ms": value["duration_ms"],
                                  "source_note_count": value["source"]["note_count"],
                                  "parts": len(value["parts"])}
            detail["catalog_songs"] = len(store.snapshot()["versions"])
        finally:
            if configured is None:
                os.environ.pop("OSAKA_CD_DATA_ROOT", None)
            else:
                os.environ["OSAKA_CD_DATA_ROOT"] = configured
    report = {"ok": True, **detail, "measurements": {
        label: {metric: summary([entry[metric] for entry in measurements])
                for metric in ("wall_ms", "python_cpu_ms", "subprocess_cpu_ms")}
        for label, measurements in records.items()}, "records": records}
    rendered = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")


if __name__ == "__main__":
    main()
