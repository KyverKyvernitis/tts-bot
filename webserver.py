from flask import Flask, jsonify, abort, send_file, request
from waitress import serve
import os
import json
import hashlib
import re
import contextlib
import threading
import time
import uuid

app = Flask(__name__)

_health_provider = None
_tts_audio_lock = threading.RLock()
_tts_audio_files: dict[str, tuple[str, float]] = {}


def _core_worker_apk_dir() -> str:
    """Diretório local usado para publicar atualizações privadas do Core Worker APK.

    O caminho pode ser definido por CORE_WORKER_APK_DIR. Por padrão fica dentro do
    repositório para facilitar publicar o APK buildado pela própria VPS.
    """
    base = os.getenv("CORE_WORKER_APK_DIR")
    if not base:
        base = os.path.join(os.getcwd(), "android", "core-worker-app", "releases")
    return os.path.abspath(base)


def _safe_core_worker_apk_file(filename: str) -> str | None:
    filename = str(filename or "").strip().replace("\\", "/")
    if not filename or filename.startswith("/") or ".." in filename.split("/"):
        return None
    lowered = filename.lower()
    if not lowered.endswith((".apk", ".json", ".txt", ".zip")):
        return None
    base = _core_worker_apk_dir()
    full = os.path.abspath(os.path.join(base, filename))
    if full != base and not full.startswith(base + os.sep):
        return None
    return full




def _safe_release_filename(value: str, *, default: str = "CoreWorker.apk") -> str:
    filename = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(value or default).replace("\\", "/").split("/")[-1]).strip("-._")
    if not filename:
        filename = default
    return filename[:120]


def _json_field(value: str, fallback):
    try:
        parsed = json.loads(str(value or ""))
        return parsed
    except Exception:
        return fallback

def set_health_provider(provider):
    global _health_provider
    _health_provider = provider


def _purge_expired_tts_audio(now: float | None = None) -> None:
    now = time.time() if now is None else float(now)
    with _tts_audio_lock:
        expired = [token for token, (_path, expires_at) in _tts_audio_files.items() if expires_at <= now]
        for token in expired:
            _tts_audio_files.pop(token, None)


def register_tts_audio_file(path: str, *, ttl_seconds: float = 240.0) -> str | None:
    """Registra um áudio temporário para o Lavalink buscar via HTTP.

    O token é aleatório e expira rápido. O arquivo não é copiado para evitar RAM/IO
    extra; o endpoint apenas faz streaming do caminho já gerado pelo TTS. A URL
    pode usar a extensão real do arquivo (.ogg/.opus/.m4a/.mp3) ou apenas o token.
    """
    try:
        abs_path = os.path.abspath(str(path or ""))
        if not os.path.isfile(abs_path):
            return None
        _purge_expired_tts_audio()
        token = uuid.uuid4().hex
        ttl = max(30.0, min(900.0, float(ttl_seconds or 240.0)))
        with _tts_audio_lock:
            _tts_audio_files[token] = (abs_path, time.time() + ttl)
        return token
    except Exception:
        return None


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



