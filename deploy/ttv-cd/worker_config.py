"""Read the installed worker configuration and render in an isolated process.

Never starts, updates, restarts or writes a Phone Worker release. The user's
trusted shell env file is evaluated exactly as the worker launcher evaluates it;
only renderer/runtime selectors are returned, never unrelated credentials.
"""
from __future__ import annotations

import array
import hashlib
import importlib.util
import io
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import wave


class WorkerConfigurationError(ValueError):
    pass


_SELECTORS = {"PHONE_WORKER_RUNTIME_ROOT", "PHONE_WORKER_DIR", "PATH", "LD_LIBRARY_PATH",
              "TMPDIR", "HOME", "PREFIX"}
_SEARCH_KEYS = {"YOUTUBE_API_KEY", "GOOGLE_YOUTUBE_API_KEY"}


def _allowed(key):
    return key in _SELECTORS or key.startswith("PHONE_WORKER_TETO_")


def load_environment(env_file=None, *, include_search_credentials=False):
    """Source trusted ~/.phone-worker.env without changing this process's env.

    The file is authoritative, as in start-phone-worker.sh. Shell expansions,
    exports and quoted values work; command output and source diagnostics are
    suppressed so secret values cannot appear in a report.
    """
    keys = _SELECTORS | (_SEARCH_KEYS if include_search_credentials else set())
    allowed = lambda key: key in keys or key.startswith("PHONE_WORKER_TETO_")
    environment = {key: value for key, value in os.environ.items() if allowed(key)}
    path = Path(env_file or os.getenv("PHONE_WORKER_ENV") or Path.home() / ".phone-worker.env").expanduser()
    if not path.exists():
        return environment
    if not path.is_file() or path.stat().st_size > 1024 * 1024:
        raise WorkerConfigurationError("invalid_worker_env_file")
    program = (
        "import json,os; keys=" + repr(sorted(keys)) + "; "
        "print(json.dumps({k:v for k,v in os.environ.items() "
        "if k in keys or k.startswith('PHONE_WORKER_TETO_')}))"
    )
    shell = shutil.which("bash")
    if not shell:
        raise WorkerConfigurationError("bash_required_for_worker_env")
    # Positional arguments avoid interpolating paths/configuration into code.
    script = 'set -a; source "$1" >/dev/null 2>&1 || exit 42; "$2" -c "$3"'
    try:
        result = subprocess.run([shell, "--noprofile", "--norc", "-c", script,
                                 "osaka-cd-env", str(path.resolve()), sys.executable, program],
                                capture_output=True, timeout=15)
        if result.returncode or len(result.stdout) > 1024 * 1024:
            raise WorkerConfigurationError("worker_env_load_failed")
        loaded = json.loads(result.stdout)
        if not isinstance(loaded, dict) or not all(isinstance(k, str) and isinstance(v, str)
                                                  and allowed(k) for k, v in loaded.items()):
            raise WorkerConfigurationError("worker_env_load_failed")
        environment.update(loaded)
        return environment
    except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
        raise WorkerConfigurationError("worker_env_load_failed") from exc


def configuration_fingerprint(env_file=None):
    """Opaque change detector; neither values nor paths are emitted."""
    env = load_environment(env_file)
    selected = {k: v for k, v in env.items() if k.startswith("PHONE_WORKER_TETO_")
                or k in {"PHONE_WORKER_RUNTIME_ROOT", "PHONE_WORKER_DIR"}}
    release = Path(env.get("PHONE_WORKER_RUNTIME_ROOT") or Path.home() / ".core-worker-runtime") / "current"
    selected["_release"] = str(release.resolve())
    return hashlib.sha256(json.dumps(selected, sort_keys=True).encode()).hexdigest()


def resource_ready(environment=None):
    env = environment if environment is not None else load_environment()
    process_env = dict(os.environ, **env)
    try:
        result = subprocess.run(["termux-battery-status"], capture_output=True, text=True,
                                timeout=8, env=process_env)
        battery = json.loads(result.stdout)
        level, temperature = battery.get("percentage"), battery.get("temperature")
        if result.returncode or type(level) is not int or not 0 <= level <= 100:
            return False
        if type(temperature) not in (int, float) or not math.isfinite(temperature) or temperature > 43:
            return False
        return level >= 25 or str(battery.get("status", "")).upper() in {"CHARGING", "FULL"}
    except (OSError, ValueError, TypeError, subprocess.TimeoutExpired):
        return False


