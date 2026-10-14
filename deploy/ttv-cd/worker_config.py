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
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import wave


class WorkerConfigurationError(ValueError):
    pass


_SELECTORS = {"PHONE_WORKER_RUNTIME_ROOT", "PHONE_WORKER_DIR", "PATH", "LD_LIBRARY_PATH",
              "TMPDIR", "HOME", "PREFIX"}
_SEARCH_KEYS = {"YOUTUBE_API_KEY", "GOOGLE_YOUTUBE_API_KEY"}
_TRANSPORT_KEYS = {"PHONE_WORKER_TOKEN", "PHONE_WORKER_HOST", "PHONE_WORKER_PORT",
                   "CORE_WORKER_TOKEN", "CORE_WORKER_DIRECT_HTTP_TOKEN", "CORE_WORKER_APK_HTTP_TOKEN"}
_RESOURCE_CACHE = {}
_RESOURCE_LOCK = threading.Lock()


def _allowed(key):
    return key in _SELECTORS or key.startswith("PHONE_WORKER_TETO_")


def load_environment(env_file=None, *, include_search_credentials=False, include_worker_credentials=False):
    """Source trusted ~/.phone-worker.env without changing this process's env.

    The file is authoritative, as in start-phone-worker.sh. Shell expansions,
    exports and quoted values work; command output and source diagnostics are
    suppressed so secret values cannot appear in a report.
    """
    keys = (_SELECTORS | (_SEARCH_KEYS if include_search_credentials else set())
            | (_TRANSPORT_KEYS if include_worker_credentials else set()))
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
    # Replacing recordings or oto timing at the same configured path changes
    # synthesis too. Match the worker's file identity convention without
    # loading another renderer or writing into an active voicebank/runtime.
    bank = Path(env.get("PHONE_WORKER_TETO_ENGLISH_VOICEBANK_DIR")
                or Path.home() / "voicebanks" / "kasane-teto-english").expanduser()
    inventory = hashlib.sha256()
    try:
        count = 0
        for path in sorted(bank.rglob("*")):
            if path.is_file() and path.suffix.lower() in {".wav", ".ini", ".map"}:
                count += 1
                if count > 50000:
                    raise WorkerConfigurationError("voicebank_inventory_limit")
                stamp = path.stat()
                inventory.update(str(path.relative_to(bank)).encode())
                inventory.update(f":{stamp.st_size}:{stamp.st_mtime_ns}:{stamp.st_ctime_ns}".encode())
    except OSError:
        inventory.update(b"inventory-unavailable")
    selected["_bank_inventory"] = inventory.hexdigest()
    try:
        command = shlex.split(env.get("PHONE_WORKER_TETO_RESAMPLER_COMMAND", ""))
        executable = shutil.which(command[0], path=env.get("PATH")) if command else None
        if executable:
            stamp = Path(executable).stat()
            selected["_resampler_file"] = f"{stamp.st_size}:{stamp.st_mtime_ns}:{stamp.st_ctime_ns}"
    except (OSError, ValueError):
        selected["_resampler_file"] = "unavailable"
    return hashlib.sha256(json.dumps(selected, sort_keys=True).encode()).hexdigest()


def resource_ready(environment=None):
    env = environment if environment is not None else load_environment()
    cache_key = hashlib.sha256(json.dumps({key: value for key, value in env.items()
                                          if key in {"PATH", "PREFIX", "HOME"}}, sort_keys=True).encode()).hexdigest()
    now = time.monotonic()
    with _RESOURCE_LOCK:
        cached = _RESOURCE_CACHE.get(cache_key)
        if cached and now - cached[0] < 3:
            return cached[1]
    ready = _resource_ready_uncached(env)
    with _RESOURCE_LOCK:
        _RESOURCE_CACHE[cache_key] = (now, ready)
    return ready


def _resource_ready_uncached(env):
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


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_):
        raise WorkerConfigurationError("worker_redirect_rejected")


