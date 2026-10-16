"""Independent Edge egress sources and source-bound connector ownership.

Only synthesis transports use these connectors. Discord, music and the phone
worker keep their existing HTTP clients and operating-system routes.
"""
from __future__ import annotations

import ipaddress
import hashlib
import re
import socket
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass

import aiohttp

from .edge_rate_limit import EdgeRateLimitPolicy


class EdgeSourceUnavailable(RuntimeError):
    """A queued reservation lost eligibility before any socket was opened."""


def parse_source_ips(raw) -> tuple[str, ...]:
    """Parse explicit local IPv4 addresses; never fall back to an unbound socket."""
    parts = re.split(r"[,;\s]+", raw.strip()) if isinstance(raw, str) else list(raw or ())
    result = []
    for part in parts:
        if not str(part).strip():
            continue
        address = ipaddress.ip_address(str(part).strip())
        if address.version != 4 or address.is_unspecified or address.is_multicast or address.is_loopback:
            raise ValueError("TTS_EDGE_SOURCE_IPS requer endereços IPv4 locais explícitos")
        value = str(address)
        if value not in result:
            result.append(value)
    if len(result) > 16:
        raise ValueError("TTS_EDGE_SOURCE_IPS aceita no máximo 16 endereços")
    return tuple(result)


@dataclass
class SourceLease:
    source_ip: str
    probe: bool = False
    started: bool = False
    finished: bool = False
    started_failure_count: int = 0


@dataclass
class SourceState:
    policy: EdgeRateLimitPolicy
    active_probe: SourceLease | None = None
    consecutive_failures: int = 0
    failed_at: float = 0.0
    attempts: int = 0
    successes: int = 0
    failures: int = 0
    rate_limits: int = 0
    cancellations: int = 0
    last_synth_ms: float = 0.0
    total_synth_ms: float = 0.0
    last_error_type: str = ""


class EdgeSourcePool:
    def __init__(self, ips, *, first_delay=90, circuit_failures=3,
                 circuit_cooldown=15, circuit_enabled=True):
        self.ips = parse_source_ips(ips)
        self.states = {ip: SourceState(EdgeRateLimitPolicy(first_delay=first_delay)) for ip in self.ips}
        self.circuit_failures = int(circuit_failures)
        self.circuit_cooldown = float(circuit_cooldown)
        self.circuit_enabled = bool(circuit_enabled)
        self.config_error = ""

    def _circuit_open(self, state, now):
        return bool(self.circuit_enabled and state.consecutive_failures >= self.circuit_failures
                    and now - state.failed_at < self.circuit_cooldown)

    def available(self, ip, *, now=None):
        now = time.monotonic() if now is None else now
        state = self.states[ip]
        return not (self._circuit_open(state, now) or state.policy.probing
                    or state.policy.level and now < state.policy.retry_at)

    def any_available(self, *, now=None):
        return any(self.available(ip, now=now) for ip in self.ips)

    def reserve(self, guild_id, exclude=(), *, now=None):
        now = time.monotonic() if now is None else now
        if not self.ips:
            return None
        # Discord Snowflakes often have identical low sequence bits; hash the
        # full identity so two IPs do not receive all even-numbered guilds on one.
        digest = hashlib.sha256(str(int(guild_id or 0)).encode("ascii")).digest()
        offset = int.from_bytes(digest[:8], "big") % len(self.ips)
        ordered = self.ips[offset:] + self.ips[:offset]
        for ip in ordered:
            if ip in exclude or not self.available(ip, now=now):
                continue
            state = self.states[ip]
            if state.policy.should_bypass(now=now):
                continue
            lease = SourceLease(ip, probe=bool(state.policy.probing))
            if lease.probe:
                state.active_probe = lease
            return lease
        return None

    def start(self, lease):
        if lease.finished or lease.started:
            return
        state = self.states[lease.source_ip]
        own_probe = lease.probe and state.active_probe is lease and state.policy.probing
        if not own_probe and not self.available(lease.source_ip):
            self.release(lease)
            raise EdgeSourceUnavailable("origem Edge entrou em cooldown enquanto aguardava vaga")
        lease.started = True
        lease.started_failure_count = state.failures
        state.attempts += 1

    def success(self, lease, duration_ms=0.0):
        if lease.finished:
            return
        state = self.states[lease.source_ip]
        current_result = state.failures == lease.started_failure_count
        if state.active_probe is lease:
            if current_result:
                state.policy.success()
            else:
                state.policy.failed_other()
            state.active_probe = None
        if current_result:
            state.consecutive_failures = 0
            state.last_error_type = ""
        state.successes += 1
        state.last_synth_ms = float(duration_ms)
        state.total_synth_ms += float(duration_ms)
        lease.finished = True

    def failure(self, lease, error, duration_ms=0.0):
        if lease.finished:
            return None
        if isinstance(error, EdgeSourceUnavailable):
            self.release(lease)
            return None
        state = self.states[lease.source_ip]
        state.failures += 1
        state.consecutive_failures += 1
        state.failed_at = time.monotonic()
        state.last_synth_ms = float(duration_ms or 0.0)
        state.last_error_type = type(error).__name__
        result = None
        if getattr(error, "status", None) == 429:
            state.rate_limits += 1
            result = state.policy.failed_429()
        elif state.active_probe is lease:
            state.policy.failed_other()
        if state.active_probe is lease:
            state.active_probe = None
        lease.finished = True
        return result

    def release(self, lease):
        if lease is None or lease.finished:
            return
        state = self.states[lease.source_ip]
        if state.active_probe is lease:
            state.policy.failed_other()
            state.active_probe = None
        state.cancellations += int(lease.started)
        lease.finished = True

    def snapshot(self):
        return {ip: {
            "attempts": state.attempts, "successes": state.successes,
            "failures": state.failures, "http_429": state.rate_limits,
            "cancellations": state.cancellations,
            "rate_limit_level": state.policy.level,
            "rate_limit_remaining_seconds": state.policy.remaining_seconds(),
            "probing": state.policy.probing,
            "circuit_open": self._circuit_open(state, time.monotonic()),
            "last_synth_ms": round(state.last_synth_ms, 2),
            "avg_synth_ms": round(state.total_synth_ms / state.successes, 2) if state.successes else 0.0,
            "last_error_type": state.last_error_type,
        } for ip, state in self.states.items()}


class SourceBoundConnector(aiohttp.TCPConnector):
    """Lend the connector to edge-tts sessions until its real owner finishes.

    edge-tts 7.3.1 closes each ClientSession with connector_owner=True, then
    reuses its connector for a 403 retry or the next 4096-byte text block.
    Suppressing those borrowed closes preserves retries without a global patch.
    The context manager below always calls the base close explicitly.
    """
    async def close(self, *args, **kwargs):
        return None

    async def _create_proxy_connection(self, *args, **kwargs):
        # edge-tts enables trust_env internally. Refuse only an actually
        # selected proxy instead of mutating process-wide environment/session
        # settings used by other engines. A proxy's public IP is not this source.
        raise RuntimeError("proxy de ambiente incompatível com a origem Edge local explícita")


@asynccontextmanager
async def bound_communicate(factory, source_ip, **kwargs):
    connector = SourceBoundConnector(local_addr=(source_ip, 0), family=socket.AF_INET)
    try:
        yield factory(connector=connector, **kwargs)
    finally:
        await aiohttp.TCPConnector.close(connector)
