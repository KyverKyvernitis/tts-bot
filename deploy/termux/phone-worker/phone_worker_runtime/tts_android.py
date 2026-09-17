"""Android TTS HTTP and raw-to-JSON adapter; no runtime state or import side effects.

The facade supplies live configuration and callbacks. Status cache, admission,
timings/logs and output/cache envelopes keep their existing owners.
"""
import contextlib
import json
from typing import Any


def json_request(path: str, *, payload: dict[str, Any] | None = None, timeout: float = 1.5, base_url: str, version: str, request_factory, open_url) -> dict[str, Any]:
    url = base_url + path
    data: bytes | None = None
    method = "GET"
    headers = {"Accept": "application/json", "User-Agent": f"CorePhoneWorker/{version}"}
    if payload is not None:
        method = "POST"
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    request = request_factory(url, data=data, method=method, headers=headers)
    with open_url(request, timeout=max(0.1, float(timeout))) as response:
        raw = response.read(16 * 1024 * 1024)
    parsed = json.loads(raw.decode("utf-8", errors="replace") or "{}")
    if not isinstance(parsed, dict):
        raise RuntimeError("Android TTS retornou JSON inválido")
    return parsed


def raw_request(path: str, *, payload: dict[str, Any], timeout: float = 1.5, max_audio_bytes: int = 8 * 1024 * 1024, base_url: str, version: str, request_factory, open_url, short_text) -> tuple[bytes, dict[str, Any]]:
    url = base_url + path
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {
        "Accept": "audio/wav,application/octet-stream,application/json;q=0.4,*/*;q=0.1",
        "Content-Type": "application/json; charset=utf-8",
        "User-Agent": f"CorePhoneWorker/{version}",
    }
    request = request_factory(url, data=data, method="POST", headers=headers)
    with open_url(request, timeout=max(0.1, float(timeout))) as response:
        content_type = str(response.headers.get("Content-Type") or "").lower()
        raw = response.read(max(1024, max_audio_bytes) + 1)
        if "application/json" in content_type:
            try:
                parsed = json.loads(raw.decode("utf-8", errors="replace") or "{}")
            except Exception as exc:
                raise RuntimeError(f"Android TTS raw retornou JSON inválido: {short_text(exc, limit=100)}") from exc
            if isinstance(parsed, dict) and parsed.get("ok") is False:
                raise RuntimeError(str(parsed.get("error") or "Android TTS raw retornou ok=false"))
            raise RuntimeError("Android TTS raw retornou JSON sem áudio")
        if len(raw) > max_audio_bytes:
            raise RuntimeError(f"Android TTS raw grande demais: {len(raw)} bytes")
        meta = {
            "content_type": content_type,
            "audio_format": str(response.headers.get("X-Core-Worker-Audio-Format") or "wav").strip().lower() or "wav",
            "android_synth_ms": str(response.headers.get("X-Core-Worker-Android-Synth-Ms") or "").strip(),
            "locale": str(response.headers.get("X-Core-Worker-Locale") or "").strip(),
            "voice": str(response.headers.get("X-Core-Worker-Voice") or "").strip(),
            "sha256": str(response.headers.get("X-Core-Worker-Sha256") or "").strip(),
        }
    if not raw:
        raise RuntimeError("Android TTS raw não retornou áudio")
    return raw, meta


def synthesize(body, *, text, timeout, max_audio_bytes, logs, stage_ms,
               getenv, env_bool, monotonic, raw_request, json_request,
               short_text, decode_audio, normalize_format):
    data = b""
    audio_format = "mp3"
    synth_timeout_ms = max(1000, min(timeout * 1000, int(float(body.get("android_timeout_ms") or getenv("PHONE_WORKER_ANDROID_TTS_SYNTH_TIMEOUT_MS") or timeout * 1000))))
    android_payload = {
        "text": text,
        "language": str(body.get("language") or body.get("fallback_language") or "pt-BR"),
        "locale": str(body.get("locale") or body.get("language") or body.get("fallback_language") or "pt-BR"),
        "voice": str(body.get("voice") or ""),
        "rate": str(body.get("rate") or "1.0"),
        "pitch": str(body.get("pitch") or "1.0"),
        "timeout_ms": synth_timeout_ms,
        "max_audio_bytes": max_audio_bytes,
    }
    android_payload["prefer_local_voice"] = env_bool("PHONE_WORKER_ANDROID_TTS_PREFER_LOCAL_VOICE", True)
    android_started = monotonic()
    raw_enabled = env_bool("PHONE_WORKER_ANDROID_TTS_RAW_ENABLED", True)
    raw_error = ""
    response = {}
    if raw_enabled:
        try:
            data, raw_meta = raw_request(
                "/native-tts/synthesize.raw",
                payload=android_payload,
                timeout=max(1.0, min(timeout + 1.0, (synth_timeout_ms / 1000.0) + 1.0)),
                max_audio_bytes=max_audio_bytes,
            )
            audio_format = normalize_format(raw_meta.get("audio_format") or "wav")
            android_ms = (monotonic() - android_started) * 1000.0
            stage_ms["android_roundtrip"] = round(android_ms, 2)
            apk_ms = raw_meta.get("android_synth_ms") or ""
            with contextlib.suppress(Exception):
                stage_ms["android_synth"] = round(float(apk_ms), 2)
            voice_used = raw_meta.get("voice") or "auto"
            if raw_meta.get("sha256"):
                response["sha256"] = raw_meta.get("sha256")
            response["android_synth_ms"] = apk_ms
            response["voice"] = voice_used
            response["locale"] = raw_meta.get("locale") or android_payload["locale"]
            logs.append(f"android-native raw locale={response['locale']} voice={voice_used} format={audio_format} apk_ms={apk_ms or 0} roundtrip={android_ms:.1f}ms bytes={len(data)}")
        except Exception as exc:
            raw_error = f"{type(exc).__name__}: {short_text(exc, limit=110)}"
            data = b""
            logs.append(f"android-native raw indisponível; fallback json: {raw_error}")
    if not data:
        response = json_request(
            "/native-tts/synthesize",
            payload=android_payload,
            timeout=max(1.0, min(timeout + 1.0, (synth_timeout_ms / 1000.0) + 1.0)),
        )
        if response.get("ok") is False:
            raise RuntimeError(str(response.get("error") or "Android TTS retornou ok=false"))
        data = decode_audio(str(response.get("data_b64") or ""), max_bytes=max_audio_bytes)
        audio_format = normalize_format(response.get("audio_format") or "wav")
        android_ms = (monotonic() - android_started) * 1000.0
        stage_ms["android_roundtrip"] = round(android_ms, 2)
        with contextlib.suppress(Exception):
            stage_ms["android_synth"] = round(float(response.get("android_synth_ms") or response.get("worker_synth_ms") or 0), 2)
        logs.append(f"android-native json locale={android_payload['locale']} format={audio_format} apk_ms={response.get('android_synth_ms') or response.get('worker_synth_ms') or 0} roundtrip={android_ms:.1f}ms")
    return data, audio_format, response
