"""Optional Rust policy; Python/Discord objects stay on their owning event loop.

TTS_POLICY_RUST=1 opts into native policy. TTS_BACKEND=rust also opts in.
The granular flag permits legacy behavior when the extension is unavailable.
TTS_BACKEND=rust requires the native extension and never selects Python silently.
Validation errors remain visible and never select a different voice.
"""
from __future__ import annotations

import json
import os
import asyncio
import math
from collections.abc import Mapping
from typing import Any


def enabled() -> bool:
    return (os.environ.get("TTS_POLICY_RUST", "0").strip() == "1"
            or os.environ.get("TTS_BACKEND", "").strip().lower() == "rust")


def required() -> bool:
    return os.environ.get("TTS_BACKEND", "").strip().lower() == "rust"


def _safe_numbers(value):
    # Rust validates these strings using the same number grammar. This keeps
    # invalid NaN/Infinity observable without emitting non-standard JSON or
    # falling back to Python in the explicitly selected Rust backend.
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    if isinstance(value, dict):
        return {key: _safe_numbers(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe_numbers(item) for item in value]
    return value


def _native():
    # Reuse one extension loader rather than introducing a second module copy.
    from .fluxo_rust import _carregar_nucleo
    return _carregar_nucleo()


def call(operation: str, **payload: Any) -> Any:
    """Call an explicitly selected native operation (raises on invalid data)."""
    encoded = json.dumps(payload if operation == "score_sha256" else _safe_numbers(payload),
                         ensure_ascii=False, allow_nan=False,
                         separators=(",", ":"))
    return json.loads(_native().policy_call(operation, encoded))


def optional(operation: str, **payload: Any) -> tuple[bool, Any]:
    """Return availability separately: a valid result can itself be None."""
    if not enabled():
        return False, None
    try:
        native = _native()
    except (ImportError, OSError) as error:
        if required():
            raise RuntimeError("TTS_BACKEND=rust exige a extensão nativa de políticas TTS.") from error
        return False, None
    operation_fn = getattr(native, "policy_call", None)
    if not callable(operation_fn):
        if required():
            raise RuntimeError("A extensão Rust instalada não oferece policy_call; atualize o artefato nativo.")
        return False, None
    try:
        encoded = json.dumps(payload if operation == "score_sha256" else _safe_numbers(payload),
                             ensure_ascii=False, allow_nan=False,
                             separators=(",", ":"))
    except (TypeError, ValueError) as error:
        if required():
            raise ValueError(f"Entrada não serializável para a política TTS Rust: {operation}") from error
        # Preserve legacy handling of arbitrary Python values, NaN and infinity.
        return False, None
    return True, json.loads(operation_fn(operation, encoded))


def split_text(text: str, *, escaped_utf8: bool, limit: int, max_parts: int) -> list[str]:
    available, result = optional("split_text", text=text, escaped_utf8=escaped_utf8,
                                 limit=limit, max_parts=max_parts)
    if available:
        return result
    from .runtime import split_text as python_split
    return python_split(text, escaped_utf8=escaped_utf8, limit=limit, max_parts=max_parts)


def compose_audio_filters(options: str, effect_filter: str) -> tuple[str, str]:
    return tuple(call("compose_audio_filters", options=options, effect_filter=effect_filter))


class QueueCoordinator:
    """Native atomic coordinator; stores serialized domain values, no callbacks."""

    def __init__(self, *, max_items: int, max_bytes: int, dedup_limit: int = 4096):
        self._queue = _native().NativeQueueCoordinator(max_items, max_bytes, dedup_limit)

    def admit(self, guild_id: int, message_id: int, items: list[dict]) -> list[str]:
        return self._queue.admit(guild_id, message_id, json.dumps(items, ensure_ascii=False,
                                                               allow_nan=False))

    def dequeue(self, guild_id: int) -> dict | None:
        value = self._queue.dequeue(guild_id)
        return json.loads(value) if value is not None else None

    def complete(self, guild_id: int, request_id: str) -> bool:
        return self._queue.complete(guild_id, request_id)

    def promote(self, guild_id: int, request_id: str) -> bool:
        return self._queue.promote(guild_id, request_id)

    def cancel_request(self, guild_id: int, request_id: str) -> bool:
        return self._queue.cancel_request(guild_id, request_id)

    def cancel_guild(self, guild_id: int) -> int:
        return self._queue.cancel_guild(guild_id)

    def snapshot(self, guild_id: int) -> dict:
        return json.loads(self._queue.snapshot(guild_id))


class _PendingSignatures(Mapping):
    """Read projection of Rust's signature counts, without a second cache."""

    def __init__(self, queue):
        self._queue = queue

    def _snapshot(self):
        return json.loads(self._queue._domain.signatures())

    def __getitem__(self, key):
        return self._snapshot()[key]

    def __iter__(self):
        return iter(self._snapshot())

    def __len__(self):
        return len(self._snapshot())

    def clear(self):
        self._queue._domain.clear_signatures()


class PendingQueue(asyncio.Queue):
    """Asyncio waiters around one Rust owner of pending order and admission.

    The Python dictionary contains only handles to Discord-facing QueueItems.
    Rust owns their order, group identity, signature counts and generation.
    Asyncio retains cancellation-safe waiters, task_done() and join() behavior.
    """

    native_policy = True

    def __init__(self, maxsize: int, signature):
        self._domain = _native().NativePendingQueue(maxsize)
        self._handles = {}
        self._next_token = 0
        self._signature = signature
        self.signatures = _PendingSignatures(self)
        super().__init__(maxsize=maxsize)

    def _init(self, maxsize):
        # asyncio's storage is replaced, while its waiters/accounting stay intact.
        pass

    @property
    def _queue(self):
        # Existing diagnostics read this view; mutating it cannot reorder Rust.
        return tuple(self._handles[token] for token in self._domain.tokens())

    def qsize(self):
        return self._domain.qsize()

    def empty(self):
        return self.qsize() == 0

    def _entry(self, item):
        self._next_token += 1
        return {"token": self._next_token,
                "group": str(getattr(item, "request_id", "")),
                "signature": self._signature(item)}

    def _put(self, item):
        entry = self._entry(item)
        self._domain.put(entry["token"], entry["group"], entry["signature"])
        self._handles[entry["token"]] = item

    def _get(self):
        token = self._domain.pop()
        if token is None:
            raise asyncio.QueueEmpty()
        return self._handles.pop(token)

    def admit_group(self, items):
        entries = [self._entry(item) for item in items]
        result = json.loads(self._domain.admit(json.dumps(entries, ensure_ascii=False)))
        for token in result["dropped"]:
            self._handles.pop(token)
            self.task_done()
        if result["accepted"]:
            for entry, item in zip(entries, items):
                item.generation = result["generation"]
                self._handles[entry["token"]] = item
            self._unfinished_tasks += len(items)
            self._finished.clear()
            for _ in items:
                self._wakeup_next(self._getters)
        return result
