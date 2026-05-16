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
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path

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


def _env_bool(name: str, default: bool = False) -> bool:
    raw = str(os.getenv(name, "")).strip().lower()
    if not raw:
        return bool(default)
    return raw in {"1", "true", "yes", "y", "on", "sim"}


def _expand_path(value: str | None) -> str:
    return os.path.abspath(os.path.expanduser(str(value or "")))


def _find_android_build_tool(tool: str) -> str | None:
    explicit = os.getenv(f"CORE_WORKER_APK_{tool.upper()}")
    if explicit and os.path.isfile(_expand_path(explicit)):
        return _expand_path(explicit)
    found = shutil.which(tool)
    if found:
        return found
    roots = []
    for env_name in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        root = os.getenv(env_name)
        if root:
            roots.append(_expand_path(root))
    roots.extend([
        _expand_path("~/android-sdk"),
        _expand_path("~/Android/Sdk"),
        "/opt/android-sdk",
        "/usr/lib/android-sdk",
    ])
    candidates: list[str] = []
    for root in dict.fromkeys(roots):
        build_tools = os.path.join(root, "build-tools")
        if not os.path.isdir(build_tools):
            continue
        for version in sorted(os.listdir(build_tools), reverse=True):
            candidates.append(os.path.join(build_tools, version, tool))
    for candidate in candidates:
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def _strip_apk_signatures(source: str, target: str) -> None:
    """Recria o APK removendo assinaturas antigas.

    Isso evita publicar APK assinado com a chave debug do worker. A regravação do
    ZIP também remove blocos de assinatura v2/v3, e depois a VPS assina com uma
    chave fixa local.
    """
    with zipfile.ZipFile(source, "r") as zin, zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as zout:
        for info in zin.infolist():
            name = str(info.filename or "")
            upper = name.upper()
            if upper.startswith("META-INF/") and (
                upper.endswith(".RSA") or upper.endswith(".DSA") or upper.endswith(".EC") or upper.endswith(".SF") or upper == "META-INF/MANIFEST.MF"
            ):
                continue
            data = zin.read(info.filename)
            info.date_time = info.date_time or (1980, 1, 1, 0, 0, 0)
            zout.writestr(info, data)


def _fixed_apk_signing_config() -> dict[str, str] | None:
    mode = str(os.getenv("CORE_WORKER_APK_SIGNING_MODE") or "debug").strip().lower()
    if mode in {"off", "none", "disabled", "false", "0"} or _env_bool("CORE_WORKER_APK_SIGNING_DISABLED", False):
        return None
    if mode == "debug":
        keystore = _expand_path(os.getenv("CORE_WORKER_APK_KEYSTORE") or "~/.android/debug.keystore")
        return {
            "mode": "debug",
            "keystore": keystore,
            "alias": os.getenv("CORE_WORKER_APK_KEY_ALIAS") or "androiddebugkey",
            "storepass": os.getenv("CORE_WORKER_APK_KEYSTORE_PASSWORD") or "android",
            "keypass": os.getenv("CORE_WORKER_APK_KEY_PASSWORD") or os.getenv("CORE_WORKER_APK_KEYSTORE_PASSWORD") or "android",
        }
    keystore = _expand_path(os.getenv("CORE_WORKER_APK_KEYSTORE") or "")
    return {
        "mode": mode or "keystore",
        "keystore": keystore,
        "alias": os.getenv("CORE_WORKER_APK_KEY_ALIAS") or "",
        "storepass": os.getenv("CORE_WORKER_APK_KEYSTORE_PASSWORD") or "",
        "keypass": os.getenv("CORE_WORKER_APK_KEY_PASSWORD") or os.getenv("CORE_WORKER_APK_KEYSTORE_PASSWORD") or "",
    }