def find_release(environment):
    root = Path(environment.get("PHONE_WORKER_RUNTIME_ROOT") or Path.home() / ".core-worker-runtime").expanduser()
    current = root / "current"
    if not current.exists():
        raise WorkerConfigurationError("worker_release_missing")
    release = current.resolve()
    if not release.is_dir() or not release.is_relative_to((root / "releases").resolve()):
        raise WorkerConfigurationError("worker_release_outside_releases")
    source = release / "phone_worker.py"
    if not source.is_file():
        raise WorkerConfigurationError("worker_version_missing")
    match = re.search(r'^PHONE_WORKER_VERSION\s*=\s*[\'\"]([^\'\"]+)', source.read_text(), re.M)
    if not match or match.group(1) != "1.11.41":
        raise WorkerConfigurationError("worker_1_11_41_required")
    return release


def _renderer_type(release):
    # Unique namespace prevents a previously imported old ~/phone-worker package
    # from shadowing the installed release. Relative imports remain unchanged.
    package = release / "teto_renderer"
    name = "_osaka_cd_installed_teto_" + hashlib.sha256(str(release).encode()).hexdigest()[:16]
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, package / "__init__.py",
                                                     submodule_search_locations=[str(package)])
        if not spec or not spec.loader:
            raise WorkerConfigurationError("worker_renderer_missing")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        previous_bytecode = sys.dont_write_bytecode
        try:
            # Loading a read-only installed release must not create/update pyc
            # files inside that release, including relative submodule imports.
            sys.dont_write_bytecode = True
            spec.loader.exec_module(module)
        except Exception:
            sys.modules.pop(name, None)
            raise
        finally:
            sys.dont_write_bytecode = previous_bytecode
    return sys.modules[name].TetoRenderer


def render_sample(melody, target):
    """Produce and decode an MP3 before atomically replacing the old sample."""
    try:
        environment = load_environment()
        release = find_release(environment)
        if not resource_ready(environment):
            return {"status": "pending_battery_or_battery_status", "retryable": True}
        target = Path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        environment["PHONE_WORKER_TETO_FRAGMENT_CACHE_DIR"] = str(target.parent / ".utau-fragments")
        renderer = _renderer_type(release)(environment=environment, voice_id="kasane-teto",
                                           resource_guard=lambda: {"ok": resource_ready(environment)})
        score = {"version": 1, "track_index": 0, "duration_ms": melody["duration_ms"],
                 "lyrics_source": "message", "notes": melody["notes"]}
        raw, metadata = renderer.synthesize_performance(
            " ".join(["lá"] * len(score["notes"])), score=score, reading_language="pt-BR",
            pitch_offset_semitones=0, speech_rate=1.0, timeout_seconds=95,
            max_audio_bytes=12 * 1024 * 1024)
        with wave.open(io.BytesIO(raw), "rb") as audio:
            if audio.getsampwidth() != 2 or audio.getnchannels() != 1 or not audio.getnframes():
                raise WorkerConfigurationError("invalid_renderer_audio")
            samples = array.array("h", audio.readframes(audio.getnframes()))
            if sys.byteorder != "little":
                samples.byteswap()
            if not max((abs(n) for n in samples), default=0):
                raise WorkerConfigurationError("silent_renderer_audio")
        with tempfile.TemporaryDirectory(prefix=".cd-sample-", dir=target.parent) as temp:
            candidate = Path(temp) / "sample.mp3"
            ffmpeg = subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", "pipe:0", "-vn",
                                     "-codec:a", "libmp3lame", "-b:a", "128k", str(candidate)],
                                    input=raw, capture_output=True, timeout=40)
            if ffmpeg.returncode or not candidate.is_file() or not 256 < candidate.stat().st_size <= 2_000_000:
                raise WorkerConfigurationError("mp3_encoding_failed")
            decoded = subprocess.run(["ffmpeg", "-v", "error", "-xerror", "-i", str(candidate),
                                      "-f", "null", "-"], capture_output=True, timeout=45)
            if decoded.returncode:
                raise WorkerConfigurationError("mp3_decode_failed")
            os.replace(candidate, target)
        return {"status": "ready", "retryable": False, "worker_version": "1.11.41",
                "renderer_version": metadata.get("renderer_version"),
                "renderer_fingerprint": metadata.get("renderer_fingerprint"),
                "mp3_sha256": hashlib.sha256(target.read_bytes()).hexdigest()}
    except Exception as exc:
        # Never propagate error text, paths, subprocess diagnostics or env values.
        code = str(exc) if isinstance(exc, WorkerConfigurationError) else type(exc).__name__
        return {"status": "pending_" + code, "retryable": True}
