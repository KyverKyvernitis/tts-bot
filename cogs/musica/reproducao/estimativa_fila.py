"""Previsões de espera usando somente snapshots e metadados já em memória.

Não resolve áudio, consulta providers, preenche caches ou agenda trabalho. A
previsão representa uma passagem pela ordem atual, sem contar o próprio alvo.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import math
import time
from typing import Any


MAX_AGENT_MIRROR_AGE_SECONDS = 15.0


@dataclass(frozen=True)
class QueueWaitEstimate:
    seconds: float | None
    paused: bool = False

    def __post_init__(self) -> None:
        # Keep the public formatter total even for malformed finite inputs whose
        # final speed conversion overflowed, or direct diagnostic construction.
        object.__setattr__(self, "seconds", _number(self.seconds))

    def _time_label(self) -> str:
        total = max(0, math.ceil(float(self.seconds or 0)))
        hours, remainder = divmod(total, 3600)
        minutes, seconds = divmod(remainder, 60)
        return f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes}:{seconds:02d}"

    def _word_time_label(self) -> str:
        total = max(0, math.ceil(float(self.seconds or 0)))
        minutes, seconds = divmod(total, 60)
        minute_unit = "minuto" if minutes == 1 else "minutos"
        second_unit = "segundo" if seconds == 1 else "segundos"
        return f"{minutes} {minute_unit} e {seconds} {second_unit}"

    def label(self) -> str:
        if self.seconds is None:
            return "-# ⏳ Previsão indisponível"
        if self.paused:
            return f"-# ⏸️ Após retomar: {self._word_time_label()}"
        return f"-# ⏳ Vai começar em: {self._word_time_label()}"

    def metadata_label(self) -> str:
        if self.seconds is None:
            return "⏳ previsão indisponível"
        if self.paused:
            return f"⏸️ após retomar: **{self._time_label()}**"
        return f"⏳ aproximadamente **{self._time_label()}**"


def _value(source: Any, key: str, default: Any = None) -> Any:
    return source.get(key, default) if isinstance(source, Mapping) else getattr(source, key, default)


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) and result >= 0 else None


def _integer(value: Any) -> int | None:
    number = _number(value)
    return int(number) if number is not None and number.is_integer() else None


def _track_id(track: Any) -> str:
    if isinstance(track, str):
        return track.strip()
    return str(_value(track, "queue_item_id", "") or "").strip()


def _virtual_identity(track: Any) -> tuple[str, str, str, int] | None:
    instance = str(_value(track, "virtual_playlist_instance_id", "") or "").strip()
    provider = str(_value(track, "virtual_provider", "") or "").strip()
    url = str(_value(track, "virtual_source_url", "") or "").strip()
    index = _integer(_value(track, "virtual_source_index"))
    return (instance, provider, url, index) if instance and provider and url and index is not None else None


def _matches(target: Any, track: Any) -> bool:
    identity = _track_id(target)
    if identity:
        return identity == _track_id(track)
    virtual = _virtual_identity(target)
    if virtual is not None:
        return virtual == _virtual_identity(track)
    # Local queues can contain the original MusicTrack before it has an ID.
    # Comparing titles/URLs would confuse legitimate repeated requests.
    return target is track and target is not None and not isinstance(target, str)


def _duration(track: Any) -> float | None:
    if track is None or bool(_value(track, "is_live", False)):
        return None
    return _number(_value(track, "duration"))


def _playback_clock(state: Any, now: float) -> tuple[float | None, float, bool]:
    """Return remaining media seconds, speed and whether resume is required."""
    raw = isinstance(state, Mapping)
    status = str(_value(state, "status" if raw else "current_status", "") or "").lower()
    paused = bool(_value(state, "paused", False)) or status == "paused"
    speed = _number(_value(state, "speed_multiplier" if raw else "playback_speed", 1.0))
    if speed is None or not 0.4 <= speed <= 1.75:
        return None, 1.0, paused
    loop = _value(state, "loop_mode", _value(state, "repeat", "off"))
    loop = str(getattr(loop, "value", loop) or "off").lower()
    if loop != "off" and loop != "all":
        return None, speed, paused
    if (status not in {"playing", "paused"}
            or bool(_value(state, "voice_runtime_recovery_pending" if raw else "agent_voice_recovery_pending", False))):
        return None, speed, paused
    if raw:
        if _value(state, "voice_connected") is False:
            return None, speed, paused
        if not paused and _value(state, "confirmed_playing") is False:
            return None, speed, paused
    elif str(_value(state, "current_backend", "") or "").lower() == "agent":
        if _value(state, "agent_voice_connected") is False:
            return None, speed, paused
        last_success = _number(_value(state, "agent_monitor_last_success_at"))
        monitor = _value(state, "agent_monitor_task")
        monitor_active = monitor is not None and callable(getattr(monitor, "done", None)) and not monitor.done()
        if monitor_active and last_success and now - last_success > MAX_AGENT_MIRROR_AGE_SECONDS:
            return None, speed, paused
    current = _value(state, "current")
    duration = _duration(current)
    if duration is None:
        return None, speed, paused
    if raw:
        milliseconds = _number(_value(state, "position_ms"))
        position = milliseconds / 1000.0 if milliseconds is not None else None
    elif paused:
        position = _number(_value(state, "voice_status_pause_position_seconds"))
    else:
        started = _number(_value(state, "current_started_at_monotonic"))
        offset = _number(_value(state, "current_start_offset_seconds", 0.0))
        position = (offset + max(0.0, now - started) * speed
                    if started is not None and started > 0 and offset is not None else None)
    return (max(0.0, duration - position) if position is not None else None), speed, paused


def _layout(state: Any) -> list[Any]:
    if isinstance(state, Mapping):
        if "queue_layout" in state:
            layout = state.get("queue_layout")
            return list(layout) if isinstance(layout, list) else []
        preview = state.get("queue")
        # Older worker snapshots expose only a leading preview. Numeric waits
        # are safe only when that preview proves the whole queue is present.
        count = _integer(state.get("logical_queue_size", state.get("queue_size")))
        virtual = state.get("virtual_playlists") or state.get("virtual_playlist")
        if not isinstance(preview, list) or virtual or count is None or count != len(preview):
            return []
        return [{"kind": "track", "track": item} for item in preview]
    layout = _value(state, "agent_queue_layout")
    if isinstance(layout, list) and layout:
        return list(layout)
    if (_value(state, "agent_virtual_playlists") or _value(state, "agent_virtual_playlist")):
        return []  # A short materialized preview cannot replace logical order.
    forward = list(_value(state, "forward_queue", ()) or ())
    queue = _value(state, "queue")
    stored = _value(queue, "_queue", queue if isinstance(queue, (list, tuple)) else ())
    items = forward + list(stored or ())
    remote_count = _integer(_value(state, "agent_remote_queue_size"))
    if str(_value(state, "current_backend", "") or "").lower() == "agent" and remote_count != len(items):
        return []
    return [{"kind": "track", "track": item} for item in items]


def _virtual_info(entry: Mapping[str, Any]) -> dict[str, Any] | None:
    local = entry.get("virtual")
    if isinstance(local, Mapping):
        return dict(local)
    cursor = entry.get("cursor")
    if not isinstance(cursor, Mapping):
        return None
    return {**cursor, "remaining": entry.get("remaining")}


def _virtual_prefixes(info: Mapping[str, Any], duration_state: Any) -> tuple[
    tuple[str, str, str] | None, int | None, int | None, dict[int, float],
]:
    """Read cached contiguous prefixes, never iterate an unbounded source range."""
    key = tuple(str(info.get(field) or "").strip() for field in ("instance_id", "provider", "source_url"))
    start = _integer(info.get("next_offset", 0))
    if not all(key) or start is None or _integer(info.get("shuffle_seed", 0)) != 0:
        return None, None, None, {}
    raw_end = info.get("block_end_offset")
    if raw_end in (None, ""):
        raw_end = info.get("total_tracks")
    ends = _value(duration_state, "agent_virtual_duration_ends", {})
    if raw_end in (None, "") and isinstance(ends, Mapping):
        raw_end = ends.get(key)
    end = _integer(raw_end)
    remaining = _integer(info.get("remaining"))
    if remaining is not None:
        end = start + remaining if end is None else min(end, start + remaining)
    if end is None or end < start:
        return None, None, None, {}
    cache = _value(duration_state, "agent_virtual_duration_cache", {})
    saved = cache.get(key, {}) if isinstance(cache, Mapping) else {}
    prefixes = {start: 0.0}
    if not isinstance(saved, Mapping):
        return key, start, end, prefixes
    # Work is bounded by actual in-memory metadata, including malformed huge
    # playlist totals. Missing positions stop the exact prefix immediately.
    indexes = sorted(index for index in saved if isinstance(index, int) and not isinstance(index, bool)
                     and start <= index < end)
    cursor, total = start, 0.0
    for index in indexes:
        if index != cursor:
            break
        seconds = _number(saved[index])
        if seconds is None:
            break
        total += seconds
        if not math.isfinite(total):
            break
        cursor += 1
        prefixes[cursor] = total
    return key, start, end, prefixes


def estimate_queue_waits(
    state: Any, targets: Any, *, duration_state: Any = None, now: float | None = None,
) -> list[QueueWaitEstimate]:
    """Estimate several visible entries in one ordered-prefix traversal.

    ``duration_state`` may supply the VPS cache for a fresh raw worker snapshot;
    only matching occurrence/provider/source keys are read. Neither input is
    modified. Missing or contradictory information returns an unknown estimate.
    """
    targets = list(targets)
    clock = time.monotonic() if now is None else _number(now)
    if clock is None:
        return [QueueWaitEstimate(None) for _target in targets]
    media_seconds, speed, paused = _playback_clock(state, clock)
    results = [QueueWaitEstimate(None, paused) for _target in targets]
    if media_seconds is None:
        return results
    duration_state = state if duration_state is None else duration_state
    current = _value(state, "current")
    invalid_targets = [_matches(target, current) for target in targets]
    matches = [0 for _target in targets]
    total: float | None = media_seconds

    def record(index: int, seconds: float | None) -> None:
        matches[index] += 1
        valid = not invalid_targets[index] and matches[index] == 1 and seconds is not None
        results[index] = QueueWaitEstimate(seconds / speed if valid else None, paused)

    for entry in _layout(state):
        if not isinstance(entry, Mapping):
            total = None
            continue
        kind = str(entry.get("kind") or "").lower()
        if kind == "track":
            track = entry.get("track")
            if _matches(current, track):
                total = None  # An unrepaired current/queue alias has no safe order.
            for index, target in enumerate(targets):
                if _matches(target, track):
                    record(index, total)
            duration = _duration(track)
            total = total + duration if total is not None and duration is not None else None
        elif kind == "virtual":
            info = _virtual_info(entry)
            if info is None:
                total = None
                continue
            key, start, end, prefixes = _virtual_prefixes(info, duration_state)
            for index, target in enumerate(targets):
                virtual = _virtual_identity(target)
                if (not _track_id(target) and virtual is not None and key == virtual[:3]
                        and start is not None and end is not None and start <= virtual[3] < end):
                    prefix = prefixes.get(virtual[3])
                    record(index, total + prefix if total is not None and prefix is not None else None)
            duration = prefixes.get(end) if end is not None else None
            total = total + duration if total is not None and duration is not None else None
        else:
            total = None
        if total is not None and not math.isfinite(total):
            total = None
    return results


def estimate_queue_wait(
    state: Any, target: Any, *, duration_state: Any = None, now: float | None = None,
) -> QueueWaitEstimate:
    return estimate_queue_waits(state, [target], duration_state=duration_state, now=now)[0]


def queue_wait_line(
    state: Any, target: Any, *, duration_state: Any = None, now: float | None = None,
) -> str:
    return estimate_queue_wait(state, target, duration_state=duration_state, now=now).label()
