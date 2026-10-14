"""Authenticated CD data publication; never invokes the Git/code updater."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import hmac
import json
import logging
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import time

from flask import jsonify, request

from osaka_cd.store import StoreError, get_store

MAX_BYTES = 25 * 1024 * 1024
_log = logging.getLogger(__name__)


class _Publisher:
    """Only two validation workers; SQLite jobs provide the durable queue."""
    def __init__(self, store):
        self.store = store
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="cd-publish")
        self.lock = threading.Lock()
        self.running = set()
        self.retry_timer = None
        self.not_before = 0.0
        self.closed = False

    def _schedule_retry_locked(self, delay):
        if self.retry_timer is None and not self.closed:
            self.retry_timer = threading.Timer(max(1, delay), self._retry)
            self.retry_timer.daemon = True
            self.retry_timer.start()

    def kick(self):
        with self.lock:
            if self.closed:
                return
            if self.not_before > time.monotonic():
                self._schedule_retry_locked(self.not_before - time.monotonic())
                return
            available = 2 - len(self.running)
            if available <= 0:
                return
            jobs = [identity for identity in self.store.outstanding() if identity not in self.running]
            for identity in jobs[:available]:
                self.running.add(identity)
                self.pool.submit(self._run, identity)
            if not jobs and not self.running and self.retry_timer is None:
                delay = self.store.next_retry_delay()
                if delay is not None:
                    self._schedule_retry_locked(delay)

    def _retry(self):
        with self.lock:
            self.retry_timer = None
        try:
            self.kick()
        except Exception:
            _log.exception("CD publication queue unavailable; retry deferred")
            with self.lock:
                self.not_before = time.monotonic() + 30
                self._schedule_retry_locked(30)

    def _run(self, identity):
        try:
            self.store.process(identity)
        except Exception:
            _log.exception("CD publication worker failed; durable job retained")
            with self.lock:
                self.not_before = time.monotonic() + 30
        finally:
            with self.lock:
                self.running.discard(identity)
            try:
                self.kick()
            except Exception:
                _log.exception("CD publication queue unavailable; retry deferred")
                with self.lock:
                    self.not_before = time.monotonic() + 30
                    self._schedule_retry_locked(30)

    def shutdown(self, wait=True):
        with self.lock:
            self.closed = True
            if self.retry_timer is not None:
                self.retry_timer.cancel()
        self.pool.shutdown(wait=wait)


def register_cd_publish_route(app, provider_getter):
    """Keep the webserver registration signature; provider is not required."""
    if not hasattr(app, "extensions"):
        app.extensions = {}
    publisher_lock = threading.Lock()

    def enabled():
        return os.getenv("OSAKA_CD_PUBLISH_ENABLED", "").strip().lower() in {"1", "true", "yes", "on"}

    def authorize():
        if not enabled():
            return jsonify({"ok": False, "error": "CD publication disabled"}), 404
        expected = os.getenv("OSAKA_CD_PUBLISH_TOKEN", "").strip()
        supplied = request.headers.get("Authorization", "")
        if (len(expected) < 32 or not hmac.compare_digest(supplied.encode("utf-8"), f"Bearer {expected}".encode("utf-8"))):
            return jsonify({"ok": False, "error": "unauthorized"}), 403
        return None

    def service():
        with publisher_lock:
            publisher = app.extensions.get("osaka_cd_publisher")
            if publisher is None:
                store = app.extensions.get("osaka_cd_store") or get_store()
                app.extensions["osaka_cd_store"] = store
                publisher = _Publisher(store)
                app.extensions["osaka_cd_publisher"] = publisher
        publisher.kick()
        return publisher

    def error_response(exc):
        code = getattr(exc, "code", "invalid_package")
        status = 503 if code in {"storage_quota", "storage_unavailable", "configuration"} else 409 if code == "conflict" else 404 if code == "not_found" else 422
        return jsonify({"ok": False, "error": str(exc)[:512], "error_code": code}), status

    @app.post("/api/osaka/cd/publish")
    def publish_cd():
        denied = authorize()
        if denied:
            return denied
        if request.mimetype != "application/zip":
            return jsonify({"ok": False, "error": "application/zip required"}), 415
        if request.content_length is not None and request.content_length > MAX_BYTES:
            return jsonify({"ok": False, "error": "ZIP exceeds 25 MiB"}), 413
        expected_versions = request.headers.get("X-CD-Expected-Versions", "")
        try:
            expected_versions = json.loads(expected_versions) if expected_versions else {}
        except (ValueError, TypeError):
            return jsonify({"ok": False, "error": "invalid expected versions"}), 400
        with tempfile.TemporaryDirectory(prefix="osaka-cd-request-") as temp:
            path = Path(temp) / "received.zip"
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
                publisher = service()
                retry_failed = request.headers.get("X-CD-Retry-Failed", "").strip().lower() in {"1", "true", "yes"}
                result = publisher.store.receive(path, digest.hexdigest(), expected_versions, retry_failed=retry_failed)
                publisher.kick()
                result = publisher.store.job(result["job_id"])
            except StoreError as exc:
                return error_response(exc)
            except (OSError, RuntimeError, sqlite3.Error):
                return jsonify({"ok": False, "error": "CD durable storage unavailable"}), 503
        return jsonify(result), 200 if result["state"] == "activated" else 202

    @app.get("/api/osaka/cd/jobs/<job_id>")
    def publication_status(job_id):
        denied = authorize()
        if denied:
            return denied
        try:
            return jsonify(service().store.job(job_id)), 200
        except StoreError as exc:
            return error_response(exc)
        except (OSError, sqlite3.Error):
            return jsonify({"ok": False, "error": "CD durable storage unavailable"}), 503

    @app.get("/api/osaka/cd/status")
    def catalog_status():
        denied = authorize()
        if denied:
            return denied
        try:
            snapshot = service().store.snapshot(strict=True, advertise_legacy=True)
            return jsonify({"ok": True, **{key: snapshot[key] for key in ("generation", "initialized", "activated_generation", "versions")}}), 200
        except (OSError, sqlite3.Error):
            return jsonify({"ok": False, "error": "CD durable storage unavailable"}), 503

    @app.post("/api/osaka/cd/rollback")
    def rollback_cd():
        denied = authorize()
        if denied:
            return denied
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify({"ok": False, "error": "JSON rollback request required"}), 400
        try:
            publisher = service()
            result = publisher.store.enqueue_rollback(payload.get("song_id"), payload.get("generation"), payload.get("expected_version"))
            publisher.kick()
            return jsonify(result), 202
        except StoreError as exc:
            return error_response(exc)
        except (OSError, sqlite3.Error):
            return jsonify({"ok": False, "error": "CD durable storage unavailable"}), 503
        except ValueError:
            return jsonify({"ok": False, "error": "rollback validation failed"}), 422

    if enabled():
        try:
            service()
        except (OSError, RuntimeError, sqlite3.Error, StoreError):
            _log.exception("CD publication recovery unavailable; service will retry on request")