@app.post("/core-worker/app/publish")
def core_worker_app_publish():
    """Recebe APK compilado por um worker builder e publica latest.json.

    Apenas workers pareados com role/capability apk-builder podem publicar.
    O APK é salvo em CORE_WORKER_APK_DIR e o manifest latest.json é refeito.
    """
    from utility.commands.workers_registry import core_worker_authenticate_http

    form = request.form.to_dict(flat=True)
    status, auth_body = core_worker_authenticate_http(request.headers, {"worker_id": form.get("worker_id") or request.headers.get("X-Core-Worker-ID") or ""}, remote_addr=request.remote_addr or "")
    if status != 200:
        return jsonify(auth_body), status
    worker = auth_body.get("worker") if isinstance(auth_body.get("worker"), dict) else {}
    roles = set(str(item) for item in (worker.get("roles") or []))
    capabilities = set(str(item) for item in (worker.get("capabilities") or [])) | roles
    if "apk-builder" not in capabilities:
        return jsonify({"ok": False, "error": "worker não tem função apk-builder"}), 403

    upload = request.files.get("apk")
    if upload is None:
        return jsonify({"ok": False, "error": "arquivo apk ausente"}), 400
    version_name = str(form.get("versionName") or form.get("version") or "0.0.0").strip()[:48]
    try:
        version_code = int(str(form.get("versionCode") or 0).strip() or 0)
    except Exception:
        version_code = 0
    filename = _safe_release_filename(form.get("filename") or upload.filename or f"CoreWorker-v{version_name}-debug.apk")
    if not filename.lower().endswith(".apk"):
        return jsonify({"ok": False, "error": "arquivo precisa terminar com .apk"}), 400
    base = _core_worker_apk_dir()
    os.makedirs(base, exist_ok=True)
    target = os.path.abspath(os.path.join(base, filename))
    if target != base and not target.startswith(base + os.sep):
        return jsonify({"ok": False, "error": "nome de arquivo inválido"}), 400
    tmp = target + ".tmp"
    expected_sha = str(form.get("sha256") or "").strip().lower()
    digest = hashlib.sha256()
    total = 0
    max_bytes = int(os.getenv("CORE_WORKER_APK_UPLOAD_MAX_BYTES", str(220 * 1024 * 1024)))
    try:
        with open(tmp, "wb") as fh:
            while True:
                chunk = upload.stream.read(128 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise ValueError("APK grande demais")
                digest.update(chunk)
                fh.write(chunk)
        actual_sha = digest.hexdigest()
        if expected_sha and expected_sha != actual_sha:
            with contextlib.suppress(Exception):
                os.remove(tmp)
            return jsonify({"ok": False, "error": "sha256 divergente", "expected": expected_sha, "actual": actual_sha}), 400
        os.replace(tmp, target)
    except Exception as exc:
        with contextlib.suppress(Exception):
            os.remove(tmp)
        return jsonify({"ok": False, "error": f"falha salvando APK: {type(exc).__name__}"}), 500

    changelog = _json_field(form.get("changelog") or "", ["APK compilado por worker builder"])
    if not isinstance(changelog, list):
        changelog = [str(changelog)[:160]]
    required_agent = str(form.get("requiredAgentVersion") or "").strip()[:48]
    manifest = {
        "ok": True,
        "versionName": version_name,
        "versionCode": version_code,
        "apkUrl": f"/core-worker/app/{filename}",
        "sha256": actual_sha,
        "requiredAgentVersion": required_agent,
        "changelog": [str(item)[:180] for item in changelog[:8]],
        "publishedByWorker": str(worker.get("name") or worker.get("worker_id") or "worker builder")[:80],
        "publishedAt": int(time.time()),
        "bytes": total,
    }
    manifest_path = os.path.join(base, "latest.json")
    tmp_manifest = manifest_path + ".tmp"
    with open(tmp_manifest, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=2)
    os.replace(tmp_manifest, manifest_path)
    return jsonify({"ok": True, "filename": filename, "bytes": total, "sha256": actual_sha, "latest": manifest}), 200


@app.get("/core-worker/app/latest.json")
def core_worker_app_latest():
    """Manifesto privado de atualização do Core Worker APK.

    Publique um arquivo latest.json em CORE_WORKER_APK_DIR contendo versionCode,
    versionName, apkUrl e sha256. Este endpoint não usa segredos e deve ser usado
    preferencialmente apenas pela rede privada/Tailscale.
    """
    base = _core_worker_apk_dir()
    manifest = os.path.join(base, "latest.json")
    if not os.path.isfile(manifest):
        return jsonify({
            "ok": False,
            "error": "Core Worker APK ainda não publicado na VPS.",
            "expected": manifest,
            "hint": "Crie latest.json e coloque o APK no diretório de releases.",
        }), 404
    return send_file(manifest, mimetype="application/json", conditional=True, max_age=0)


@app.get("/core-worker/app/<path:filename>")
def core_worker_app_file(filename: str):
    """Serve APKs privados do Core Worker a partir do diretório de releases."""
    full = _safe_core_worker_apk_file(filename)
    if not full or not os.path.isfile(full):
        abort(404)
    lowered = full.lower()
    if lowered.endswith(".apk"):
        return send_file(full, mimetype="application/vnd.android.package-archive", conditional=True, max_age=0)
    if lowered.endswith(".json"):
        return send_file(full, mimetype="application/json", conditional=True, max_age=0)
    if lowered.endswith(".zip"):
        return send_file(full, mimetype="application/zip", conditional=True, max_age=0)
    return send_file(full, mimetype="text/plain", conditional=True, max_age=0)


@app.post("/core-worker/pair")
def core_worker_pair():
    from utility.commands.workers_registry import redeem_core_worker_pairing_http

    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        payload = {}
    status, body = redeem_core_worker_pairing_http(payload, remote_addr=request.remote_addr or "")
    return jsonify(body), status


@app.post("/core-worker/heartbeat")
def core_worker_heartbeat():
    from utility.commands.workers_registry import core_worker_heartbeat_http

    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        payload = {}
    status, body = core_worker_heartbeat_http(request.headers, payload, remote_addr=request.remote_addr or "")
    return jsonify(body), status




@app.post("/core-worker/jobs/poll")
def core_worker_jobs_poll():
    from utility.commands.workers_registry import core_worker_poll_job_http

    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        payload = {}
    status, body = core_worker_poll_job_http(request.headers, payload, remote_addr=request.remote_addr or "")
    return jsonify(body), status


@app.post("/core-worker/jobs/result")
def core_worker_jobs_result():
    from utility.commands.workers_registry import core_worker_job_result_http

    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        payload = {}
    status, body = core_worker_job_result_http(request.headers, payload, remote_addr=request.remote_addr or "")
    return jsonify(body), status


@app.get("/tts-audio/<token>")
@app.get("/tts-audio/<token>.<ext>")
def tts_audio(token: str, ext: str | None = None):
    token = str(token or "").strip()
    # Compatibilidade com rotas antigas onde o sufixo vinha incorporado no token.
    for suffix in (".mp3", ".ogg", ".opus", ".m4a", ".aac", ".wav"):
        if token.lower().endswith(suffix):
            token = token[: -len(suffix)]
            break
    if not token:
        abort(404)
    now = time.time()
    with _tts_audio_lock:
        record = _tts_audio_files.get(token)
        if not record:
            abort(404)
        path, expires_at = record
        if expires_at <= now:
            _tts_audio_files.pop(token, None)
            abort(404)
    if not os.path.isfile(path):
        with _tts_audio_lock:
            _tts_audio_files.pop(token, None)
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


def run_webserver():
    port = int(os.getenv("PORT", "10000"))
    print(f"[webserver] usando porta {port}")
    serve(app, host="0.0.0.0", port=port, threads=4)