def _validate_core_worker_apk(apk_path: str) -> dict[str, object]:
    """Valida o APK antes de publicar latest.json.

    A validação é intencionalmente local e barata: ZIP íntegro, manifest/classes
    presentes, assinatura verificável quando apksigner existe e alinhamento quando
    zipalign existe. Se falhar, a VPS não deve apontar latest.json para esse APK.
    """
    result: dict[str, object] = {"ok": False, "checks": []}
    path = str(apk_path or "")
    if not path or not os.path.isfile(path):
        result["error"] = "APK não encontrado"
        return result
    try:
        with zipfile.ZipFile(path, "r") as zf:
            bad = zf.testzip()
            if bad:
                result["error"] = f"ZIP corrompido em {bad}"
                return result
            names = set(zf.namelist())
            missing = [name for name in ("AndroidManifest.xml", "classes.dex") if name not in names]
            if missing:
                result["error"] = "APK sem " + ", ".join(missing)
                return result
            result["checks"].append("zip")
            result["checks"].append("manifest")
            result["entries"] = len(names)
    except Exception as exc:
        result["error"] = f"ZIP inválido: {type(exc).__name__}: {exc}"
        return result

    zipalign = _find_android_build_tool("zipalign")
    if zipalign:
        proc = subprocess.run([zipalign, "-c", "-p", "4", path], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=60)
        result["zipalign"] = proc.returncode == 0
        if proc.returncode != 0:
            result["error"] = "zipalign -c falhou: " + (proc.stderr or proc.stdout or "sem saída")[-400:]
            return result
        result["checks"].append("zipalign")

    apksigner = _find_android_build_tool("apksigner")
    if apksigner:
        proc = subprocess.run([apksigner, "verify", "--verbose", path], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=60)
        result["apksigner"] = proc.returncode == 0
        result["apksigner_tail"] = (proc.stdout or proc.stderr or "")[-800:]
        if proc.returncode != 0:
            result["error"] = "apksigner verify falhou: " + (proc.stderr or proc.stdout or "sem saída")[-500:]
            return result
        result["checks"].append("apksigner")

    aapt = _find_android_build_tool("aapt")
    if aapt:
        proc = subprocess.run([aapt, "dump", "badging", path], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=60)
        result["aapt"] = proc.returncode == 0
        if proc.returncode != 0:
            result["error"] = "aapt dump badging falhou: " + (proc.stderr or proc.stdout or "sem saída")[-500:]
            return result
        result["checks"].append("aapt")
        first_line = next((line for line in (proc.stdout or "").splitlines() if line.startswith("package:")), "")
        if first_line:
            result["badging"] = first_line[:500]

    result["ok"] = True
    return result


