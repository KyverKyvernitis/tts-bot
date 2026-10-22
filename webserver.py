"""VPS HTTP endpoints: bot, music, updater, Termux Phone Worker.
The retired Core Worker Android APK is deliberately not served or enrolled.
"""
from flask import Flask, jsonify, abort, send_file, request
from waitress import serve
import os
import json
import hashlib
import hmac
import re
import stat
import time
import subprocess
import threading
import sys
from pathlib import Path
from urllib.parse import urlsplit

from utility.openai_compat import register_openai_compat_routes
from cogs.musica.integracoes.webserver import registrar_rotas_musica
from updater.api_cd import register_cd_publish_route

app = Flask(__name__)
registrar_rotas_musica(app)
register_openai_compat_routes(app)
_health_provider = None
_update_action_provider = None
register_cd_publish_route(app, lambda: _update_action_provider)

def _env_int(name: str, default: int, *, minimum: int = 0, maximum: int = 1_000_000) -> int:
    try:
        raw = int(os.getenv(name, str(default)) or default)
    except Exception:
        raw = default
    return max(minimum, min(raw, maximum))


def set_health_provider(provider):
    global _health_provider
    _health_provider = provider


def set_update_action_provider(provider):
    global _update_action_provider
    _update_action_provider = provider


def _is_local_request() -> bool:
    remote = (request.remote_addr or "").strip()
    return remote in {"127.0.0.1", "::1", "localhost"}


@app.get("/")
def index():
    return "ok", 200


@app.get("/health")
def health():
    if callable(_health_provider):
        try:
            return jsonify(_health_provider()), 200
        except Exception as e:
            return jsonify({
                "ok": False,
                "healthy": False,
                "error": str(e),
            }), 500
    return jsonify({"ok": True}), 200


def _dispatch_internal_update_action(action: str):
    if not _is_local_request():
        abort(403)
    expected_token = os.getenv("BOT_INTERNAL_UPDATE_TOKEN", "").strip()
    if expected_token and request.headers.get("X-Update-Token", "") != expected_token:
        abort(403)
    if not callable(_update_action_provider):
        return jsonify({"ok": False, "error": "update action provider indisponível"}), 503
    payload = request.get_json(silent=True) or {}
    try:
        result = _update_action_provider(action, payload)
        status = 200 if isinstance(result, dict) and result.get("ok") else 500
        return jsonify(result), status
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.post("/internal/update/reload-cogs")
def internal_update_reload_cogs():
    return _dispatch_internal_update_action("reload_cogs")


@app.post("/internal/update/zip-status")
def internal_update_zip_status():
    return _dispatch_internal_update_action("zip_status")


@app.post("/internal/update/create-zip-status")
def internal_update_create_zip_status():
    return _dispatch_internal_update_action("create_zip_status")


@app.post("/internal/update/voice-notices")
def internal_update_voice_notices():
    return _dispatch_internal_update_action("voice_notices")


@app.post("/internal/update/flush-logs")
def internal_update_flush_logs():
    return _dispatch_internal_update_action("flush_logs")


def _is_retired_app(payload: object) -> bool:
    if not isinstance(payload, dict):
        return False
    source = str(payload.get("source") or "").lower()
    runtime = str(payload.get("runtime_kind") or payload.get("runtime_mode") or "").lower()
    return source.startswith("core-worker-apk") or runtime == "apk" or runtime.startswith("apk-")


# The Termux agent update queue still needs heartbeat-triggered processing.
# Only the former APK-build automation was removed.
_automation_lock = threading.Lock()
_automation_processes = {}
_automation_last_started = {}


