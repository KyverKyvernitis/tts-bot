"""TTS provider adapters with no runtime state or import-time provider loading.

The facade supplies its live locks, renderer, clocks and normalization helpers.
Admission, cache, output envelopes and the shared transport keep their owners.
"""


def synthesize_teto(*, text, timeout, max_audio_bytes, logs, stage_ms,
                    heavy_lock, get_renderer, monotonic, normalize_format,
                    pitch_offset_semitones=0.0):
    if not heavy_lock.acquire(blocking=False):
        raise RuntimeError("recurso pesado ocupado por build ou manutenção")
    teto_started = monotonic()
    try:
        rendered = get_renderer().synthesize(
            text,
            timeout_seconds=float(timeout),
            max_audio_bytes=max_audio_bytes,
            pitch_offset_semitones=float(pitch_offset_semitones),
        )
    finally:
        heavy_lock.release()
    data = bytes(rendered.pop("audio", b"") or b"")
    audio_format = normalize_format(rendered.get("audio_format") or "wav")
    teto_meta = dict(rendered)
    stage_ms["teto_render"] = round((monotonic() - teto_started) * 1000.0, 2)
    logs.append(
        f"teto voicebank={rendered.get('voicebank') or 'Kasane Teto'} "
        f"renderer={rendered.get('renderer_version') or '?'} "
        f"phonemizer={rendered.get('phonemizer_version') or '?'} "
        f"rendered={rendered.get('rendered_phonemes') or 0} "
        f"aux={rendered.get('auxiliary_phonemes') or 0} "
        f"epenthetic={rendered.get('epenthetic_phonemes') or 0} "
        f"profile={rendered.get('voicebank_profile') or 'standard'} "
        f"coverage={rendered.get('coverage_percent') if rendered.get('coverage_percent') is not None else '-'} "
        f"clusters={rendered.get('cluster_hits') or 0} "
        f"timeline={rendered.get('timeline_mode') or 'serial'} "
        f"pitch={rendered.get('pitch_offset_semitones') if rendered.get('pitch_offset_semitones') is not None else 0.0:+.1f}st "
        f"overlays={rendered.get('timeline_aux_overlays') or 0} "
        f"path_cost={rendered.get('alias_path_cost') if rendered.get('alias_path_cost') is not None else '-'} "
        f"pitch_jump={rendered.get('pitch_boundary_max_cents') if rendered.get('pitch_boundary_max_cents') is not None else '-'} "
        f"energy_jump={rendered.get('energy_boundary_max_db') if rendered.get('energy_boundary_max_db') is not None else '-'} "
        f"repairs={rendered.get('continuity_repairs') or 0} "
        f"missing={len(rendered.get('missing_phonemes') or [])}"
    )
    return data, audio_format, teto_meta


def synthesize_edge(body, *, text, timeout, logs, normalize_rate, normalize_pitch,
                    short_text, asyncio_api, io_api):
    try:
        import edge_tts  # type: ignore
    except Exception as exc:
        raise RuntimeError(f"edge-tts não instalado no worker: {type(exc).__name__}: {short_text(exc, limit=120)}") from exc
    voice = str(body.get("voice") or body.get("fallback_voice") or "pt-BR-FranciscaNeural").strip() or "pt-BR-FranciscaNeural"
    rate = normalize_rate(body.get("rate"))
    pitch = normalize_pitch(body.get("pitch"))

    async def _edge_bytes() -> bytes:
        communicate = edge_tts.Communicate(text=text, voice=voice, rate=rate, pitch=pitch)
        buffer = io_api.BytesIO()
        async for chunk in communicate.stream():
            if chunk.get("type") == "audio" and chunk.get("data"):
                buffer.write(chunk["data"])
        return buffer.getvalue()

    data = asyncio_api.run(asyncio_api.wait_for(_edge_bytes(), timeout=timeout))
    logs.append(f"edge voice={voice} rate={rate} pitch={pitch}")
    return data


