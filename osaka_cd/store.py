"""Durable CD data generations, independent from Git and the code updater.

Every generation has repository-shaped paths. Bytes are durable before the
SQLite transaction publishes their directory. A failed upload never replaces
the active pointer. Only the bot's explicit acknowledgement marks it activated.
"""
from __future__ import annotations

from contextlib import contextmanager
from functools import lru_cache
import fcntl
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import tempfile
import threading
import time
import uuid
import zipfile

from . import events

MAX_BYTES = 25 * 1024 * 1024
MAX_STORAGE_BYTES = 1024 * 1024 * 1024
MIN_FREE_BYTES = 32 * 1024 * 1024
MAX_INDEX_BYTES = 8 * 1024 * 1024
_ID = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
_JOB = re.compile(r"[a-f0-9]{64}\Z")
_instances = {}
_instances_lock = threading.Lock()


class StoreError(ValueError):
    def __init__(self, message, code="invalid_package"):
        super().__init__(message)
        self.code = code


@lru_cache(maxsize=1)
def _contract():
    path = Path(__file__).resolve().parents[1] / "deploy/ttv-cd/package_validation.py"
    spec = importlib.util.spec_from_file_location("osaka_runtime_cd_contract", path)
    if spec is None or spec.loader is None:
        raise StoreError("CD validator unavailable", "configuration")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _digest(value):
    return hashlib.sha256(value).hexdigest()


def _read(path, default):
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else default


def _write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    # Replacing the inode keeps hard-linked previous generations immutable.
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".json-", delete=False) as handle:
        temporary = Path(handle.name)
        handle.write(_canonical(value) + b"\n")
    os.replace(temporary, path)


