from __future__ import annotations

import os
import threading
import time
import uuid
from typing import Any

from flask import abort, send_file

_TTS_AUDIO_LOCK = threading.RLock()
_TTS_AUDIO_FILES: dict[str, tuple[str, float]] = {}


def _purge_expired_tts_audio(now: float | None = None) -> None:
    now = time.time() if now is None else float(now)
    with _TTS_AUDIO_LOCK:
        expired = [token for token, (_path, expires_at) in _TTS_AUDIO_FILES.items() if expires_at <= now]
        for token in expired:
            _TTS_AUDIO_FILES.pop(token, None)


def register_tts_audio_file(path: str, *, ttl_seconds: float = 240.0) -> str | None:
    """Publica temporariamente áudio gerado pelo TTS para o domínio de música."""
    try:
        abs_path = os.path.abspath(str(path or ""))
        if not os.path.isfile(abs_path):
            return None
        _purge_expired_tts_audio()
        token = uuid.uuid4().hex
        ttl = max(30.0, min(900.0, float(ttl_seconds or 240.0)))
        with _TTS_AUDIO_LOCK:
            _TTS_AUDIO_FILES[token] = (abs_path, time.time() + ttl)
        return token
    except Exception:
        return None


def _serve_tts_audio(token: str, ext: str | None = None):
    del ext
    token = str(token or "").strip()
    for suffix in (".mp3", ".ogg", ".opus", ".m4a", ".aac", ".wav"):
        if token.lower().endswith(suffix):
            token = token[: -len(suffix)]
            break
    if not token:
        abort(404)
    now = time.time()
    with _TTS_AUDIO_LOCK:
        record = _TTS_AUDIO_FILES.get(token)
        if not record:
            abort(404)
        path, expires_at = record
        if expires_at <= now:
            _TTS_AUDIO_FILES.pop(token, None)
            abort(404)
    if not os.path.isfile(path):
        with _TTS_AUDIO_LOCK:
            _TTS_AUDIO_FILES.pop(token, None)
        abort(404)
    lowered = path.lower()
    if lowered.endswith((".ogg", ".opus")):
        mimetype = "audio/ogg"
    elif lowered.endswith((".m4a", ".aac")):
        mimetype = "audio/mp4"
    elif lowered.endswith(".wav"):
        mimetype = "audio/wav"
    else:
        mimetype = "audio/mpeg"
    return send_file(path, mimetype=mimetype, conditional=True, max_age=0)


def registrar_rotas_musica(app: Any) -> None:
    """Registra somente os endpoints HTTP pertencentes ao domínio de música."""
    if "musica_tts_audio" in getattr(app, "view_functions", {}):
        return
    app.add_url_rule("/tts-audio/<token>", endpoint="musica_tts_audio", view_func=_serve_tts_audio, methods=["GET"])
    app.add_url_rule("/tts-audio/<token>.<ext>", endpoint="musica_tts_audio_ext", view_func=_serve_tts_audio, methods=["GET"])