class LocalWorkerClient:
    """Use the worker's admission and heavy-resource lock over local HTTP.

    Tokens remain private fields and error bodies are never surfaced. Hosts
    are fixed loopback; PHONE_WORKER_HOST is a bind address, not an authority
    supplied by a MIDI or an imported manifest.
    """
    def __init__(self, environment):
        try:
            port = int(environment.get("PHONE_WORKER_PORT", "8766"))
        except (TypeError, ValueError) as exc:
            raise WorkerConfigurationError("invalid_worker_port") from exc
        if not 1 <= port <= 65535:
            raise WorkerConfigurationError("invalid_worker_port")
        self._base = f"http://127.0.0.1:{port}"
        self._token = next((environment.get(key) for key in
            ("PHONE_WORKER_TOKEN", "CORE_WORKER_DIRECT_HTTP_TOKEN", "CORE_WORKER_APK_HTTP_TOKEN", "CORE_WORKER_TOKEN")
            if environment.get(key)), "")
        if not self._token:
            raise WorkerConfigurationError("worker_auth_not_configured")
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
        self._snapshot = None

    def _request(self, route, body=None, *, timeout=8, limit=1024 * 1024):
        request = urllib.request.Request(self._base + route,
            data=None if body is None else json.dumps(body, ensure_ascii=False).encode(),
            headers={"Authorization": "Bearer " + self._token, "Content-Type": "application/json"})
        try:
            with self._opener.open(request, timeout=timeout) as response:
                raw = response.read(limit + 1)
                if len(raw) > limit:
                    raise WorkerConfigurationError("worker_response_size_limit")
                return raw, response.headers
        except urllib.error.HTTPError as exc:
            # An alive worker rejecting auth/busy/input must never cause an
            # isolated renderer to bypass its resource/admission guards.
            code = "worker_auth_rejected" if exc.code in {401, 403} else "worker_request_rejected"
            raise WorkerConfigurationError(code) from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise WorkerConfigurationError("worker_unreachable") from None

    def status(self):
        raw, _ = self._request("/tts-agent/status")
        try:
            snapshot = json.loads(raw)
            agent = snapshot["tts_agent"]
            if snapshot.get("version") != "1.11.41":
                raise WorkerConfigurationError("worker_1_11_41_required")
            if not isinstance(agent, dict) or not agent.get("synth_ready"):
                raise WorkerConfigurationError("worker_not_ready")
            if agent.get("active", 0) != 0:
                raise WorkerConfigurationError("worker_busy")
            self._snapshot = snapshot
            return snapshot
        except WorkerConfigurationError:
            raise
        except (ValueError, KeyError, TypeError):
            raise WorkerConfigurationError("invalid_worker_status") from None

    def render_profile_fingerprint(self):
        snapshot = self._snapshot or self.status()
        teto = snapshot["tts_agent"].get("teto") or {}
        voices = teto.get("ttv_voices", [teto])
        selected = [{key: voice.get(key) for key in ("id", "voice_id", "renderer_version", "fingerprint",
                    "singing_fingerprints", "voicebank_fingerprint")}
                    for voice in voices if isinstance(voice, dict)]
        # No fingerprint means no safe persistent synthesis cache.
        if not any(item.get("singing_fingerprints") for item in selected):
            return ""
        return hashlib.sha256(json.dumps(selected, sort_keys=True).encode()).hexdigest()

    def render_part(self, *, score, text, voice_id="kasane-teto", language="pt-BR", segments=None, **_):
        self.status()  # idle admission immediately before every physical chunk
        body = {"engine": "teto", "text": text, "ttv_score": score,
                "ttv_voice_id": voice_id, "ttv_language": language,
                "ttv_pitch_semitones": 0, "ttv_speech_rate": 1,
                "timeout_seconds": 95, "max_audio_bytes": 12 * 1024 * 1024}
        if segments is not None:
            body["ttv_segments"] = segments
        raw, headers = self._request("/tts-agent/synthesize.raw", body, timeout=100,
                                     limit=12 * 1024 * 1024)
        if headers.get("X-Core-Worker-Audio-Format") != "wav":
            raise WorkerConfigurationError("worker_pcm_required")
        expected = headers.get("X-Core-Worker-Sha256")
        if not expected or hashlib.sha256(raw).hexdigest() != expected:
            raise WorkerConfigurationError("worker_audio_hash_mismatch")
        return raw, {"sha256": expected,
                     "worker_version": headers.get("X-Core-Worker-Version"),
                     "renderer_version": headers.get("X-Core-Worker-Teto-Renderer"),
                     "cache_hit": headers.get("X-Core-Worker-Cache-Hit") == "true",
                     "teto_timeline_mode": headers.get("X-Core-Worker-Teto-Timeline")}


def _isolated_adapter(release, environment, target):
    isolated_env = {key: value for key, value in environment.items() if _allowed(key)}
    isolated_env["PHONE_WORKER_TETO_FRAGMENT_CACHE_DIR"] = str(target.parent / ".utau-fragments")
    renderer = _renderer_type(release)(environment=isolated_env, voice_id="kasane-teto",
        resource_guard=lambda: {"ok": resource_ready(environment)})

    def render_part(*, score, text, voice_id="kasane-teto", language="pt-BR", segments=None, **_):
        if voice_id != "kasane-teto":
            raise WorkerConfigurationError("isolated_voice_not_installed")
        return renderer.synthesize_performance(text, score=score, segments=segments, reading_language=language,
            pitch_offset_semitones=0, speech_rate=1, timeout_seconds=95,
            max_audio_bytes=12 * 1024 * 1024)
    return render_part, renderer.singing_fingerprint(reading_language="pt-BR")


