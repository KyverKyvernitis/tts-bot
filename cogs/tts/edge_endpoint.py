"""Owner-configured Edge synthesis endpoints, scoped to each asyncio task.

edge-tts 7.3.1 interpolates a module-level WSS_URL inside Communicate.__stream.
An immutable str subclass resolves that value through a ContextVar at the
moment the WebSocket is opened. Never mutate WSS_URL on a live request: the
other in-flight jobs keep their selected endpoint, including 403 retries.
"""
from __future__ import annotations

import contextvars
import json
import os
import tempfile
import time
from collections import deque
from contextlib import contextmanager
from pathlib import Path

_LEGACY_PREFIX = "wss://speech.platform.bing.com/consumer/speech/synthesize/readaloud/edge/v1?"
_NEW_ENDPOINT = "wss://api.msedgeservices.com/tts/cognitiveservices/websocket/v1"
ENDPOINT_LABELS = {"services": "Edge Services", "bing": "Bing Speech"}
DEFAULT_ENDPOINTS = ("services",)
_ACTIVE_ENDPOINT = contextvars.ContextVar("tts_edge_synthesis_endpoint", default="services")
_CONFIG_PATH = Path(os.getenv("TTS_EDGE_ENDPOINT_CONFIG_PATH", "") or Path(__file__).resolve().parents[2] / "data" / "tts_edge_endpoints.json")


class _ScopedWssUrl(str):
    """Preserves the original string interface for edge-tts and legacy tests."""

    def __new__(cls, default_url: str, legacy_url: str):
        obj = super().__new__(cls, default_url)
        obj._legacy_url = legacy_url
        return obj

    def __str__(self) -> str:
        return self._legacy_url if _ACTIVE_ENDPOINT.get() == "bing" else super().__str__()

    def __format__(self, spec: str) -> str:
        return format(str(self), spec)


@contextmanager
def edge_endpoint_scope(endpoint: str):
    if endpoint not in ENDPOINT_LABELS:
        raise ValueError("endpoint Edge desconhecido")
    marker = _ACTIVE_ENDPOINT.set(endpoint)
    try:
        yield
    finally:
        _ACTIVE_ENDPOINT.reset(marker)


def configure_edge_endpoint() -> bool:
    """Install the scoped URL one time only for the known edge-tts layout."""
    try:
        from edge_tts import communicate
        from edge_tts.constants import TRUSTED_CLIENT_TOKEN
    except (ImportError, AttributeError):
        return False
    existing = getattr(communicate, "WSS_URL", None)
    if isinstance(existing, _ScopedWssUrl):
        return True
    if not isinstance(existing, str) or not isinstance(TRUSTED_CLIENT_TOKEN, str) or not TRUSTED_CLIENT_TOKEN:
        return False
    new_url = _NEW_ENDPOINT + "?Ocp-Apim-Subscription-Key=" + TRUSTED_CLIENT_TOKEN
    legacy_url = _LEGACY_PREFIX + "TrustedClientToken=" + TRUSTED_CLIENT_TOKEN
    if existing != legacy_url and existing != new_url:
        return False
    communicate.WSS_URL = _ScopedWssUrl(new_url, legacy_url)
    return True


def transport_ready() -> bool:
    """Guard the admin UI if a future edge-tts layout is not supported."""
    try:
        from edge_tts import communicate
    except ImportError:
        return False
    return isinstance(getattr(communicate, "WSS_URL", None), _ScopedWssUrl)


def validate_endpoints(values) -> tuple[str, ...]:
    selected = tuple(key for key in ENDPOINT_LABELS if key in values)
    if not selected or len(set(values)) != len(values) or len(selected) != len(values):
        raise ValueError("Selecione um ou os dois endpoints disponíveis")
    return selected


class EndpointSettings:
    """Small, process-wide configuration. Reads only at construction."""

    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path is not None else _CONFIG_PATH
        self.enabled = DEFAULT_ENDPOINTS
        self.load_error = ""
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict) or payload.get("version") != 1:
                raise ValueError("formato inválido")
            self.enabled = validate_endpoints(payload.get("enabled", []))
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError) as exc:
            self.load_error = type(exc).__name__

    def save(self, values) -> tuple[str, ...]:
        """Persist first; publish to in-flight readers only after atomic replace."""
        selected = validate_endpoints(values)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = None
        try:
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=self.path.parent,
                                             prefix=".tts-edge-", suffix=".tmp", delete=False) as tmp:
                temp_path = Path(tmp.name)
                os.chmod(temp_path, 0o600)
                json.dump({"version": 1, "enabled": list(selected)}, tmp, separators=(",", ":"))
                tmp.write("\n")
                tmp.flush()
                os.fsync(tmp.fileno())
            os.replace(temp_path, self.path)
            # Durability for a completed administrative change; only runs on submit.
            try:
                directory_fd = os.open(self.path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except OSError:
                pass
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)
        self.enabled = selected
        self.load_error = ""
        return selected


