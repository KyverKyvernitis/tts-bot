"""Optional authenticated CD ingress; enqueue through the existing updater."""
from __future__ import annotations

import hashlib
import hmac
import os
import tempfile
from pathlib import Path

from flask import jsonify, request

MAX_BYTES = 25 * 1024 * 1024


def register_cd_publish_route(app, provider_getter):
    @app.post("/api/osaka/cd/publish")
    def publish_cd():
        if os.getenv("OSAKA_CD_PUBLISH_ENABLED", "").strip().lower() not in {"1", "true", "yes", "on"}:
            return jsonify({"ok": False, "error": "CD publication disabled"}), 404
        expected = os.getenv("OSAKA_CD_PUBLISH_TOKEN", "").strip()
        supplied = request.headers.get("Authorization", "")
        if not expected or not hmac.compare_digest(supplied.encode("utf-8"), f"Bearer {expected}".encode("utf-8")):
            return jsonify({"ok": False, "error": "unauthorized"}), 403
        provider = provider_getter()
        if not callable(provider):
            return jsonify({"ok": False, "error": "updater unavailable"}), 503
        if request.mimetype != "application/zip":
            return jsonify({"ok": False, "error": "application/zip required"}), 415
        if request.content_length is not None and request.content_length > MAX_BYTES:
            return jsonify({"ok": False, "error": "ZIP exceeds 25 MiB"}), 413
        # Never accept a client-supplied filesystem path or package filename.
        with tempfile.TemporaryDirectory(prefix="osaka-cd-publish-") as temp:
            path = Path(temp) / "cd-artifacts.zip"
            count, digest = 0, hashlib.sha256()
            with path.open("wb") as handle:
                while True:
                    chunk = request.stream.read(1024 * 1024)
                    if not chunk:
                        break
                    count += len(chunk)
                    if count > MAX_BYTES:
                        return jsonify({"ok": False, "error": "ZIP exceeds 25 MiB"}), 413
                    handle.write(chunk)
                    digest.update(chunk)
            expected_hash = request.headers.get("X-CD-Package-SHA256", "").strip().lower()
            if not count or (expected_hash and not hmac.compare_digest(expected_hash, digest.hexdigest())):
                return jsonify({"ok": False, "error": "package checksum mismatch"}), 400
            try:
                result = provider("publish_cd", {"zip_path": str(path), "zip_sha256": digest.hexdigest()})
            except Exception:
                return jsonify({"ok": False, "error": "package rejected or updater unavailable"}), 422
            if not isinstance(result, dict) or not result.get("ok"):
                return jsonify(result if isinstance(result, dict) else {"ok": False}), 422
            # queued describes receipt, never claims that VPS installation passed.
            return jsonify(result), 202
