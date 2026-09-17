"""Private build inputs and runtime-asset hydration for APK self-builds."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
from pathlib import Path
from typing import Any


def decode_b64(payload: dict[str, Any], names: tuple[str, ...], max_bytes: int, label: str) -> bytes:
    raw = next((str(payload.get(name) or "").strip() for name in names if str(payload.get(name) or "").strip()), "")
    if not raw:
        raise FileNotFoundError(f"{label} ausente no payload autenticado")
    try:
        data = base64.b64decode(raw.encode("ascii"), validate=True)
    except Exception as exc:
        raise ValueError(f"{label} base64 inválido: {type(exc).__name__}") from exc
    if len(data) > max_bytes:
        raise ValueError(f"{label} excede o limite")
    return data


def _safe_property(value: Any, *, label: str) -> str:
    text = str(value or "").strip()
    if not text or any(marker in text for marker in ("\r", "\n", "\x00")):
        raise ValueError(f"{label} da assinatura compatível inválido")
    return text


def inject_private_files(project: Path, payload: dict[str, Any]) -> dict[str, Any]:
    """Validate all private inputs before writing any private build file."""
    google = decode_b64(payload, ("googleServicesJsonB64", "google_services_json_b64"), 512 * 1024, "google-services.json")
    expected_google = str(payload.get("googleServicesSha256") or payload.get("google_services_sha256") or "").lower().strip()
    google_sha = hashlib.sha256(google).hexdigest()
    if expected_google and expected_google != google_sha:
        raise ValueError("sha256 do google-services.json divergente")
    try:
        parsed = json.loads(google.decode("utf-8"))
    except Exception as exc:
        raise ValueError(f"google-services.json inválido: {type(exc).__name__}") from exc
    if not isinstance(parsed, dict):
        raise ValueError("google-services.json inválido")
    package = str(payload.get("googleServicesPackage") or "dev.core.worker")
    clients = parsed.get("client") if isinstance(parsed.get("client"), list) else []
    matching = []
    for client in clients:
        if not isinstance(client, dict):
            continue
        info = client.get("client_info") if isinstance(client.get("client_info"), dict) else {}
        android = info.get("android_client_info") if isinstance(info.get("android_client_info"), dict) else {}
        if str(android.get("package_name") or "") == package:
            matching.append(client)
    if not matching:
        raise ValueError("google-services.json não contém o package do Core Worker")

    keystore = decode_b64(payload, ("apkSigningKeystoreB64", "apk_signing_keystore_b64"), 1024 * 1024, "keystore compatível")
    expected_key = str(payload.get("apkSigningKeystoreSha256") or payload.get("apk_signing_keystore_sha256") or "").lower().strip()
    key_sha = hashlib.sha256(keystore).hexdigest()
    if expected_key and expected_key != key_sha:
        raise ValueError("sha256 da keystore divergente")
    alias = _safe_property(
        payload.get("apkSigningKeyAlias") or payload.get("apk_signing_key_alias") or "androiddebugkey",
        label="alias",
    )
    store_password = _safe_property(
        payload.get("apkSigningStorePassword") or payload.get("apk_signing_store_password") or "",
        label="senha",
    )
    key_password = _safe_property(
        payload.get("apkSigningKeyPassword") or payload.get("apk_signing_key_password") or store_password,
        label="senha da chave",
    )
    properties = "\n".join((
        "CORE_WORKER_SIGNING_KEYSTORE=core-worker-upload.keystore",
        f"CORE_WORKER_SIGNING_KEY_ALIAS={alias}",
        f"CORE_WORKER_SIGNING_STORE_PASSWORD={store_password}",
        f"CORE_WORKER_SIGNING_KEY_PASSWORD={key_password or store_password}",
        "",
    ))

    app = project / "app"
    app.mkdir(parents=True, exist_ok=True)
    google_path = app / "google-services.json"
    key_path = app / "core-worker-upload.keystore"
    props_path = app / "core-worker-signing.properties"
    google_path.write_bytes(google)
    os.chmod(google_path, 0o600)
    key_path.write_bytes(keystore)
    os.chmod(key_path, 0o600)
    props_path.write_text(properties, encoding="utf-8")
    os.chmod(props_path, 0o600)
    return {
        "googleServicesSha256": google_sha,
        "signingKeystoreSha256": key_sha,
        "signingMode": str(payload.get("apkSigningMode") or "compat-vps-debug-keystore")[:80],
    }


def _needs_copy(source: Path, target: Path, *, sha256_file) -> bool:
    if target.is_symlink() or not target.is_file():
        return True
    if target.stat().st_size != source.stat().st_size:
        return True
    return sha256_file(target) != sha256_file(source)


def hydrate_runtime_assets(project: Path, native_dir: Path, repro_assets: Path, *, sha256_file) -> dict[str, Any]:
    copied: list[str] = []
    jni = project / "app/src/main/jniLibs/arm64-v8a"
    jni.mkdir(parents=True, exist_ok=True)
    allowed_native = {
        "libcoreworker_executor.so", "libcoreworker_runner.so", "libcoreworker_proot.so",
        "libcoreworker_proot_loader.so", "libcoreworker_proot_loader32.so",
        "libcoreworker_busybox.so", "libbusybox.so", "libandroid-selinux.so",
        "libpcre2-8.so", "libtalloc.so",
    }
    if native_dir.is_dir():
        for source in native_dir.iterdir():
            if source.name not in allowed_native or source.is_symlink() or not source.is_file():
                continue
            target = jni / source.name
            if _needs_copy(source, target, sha256_file=sha256_file):
                if target.is_symlink():
                    target.unlink()
                shutil.copy2(source, target)
                copied.append(str(target.relative_to(project)))
    if repro_assets.is_dir():
        for source in repro_assets.rglob("*"):
            if source.is_symlink() or not source.is_file():
                continue
            rel = source.relative_to(repro_assets)
            target = project / "app/src/main/assets" / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            if _needs_copy(source, target, sha256_file=sha256_file):
                if target.is_symlink():
                    target.unlink()
                shutil.copy2(source, target)
                copied.append(str(target.relative_to(project)))
    toolchain_assets = project / "app/src/main/assets/core-linux/android-builder"
    removed_forbidden: list[str] = []
    if toolchain_assets.is_dir():
        forbidden = [
            toolchain_assets / "android-builder-toolchain.zip",
            toolchain_assets / "android-builder-toolchain.parts.json",
            *toolchain_assets.glob("*.cwpart"),
        ]
        for stale in forbidden:
            if stale.is_file() or stale.is_symlink():
                removed_forbidden.append(str(stale.relative_to(project)))
                stale.unlink()
    return {
        "copied": copied,
        "count": len(copied),
        "removedForbiddenToolchainAssets": removed_forbidden,
        "externalToolchainOnly": True,
    }
