"""Cache file IO; the facade owns all state, locks and maintenance scheduling.

System/clock operations are supplied at each call so runtime rebinding remains
visible. Importing this module opens no files and starts no work.
"""
import contextlib
from pathlib import Path


def prune_audio_cache(root, max_bytes, max_files, *, protected=None, os_api, wall_time):
    import fcntl
    stats = []
    with contextlib.suppress(OSError):
        with os_api.scandir(root) as entries:
            for entry in entries:
                if Path(entry.name).suffix.lower() not in {".mp3", ".wav", ".ogg"} or not entry.is_file(follow_symlinks=False):
                    continue
                with contextlib.suppress(OSError):
                    st = entry.stat(follow_symlinks=False)
                    stats.append((st.st_mtime, st.st_size, entry.path, st.st_dev, st.st_ino))
    remaining, total = len(stats), sum(row[1] for row in stats)
    if remaining <= max_files and total <= max_bytes:
        return
    fresh = wall_time() - 180
    for mtime, size, path, device, inode in sorted(stats):
        if remaining <= max_files and total <= max_bytes:
            break
        if mtime > fresh or (protected is not None and path == str(protected)):
            continue
        try:
            with open(path, "rb") as handle:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                before, current = os_api.fstat(handle.fileno()), os_api.stat(path, follow_symlinks=False)
                # Skip candidates replaced or touched since the directory scan.
                if (before.st_dev, before.st_ino) != (device, inode):
                    continue
                if (before.st_dev, before.st_ino) != (current.st_dev, current.st_ino):
                    continue
                if before.st_mtime > fresh or current.st_mtime > fresh:
                    continue
                os_api.unlink(path)
                remaining -= 1
                total -= size
        except OSError:
            continue


def find_file(root, key):
    for fmt in ("mp3", "wav", "ogg"):
        path = root / f"{key}.{fmt}"
        with contextlib.suppress(OSError):
            if path.stat().st_size > 0:
                return path, fmt
    return None, ""


def touch_file(path, *, lock, touches, monotonic, utime):
    now = monotonic()
    key = str(path)
    with lock:
        if now - touches.get(key, -60.0) < 30:
            return
        if len(touches) >= 4096:
            touches.pop(next(iter(touches)))
        touches[key] = now
    with contextlib.suppress(OSError):
        utime(path, None)


def read_bytes(path, *, optional=False):
    try:
        return path.read_bytes()
    except OSError:
        if optional:
            return None
        raise


def publish_bytes(path, data, *, os_api, thread_id, create_parent=False, cleanup_errors=OSError):
    temporary = path.with_suffix(path.suffix + f".tmp-{os_api.getpid()}-{thread_id()}")
    try:
        if create_parent:
            path.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_bytes(data)
        os_api.replace(temporary, path)
    finally:
        with contextlib.suppress(cleanup_errors):
            temporary.unlink()