def synthesize_gtts(body, *, text, timeout, logs, normalize_language, short_text, io_api):
    try:
        from gtts import gTTS  # type: ignore
    except Exception as exc:
        raise RuntimeError(f"gTTS não instalado no worker: {type(exc).__name__}: {short_text(exc, limit=120)}") from exc
    language = normalize_language(body.get("language") or body.get("fallback_language"))
    buffer = io_api.BytesIO()
    tts = gTTS(text=text, lang=language, timeout=(min(3.5, timeout), min(8.0, timeout)))
    tts.write_to_fp(buffer)
    data = buffer.getvalue()
    logs.append(f"gtts language={language}")
    return data


class VoicepeakRenderer:
    """Licensed desktop VOICEPEAK CLI, locally or through the private bridge.

    Nothing is discovered or started on import. The configured executable is a
    single file, never a shell command. Android workers should use the bridge.
    """

    RENDER_VERSION = "voicepeak-teto-1"
    PHONEMIZER_VERSION = "ptbr-kana-v1"
    MAX_RESPONSE_BYTES = 12 * 1024 * 1024
    CLI_CHAR_LIMIT = 140

    def __init__(self, *, resource_guard=None, allow_remote=True):
        import threading
        self._resource_guard = resource_guard
        self._allow_remote = bool(allow_remote)
        self._lock = threading.Lock()
        self._last_status = {}
        self._last_status_at = 0.0
        self._last_config = ""

    @staticmethod
    def _env_int(name, default, minimum, maximum):
        import os
        try:
            value = int(os.getenv(name, str(default)))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{name} deve ser inteiro") from exc
        if not minimum <= value <= maximum:
            raise ValueError(f"{name} deve estar entre {minimum} e {maximum}")
        return value

    @classmethod
    def _reading_version(cls):
        from teto_renderer.phonemizer import VOICEPEAK_READING_VERSION
        return VOICEPEAK_READING_VERSION

    def _config(self):
        import os
        import re
        import urllib.parse
        mode = str(os.getenv("PHONE_WORKER_VOICEPEAK_TEXT_MODE", "ja")).strip().lower()
        if mode not in {"ja", "ptbr-kana"}:
            raise ValueError("PHONE_WORKER_VOICEPEAK_TEXT_MODE deve ser ja ou ptbr-kana")
        narrator = str(os.getenv("PHONE_WORKER_VOICEPEAK_NARRATOR", "重音テト")).strip()
        if narrator not in {"重音テト", "Kasane Teto", "Teto"}:
            raise ValueError("PHONE_WORKER_VOICEPEAK_NARRATOR deve identificar a Teto: 重音テト, Kasane Teto ou Teto")
        emotion = str(os.getenv("PHONE_WORKER_VOICEPEAK_EMOTION", "")).strip()
        cache_revision = str(os.getenv("PHONE_WORKER_VOICEPEAK_CACHE_REVISION", "")).strip()
        if len(cache_revision) > 128 or any(ord(ch) < 32 for ch in cache_revision):
            raise ValueError("PHONE_WORKER_VOICEPEAK_CACHE_REVISION deve ter até 128 caracteres sem controles")
        if emotion:
            parts = emotion.split(",")
            seen = set()
            for part in parts:
                match = re.fullmatch(r"([A-Za-z][A-Za-z0-9_-]*)=(\d{1,3})", part.strip())
                if not match or int(match.group(2)) > 100 or match.group(1) in seen:
                    raise ValueError("PHONE_WORKER_VOICEPEAK_EMOTION deve usar nome=0..100, sem duplicatas")
                seen.add(match.group(1))
            emotion = ",".join(part.strip() for part in parts)
        url = str(os.getenv("PHONE_WORKER_VOICEPEAK_URL", "")).strip().rstrip("/") if self._allow_remote else ""
        token = str(os.getenv("PHONE_WORKER_VOICEPEAK_TOKEN", "")).strip()
        if url:
            parsed = urllib.parse.urlsplit(url)
            if (parsed.scheme not in {"http", "https"} or not parsed.netloc
                    or parsed.username or parsed.password or parsed.query or parsed.fragment):
                raise ValueError("PHONE_WORKER_VOICEPEAK_URL deve ser uma URL HTTP(S), sem credenciais ou query")
            if not token or any(ord(ch) < 32 for ch in token):
                raise ValueError("PHONE_WORKER_VOICEPEAK_TOKEN obrigatório para o bridge")
        return {
            "enabled": str(os.getenv("PHONE_WORKER_TETO_ENABLED", "false")).strip().lower() in {"1", "true", "yes", "on", "sim"},
            "command": str(os.getenv("PHONE_WORKER_VOICEPEAK_COMMAND", "")).strip(),
            "url": url, "token": token, "narrator": narrator, "reading_mode": mode,
            "speed": self._env_int("PHONE_WORKER_VOICEPEAK_SPEED", 100, 50, 200),
            "pitch": self._env_int("PHONE_WORKER_VOICEPEAK_PITCH", 0, -300, 300),
            "emotion": emotion,
            "max_chars": self._env_int("PHONE_WORKER_TETO_MAX_CHARACTERS", 180, 1, 4096),
            "max_seconds": self._env_int("PHONE_WORKER_TETO_MAX_AUDIO_SECONDS", 20, 1, 120),
            "status_ttl": self._env_int("PHONE_WORKER_VOICEPEAK_STATUS_CACHE_SECONDS", 15, 1, 300),
            "status_timeout": self._env_int("PHONE_WORKER_VOICEPEAK_STATUS_TIMEOUT_SECONDS", 5, 1, 60),
            "reading_version": self._reading_version(),
            "cache_revision": cache_revision,
        }

    @staticmethod
    def _digest(value):
        import hashlib
        import json
        return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()

    @staticmethod
    def _remaining(deadline):
        import time
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("prazo de síntese VOICEPEAK excedido")
        return remaining

    @staticmethod
    def _executable(config):
        import os
        import shutil
        from pathlib import Path
        raw = config["command"]
        if not raw:
            raise RuntimeError("configure PHONE_WORKER_VOICEPEAK_COMMAND ou PHONE_WORKER_VOICEPEAK_URL")
        candidate = Path(raw).expanduser()
        executable = str(candidate.resolve()) if candidate.is_file() else shutil.which(raw)
        if not executable or not Path(executable).is_file():
            raise RuntimeError("executável VOICEPEAK não encontrado (configure um único caminho, sem argumentos)")
        if os.name != "nt" and not os.access(executable, os.X_OK):
            raise RuntimeError("executável VOICEPEAK sem permissão de execução")
        return executable

    def _run(self, executable, args, deadline):
        import os
        import signal
        import subprocess
        import tempfile
        # CLI output is diagnostic, not audio. Keep it out of memory and errors.
        with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
            self._remaining(deadline)
            with subprocess.Popen([executable, *args], stdout=stdout, stderr=stderr,
                                  start_new_session=os.name == "posix") as process:
                try:
                    process.wait(timeout=self._remaining(deadline))
                except (subprocess.TimeoutExpired, TimeoutError) as exc:
                    # A Termux launcher has PRoot/QEMU descendants. Terminate
                    # this request's process group before releasing admission.
                    if os.name == "posix":
                        try:
                            os.killpg(process.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                    else:
                        process.kill()
                    process.wait()
                    raise TimeoutError("prazo de síntese VOICEPEAK excedido") from exc
                if process.returncode:
                    raise RuntimeError(f"VOICEPEAK CLI falhou (código {process.returncode})")
            stdout.seek(0)
            result = stdout.read(65537)
            if len(result) > 65536:
                raise RuntimeError("resposta de diagnóstico VOICEPEAK grande demais")
            # The official Linux app prints --help to stderr. Some builds may
            # use it for inventory too; only successful, bounded output enters
            # parsing/fingerprints. Failed CLI diagnostics are never returned.
            stderr.seek(0)
            diagnostic = stderr.read(65537 - len(result))
            if diagnostic:
                result += (b"\n" if result and not result.endswith(b"\n") else b"") + diagnostic
            if len(result) > 65536:
                raise RuntimeError("resposta de diagnóstico VOICEPEAK grande demais")
            return result.decode("utf-8-sig", errors="replace")

    @staticmethod
    def _has_narrator(output, narrator):
        import re
        for line in output.splitlines():
            item = re.sub(r"^\s*(?:[-*]\s+|\d+[.)]\s+)", "", line).strip().strip("\"'")
            if item == narrator:
                return True
        return False

    def _request(self, config, method, path, deadline, body=None, max_bytes=None):
        import json
        import urllib.error
        import urllib.request
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                return None
        payload = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(config["url"] + path, data=payload, method=method,
            headers={"Authorization": "Bearer " + config["token"], "Content-Type": "application/json", "Accept": "application/json"})
        limit = min(max_bytes or self.MAX_RESPONSE_BYTES, self.MAX_RESPONSE_BYTES)
        try:
            with urllib.request.build_opener(NoRedirect).open(request, timeout=self._remaining(deadline)) as response:
                length = response.headers.get("Content-Length")
                if length and (not length.isdigit() or int(length) > limit):
                    raise RuntimeError("resposta do bridge VOICEPEAK grande demais")
                data = bytearray()
                while len(data) <= limit:
                    self._remaining(deadline)
                    remaining = self._remaining(deadline)
                    # read1 returns available data; a trickling peer must not
                    # reset a total request deadline on every byte.
                    transport = getattr(getattr(getattr(response, "fp", None), "raw", None), "_sock", None)
                    if transport is not None:
                        transport.settimeout(remaining)
                    read = getattr(response, "read1", response.read)
                    chunk = read(min(65536, limit + 1 - len(data)))
                    if not chunk:
                        break
                    data.extend(chunk)
                if len(data) > limit:
                    raise RuntimeError("resposta do bridge VOICEPEAK grande demais")
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                raise RuntimeError("autenticação do bridge VOICEPEAK recusada") from exc
            if exc.code == 409:
                raise RuntimeError("bridge VOICEPEAK ocupado") from exc
            raise RuntimeError(f"bridge VOICEPEAK retornou HTTP {exc.code}") from exc
        except (urllib.error.URLError, OSError) as exc:
            raise RuntimeError("bridge VOICEPEAK indisponível ou prazo excedido") from exc
        try:
            result = json.loads(bytes(data).decode("utf-8"))
        except (ValueError, UnicodeError) as exc:
            raise RuntimeError("resposta JSON inválida do bridge VOICEPEAK") from exc
        if not isinstance(result, dict):
            raise RuntimeError("resposta JSON inválida do bridge VOICEPEAK")
        return result

    def _status(self, *, force=False, deadline=None):
        import time
        import re
        from pathlib import Path
        deadline = deadline or time.monotonic() + 5.0
        now = time.monotonic()
        result = {"ok": False, "available": False, "ready": False, "enabled": False,
                  "engine": "teto", "backend": "voicepeak", "voice": "kasane-teto-voicepeak",
                  "renderer_version": self.RENDER_VERSION, "phonemizer_version": self._reading_version()}
        try:
            config = self._config()
            key = self._digest(config)  # Token changes invalidate readiness; token is never returned.
            if not force and key == self._last_config and self._last_status and now - self._last_status_at <= config["status_ttl"]:
                return dict(self._last_status)
            result.update(enabled=config["enabled"], narrator=config["narrator"],
                          reading_mode=config["reading_mode"], voicebank_profile="voicepeak-" + config["reading_mode"],
                          source="remote" if config["url"] else "native",
                          speed=config["speed"], pitch=config["pitch"], emotion=config["emotion"])
            if not config["enabled"]:
                raise RuntimeError("PHONE_WORKER_TETO_ENABLED=false")
            if config["url"]:
                remote = self._request(config, "GET", "/status", deadline, max_bytes=65536)
                if not remote.get("ready") or remote.get("backend") != "voicepeak":
                    raise RuntimeError("bridge não tem VOICEPEAK pronto com a voz licenciada")
                if remote.get("narrator") != config["narrator"]:
                    raise RuntimeError("narrador do bridge VOICEPEAK difere do configurado")
                if remote.get("reading_mode") != config["reading_mode"]:
                    raise RuntimeError("PHONE_WORKER_VOICEPEAK_TEXT_MODE difere do bridge")
                host_fingerprint = str(remote.get("fingerprint") or "")
                if not host_fingerprint or len(host_fingerprint) > 128:
                    raise RuntimeError("bridge VOICEPEAK não informou fingerprint válido")
                result.update(engine_version=str(remote.get("engine_version") or "VOICEPEAK")[:120],
                              host_fingerprint=host_fingerprint,
                              phonemizer_version=str(remote.get("phonemizer_version") or self.PHONEMIZER_VERSION),
                              speed=remote.get("speed"), pitch=remote.get("pitch"), emotion=remote.get("emotion"))
                fingerprint_data = {"renderer": self.RENDER_VERSION, "source": "remote", "url": config["url"],
                                    "narrator": config["narrator"], "reading_mode": config["reading_mode"],
                                    "host": host_fingerprint, "adapter": result["phonemizer_version"],
                                    "cache_revision": config["cache_revision"],
                                    "max_seconds": config["max_seconds"], "max_chars": config["max_chars"]}
            else:
                executable = self._executable(config)
                narrators = self._run(executable, ["--list-narrator"], deadline)
                if not self._has_narrator(narrators, config["narrator"]):
                    raise RuntimeError("voz licenciada não encontrada em --list-narrator: " + config["narrator"])
                help_text = self._run(executable, ["--help"], deadline)
                if config["emotion"]:
                    emotions = self._run(executable, ["--list-emotion", config["narrator"]], deadline)
                    for expression in config["emotion"].split(","):
                        name = expression.split("=", 1)[0]
                        if not re.search(r"(?m)^\s*(?:[-*]\s+)?" + re.escape(name) + r"(?:\s|:|$)", emotions):
                            raise RuntimeError("emoção não disponível para a voz VOICEPEAK: " + name)
                stamp = Path(executable).stat()
                result["engine_version"] = next((line.strip()[:120] for line in help_text.splitlines() if line.strip()), "VOICEPEAK")
                fingerprint_data = {"renderer": self.RENDER_VERSION, "adapter": config["reading_version"],
                                    "source": "native", "executable": executable,
                                    "stamp": [stamp.st_size, stamp.st_mtime_ns], "help": self._digest(help_text),
                                    "narrators": self._digest(narrators), "narrator": config["narrator"],
                                    "reading_mode": config["reading_mode"], "speed": config["speed"],
                                    "pitch": config["pitch"], "emotion": config["emotion"],
                                    "cache_revision": config["cache_revision"],
                                    "max_seconds": config["max_seconds"], "max_chars": config["max_chars"]}
            result.update(ok=True, available=True, ready=True, fingerprint=self._digest(fingerprint_data))
            result["voicebank_fingerprint"] = result["fingerprint"]
        except Exception as exc:
            result["last_error"] = str(exc)[:420]
            try:
                key = self._digest(self._config())
            except Exception:
                key = "invalid"
        self._last_config = key
        self._last_status_at = time.monotonic()
        self._last_status = dict(result)
        return result

    def status(self, *, force=False, timeout_seconds=None):
        import math
        import time
        configured_timeout = self._env_int("PHONE_WORKER_VOICEPEAK_STATUS_TIMEOUT_SECONDS", 5, 1, 60)
        timeout = float(configured_timeout if timeout_seconds is None else timeout_seconds)
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("prazo de status VOICEPEAK inválido")
        deadline = time.monotonic() + min(float(configured_timeout), timeout)
        # Health probes must not launch another native CLI while rendering.
        if not self._lock.acquire(blocking=False):
            try:
                matching = self._last_config == self._digest(self._config())
            except Exception:
                matching = False
            if matching and self._last_status:
                return dict(self._last_status, busy=True)
            return {"ok": False, "available": False, "ready": False, "backend": "voicepeak",
                    "renderer_version": self.RENDER_VERSION, "last_error": "renderer VOICEPEAK ocupado", "busy": True}
        try:
            return self._status(force=force, deadline=deadline)
        finally:
            self._lock.release()

    def fingerprint(self):
        return str(self.status().get("fingerprint") or "unavailable")

    @classmethod
    def _chunks(cls, text):
        # Splitting only; never truncate a request to the native 140-char cap.
        chunks = []
        remaining = text
        punctuation = "。！？.!?;；,，、\n "
        while len(remaining) > cls.CLI_CHAR_LIMIT:
            boundary = max((i + 1 for i, ch in enumerate(remaining[:cls.CLI_CHAR_LIMIT]) if ch in punctuation), default=cls.CLI_CHAR_LIMIT)
            chunks.append(remaining[:boundary])
            remaining = remaining[boundary:]
        if remaining:
            chunks.append(remaining)
        return chunks

    @staticmethod
    def _wav_parts(data, max_audio_bytes, max_seconds):
        import io
        import wave
        if not data or len(data) > max_audio_bytes:
            raise RuntimeError("áudio VOICEPEAK vazio ou grande demais")
        try:
            with wave.open(io.BytesIO(data), "rb") as reader:
                if reader.getcomptype() != "NONE" or reader.getnchannels() not in {1, 2} or reader.getsampwidth() not in {1, 2, 3, 4}:
                    raise ValueError("PCM não suportado")
                params = (reader.getnchannels(), reader.getsampwidth(), reader.getframerate())
                if not 8000 <= params[2] <= 192000 or reader.getnframes() <= 0:
                    raise ValueError("formato inválido")
                if reader.getnframes() / params[2] > max_seconds:
                    raise RuntimeError("áudio VOICEPEAK excedeu o limite de duração")
                expected = reader.getnframes() * params[0] * params[1]
                if expected > max_audio_bytes:
                    raise RuntimeError("áudio VOICEPEAK grande demais")
                pcm = reader.readframes(reader.getnframes())
                if len(pcm) != expected:
                    raise ValueError("PCM truncado")
                return params, pcm
        except (wave.Error, EOFError, ValueError) as exc:
            raise RuntimeError("VOICEPEAK não gerou WAV PCM válido") from exc

    def synthesize(self, text, *, timeout_seconds=30.0, max_audio_bytes=8 * 1024 * 1024, pitch_offset_semitones=0.0):
        import base64
        import io
        import math
        import tempfile
        import time
        import wave
        from pathlib import Path
        timeout = float(timeout_seconds)
        max_audio_bytes = int(max_audio_bytes)
        offset = float(pitch_offset_semitones)
        if not math.isfinite(timeout) or timeout <= 0 or not math.isfinite(offset):
            raise ValueError("prazo ou pitch VOICEPEAK inválido")
        if offset != 0.0:
            raise ValueError("VOICEPEAK usa PHONE_WORKER_VOICEPEAK_PITCH; pitch_offset_semitones deve ser 0")
        if not 44 < max_audio_bytes <= 8 * 1024 * 1024:
            raise ValueError("limite de áudio VOICEPEAK inválido")
        deadline = time.monotonic() + min(timeout, 120.0)
        config = self._config()
        clean_text = str(text or "").strip()
        if not clean_text:
            raise ValueError("texto vazio")
        if len(clean_text) > config["max_chars"]:
            raise ValueError(f"texto grande demais para VOICEPEAK ({len(clean_text)} > {config['max_chars']})")
        if not self._lock.acquire(blocking=False):
            raise RuntimeError("renderer VOICEPEAK ocupado")
        try:
            status = self._status(deadline=deadline)
            self._remaining(deadline)
            if not status.get("ready"):
                raise RuntimeError(str(status.get("last_error") or "VOICEPEAK indisponível"))
            self._remaining(deadline)
            if config["url"]:
                # Host controls the narrator, executable and all voice settings.
                result = self._request(config, "POST", "/synthesize", deadline, {
                    "text": clean_text, "timeout_seconds": self._remaining(deadline),
                    "max_audio_bytes": max_audio_bytes,
                }, max_bytes=min(self.MAX_RESPONSE_BYTES, ((max_audio_bytes + 2) // 3) * 4 + 65536))
                metadata = result.get("metadata")
                if not isinstance(metadata, dict) or metadata.get("backend") != "voicepeak" or metadata.get("narrator") != config["narrator"]:
                    raise RuntimeError("metadados inválidos do bridge VOICEPEAK")
                if metadata.get("renderer_fingerprint") != status.get("host_fingerprint"):
                    self._last_status = {}
                    raise RuntimeError("configuração VOICEPEAK mudou; repita a solicitação")
                encoded = result.get("audio_base64")
                if not isinstance(encoded, str) or len(encoded) > ((max_audio_bytes + 2) // 3) * 4:
                    raise RuntimeError("áudio do bridge VOICEPEAK grande demais")
                try:
                    data = base64.b64decode(encoded, validate=True)
                except (ValueError, TypeError) as exc:
                    raise RuntimeError("áudio inválido do bridge VOICEPEAK") from exc
                self._wav_parts(data, max_audio_bytes, config["max_seconds"])
                self._remaining(deadline)
                metadata.update(audio=data, audio_format="wav", source="remote",
                                renderer_fingerprint=status["fingerprint"], voicebank_fingerprint=status["fingerprint"])
                return metadata
            if self._resource_guard is not None:
                snapshot = self._resource_guard() or {}
                if not snapshot.get("ok", False):
                    raise RuntimeError(str(snapshot.get("reason") or "recursos insuficientes para VOICEPEAK"))
            from teto_renderer.phonemizer import prepare_voicepeak_text
            prepared = prepare_voicepeak_text(clean_text, mode=config["reading_mode"])
            if not prepared or len(prepared) > 4096:
                raise ValueError("leitura VOICEPEAK vazia ou grande demais")
            chunks = self._chunks(prepared)
            executable = self._executable(config)
            params = None
            parts = []
            pcm_bytes = 0
            with tempfile.TemporaryDirectory(prefix="voicepeak-teto-") as directory:
                for index, chunk in enumerate(chunks):
                    self._remaining(deadline)
                    output = Path(directory) / f"chunk-{index:03d}.wav"
                    # Native CLI args verified against voicepeak-cli/src/voicepeak.rs.
                    args = ["-s", chunk, "-n", config["narrator"], "-o", str(output),
                            "--speed", str(config["speed"]), "--pitch", str(config["pitch"])]
                    if config["emotion"]:
                        args += ["-e", config["emotion"]]
                    self._run(executable, args, deadline)
                    if not output.is_file() or output.stat().st_size > max_audio_bytes:
                        raise RuntimeError("áudio VOICEPEAK ausente ou grande demais")
                    current, pcm = self._wav_parts(output.read_bytes(), max_audio_bytes, config["max_seconds"])
                    if params is not None and params != current:
                        raise RuntimeError("VOICEPEAK mudou o formato WAV entre os trechos")
                    params = current
                    pcm_bytes += len(pcm)
                    if pcm_bytes + 44 > max_audio_bytes:
                        raise RuntimeError("áudio VOICEPEAK grande demais")
                    if pcm_bytes / (params[0] * params[1] * params[2]) > config["max_seconds"]:
                        raise RuntimeError("áudio VOICEPEAK excedeu o limite de duração")
                    parts.append(pcm)
            buffer = io.BytesIO()
            with wave.open(buffer, "wb") as writer:
                writer.setnchannels(params[0])
                writer.setsampwidth(params[1])
                writer.setframerate(params[2])
                for pcm in parts:
                    writer.writeframesraw(pcm)
            data = buffer.getvalue()
            self._wav_parts(data, max_audio_bytes, config["max_seconds"])
            self._remaining(deadline)
            return {"audio": data, "audio_format": "wav", "backend": "voicepeak", "source": "native",
                    "voicebank": "Kasane Teto / VOICEPEAK", "voicebank_profile": status["voicebank_profile"],
                    "voicebank_fingerprint": status["fingerprint"], "renderer_fingerprint": status["fingerprint"],
                    "renderer_version": self.RENDER_VERSION, "engine_version": status["engine_version"],
                    "phonemizer_version": config["reading_version"], "narrator": config["narrator"],
                    "reading_mode": config["reading_mode"], "reading_text": prepared,
                    "experimental_pronunciation": config["reading_mode"] == "ptbr-kana", "chunks": len(chunks),
                    "speed": config["speed"], "pitch": config["pitch"], "emotion": config["emotion"],
                    "pitch_offset_semitones": 0.0, "audio_seconds": round(pcm_bytes / (params[0] * params[1] * params[2]), 3)}
        finally:
            self._lock.release()
