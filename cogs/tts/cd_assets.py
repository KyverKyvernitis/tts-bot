"""Bounded reuse of validated CD files; every hit checks the installed version."""
from __future__ import annotations

from collections import OrderedDict
from pathlib import Path
import hashlib
import stat


def file_signature(path: Path) -> tuple[int, ...] | None:
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode):
            return None
        return info.st_mtime_ns, info.st_ctime_ns, info.st_size, info.st_ino
    except OSError:
        return None


class CheckedFileCache:
    """Keep small bytes separately from hash approvals for large catalogues.

    Revision checks remain cheap after bytes are evicted. A rewrite, replacement,
    missing file, symlink or manifest hash change immediately invalidates a hit.
    """

    def __init__(self, *, max_bytes=8 * 1024 * 1024, max_entries=512):
        self.max_bytes, self.max_entries = max_bytes, max_entries
        self._checked = OrderedDict()
        self._bytes = OrderedDict()
        self._size = 0

    def _key(self, path, digest, limit, kind):
        signature = file_signature(path)
        if signature is None or not 0 < signature[2] <= limit:
            return None
        return str(path), signature, digest, limit, kind

    def _load(self, path, key, validate):
        if key in self._checked:
            self._checked.move_to_end(key)
            if not self._checked[key]:
                return None
        cached = self._bytes.get(key)
        if cached is not None:
            self._bytes.move_to_end(key)
            return cached
        try:
            data = path.read_bytes()
        except OSError:
            # A transient read failure does not prove the bytes are invalid.
            # Keep the unchanged file eligible for the next automatic retry.
            return None
        try:
            valid = (file_signature(path) == key[1]
                     and hashlib.sha256(data).hexdigest() == key[2]
                     and bool(validate(data)))
        except (OSError, ValueError):
            valid = False
        self._checked[key] = valid
        self._checked.move_to_end(key)
        while len(self._checked) > self.max_entries:
            expired, _ = self._checked.popitem(last=False)
            old = self._bytes.pop(expired, None)
            if old is not None:
                self._size -= len(old)
        if not valid:
            return None
        if len(data) <= self.max_bytes:
            self._bytes[key] = data
            self._size += len(data)
            while self._size > self.max_bytes or len(self._bytes) > self.max_entries:
                _, old = self._bytes.popitem(last=False)
                self._size -= len(old)
        return data

    def read(self, path, digest, limit, kind, validate):
        path = Path(path)
        key = self._key(path, digest, limit, kind)
        return self._load(path, key, validate) if key is not None else None

    def revision(self, path, digest, limit, kind, validate):
        path = Path(path)
        key = self._key(path, digest, limit, kind)
        if key is None:
            return ''
        if key not in self._checked:
            self._load(path, key, validate)
        else:
            self._checked.move_to_end(key)
        return digest if self._checked.get(key) else ''


FILES = CheckedFileCache()