def render_sample(melody, target, *, allow_isolated=None):
    """Produce and decode MP3 atomically using the active worker first.

    Isolated synthesis is an explicit offline diagnostic option, allowed only
    after a configured local worker was unreachable. It is never a fallback
    for busy, not-ready or rejected requests.
    """
    try:
        environment = load_environment(include_worker_credentials=True)
        config_fingerprint = configuration_fingerprint()
        release = find_release(environment)
        if not resource_ready(environment):
            return {"status": "pending_battery_or_battery_status", "retryable": True}
        target = Path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        client = LocalWorkerClient(environment)
        try:
            client.status()
            render_part = client.render_part
            profile = client.render_profile_fingerprint()
            origin = "active-worker-http"
        except WorkerConfigurationError as exc:
            isolated = allow_isolated if allow_isolated is not None else str(
                os.getenv("OSAKA_CD_ALLOW_ISOLATED_RENDER", "")).lower() in {"true", "1", "yes"}
            if str(exc) != "worker_unreachable" or not isolated:
                raise
            render_part, profile = _isolated_adapter(release, environment, target)
            origin = "explicit-offline-isolated"
        arrangement = melody.get("arrangement")
        composition = None
        if arrangement is not None:
            from osaka_cd.render import render_arrangement
            composition = render_arrangement(arrangement, "", render_part,
                cache_dir=target.parent / ".composition-cache", render_profile_fingerprint=profile,
                language="pt-BR", output_format="mp3")
            raw, metadata = composition["audio"], composition["metadata"]
        else:
            score = {"version": 1, "track_index": 0, "duration_ms": melody["duration_ms"],
                     "lyrics_source": "message", "notes": melody["notes"]}
            raw, metadata = render_part(text=" ".join(["lá"] * len(score["notes"])),
                score=score, language="pt-BR", voice_id="kasane-teto")
        if composition is None:
            _validate_wave(raw)
        with tempfile.TemporaryDirectory(prefix=".cd-sample-", dir=target.parent) as temp:
            candidate = Path(temp) / "sample.mp3"
            if composition is not None:
                candidate.write_bytes(raw)
            else:
                ffmpeg = subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", "pipe:0", "-vn",
                                         "-codec:a", "libmp3lame", "-b:a", "128k", str(candidate)],
                                        input=raw, capture_output=True, timeout=40)
                if ffmpeg.returncode:
                    raise WorkerConfigurationError("mp3_encoding_failed")
            if not candidate.is_file() or not 256 < candidate.stat().st_size <= 20_000_000:
                raise WorkerConfigurationError("mp3_encoding_failed")
            decoded = subprocess.run(["ffmpeg", "-v", "error", "-xerror", "-i", str(candidate),
                                      "-f", "null", "-"], capture_output=True, timeout=45)
            if decoded.returncode:
                raise WorkerConfigurationError("mp3_decode_failed")
            if configuration_fingerprint() != config_fingerprint:
                raise WorkerConfigurationError("worker_configuration_changed")
            os.replace(candidate, target)
        result = {"status": "ready", "retryable": False, "worker_version": "1.11.41",
                  "sample_origin": origin, "configuration_fingerprint": config_fingerprint,
                  "renderer_version": metadata.get("renderer_version"),
                  "renderer_fingerprint": profile,
                  "mp3_sha256": hashlib.sha256(target.read_bytes()).hexdigest()}
        if composition is not None:
            result.update({key: composition[key] for key in
                ("arrangement_sha256", "mix_sha256", "stems", "duration_ms")})
        return result
    except Exception as exc:
        # Never propagate error text, paths, subprocess diagnostics or env values.
        from osaka_cd.arrangement import ArrangementError
        from osaka_cd.render import RenderError
        if isinstance(exc, ArrangementError):
            code, retryable = exc.code, False
        elif isinstance(exc, RenderError):
            code = str(exc)
            retryable = code in {"render_cancelled", "render_deadline", "composition_process_failed",
                                 "invalid_stem_audio", "truncated_stem_audio", "silent_stem_audio"}
        else:
            code = str(exc) if isinstance(exc, WorkerConfigurationError) else type(exc).__name__
            retryable = code not in {"invalid_worker_port", "worker_auth_not_configured", "worker_auth_rejected",
                "worker_1_11_41_required", "worker_release_missing", "worker_release_outside_releases",
                "worker_version_missing", "worker_renderer_missing", "invalid_worker_env_file",
                "bash_required_for_worker_env", "worker_env_load_failed", "isolated_voice_not_installed",
                "worker_pcm_required"}
        if not re.fullmatch(r"[A-Za-z0-9_]{1,96}", code):
            code, retryable = type(exc).__name__, True
        return {"status": "pending_" + code, "error_code": code, "retryable": retryable}


def _validate_wave(raw):
    with wave.open(io.BytesIO(raw), "rb") as audio:
        if audio.getsampwidth() != 2 or audio.getnchannels() != 1 or not audio.getnframes():
            raise WorkerConfigurationError("invalid_renderer_audio")
        samples = array.array("h", audio.readframes(audio.getnframes()))
        if sys.byteorder != "little":
            samples.byteswap()
        if not max((abs(n) for n in samples), default=0):
            raise WorkerConfigurationError("silent_renderer_audio")
