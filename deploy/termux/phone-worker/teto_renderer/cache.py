from __future__ import annotations

import contextlib
import hashlib
import os
import time
import threading
import tempfile
from pathlib import Path


class FragmentCache:
    # Reconcile files left by an earlier worker or another cache instance, but
    # never traverse the whole cache once per generated phoneme.
    RECONCILE_SECONDS = 60.0
    RECONCILE_WRITES = 256

    def __init__(self, root: str | Path, *, max_mb: int = 256):
        self.root = Path(root).expanduser()
        self.max_bytes = max(8, int(max_mb)) * 1024 * 1024
        self.root.mkdir(parents=True, exist_ok=True)
        self._write_lock = threading.RLock()
        self._entries: dict[Path, tuple[int, int]] = {}
        self._total_bytes = 0
        self._indexed = False
        self._last_scan = 0.0
        self._writes_since_scan = 0
        self._renders = 0
        self._protected: set[Path] = set()
        self._scans = 0
        self._evictions = 0
        self._maintenance_ms = 0.0

    def statistics(self) -> dict[str, float | int]:
        with self._write_lock:
            return {"cache_scans": self._scans, "cache_evictions": self._evictions,
                    "cache_size_bytes": self._total_bytes,
                    "cache_maintenance_ms": self._maintenance_ms}

    def begin_render(self) -> None:
        """Keep returned fragment paths alive until their PCM has been read."""
        with self._write_lock:
            self._ensure_index()
            self._evict_to(self.max_bytes)
            self._renders += 1

    def end_render(self) -> None:
        with self._write_lock:
            self._renders = max(0, self._renders - 1)
            if not self._renders:
                self._protected.clear()

    @staticmethod
    def key(payload: str) -> str:
        return hashlib.sha256(payload.encode("utf-8", errors="replace")).hexdigest()

    def path_for(self, key: str) -> Path:
        safe = "".join(ch for ch in str(key or "").lower() if ch in "0123456789abcdef")[:64]
        if len(safe) < 16:
            safe = self.key(str(key or ""))
        return self.root / safe[:2] / f"{safe}.wav"

    def get(self, key: str) -> Path | None:
        path = self.path_for(key)
        with self._write_lock:
            try:
                if path.is_file() and (size := path.stat().st_size) > 44:
                    # Access recency belongs to the live index. Touching the
                    # file changes mtime/ctime on every hit, invalidating safe
                    # decoded-PCM caches and adding writes to the hot path.
                    # After restart eviction is based on write recency until
                    # this process has observed fresh accesses.
                    if self._indexed:
                        self._remember(path, size, time.time_ns())
                    if self._renders:
                        self._protected.add(path)
                    return path
            except OSError:
                pass
        return None

    def put(self, key: str, source: Path) -> Path:
        data = source.read_bytes()
        with self._write_lock:
            # A fragment larger than the cache, or a render whose live fragments
            # fill it, can use its request-scoped WAV without growing the cache.
            self._ensure_index()
            target = self.path_for(key)
            old_size = self._entries.get(target, (0, 0))[0]
            additional = len(data) - old_size
            if len(data) > self.max_bytes:
                return source
            if self._total_bytes + additional > self.max_bytes:
                self._evict_to(min(int(self.max_bytes * 0.85), self.max_bytes - additional),
                               protected={target})
            if self._total_bytes + additional > self.max_bytes:
                return source
            target.parent.mkdir(parents=True, exist_ok=True)
            descriptor, name = tempfile.mkstemp(prefix=target.stem + ".", suffix=".tmp", dir=target.parent)
            temporary = Path(name)
            try:
                with os.fdopen(descriptor, "wb") as output:
                    output.write(data)
                temporary.replace(target)
            finally:
                with contextlib.suppress(OSError):
                    temporary.unlink()
            self._remember(target, len(data), time.time_ns())
            self._writes_since_scan += 1
            if self._renders:
                self._protected.add(target)
            return target

    def prune(self) -> None:
        with self._write_lock:
            self._ensure_index(force=True)
            self._evict_to(int(self.max_bytes * 0.85) if self._total_bytes > self.max_bytes else self.max_bytes)

    def _remember(self, path: Path, size: int, modified_ns: int) -> None:
        previous = self._entries.get(path, (0, 0))[0]
        self._entries[path] = (size, modified_ns)
        self._total_bytes += size - previous

    def _ensure_index(self, *, force: bool = False) -> None:
        now = time.monotonic()
        if self._indexed and not force and (self._renders or (
                now - self._last_scan < self.RECONCILE_SECONDS
                and self._writes_since_scan < self.RECONCILE_WRITES)):
            return
        started = time.monotonic()
        entries = {}
        try:
            for path in self.root.rglob("*.wav"):
                try:
                    if path.is_file():
                        info = path.stat()
                        previous = self._entries.get(path)
                        recent = previous[1] if previous and previous[0] == info.st_size else 0
                        entries[path] = (info.st_size, max(info.st_mtime_ns, recent))
                except OSError:
                    continue
        except OSError:
            return
        finally:
            self._maintenance_ms += (time.monotonic() - started) * 1000
        self._entries = entries
        self._total_bytes = sum(size for size, _ in entries.values())
        self._indexed = True
        self._last_scan = now
        self._writes_since_scan = 0
        self._scans += 1

    def _evict_to(self, target_bytes: int, *, protected: set[Path] | None = None) -> None:
        if self._total_bytes <= target_bytes:
            return
        started = time.monotonic()
        protected = self._protected | (protected or set())
        try:
            for path, (size, _) in sorted(self._entries.items(), key=lambda entry: entry[1][1]):
                if self._total_bytes <= target_bytes:
                    break
                if path in protected:
                    continue
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    continue
                self._entries.pop(path, None)
                self._total_bytes -= size
                self._evictions += 1
                with contextlib.suppress(OSError):
                    path.parent.rmdir()
        finally:
            self._maintenance_ms += (time.monotonic() - started) * 1000
