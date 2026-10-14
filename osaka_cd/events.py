"""Small process-local signal; the durable SQLite pointer remains authoritative."""
from __future__ import annotations

from copy import deepcopy
import logging
import threading

_lock = threading.Lock()
_listeners = set()
_log = logging.getLogger(__name__)


def subscribe(callback):
    with _lock:
        _listeners.add(callback)

    def unsubscribe():
        with _lock:
            _listeners.discard(callback)
    return unsubscribe


def publish(snapshot):
    """Listeners may run on a publication thread and must schedule their loop."""
    with _lock:
        listeners = tuple(_listeners)
    for callback in listeners:
        try:
            callback(deepcopy(snapshot))
        except Exception:
            _log.exception("CD catalog listener failed; periodic recovery remains available")