def _sign_core_worker_apk_with_vps_key(uploaded_apk: str, final_apk: str) -> dict[str, object]:
    cfg = _fixed_apk_signing_config()
    if cfg is None:
        shutil.copyfile(uploaded_apk, final_apk)
        return {"signedByVps": False, "signingMode": "disabled"}
    apksigner = _find_android_build_tool("apksigner")
    if not apksigner:
        raise RuntimeError("apksigner não encontrado na VPS; configure ANDROID_HOME ou CORE_WORKER_APK_APKSIGNER")
    zipalign = _find_android_build_tool("zipalign")
    keystore = str(cfg.get("keystore") or "")
    alias = str(cfg.get("alias") or "")
    storepass = str(cfg.get("storepass") or "")
    keypass = str(cfg.get("keypass") or "")
    if not keystore or not os.path.isfile(keystore):
        raise RuntimeError("keystore fixa ausente na VPS; configure CORE_WORKER_APK_KEYSTORE ou preserve ~/.android/debug.keystore")
    if not alias or not storepass:
        raise RuntimeError("configuração de assinatura incompleta na VPS")

    base_dir = os.path.dirname(final_apk) or os.getcwd()
    with tempfile.TemporaryDirectory(prefix="core-worker-sign-", dir=base_dir) as tmpdir:
        stripped = os.path.join(tmpdir, "stripped.apk")
        aligned = os.path.join(tmpdir, "aligned.apk")
        _strip_apk_signatures(uploaded_apk, stripped)
        sign_input = stripped
        if zipalign:
            proc = subprocess.run([zipalign, "-f", "-p", "4", stripped, aligned], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=120)
            if proc.returncode != 0:
                raise RuntimeError("zipalign falhou: " + (proc.stderr or proc.stdout or "sem saída")[-400:])
            sign_input = aligned
        cmd = [
            apksigner,
            "sign",
            "--ks", keystore,
            "--ks-key-alias", alias,
            "--ks-pass", f"pass:{storepass}",
            "--key-pass", f"pass:{keypass}",
            "--out", final_apk,
            sign_input,
        ]
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=180)
        if proc.returncode != 0:
            raise RuntimeError("apksigner falhou: " + (proc.stderr or proc.stdout or "sem saída")[-500:])
        verify = subprocess.run([apksigner, "verify", "--verbose", final_apk], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=60)
        if verify.returncode != 0:
            raise RuntimeError("verificação da assinatura falhou: " + (verify.stderr or verify.stdout or "sem saída")[-500:])
    return {
        "signedByVps": True,
        "signingMode": str(cfg.get("mode") or "keystore"),
        "zipalign": bool(zipalign),
    }

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
    tmp = target + ".upload.tmp"
    expected_sha = str(form.get("sha256") or "").strip().lower()
    digest = hashlib.sha256()
    upload_total = 0
    max_bytes = int(os.getenv("CORE_WORKER_APK_UPLOAD_MAX_BYTES", str(220 * 1024 * 1024)))
    signing_info: dict[str, object] = {"signedByVps": False, "signingMode": "not-run"}
    try:
        with open(tmp, "wb") as fh:
            while True:
                chunk = upload.stream.read(128 * 1024)
                if not chunk:
                    break
                upload_total += len(chunk)
                if upload_total > max_bytes:
                    raise ValueError("APK grande demais")
                digest.update(chunk)
                fh.write(chunk)
        upload_sha = digest.hexdigest()
        if expected_sha and expected_sha != upload_sha:
            with contextlib.suppress(Exception):
                os.remove(tmp)
            return jsonify({"ok": False, "error": "sha256 divergente", "expected": expected_sha, "actual": upload_sha}), 400
        try:
            signing_info = _sign_core_worker_apk_with_vps_key(tmp, target)
        except Exception as sign_exc:
            with contextlib.suppress(Exception):
                os.remove(tmp)
            with contextlib.suppress(Exception):
                os.remove(target)
            return jsonify({
                "ok": False,
                "error": "falha assinando APK na VPS",
                "detail": str(sign_exc)[:500],
                "hint": "configure CORE_WORKER_APK_KEYSTORE/APKSIGNER ou preserve a chave fixa da VPS",
            }), 500
        with contextlib.suppress(Exception):
            os.remove(tmp)
    except Exception as exc:
        with contextlib.suppress(Exception):
            os.remove(tmp)
        return jsonify({"ok": False, "error": f"falha salvando APK: {type(exc).__name__}"}), 500

    validation = _validate_core_worker_apk(target)
    if not bool(validation.get("ok")):
        with contextlib.suppress(Exception):
            os.remove(target)
        return jsonify({
            "ok": False,
            "error": "APK assinado, mas falhou na validação antes da publicação",
            "validation": validation,
            "hint": "latest.json foi preservado; corrija o build/assinatura antes de publicar.",
        }), 500

    final_raw = open(target, "rb").read()
    actual_sha = hashlib.sha256(final_raw).hexdigest()
    total = len(final_raw)

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
        "uploadedSha256": upload_sha,
        "requiredAgentVersion": required_agent,
        "updateAvailable": True,
        "notifyUsers": True,
        "notificationRequested": True,
        "signedByVps": bool(signing_info.get("signedByVps")),
        "signingMode": str(signing_info.get("signingMode") or "unknown"),
        "changelog": [str(item)[:180] for item in changelog[:8]],
        "publishedByWorker": str(worker.get("name") or worker.get("worker_id") or "worker builder")[:80],
        "publishedAt": int(time.time()),
        "publishReason": "worker-builder-auto" if str(form.get("notifyUsers") or form.get("notificationRequested") or "").strip().lower() in {"1", "true", "yes", "on", "sim"} else "worker-builder",
        "bytes": total,
        "uploadedBytes": upload_total,
        "validation": validation,
    }
    manifest_path = os.path.join(base, "latest.json")
    tmp_manifest = manifest_path + ".tmp"
    with open(tmp_manifest, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=2)
    os.replace(tmp_manifest, manifest_path)
    _kick_core_worker_pending_automation(str(worker.get("worker_id") or ""))
    return jsonify({"ok": True, "filename": filename, "bytes": total, "sha256": actual_sha, "signedByVps": bool(signing_info.get("signedByVps")), "validation": validation, "latest": manifest}), 200


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




def _kick_core_worker_pending_automation(worker_id: str = "") -> None:
    """Agenda processamento leve das pendências de agent/APK após heartbeat.

    Não bloqueia o /heartbeat: se um worker antigo voltar online depois do update
    da VPS, o script tenta entregar worker_update/apk_build pendentes em segundo
    plano. A VPS continua sendo a fonte de decisão; o worker só executa jobs
    whitelist.
    """
    worker_id = str(worker_id or "").strip()
    script = os.path.join(os.getcwd(), "scripts", "core-worker-automation.py")
    if not os.path.isfile(script):
        return
    py = os.path.join(os.getcwd(), ".venv", "bin", "python")
    if not os.path.isfile(py):
        py = shutil.which("python3") or "python3"
    cmd = [py, script, "process-pending"]
    if worker_id:
        cmd.extend(["--worker-id", worker_id])
    try:
        subprocess.Popen(cmd, cwd=os.getcwd(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    except Exception:
        pass


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
    if status == 200 and isinstance(body, dict):
        worker = body.get("worker") if isinstance(body.get("worker"), dict) else {}
        worker_id = str(worker.get("worker_id") or payload.get("worker_id") or payload.get("id") or "")
        _kick_core_worker_pending_automation(worker_id)
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