settings = EndpointSettings()


class _PairStats:
    __slots__ = ("attempts", "successes", "failures", "cancellations", "http_429", "http_403",
                 "no_audio", "timeouts", "fallback_selected", "active", "peak_active", "total_ms", "total_first_audio_ms",
                 "first_audio_samples", "blocked_until", "minutes")

    def __init__(self):
        self.attempts = self.successes = self.failures = self.cancellations = 0
        self.http_429 = self.http_403 = self.no_audio = self.timeouts = self.fallback_selected = 0
        self.active = self.peak_active = 0
        self.total_ms = self.total_first_audio_ms = self.first_audio_samples = 0
        self.blocked_until = 0.0
        self.minutes = deque(maxlen=61)

    def bump(self, field: str):
        setattr(self, field, getattr(self, field) + 1)
        minute = int(time.monotonic() // 60)
        if not self.minutes or self.minutes[-1][0] != minute:
            self.minutes.append((minute, {}))
        counts = self.minutes[-1][1]
        counts[field] = counts.get(field, 0) + 1

    def snapshot(self):
        now = time.monotonic()
        minute = int(now // 60)
        def recent(window):
            totals = {}
            for bucket, counts in self.minutes:
                if minute - bucket < window:
                    for key, amount in counts.items():
                        totals[key] = totals.get(key, 0) + amount
            return totals
        return {
            "attempts": self.attempts, "successes": self.successes, "failures": self.failures,
            "cancellations": self.cancellations, "http_429": self.http_429,
            "http_403": self.http_403, "no_audio": self.no_audio, "timeouts": self.timeouts,
            "fallback_selected": self.fallback_selected,
            "active": self.active, "peak_active": self.peak_active,
            "avg_synth_ms": round(self.total_ms / self.successes, 2) if self.successes else 0,
            "avg_first_audio_ms": round(self.total_first_audio_ms / self.first_audio_samples, 2) if self.first_audio_samples else 0,
            "first_audio_samples": self.first_audio_samples,
            "unavailable_seconds": round(max(0.0, self.blocked_until - now), 1),
            "last_15m": recent(15), "last_60m": recent(60),
        }


class EdgeEndpointTelemetry:
    """A fixed number of counters and bounded minute buckets. No I/O per speech."""

    def __init__(self):
        self._pairs: dict[tuple[str, str], _PairStats] = {}

    def _pair(self, endpoint: str, ip: str) -> _PairStats:
        key = (endpoint, ip or "padrão")
        if key not in self._pairs:
            self._pairs[key] = _PairStats()
        return self._pairs[key]

    def choose(self, ip: str = "") -> str:
        enabled = settings.enabled
        now = time.monotonic()
        for endpoint in enabled:
            if self._pair(endpoint, ip).blocked_until <= now:
                return endpoint
        return enabled[0]

    def start(self, endpoint: str, ip: str):
        item = self._pair(endpoint, ip)
        item.bump("attempts")
        if len(settings.enabled) == 2 and endpoint != settings.enabled[0]:
            item.bump("fallback_selected")
        item.active += 1
        item.peak_active = max(item.peak_active, item.active)

    def success(self, endpoint: str, ip: str, duration_ms: float = 0, first_audio_ms: float = 0):
        item = self._pair(endpoint, ip)
        item.bump("successes")
        item.active = max(0, item.active - 1)
        item.total_ms += max(0, duration_ms)
        if first_audio_ms > 0:
            item.total_first_audio_ms += first_audio_ms
            item.first_audio_samples += 1
        # A completed older request must not erase a newer endpoint failure.
        # The bounded suppression expires naturally; the shared-IP 429 policy
        # is maintained independently by EdgeSourcePool.

    def failure(self, endpoint: str, ip: str, error: Exception):
        item = self._pair(endpoint, ip)
        item.bump("failures")
        item.active = max(0, item.active - 1)
        status = getattr(error, "status", None)
        error_type = type(error).__name__
        if status == 429:
            item.bump("http_429")
            # SourcePool enforces a shared cooldown on the public egress IP.
        elif status == 403:
            item.bump("http_403")
            item.blocked_until = time.monotonic() + 60
        elif error_type == "NoAudioReceived":
            item.bump("no_audio")
            item.blocked_until = time.monotonic() + 60
        elif isinstance(error, TimeoutError):
            item.bump("timeouts")

    def cancel(self, endpoint: str, ip: str):
        item = self._pair(endpoint, ip)
        item.bump("cancellations")
        item.active = max(0, item.active - 1)

    def snapshot(self):
        return {
            "enabled": list(settings.enabled),
            "mode": "automatico" if len(settings.enabled) == 2 else "fixo",
            "load_error": settings.load_error,
            "pairs": {f"{endpoint}|{ip}": stats.snapshot()
                      for (endpoint, ip), stats in self._pairs.items()},
        }


telemetry = EdgeEndpointTelemetry()
