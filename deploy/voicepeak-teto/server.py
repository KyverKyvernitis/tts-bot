#!/usr/bin/env python3
"""Private stdlib bridge to a desktop's licensed VOICEPEAK Teto installation.

The executable, narrator, reading mode and voice settings belong to the host's
configuration. Clients can supply bounded text and request limits only.
"""
from __future__ import annotations

import base64
import hmac
import json
import math
import os
from pathlib import Path
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKER_ROOT = REPO_ROOT / "deploy" / "termux" / "phone-worker"
# Resolve imports from this script's checkout, even with an external working
# directory and Python isolated mode; do not depend on PYTHONPATH or pytest.
for import_root in (REPO_ROOT, WORKER_ROOT):
    if str(import_root) not in sys.path:
        sys.path.insert(0, str(import_root))
from phone_worker_runtime.tts_providers import VoicepeakRenderer

MAX_REQUEST_BYTES = 32768
MAX_AUDIO_BYTES = 8 * 1024 * 1024


class VoicepeakServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, *, renderer, token):
        if not token or any(ord(ch) < 32 for ch in token):
            raise ValueError("VOICEPEAK_TOKEN obrigatório")
        self.renderer = renderer
        self.token = token
        super().__init__(address, VoicepeakHandler)


class VoicepeakHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "VoicepeakBridge/1"
    sys_version = ""

    def log_message(self, format, *args):
        # Never log text, authorization, CLI paths or query strings.
        pass

    def _reply(self, code, body):
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        try:
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass

    def _authenticated(self):
        provided = self.headers.get("Authorization", "")
        expected = "Bearer " + self.server.token
        if not hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8")):
            self._reply(401, {"error": "autenticação obrigatória"})
            return False
        return True

    def do_GET(self):
        if not self._authenticated():
            return
        if self.path != "/status":
            self._reply(404, {"error": "rota não encontrada"})
            return
        self._reply(200, self.server.renderer.status())

    def do_POST(self):
        if not self._authenticated():
            return
        if self.path != "/synthesize":
            self._reply(404, {"error": "rota não encontrada"})
            return
        if self.headers.get("Transfer-Encoding"):
            self._reply(400, {"error": "Transfer-Encoding não suportado"})
            return
        if not self.headers.get("Content-Type", "").lower().startswith("application/json"):
            self._reply(415, {"error": "Content-Type deve ser application/json"})
            return
        raw_length = self.headers.get("Content-Length", "")
        if not raw_length.isdigit():
            self._reply(411, {"error": "Content-Length obrigatório"})
            return
        length = int(raw_length)
        if length < 2 or length > MAX_REQUEST_BYTES:
            self._reply(413, {"error": "corpo da solicitação grande demais ou vazio"})
            return
        try:
            body_deadline = time.monotonic() + 10.0
            raw = bytearray()
            while len(raw) < length:
                remaining = body_deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("prazo de leitura excedido")
                self.connection.settimeout(remaining)
                chunk = self.rfile.read1(min(8192, length - len(raw)))
                if not chunk:
                    raise ValueError("corpo truncado")
                raw.extend(chunk)
            body = json.loads(raw.decode("utf-8"))
            if not isinstance(body, dict) or set(body) - {"text", "timeout_seconds", "max_audio_bytes"}:
                raise ValueError("campos inválidos; voz e motor são configurados no host")
            text = body.get("text")
            if not isinstance(text, str) or not text.strip():
                raise ValueError("texto obrigatório")
            timeout = float(body.get("timeout_seconds", 30.0))
            if not math.isfinite(timeout) or timeout <= 0 or timeout > 120:
                raise ValueError("prazo inválido")
            limit = body.get("max_audio_bytes", MAX_AUDIO_BYTES)
            if isinstance(limit, bool) or not isinstance(limit, int) or not 44 < limit <= MAX_AUDIO_BYTES:
                raise ValueError("limite de áudio inválido")
        except (ValueError, UnicodeError, TypeError, TimeoutError, OSError):
            self._reply(400, {"error": "solicitação JSON inválida"})
            return
        try:
            rendered = self.server.renderer.synthesize(text, timeout_seconds=timeout, max_audio_bytes=limit,
                                                      pitch_offset_semitones=0.0)
            audio = rendered.pop("audio")
            if not isinstance(audio, bytes) or len(audio) > limit:
                raise RuntimeError("áudio inválido")
            self._reply(200, {"audio_base64": base64.b64encode(audio).decode("ascii"),
                              "audio_format": "wav", "metadata": rendered})
        except ValueError:
            self._reply(400, {"error": "texto ou parâmetros de síntese inválidos"})
        except TimeoutError:
            self._reply(504, {"error": "prazo de síntese VOICEPEAK excedido"})
        except Exception as exc:
            if "ocupado" in str(exc).lower():
                self._reply(409, {"error": "VOICEPEAK ocupado"})
            else:
                self._reply(503, {"error": "síntese VOICEPEAK indisponível; verifique o host"})


def create_server(host="127.0.0.1", port=8087, *, renderer=None, token=None):
    token = token or os.getenv("VOICEPEAK_TOKEN") or os.getenv("PHONE_WORKER_VOICEPEAK_TOKEN", "")
    # Ignore PHONE_WORKER_VOICEPEAK_URL on the bridge: no recursive forwarding.
    renderer = renderer or VoicepeakRenderer(allow_remote=False)
    return VoicepeakServer((host, int(port)), renderer=renderer, token=token)


def main():
    os.environ.setdefault("PHONE_WORKER_TETO_ENABLED", "true")
    host = os.getenv("VOICEPEAK_HOST", "127.0.0.1")
    port = int(os.getenv("VOICEPEAK_PORT", "8087"))
    server = create_server(host, port)
    status = server.renderer.status(force=True)
    if not status.get("ready"):
        server.server_close()
        raise SystemExit("VOICEPEAK não está pronto: " + str(status.get("last_error", "configuração inválida")))
    print(f"VOICEPEAK Teto bridge em {host}:{server.server_port}; leitura={status['reading_mode']}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