def _kick_phone_worker_automation(worker_id: str) -> None:
    if os.getenv("CORE_WORKER_PENDING_AUTOMATION_ENABLED", "true").lower() in {"0", "false", "no"}:
        return
    worker_id = str(worker_id or "").strip()
    if not worker_id:
        return
    script = Path.cwd() / "scripts" / "core-worker-automation.py"
    if not script.is_file():
        return
    data_dir = Path(os.getenv("CORE_WORKER_DATA_DIR") or (Path.cwd() / "data"))
    try:
        pending = json.loads((data_dir / "core_worker_automation_pending.json").read_text(encoding="utf-8"))
        item = pending.get("agent_update", {}) if isinstance(pending, dict) else {}
        active = isinstance(item, dict) and bool(item.get("pending") or item.get("queued") or item.get("job"))
        urgent = active and str(item.get("phase") or "") in {"job_queued", "restart_pending"}
    except (OSError, ValueError, TypeError):
        active = urgent = False
    cooldown = 8.0 if urgent else (60.0 if active else 900.0)
    now = time.monotonic()
    if not active:
        try:
            status = json.loads((data_dir / "core_worker_automation_status.json").read_text(encoding="utf-8"))
            if isinstance(status, dict) and time.time() - float(status.get("finished_at") or 0) < cooldown:
                return
        except (OSError, ValueError, TypeError, OverflowError):
            pass
    with _automation_lock:
        current = _automation_processes.get(worker_id)
        if current is not None and current.poll() is None:
            return
        if now - _automation_last_started.get(worker_id, 0) < cooldown:
            return
        python = str(Path.cwd() / ".venv" / "bin" / "python")
        if not Path(python).is_file():
            python = sys.executable
        try:
            process = subprocess.Popen(
                [python, str(script), "process-pending", "--worker-id", worker_id],
                cwd=str(Path.cwd()), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError as exc:
            app.logger.warning("phone-worker automation spawn failed: %s", exc)
            return
        _automation_processes[worker_id] = process
        _automation_last_started[worker_id] = now
    def _reap():
        try:
            process.wait(timeout=180)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        finally:
            with _automation_lock:
                if _automation_processes.get(worker_id) is process:
                    _automation_processes.pop(worker_id, None)
    threading.Thread(target=_reap, name="termux-agent-automation", daemon=True).start()


@app.post("/core-worker/pair")
def core_worker_pair():
    from utility.commands.workers_registry import redeem_core_worker_pairing_http

    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        payload = {}
    if _is_retired_app(payload):
        return jsonify({"ok": False, "error": "Core Worker APK desativado"}), 410
    status, body = redeem_core_worker_pairing_http(payload, remote_addr=request.remote_addr or "")
    return jsonify(body), status


@app.post("/core-worker/heartbeat")
def core_worker_heartbeat():
    from utility.commands.workers_registry import core_worker_heartbeat_http

    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        payload = {}
    if _is_retired_app(payload):
        return jsonify({"ok": False, "error": "Core Worker APK desativado"}), 410
    status, body = core_worker_heartbeat_http(request.headers, payload, remote_addr=request.remote_addr or "")
    if status == 200 and isinstance(body, dict):
        worker = body.get("worker") if isinstance(body.get("worker"), dict) else {}
        _kick_phone_worker_automation(worker.get("worker_id") or payload.get("worker_id"))
    return jsonify(body), status


def _core_worker_agent_root() -> Path:
    configured = str(os.getenv("CORE_WORKER_AGENT_RELEASE_DIR") or "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return (Path(os.getcwd()) / "data" / "core_worker_agent").resolve()


def _core_worker_request_bearer() -> str:
    auth = str(request.headers.get("Authorization") or "").strip()
    if not auth.lower().startswith("bearer "):
        return ""
    return auth[7:].strip()


def _authenticate_core_worker_agent_request(worker_id: str) -> tuple[bool, tuple | None, str]:
    from utility.commands.workers_registry import core_worker_authenticate_http

    token = _core_worker_request_bearer()
    if not token:
        return False, (jsonify({"ok": False, "error": "token ausente"}), 401), ""
    payload = {"worker_id": str(worker_id or "").strip()}
    status, body = core_worker_authenticate_http(request.headers, payload, remote_addr=request.remote_addr or "")
    if status != 200 or not isinstance(body, dict) or not body.get("ok"):
        return False, (jsonify(body if isinstance(body, dict) else {"ok": False, "error": "não autorizado"}), status), ""
    worker = body.get("worker") if isinstance(body.get("worker"), dict) else {}
    runtime_kind = str(worker.get("runtime_kind") or "").strip().lower()
    source = str(worker.get("source") or "").strip().lower()
    if runtime_kind == "apk" or source.startswith("core-worker-apk"):
        return False, (jsonify({"ok": False, "error": "manifesto do agent é exclusivo do runtime Termux"}), 403), ""
    return True, None, token


@app.get("/core-worker/agent/ping")
def core_worker_agent_ping():
    """Small authenticated roundtrip; no inventory, jobs or automation kicks."""
    worker_id = str(request.args.get("worker_id") or request.headers.get("X-Core-Worker-Id") or "").strip()
    ok, error_response, _token = _authenticate_core_worker_agent_request(worker_id)
    if not ok:
        return error_response
    response = jsonify({"ok": True, "service": "osaka-phone-ssh", "worker_id": worker_id,
                        "timestamp": int(time.time())})
    response.headers["Cache-Control"] = "private, no-store"
    return response, 200


def _core_worker_response_hmac(token: str, timestamp: str, body: bytes) -> str:
    # O timestamp faz parte da mensagem autenticada. Sem isso, um adversário que
    # capturasse body+HMAC poderia trocar apenas o header de tempo e prolongar um
    # replay. O artefato pode ser imutável; a resposta autenticada continua curta.
    mac = hmac.new(token.encode("utf-8"), digestmod=hashlib.sha256)
    mac.update(timestamp.encode("ascii"))
    mac.update(b"\n")
    mac.update(body)
    return mac.hexdigest()


def _signed_agent_response_headers(body: bytes, token: str) -> dict[str, str]:
    timestamp = str(int(time.time()))
    digest = _core_worker_response_hmac(token, timestamp, body)
    return {
        "X-Core-Worker-Timestamp": timestamp,
        "X-Core-Worker-Signature": f"sha256={digest}",
        "X-Core-Worker-Signature-Schema": "ts-body-v1",
        "X-Content-Type-Options": "nosniff",
    }


def _safe_agent_release_hash(value: str) -> str:
    value = str(value or "").strip().lower()
    return value if re.fullmatch(r"[0-9a-f]{64}", value) else ""


def _read_ssh_manifest_registry(path: Path | None = None, *, owner_uid: int = 0) -> dict:
    """Only the administrator's non-secret SSH registry can select an origin."""
    path = path or Path("/etc/osaka/phone-ssh.json")
    try:
        info = path.lstat()
    except FileNotFoundError:
        return {}
    for candidate in (path, *path.parents):
        meta = candidate.lstat()
        if stat.S_ISLNK(meta.st_mode) or meta.st_uid not in {0, owner_uid} or meta.st_mode & 0o022:
            raise ValueError("registro SSH não possui origem administrativa confiável")
        if candidate != path and not stat.S_ISDIR(meta.st_mode):
            raise ValueError("diretório do registro SSH inválido")
    if not stat.S_ISREG(info.st_mode) or info.st_size > 256 * 1024:
        raise ValueError("registro SSH fora do limite")
    parsed = json.loads(path.read_text(encoding="utf-8"))
    if (not isinstance(parsed, dict) or parsed.get("schema") != 1
            or parsed.get("marker") != "Managed by Osaka Phone SSH v1."
            or not isinstance(parsed.get("workers"), dict)):
        raise ValueError("registro SSH inválido")
    return parsed


def _ssh_manifest_origin(worker_id: str) -> str | None:
    # SSH's local forward connects from loopback. X-Forwarded-* headers are not
    # used: a public request cannot select the private origin by changing Host.
    if (request.remote_addr or "").strip() not in {"127.0.0.1", "::1"}:
        return None
    registry = _read_ssh_manifest_registry()
    row = registry.get("workers", {}).get(worker_id)
    if not isinstance(row, dict) or row.get("state") not in {"enrolled", "active"} or not row.get("client_public_key"):
        return None
    try:
        local_port = int(row["local_port"])
        origin = str(row["bootstrap_origin"])
        expected = f"http://127.0.0.1:{local_port}"
        if not 1024 <= local_port <= 65535 or origin != expected:
            raise ValueError("origem privada SSH inválida")
        parsed = urlsplit(origin)
        request_origin = urlsplit(str(request.scheme) + "://" + str(request.host))
        if (request_origin.scheme, request_origin.hostname, request_origin.port) != (parsed.scheme, parsed.hostname, parsed.port):
            return None
    except (KeyError, TypeError, ValueError):
        raise ValueError("origem privada SSH inválida") from None
    return origin


def _agent_manifest_for_transport(body: bytes, worker_id: str) -> bytes:
    origin = _ssh_manifest_origin(worker_id)
    if not origin:
        return body
    manifest = json.loads(body.decode("utf-8"))
    source_hash = _safe_agent_release_hash(manifest.get("source_hash"))
    if not source_hash:
        raise ValueError("manifesto sem source_hash válido")
    # Rebuild from the immutable artifact identity rather than preserving an
    # arbitrary published hostname or accepting an origin supplied by a client.
    release_url = f"{origin}/core-worker/agent/releases/{source_hash}.zip"
    manifest["url"] = release_url
    if "release_url" in manifest:
        manifest["release_url"] = release_url
    return json.dumps(manifest, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


@app.get("/core-worker/agent/latest")
def core_worker_agent_latest():
    worker_id = str(request.args.get("worker_id") or request.headers.get("X-Core-Worker-Id") or "").strip()
    ok, error_response, token = _authenticate_core_worker_agent_request(worker_id)
    if not ok:
        return error_response
    path = _core_worker_agent_root() / "latest.json"
    try:
        body = path.read_bytes()
        parsed = json.loads(body.decode("utf-8"))
        if not isinstance(parsed, dict) or not _safe_agent_release_hash(parsed.get("source_hash")):
            raise ValueError("manifesto latest inválido")
        body = _agent_manifest_for_transport(body, worker_id)
    except FileNotFoundError:
        return jsonify({"ok": False, "error": "nenhuma release do agent publicada"}), 404
    except Exception as exc:
        app.logger.error("core-worker agent latest inválido: %s", type(exc).__name__)
        return jsonify({"ok": False, "error": "manifesto do agent inválido"}), 500
    response = app.response_class(body, status=200, mimetype="application/json")
    response.headers["Cache-Control"] = "private, no-store"
    for key, value in _signed_agent_response_headers(body, token).items():
        response.headers[key] = value
    return response


@app.get("/core-worker/agent/releases/<source_hash>.zip")
def core_worker_agent_release(source_hash: str):
    worker_id = str(request.args.get("worker_id") or request.headers.get("X-Core-Worker-Id") or "").strip()
    ok, error_response, token = _authenticate_core_worker_agent_request(worker_id)
    if not ok:
        return error_response
    safe_hash = _safe_agent_release_hash(source_hash)
    if not safe_hash:
        return jsonify({"ok": False, "error": "source hash inválido"}), 400
    root = _core_worker_agent_root()
    path = (root / "releases" / f"{safe_hash}.zip").resolve()
    expected_parent = (root / "releases").resolve()
    if path.parent != expected_parent or not path.is_file():
        return jsonify({"ok": False, "error": "release não encontrada"}), 404
    max_bytes = _env_int("CORE_WORKER_AGENT_RELEASE_MAX_BYTES", 16 * 1024 * 1024, minimum=1024, maximum=64 * 1024 * 1024)
    try:
        stat_info = path.stat()
        if stat_info.st_size <= 0 or stat_info.st_size > max_bytes:
            raise ValueError("release fora do limite")
        timestamp = str(int(time.time()))
        mac = hmac.new(token.encode("utf-8"), digestmod=hashlib.sha256)
        mac.update(timestamp.encode("ascii"))
        mac.update(b"\n")
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(256 * 1024)
                if not chunk:
                    break
                mac.update(chunk)
    except Exception as exc:
        app.logger.error("core-worker agent release inválida hash=%s error=%s", safe_hash[:12], type(exc).__name__)
        return jsonify({"ok": False, "error": "release do agent inválida"}), 500
    response = send_file(path, mimetype="application/zip", as_attachment=False, conditional=False, etag=safe_hash)
    response.headers["Cache-Control"] = "private, max-age=31536000, immutable"
    response.headers["X-Core-Worker-Timestamp"] = timestamp
    response.headers["X-Core-Worker-Signature"] = f"sha256={mac.hexdigest()}"
    response.headers["X-Core-Worker-Signature-Schema"] = "ts-body-v1"
    response.headers["X-Core-Worker-Source-Hash"] = safe_hash
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@app.post("/core-worker/jobs/poll")
def core_worker_jobs_poll():
    from utility.commands.workers_registry import core_worker_poll_job_http

    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        payload = {}
    if _is_retired_app(payload):
        return jsonify({"ok": False, "error": "Core Worker APK desativado"}), 410
    status, body = core_worker_poll_job_http(request.headers, payload, remote_addr=request.remote_addr or "")
    if status == 200 and isinstance(body, dict):
        _kick_phone_worker_automation(body.get("worker_id") or payload.get("worker_id"))
    return jsonify(body), status


@app.post("/core-worker/jobs/progress")
def core_worker_jobs_progress():
    from utility.commands.workers_registry import core_worker_job_progress_http

    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        payload = {}
    if _is_retired_app(payload):
        return jsonify({"ok": False, "error": "Core Worker APK desativado"}), 410
    status, body = core_worker_job_progress_http(request.headers, payload, remote_addr=request.remote_addr or "")
    return jsonify(body), status


@app.post("/core-worker/jobs/result")
def core_worker_jobs_result():
    from utility.commands.workers_registry import core_worker_job_result_http

    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        payload = {}
    if _is_retired_app(payload):
        return jsonify({"ok": False, "error": "Core Worker APK desativado"}), 410
    status, body = core_worker_job_result_http(request.headers, payload, remote_addr=request.remote_addr or "")
    if status == 200 and isinstance(body, dict):
        _kick_phone_worker_automation(body.get("worker_id") or payload.get("worker_id"))
    return jsonify(body), status


def run_webserver():
    port = int(os.getenv("PORT", "10000"))
    print(f"[webserver] usando porta {port}")
    serve(app, host="0.0.0.0", port=port, threads=4)