def _fsync_directory(path):
    descriptor = os.open(str(path), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _sync_tree(root):
    directories = {root}
    for path in root.rglob("*"):
        if path.is_file():
            with path.open("rb") as handle:
                os.fsync(handle.fileno())
            directories.add(path.parent)
        elif path.is_dir():
            directories.add(path)
    for path in sorted(directories, key=lambda item: len(item.parts), reverse=True):
        _fsync_directory(path)


def _copy_catalog(source, target, *, link=False):
    module = _contract()
    def copy(path):
        destination = target / path.relative_to(source)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if (destination.is_file() and destination.stat().st_size == path.stat().st_size
                and _digest(destination.read_bytes()) == _digest(path.read_bytes())):
            return
        destination.unlink(missing_ok=True)
        if link:
            try:
                os.link(path, destination)
                return
            except OSError:
                pass
        shutil.copy2(path, destination)
    for relative in (module.SCORES, module.VIDEOS, module.PREVIEWS):
        path = source / relative
        if path.is_file() and not path.is_symlink():
            copy(path)
    folder = source / "cogs/tts/cd"
    if folder.is_dir() and not folder.is_symlink():
        for path in folder.rglob("*"):
            if path.is_file() and not path.is_symlink() and not any(part.startswith(".") for part in path.relative_to(folder).parts):
                copy(path)


def _entries(root):
    module = _contract()
    scores = {item["id"]: item for item in _read(root / module.SCORES, [])}
    videos = _read(root / module.VIDEOS, {}).get("songs", {})
    previews = _read(root / module.PREVIEWS, {})
    return scores, videos, previews


def _versions(root):
    scores, videos, previews = _entries(root)
    return {identity: _digest(_canonical({"score": score, "video": videos.get(identity),
                                           "sample": previews.get(identity),
                                           "package": _read(root / f"cogs/tts/cd/packages/{identity}.json", None)}))
            for identity, score in scores.items()}


def _normalise_legacy(root):
    """Conserve scores and bytes; do not claim unbound legacy audio is valid."""
    module = _contract()
    scores, videos, previews = _entries(root)
    changed_videos = False
    for identity, video in videos.items():
        video_id = video.get("video_id")
        if not isinstance(video_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
            continue
        path = root / f"cogs/tts/cd/thumbnails/{video_id}.jpg"
        if not video.get("thumbnail_sha256") and path.is_file():
            data = path.read_bytes()
            try:
                module._jpeg(data, identity)
            except Exception:
                continue
            video["thumbnail_sha256"] = _digest(data)
            changed_videos = True
    if changed_videos:
        raw = _read(root / module.VIDEOS, {})
        _write(root / module.VIDEOS, {**raw, "songs": videos})
    valid_previews = {}
    for identity, preview in previews.items():
        score = scores.get(identity)
        path = root / f"cogs/tts/cd/previews/{identity}.mp3"
        if (score and preview.get("manifest_version") in {2, 3}
                and preview.get("score_sha256") == module.score_sha256(score)
                and path.is_file() and _digest(path.read_bytes()) == preview.get("mp3_sha256")):
            valid_previews[identity] = preview
    if valid_previews != previews:
        _write(root / module.PREVIEWS, valid_previews)
    # Legacy package snapshots are only retained when the shared validator can
    # verify them. Their bytes remain in the untouched legacy installation.
    for identity in scores:
        package = root / f"cogs/tts/cd/packages/{identity}.json"
        if package.is_file():
            try:
                module.validate_cd_tree(root, [identity])
            except Exception:
                package.unlink()


def _hydrate_shared_covers(root):
    """A validated same-video JPEG can serve several excerpts of that video."""
    module = _contract()
    raw = _read(root / module.VIDEOS, {})
    videos = raw.get("songs", {})
    changed = set()
    for identity, video in videos.items():
        if video.get("thumbnail_sha256"):
            continue
        video_id = video.get("video_id")
        path = root / f"cogs/tts/cd/thumbnails/{video_id}.jpg"
        if not path.is_file():
            continue
        content = path.read_bytes()
        checksum = _digest(content)
        # This is inheritance from an explicit verified manifest, not accepting
        # an unreferenced uploaded binary or repairing a mismatched hash.
        if not any(other.get("video_id") == video_id and other.get("thumbnail_sha256") == checksum for other in videos.values()):
            continue
        module._jpeg(content, identity)
        video["thumbnail_sha256"] = checksum
        changed.add(identity)
    if changed:
        _write(root / module.VIDEOS, {**raw, "songs": videos})
    return changed


class RuntimeStore:
    def __init__(self, root: Path, legacy_root: Path | None = None):
        self.root = Path(root).expanduser().resolve()
        self.legacy_root = Path(legacy_root).resolve() if legacy_root else Path(__file__).resolve().parents[1]
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        for folder in ("received", "staging", "generations"):
            (self.root / folder).mkdir(exist_ok=True, mode=0o700)
        self.database = self.root / "catalog.sqlite3"
        self._lock = threading.RLock()
        self._cached_snapshot = {"generation": None, "root": str(self.legacy_root),
                                 "initialized": False, "activated_generation": None, "versions": {}}
        self._legacy_versions_cache = None
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS generations (
                    generation TEXT PRIMARY KEY, root TEXT NOT NULL, versions TEXT NOT NULL, created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY, zip_path TEXT, zip_sha256 TEXT,
                    state TEXT NOT NULL, expected_versions TEXT NOT NULL,
                    generation TEXT, result TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL);
            """)
        try:
            self.database.chmod(0o600)
        except OSError:
            pass

    @contextmanager
    def _connect(self, read_only=False):
        connection = sqlite3.connect(str(self.database), timeout=0.1 if read_only else 15)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    @contextmanager
    def _exclusive(self):
        # flock also protects commits performed by a second bot/web process.
        with self._lock, (self.root / "commit.lock").open("a+b") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def snapshot(self, *, strict=False, advertise_legacy=False):
        try:
            with self._connect(read_only=True) as connection:
                meta = dict(connection.execute("SELECT key,value FROM meta").fetchall())
                generation = meta.get("current")
                row = connection.execute("SELECT root,versions FROM generations WHERE generation=?", (generation,)).fetchone() if generation else None
        except sqlite3.OperationalError:
            if strict:
                raise
            return dict(self._cached_snapshot)
        snapshot = {"generation": generation if row else None,
                "root": row["root"] if row else str(self.legacy_root),
                "initialized": bool(row), "activated_generation": meta.get("activated"),
                "versions": json.loads(row["versions"]) if row else {}}
        if advertise_legacy and row is None:
            return self._legacy_snapshot()
        self._cached_snapshot = snapshot
        return dict(snapshot)

    def _legacy_signature(self):
        module = _contract()
        paths = [self.legacy_root / relative for relative in (module.SCORES, module.VIDEOS, module.PREVIEWS)]
        folder = self.legacy_root / "cogs/tts/cd"
        if folder.is_dir() and not folder.is_symlink():
            paths.extend(path for path in folder.rglob("*") if path.is_file() and not path.is_symlink()
                         and not any(part.startswith(".") for part in path.relative_to(folder).parts))
        signatures = []
        for path in sorted(set(paths)):
            if path.is_symlink():
                raise StoreError("legacy CD catalog contains an unsupported link", "configuration")
            if not path.exists():
                signatures.append((str(path), None))
                continue
            stat = path.stat()
            signatures.append((str(path), stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size, stat.st_ino))
        return tuple(signatures)

    def _legacy_snapshot(self):
        """Advertise the same sanitized legacy versions first publication uses.

        This is status-only: the bot keeps reading legacy files, and no runtime
        generation or activation is fabricated by a GET request.
        """
        with self._exclusive():
            current = self.snapshot(strict=True)
            if current["initialized"]:
                return current
            signature = self._legacy_signature()
            cached = self._legacy_versions_cache
            if cached is None or cached[0] != signature:
                try:
                    with tempfile.TemporaryDirectory(prefix="legacy-", dir=self.root / "staging") as temp:
                        staged = Path(temp)
                        # Hard-linking would itself change source ctime/link
                        # counts and invalidate the before/after signature.
                        _copy_catalog(self.legacy_root, staged, link=False)
                        _normalise_legacy(staged)
                        module = _contract()
                        module._scores(staged)
                        module._map(staged, module.VIDEOS, "songs")
                        module._map(staged, module.PREVIEWS)
                        versions = _versions(staged)
                except ValueError as exc:
                    raise StoreError("legacy CD catalog validation unavailable", "configuration") from exc
                if self._legacy_signature() != signature:
                    raise StoreError("legacy CD catalog changed during status lookup", "storage_unavailable")
                self._legacy_versions_cache = (signature, versions)
            current["versions"] = dict(self._legacy_versions_cache[1])
            return current

    def _check_storage_budget(self, additional=0):
        """Bound retained data without deleting generations held by readers."""
        try:
            limit = int(os.getenv("OSAKA_CD_MAX_STORAGE_BYTES", str(MAX_STORAGE_BYTES)))
            minimum_free = int(os.getenv("OSAKA_CD_MIN_FREE_BYTES", str(MIN_FREE_BYTES)))
        except (TypeError, ValueError) as exc:
            raise StoreError("invalid CD storage limits", "configuration") from exc
        if limit <= 0 or minimum_free < 0:
            raise StoreError("invalid CD storage limits", "configuration")
        seen, retained = set(), 0
        for path in self.root.rglob("*"):
            try:
                if path.is_symlink() or not path.is_file():
                    continue
                stat = path.stat()
            except FileNotFoundError:
                continue  # An HTTP temporary file may finish during this scan.
            inode = (stat.st_dev, stat.st_ino)
            if inode not in seen:
                seen.add(inode)
                retained += stat.st_size
        if retained + additional > limit or shutil.disk_usage(self.root).free < additional + minimum_free:
            raise StoreError("CD storage capacity exhausted; active catalog retained", "storage_quota")

    def _clear_interrupted_staging(self):
        # Called only while holding the cross-process commit lock. No other
        # publisher can still own one of these incomplete trees.
        for path in (self.root / "staging").iterdir():
            if path.is_dir() and not path.is_symlink() and path.name.startswith(("catalog-", "rollback-", "baseline-", "legacy-")):
                shutil.rmtree(path)
        # Receiving also holds BEGIN IMMEDIATE before placing its durable ZIP.
        # Holding that transaction here prevents removing a concurrent upload
        # whose rename happened just before its INSERT committed.
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            generations = {row[0] for row in connection.execute("SELECT generation FROM generations")}
            jobs = {row[0] for row in connection.execute("SELECT job_id FROM jobs")}
            for path in (self.root / "generations").iterdir():
                if path.is_dir() and not path.is_symlink() and path.name.startswith("g-") and path.name not in generations:
                    shutil.rmtree(path)
            for path in (self.root / "received").glob("*.zip"):
                if path.is_file() and not path.is_symlink() and _JOB.fullmatch(path.stem) and path.stem not in jobs:
                    path.unlink()

    @staticmethod
    def _check_catalog_readable(root):
        # The bot readers apply the same bounds. Reject an oversized merged
        # index before publishing a pointer they would necessarily refuse.
        module = _contract()
        for relative in (module.SCORES, module.VIDEOS, module.PREVIEWS):
            path = root / relative
            if not path.is_file() or not 0 < path.stat().st_size <= MAX_INDEX_BYTES:
                raise StoreError("CD catalog index exceeds reader limits")

    def receive(self, path, sha256=None, expected_versions=None, *, retry_failed=False):
        path = Path(path)
        if not path.is_file() or path.is_symlink() or not 0 < path.stat().st_size <= MAX_BYTES:
            raise StoreError("ZIP missing or exceeds 25 MiB")
        data = path.read_bytes()
        actual = _digest(data)
        if sha256 and actual != sha256:
            raise StoreError("package checksum mismatch")
        expected_versions = {} if expected_versions is None else expected_versions
        if (not isinstance(expected_versions, dict) or len(expected_versions) > 500
                or any(not isinstance(identity, str) or not _ID.fullmatch(identity)
                       or (value is not None and (not isinstance(value, str) or not _JOB.fullmatch(value)))
                       for identity, value in expected_versions.items())):
            raise StoreError("invalid expected versions", "conflict")
        module = _contract()
        try:
            semantic = []
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                members = module._zip_members(archive)
                allowed = lambda rel: (rel in (module.SCORES, module.VIDEOS, module.PREVIEWS)
                    or bool(re.fullmatch(r"cogs/tts/cd/(?:(?:packages|arrangements)/[a-z0-9]+(?:-[a-z0-9]+)*\.json|previews/[a-z0-9]+(?:-[a-z0-9]+)*\.mp3|thumbnails/[A-Za-z0-9_-]{11}\.jpg)", rel)))
                if not members or any(not allowed(rel) for _, rel in members):
                    raise StoreError("route accepts only CD catalog artifacts")
                for info, relative in members:
                    content = archive.read(info)
                    if relative.endswith(".json"):
                        content = _canonical(json.loads(content))
                    semantic.append((relative, _digest(content)))
            job_id = _digest(_canonical(sorted(semantic)))
        except (OSError, ValueError, zipfile.BadZipFile, RuntimeError) as exc:
            raise StoreError("invalid CD archive") from exc
        destination = self.root / "received" / f"{job_id}.zip"
        temporary = None
        try:
            # Same semantic content can have different ZIP timestamps. A unique
            # durable copy per row prevents a concurrent retransmit rewriting it.
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
                if row is None:
                    self._check_storage_budget(len(data))
                    # The row never refers to a volatile HTTP request directory.
                    with tempfile.NamedTemporaryFile(dir=self.root / "received", prefix=".upload-", delete=False) as handle:
                        temporary = Path(handle.name)
                        handle.write(data)
                        handle.flush()
                        os.fsync(handle.fileno())
                    os.replace(temporary, destination)
                    _fsync_directory(destination.parent)
                    now = time.time()
                    connection.execute("INSERT INTO jobs VALUES(?,?,?,'received',?,NULL,'{}',?,?)",
                                       (job_id, str(destination), actual, _canonical(expected_versions).decode(), now, now))
                elif retry_failed and self._job_result(connection, row)["state"] == "failed":
                    connection.execute("UPDATE jobs SET state='received',expected_versions=?,generation=NULL,result='{}',updated=? WHERE job_id=?",
                                       (_canonical(expected_versions).decode(), time.time(), job_id))
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return self.job(job_id)

    def _job_result(self, connection, row):
        state, result = row["state"], json.loads(row["result"])
        activated = connection.execute("SELECT value FROM meta WHERE key='activated'").fetchone()
        current = connection.execute("SELECT value FROM meta WHERE key='current'").fetchone()
        published = connection.execute("SELECT versions FROM generations WHERE generation=?", (row["generation"],)).fetchone()
        active = connection.execute("SELECT versions FROM generations WHERE generation=?", (current[0],)).fetchone() if current else None
        published_versions = json.loads(published[0]) if published else {}
        current_versions = json.loads(active[0]) if active else {}
        songs = result.get("songs", [])
        compatible = bool(songs) and all(current_versions.get(identity) == published_versions.get(identity) for identity in songs)
        if state in {"applied", "activated"} and not compatible:
            state = "failed"
            result = {**result, "error": "publication superseded by current catalog", "error_code": "superseded"}
        elif state == "applied" and activated and activated[0] == row["generation"]:
            state = "activated"
        return {**result, "ok": state != "failed", "state": state, "job_id": row["job_id"],
                "candidate_id": row["job_id"], "generation": row["generation"],
                "song_versions": {identity: published_versions[identity] for identity in songs if identity in published_versions},
                "current": compatible, "applied": state in {"applied", "activated"}, "installed": state == "activated",
                "published_to_vps": True, "status_url": f"/api/osaka/cd/jobs/{row['job_id']}"}

    def job(self, job_id):
        if not isinstance(job_id, str) or not _JOB.fullmatch(job_id):
            raise StoreError("job not found", "not_found")
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            if row is None:
                raise StoreError("job not found", "not_found")
            return self._job_result(connection, row)

    def outstanding(self):
        with self._connect() as connection:
            rows = connection.execute("SELECT job_id,result FROM jobs WHERE state IN ('received','processing') ORDER BY created").fetchall()
        return [row["job_id"] for row in rows if json.loads(row["result"]).get("retry_at", 0) <= time.time()]

    def next_retry_delay(self):
        with self._connect(read_only=True) as connection:
            rows = connection.execute("SELECT result FROM jobs WHERE state='received'").fetchall()
        retries = [json.loads(row[0]).get("retry_at", 0) for row in rows]
        return max(0.1, min(retries) - time.time()) if retries else None

    def _check_expected(self, expected, versions):
        for identity, version in expected.items():
            if versions.get(identity) != version:
                raise StoreError(f"catalog version conflict: {identity}", "conflict")

    def _manifest(self, root, identity):
        module = _contract()
        scores, videos, previews = _entries(root)
        score = scores.get(identity)
        if not score:
            return
        video, preview = videos.get(identity, {}), previews.get(identity, {})
        package_path = root / f"cogs/tts/cd/packages/{identity}.json"
        package = _read(package_path, {})
        artifacts = {"score": {"status": "ready", "sha256": module.score_sha256(score)}}
        artifacts["thumbnail"] = ({"status": "ready", "path": f"cogs/tts/cd/thumbnails/{video['video_id']}.jpg", "sha256": video["thumbnail_sha256"]}
            if video.get("video_id") and video.get("thumbnail_sha256") else {"status": "pending"})
        artifacts["sample"] = ({"status": "ready", "path": f"cogs/tts/cd/previews/{identity}.mp3", "sha256": preview["mp3_sha256"]}
            if preview.get("score_sha256") == module.score_sha256(score) and preview.get("mp3_sha256") else {"status": "pending"})
        if score.get("arrangement_path"):
            arrangement_path = root / score["arrangement_path"]
            artifacts["arrangement"] = {"status": "ready", "path": score["arrangement_path"],
                                         "sha256": _digest(arrangement_path.read_bytes())}
            package["arrangement_sha256"] = score["arrangement_sha256"]
        else:
            package.pop("arrangement_sha256", None)
        _write(package_path, {**package, "manifest_version": 2 if score.get("arrangement_path") else 1, "id": identity,
                              "revision": score.get("revision", 1), "source_sha256": score["source_sha256"],
                              "score_sha256": module.score_sha256(score), "artifacts": artifacts})

    def _commit(self, staged, job_id, report):
        module = _contract()
        for relative, default in ((module.SCORES, []), (module.VIDEOS, {"version": 1, "songs": {}}), (module.PREVIEWS, {})):
            if not (staged / relative).is_file():
                _write(staged / relative, default)
        self._check_catalog_readable(staged)
        self._check_storage_budget()
        versions = _versions(staged)
        generation = "g-" + _digest(_canonical(versions))[:24] + "-" + uuid.uuid4().hex[:8]
        destination = self.root / "generations" / generation
        _sync_tree(staged)
        os.replace(staged, destination)
        _fsync_directory(destination.parent)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("INSERT INTO generations VALUES(?,?,?,?)", (generation, str(destination), _canonical(versions).decode(), time.time()))
            connection.execute("INSERT INTO meta VALUES('current',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (generation,))
            connection.execute("UPDATE jobs SET state='applied',generation=?,result=?,updated=? WHERE job_id=?",
                               (generation, _canonical(report).decode(), time.time(), job_id))
        self._cached_snapshot = {"generation": generation, "root": str(destination), "initialized": True,
                                 "activated_generation": None, "versions": versions}
        return generation

    def _save_legacy_baseline(self, source):
        """Keep the first normalized legacy state available for per-song rollback."""
        versions = _versions(source)
        if not versions:
            return
        generation = "g-baseline-" + _digest(_canonical(versions))[:32]
        destination = self.root / "generations" / generation
        if not destination.is_dir():
            staged = Path(tempfile.mkdtemp(prefix="baseline-", dir=self.root / "staging"))
            try:
                _copy_catalog(source, staged, link=True)
                _sync_tree(staged)
                os.replace(staged, destination)
                _fsync_directory(destination.parent)
            finally:
                shutil.rmtree(staged, ignore_errors=True)
        with self._connect() as connection:
            connection.execute("INSERT OR IGNORE INTO generations VALUES(?,?,?,?)",
                               (generation, str(destination), _canonical(versions).decode(), time.time()))

    def process(self, job_id):
        with self._connect(read_only=True) as connection:
            initial = connection.execute("SELECT zip_path,state,result FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        if initial and initial["zip_path"] is None and initial["state"] in {"received", "processing"}:
            request = json.loads(initial["result"]).get("rollback_request", {})
            try:
                return self.rollback(request.get("song_id"), request.get("generation"),
                                     request.get("expected_version"), _job_id=job_id)
            except Exception as exc:
                temporary_failure = isinstance(exc, (OSError, sqlite3.OperationalError)) or getattr(exc, "code", "") == "storage_quota"
                result = {"rollback_request": request, "error": "rollback storage unavailable" if temporary_failure else str(exc)[:512],
                          "error_code": "storage_unavailable" if temporary_failure else getattr(exc, "code", "validation_failed")}
                if temporary_failure:
                    result.update(retry_at=time.time() + 30, retry_after_seconds=30)
                with self._connect() as connection:
                    connection.execute("UPDATE jobs SET state=?,result=?,updated=? WHERE job_id=? AND state IN ('received','processing')",
                        ("received" if temporary_failure else "failed", _canonical(result).decode(), time.time(), job_id))
                return self.job(job_id)
        snapshot = None
        with self._exclusive():
            self._clear_interrupted_staging()
            current = self.job(job_id)
            if current["state"] in {"applied", "activated", "failed"}:
                return current
            with self._connect() as connection:
                row = connection.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
                connection.execute("UPDATE jobs SET state='processing',updated=? WHERE job_id=?", (time.time(), job_id))
            staged = Path(tempfile.mkdtemp(prefix="catalog-", dir=self.root / "staging"))
            try:
                active = self.snapshot(strict=True)
                base = staged / "base"
                base.mkdir()
                _copy_catalog(Path(active["root"]), base, link=active["initialized"])
                if not active["initialized"]:
                    _normalise_legacy(base)
                self._check_expected(json.loads(row["expected_versions"]),
                                     active["versions"] if active["initialized"] else _versions(base))
                module = _contract()
                zip_path = Path(row["zip_path"])
                if _digest(zip_path.read_bytes()) != row["zip_sha256"]:
                    raise StoreError("durable package checksum mismatch")
                overlay = staged / "overlay"
                overlay.mkdir()
                with zipfile.ZipFile(row["zip_path"]) as archive:
                    members = module._zip_members(archive)
                    self._check_storage_budget(sum(info.file_size for info, _ in members))
                    paths = [relative for _, relative in members]
                    for info, relative in members:
                        destination = overlay / relative
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        destination.write_bytes(archive.read(info))
                # Reuse the shared ZIP parser, merger and artifact/tree
                # validators without decoding every MP3 twice.
                incoming_scores, incoming_videos, incoming_previews = _entries(overlay)
                songs = set(incoming_scores) | set(incoming_videos) | set(incoming_previews)
                songs.update(Path(relative).stem for relative in paths
                             if relative.startswith(("cogs/tts/cd/packages/", "cogs/tts/cd/arrangements/")))
                module.merge_cd_overlay(overlay, base, paths)
                candidate = staged / "generation"
                candidate.mkdir()
                _copy_catalog(base, candidate, link=True)
                _copy_catalog(overlay, candidate, link=True)
                songs.update(_hydrate_shared_covers(candidate))
                _, merged_videos, _ = _entries(candidate)
                for relative in paths:
                    if relative.startswith("cogs/tts/cd/previews/") and relative.endswith(".mp3"):
                        songs.add(Path(relative).stem)
                    elif relative.startswith("cogs/tts/cd/thumbnails/"):
                        songs.update(identity for identity, video in merged_videos.items() if video.get("video_id") == Path(relative).stem)
                if not songs:
                    raise StoreError("CD publication contains no song")
                module.validate_artifact_references(candidate, paths)
                module.validate_cd_tree(candidate, songs, audio_probe=True)
                for identity in songs:
                    self._manifest(candidate, identity)
                final_report = module.validate_cd_tree(candidate, songs, audio_probe=False)
                if not active["initialized"]:
                    self._save_legacy_baseline(base)
                generation = self._commit(candidate, job_id, {"songs": sorted(songs), "validation": final_report})
                snapshot = dict(self._cached_snapshot)
            except Exception as exc:
                code = getattr(exc, "code", "validation_failed")
                # Validator messages contain song/path identities, not secrets;
                # never expose arbitrary traceback or subprocess stderr via API.
                error = str(exc)[:512] if isinstance(exc, (StoreError, ValueError)) else "CD validation or storage failed"
                temporary_failure = isinstance(exc, (OSError, sqlite3.OperationalError)) or code == "storage_quota"
                result = {"error": error, "error_code": "storage_unavailable" if temporary_failure else code}
                if temporary_failure:
                    result.update(retry_at=time.time() + 30, retry_after_seconds=30)
                with self._connect() as connection:
                    connection.execute("UPDATE jobs SET state=?,result=?,updated=? WHERE job_id=?",
                                       ("received" if temporary_failure else "failed", _canonical(result).decode(), time.time(), job_id))
            finally:
                shutil.rmtree(staged, ignore_errors=True)
        if snapshot:
            events.publish(snapshot)
        return self.job(job_id)

    def acknowledge(self, generation):
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute("SELECT value FROM meta WHERE key='current'").fetchone()
            if not current or generation != current[0]:
                return False
            connection.execute("INSERT INTO meta VALUES('activated',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (generation,))
            active = connection.execute("SELECT versions FROM generations WHERE generation=?", (generation,)).fetchone()
            current_versions = json.loads(active[0])
            # A later generation may include several earlier publications that
            # arrived before the event loop could reload. Acknowledge all those
            # compatible song versions instead of stranding their outbox jobs.
            jobs = connection.execute("SELECT job_id,generation,result FROM jobs WHERE state='applied'").fetchall()
            for job in jobs:
                published = connection.execute("SELECT versions FROM generations WHERE generation=?", (job["generation"],)).fetchone()
                published_versions = json.loads(published[0]) if published else {}
                songs = json.loads(job["result"]).get("songs", [])
                if songs and all(current_versions.get(identity) == published_versions.get(identity) for identity in songs):
                    connection.execute("UPDATE jobs SET state='activated',updated=? WHERE job_id=?", (time.time(), job["job_id"]))
                elif songs:
                    connection.execute("UPDATE jobs SET state='failed',result=?,updated=? WHERE job_id=?", (
                        _canonical({"error": "publication superseded before activation", "error_code": "superseded", "songs": songs}).decode(),
                        time.time(), job["job_id"]))
        return True

    @staticmethod
    def _validate_rollback(song_id, generation, expected_version):
        if not isinstance(song_id, str) or not _ID.fullmatch(song_id):
            raise StoreError("invalid song id")
        if generation is not None and (not isinstance(generation, str) or len(generation) > 128):
            raise StoreError("invalid generation")
        if expected_version is not None and (not isinstance(expected_version, str) or not _JOB.fullmatch(expected_version)):
            raise StoreError("invalid expected version", "conflict")

    def _choose_rollback(self, current, song_id, generation):
        if not current["initialized"]:
            raise StoreError("catalog is not initialized", "not_found")
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM generations ORDER BY created DESC").fetchall()
        chosen = next((row for row in rows if row["generation"] != current["generation"]
                       and song_id in json.loads(row["versions"])
                       and (row["generation"] == generation if generation else json.loads(row["versions"])[song_id] != current["versions"].get(song_id))), None)
        if chosen is None:
            raise StoreError("previous song generation not found", "not_found")
        return chosen

    def enqueue_rollback(self, song_id, generation=None, expected_version=None):
        """Persist a bounded request; HTTP never performs media validation."""
        self._validate_rollback(song_id, generation, expected_version)
        with self._exclusive():
            current = self.snapshot(strict=True)
            if expected_version is not None:
                self._check_expected({song_id: expected_version}, current["versions"])
            chosen = self._choose_rollback(current, song_id, generation)
            job_id = _digest(_canonical({"rollback": song_id, "from": current["generation"], "to": chosen["generation"]}))
            request = {"song_id": song_id, "generation": chosen["generation"],
                       "expected_version": current["versions"].get(song_id)}
            with self._connect() as connection:
                now = time.time()
                connection.execute("INSERT OR IGNORE INTO jobs VALUES(?,NULL,NULL,'received','{}',NULL,?,?,?)",
                    (job_id, _canonical({"rollback_request": request}).decode(), now, now))
        return self.job(job_id)

    def rollback(self, song_id, generation=None, expected_version=None, *, _job_id=None):
        self._validate_rollback(song_id, generation, expected_version)
        with self._exclusive():
            if _job_id is not None:
                with self._connect() as connection:
                    row = connection.execute("SELECT * FROM jobs WHERE job_id=?", (_job_id,)).fetchone()
                    if row is not None and row["state"] in {"applied", "activated", "failed"}:
                        return self._job_result(connection, row)
            self._clear_interrupted_staging()
            current = self.snapshot(strict=True)
            if expected_version is not None:
                self._check_expected({song_id: expected_version}, current["versions"])
            chosen = self._choose_rollback(current, song_id, generation)
            job_id = _job_id or _digest(_canonical({"rollback": song_id, "from": current["generation"], "to": chosen["generation"]}))
            with self._connect() as connection:
                now = time.time()
                rollback_request = {"song_id": song_id, "generation": chosen["generation"],
                                    "expected_version": current["versions"].get(song_id)}
                connection.execute("INSERT OR IGNORE INTO jobs VALUES(?,NULL,NULL,'processing','{}',NULL,?,?,?)",
                    (job_id, _canonical({"rollback_request": rollback_request}).decode(), now, now))
            staged = Path(tempfile.mkdtemp(prefix="rollback-", dir=self.root / "staging"))
            try:
                _copy_catalog(Path(current["root"]), staged, link=True)
                previous = Path(chosen["root"])
                old_scores, old_videos, old_previews = _entries(previous)
                scores, videos, previews = _entries(staged)
                scores[song_id] = old_scores[song_id]
                for target, old in ((videos, old_videos), (previews, old_previews)):
                    target.pop(song_id, None)
                    if song_id in old:
                        target[song_id] = old[song_id]
                module = _contract()
                _write(staged / module.SCORES, list(scores.values()))
                raw = _read(staged / module.VIDEOS, {})
                _write(staged / module.VIDEOS, {**raw, "songs": videos})
                _write(staged / module.PREVIEWS, previews)
                paths = [f"cogs/tts/cd/packages/{song_id}.json", f"cogs/tts/cd/previews/{song_id}.mp3"]
                arrangement_path = old_scores[song_id].get("arrangement_path")
                if arrangement_path:
                    paths.append(arrangement_path)
                if song_id in old_videos:
                    video_id = old_videos[song_id]['video_id']
                    cover_path = f"cogs/tts/cd/thumbnails/{video_id}.jpg"
                    other_hashes = {video.get("thumbnail_sha256") for identity, video in videos.items()
                                    if identity != song_id and video.get("video_id") == video_id and video.get("thumbnail_sha256")}
                    old_cover = previous / cover_path
                    if other_hashes and old_cover.is_file() and other_hashes != {_digest(old_cover.read_bytes())}:
                        raise StoreError("rollback cover is shared by newer song versions", "conflict")
                    # A pending old cover must not delete a verified shared JPEG.
                    if not other_hashes or old_cover.is_file():
                        paths.append(cover_path)
                for relative in paths:
                    source, destination = previous / relative, staged / relative
                    destination.unlink(missing_ok=True)
                    if source.is_file():
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(source, destination)
                self._manifest(staged, song_id)
                report = module.validate_cd_tree(staged, [song_id], audio_probe=True)
                module.validate_cd_tree(staged, scores, audio_probe=False)
                self._commit(staged, job_id, {"songs": [song_id], "rollback_from": current["generation"], "validation": report})
                snapshot = dict(self._cached_snapshot)
            except Exception as exc:
                temporary_failure = isinstance(exc, (OSError, sqlite3.OperationalError)) or getattr(exc, "code", "") == "storage_quota"
                result = {"rollback_request": rollback_request,
                          "error": "rollback storage unavailable" if temporary_failure else str(exc)[:512],
                          "error_code": "storage_unavailable" if temporary_failure else getattr(exc, "code", "validation_failed")}
                if temporary_failure:
                    result.update(retry_at=time.time() + 30, retry_after_seconds=30)
                with self._connect() as connection:
                    connection.execute("UPDATE jobs SET state=?,result=?,updated=? WHERE job_id=? AND state IN ('received','processing')",
                        ("received" if temporary_failure else "failed", _canonical(result).decode(), time.time(), job_id))
                raise
            finally:
                shutil.rmtree(staged, ignore_errors=True)
        events.publish(snapshot)
        return self.job(job_id)


def get_store():
    root = Path(os.getenv("OSAKA_CD_DATA_ROOT", str(Path.home() / ".local/share/osaka/cd"))).expanduser().resolve()
    legacy = Path(__file__).resolve().parents[1]
    key = (str(root), str(legacy))
    with _instances_lock:
        store = _instances.get(key)
        if store is None:
            store = RuntimeStore(root, legacy)
            _instances[key] = store
        return store


def runtime_snapshot():
    return get_store().snapshot()


def runtime_root():
    return Path(runtime_snapshot()["root"])
