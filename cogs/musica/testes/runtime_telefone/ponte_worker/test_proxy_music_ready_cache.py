"""Reuso curto da checagem de saúde antes do comando musical."""
from __future__ import annotations

import json
from types import SimpleNamespace

from cogs.musica.runtime_telefone.ponte_worker import proxy


class Response:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _limit):
        return json.dumps({"ok": True, "available": True}).encode()


def setup(monkeypatch):
    monkeypatch.setenv("MUSIC_AGENT_TOKEN", "test-token")
    monkeypatch.setenv("MUSIC_AGENT_BOT_TOKEN", "test-bot-token")
    monkeypatch.setenv("MUSIC_AGENT_COMMAND_READY_TTL_SECONDS", "8")
    monkeypatch.setattr(proxy, "_READY_CACHE_KEY", None)
    monkeypatch.setattr(proxy, "_READY_CACHE_UNTIL", 0.0)
    now = [100.0]
    monkeypatch.setattr(proxy.time, "monotonic", lambda: now[0])
    snapshots = []
    services = []
    versions = ["1"]

    def snapshot():
        snapshots.append(now[0])
        return {"available": True, "runtime_version": versions[0], "file_version": "1"}

    hooks = SimpleNamespace(
        load_runtime_env=lambda: None,
        ensure_token=lambda **_kwargs: "unused",
        safe_telemetry=lambda _name, callback, _default: callback(),
        snapshot=snapshot,
        version_lt=lambda running, installed: running < installed,
        run_service=lambda _name, action: services.append(action) or {"ok": True},
        truthy=lambda value, default: bool(value) if value is not None else default,
        short_text=lambda value, **_kwargs: str(value),
    )
    return hooks, now, snapshots, services, versions


def play(hooks):
    return proxy.proxy_music_agent({"action": "play", "guild_id": 123, "query": "teste"}, max_output_bytes=4096, hooks=hooks)


def test_second_command_skips_snapshot_but_expiry_rechecks_version(monkeypatch):
    hooks, now, snapshots, services, versions = setup(monkeypatch)
    calls = []

    def urlopen(request, **_kwargs):
        calls.append(request.full_url)
        return Response()

    monkeypatch.setattr(proxy.urllib.request, "urlopen", urlopen)
    assert play(hooks)["ok"]
    now[0] += 1
    assert play(hooks)["ok"]
    assert len(snapshots) == 1 and len(calls) == 2 and not services
    now[0] += 8
    versions[0] = "0"
    assert play(hooks)["ok"]
    assert len(snapshots) == 2 and services == ["restart"]


def test_command_connection_failure_invalidates_cache_and_retries_start(monkeypatch):
    hooks, now, snapshots, services, _versions = setup(monkeypatch)
    calls = [0]

    def urlopen(_request, **_kwargs):
        calls[0] += 1
        if calls[0] == 2:
            raise OSError("connection refused")
        return Response()

    monkeypatch.setattr(proxy.urllib.request, "urlopen", urlopen)
    assert play(hooks)["ok"]
    now[0] += 1
    assert play(hooks)["ok"]  # 2ª tentativa recupera o Agent pelo caminho antigo.
    assert services == ["start"] and len(snapshots) == 1 and calls[0] == 3
    assert play(hooks)["ok"]
    assert len(snapshots) == 2 and calls[0] == 4


def test_zero_ttl_keeps_full_health_check_on_every_command(monkeypatch):
    hooks, _now, snapshots, _services, _versions = setup(monkeypatch)
    monkeypatch.setenv("MUSIC_AGENT_COMMAND_READY_TTL_SECONDS", "0")
    monkeypatch.setattr(proxy.urllib.request, "urlopen", lambda _request, **_kwargs: Response())
    assert play(hooks)["ok"] and play(hooks)["ok"]
    assert len(snapshots) == 2
