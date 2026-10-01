"""Comandos diretos, reparo excepcional e keep-alive do proxy musical."""
from __future__ import annotations

import http.server
import io
import json
from pathlib import Path
from types import SimpleNamespace
import threading
import urllib.error

import pytest

from cogs.musica.runtime_telefone.ponte_worker import proxy


@pytest.fixture
def setup(monkeypatch):
    monkeypatch.setenv("MUSIC_AGENT_TOKEN", "test-token")
    monkeypatch.setenv("MUSIC_AGENT_BOT_TOKEN", "test-bot-token")
    monkeypatch.setenv("MUSIC_AGENT_COMMAND_READY_TTL_SECONDS", "8")
    monkeypatch.setattr(proxy, "_READY_RUNTIME_VERSION", {})
    monkeypatch.setattr(proxy, "_installed_version", lambda: "0.3.83")
    now = [100.0]
    monkeypatch.setattr(proxy.time, "monotonic", lambda: now[0])
    snapshots, services, requests = [], [], []
    health = {"available": True, "runtime_version": "0.3.83", "file_version": "0.3.83"}

    def snapshot():
        snapshots.append(now[0])
        return dict(health)

    def request(host, port, path, **kwargs):
        requests.append((host, port, path, kwargs))
        return {"ok": True}, {"x-music-agent-version": "0.3.83", "x-music-agent-discord-ready": "1"}

    monkeypatch.setattr(proxy, "_pooled_request", request)
    hooks = SimpleNamespace(
        load_runtime_env=lambda: None,
        ensure_token=lambda **_kwargs: "unused",
        safe_telemetry=lambda _name, callback, _default: callback(),
        snapshot=snapshot,
        version_lt=lambda running, installed: tuple(map(int, running.split("."))) < tuple(map(int, installed.split("."))),
        run_service=lambda _name, action: services.append(action) or {"ok": True},
        truthy=lambda value, default: bool(value) if value is not None else default,
        short_text=lambda value, **_kwargs: str(value),
    )
    return hooks, now, snapshots, services, requests, health


def play(hooks, **fields):
    return proxy.proxy_music_agent({"action": "play", "guild_id": 123, "query": "teste", **fields}, max_output_bytes=4096, hooks=hooks)


def test_even_first_command_and_expired_ttl_do_not_preflight_health(setup):
    hooks, now, snapshots, services, requests, _health = setup
    assert play(hooks)["ok"]
    now[0] += 60
    result = play(hooks)
    assert result["ok"] and result["available"]
    assert not snapshots and not services and len(requests) == 2
    assert all(request[2] == "/command" for request in requests)
    assert result["proxy_timing_ms"]["request_count"] == 1
    assert result["proxy_timing_ms"]["repair"] == 0


def test_installed_version_change_restarts_before_next_mutation(setup, monkeypatch):
    hooks, _now, snapshots, services, requests, health = setup
    assert play(hooks)["ok"]
    monkeypatch.setattr(proxy, "_installed_version", lambda: "0.3.84")
    health["file_version"] = "0.3.84"
    events = []
    hooks.run_service = lambda _name, action: events.append(action) or {"ok": True}
    original = proxy._pooled_request
    monkeypatch.setattr(proxy, "_pooled_request", lambda *args, **kwargs: events.append("post") or original(*args, **kwargs))
    assert play(hooks)["ok"]
    assert events == ["restart", "post"] and len(snapshots) == 1
    assert len(requests) == 2


def test_accepted_old_command_is_not_replayed_or_restarted_after_success(setup, monkeypatch):
    hooks, _now, snapshots, services, _requests, _health = setup
    monkeypatch.setattr(proxy, "_pooled_request", lambda *_args, **_kwargs: ({"ok": True}, {"x-music-agent-version": "0.3.82"}))
    assert play(hooks)["ok"]
    assert not snapshots and not services
    assert list(proxy._READY_RUNTIME_VERSION.values()) == ["0.3.82"]


def test_lost_response_retries_same_id_on_reachable_agent_without_restart(setup, monkeypatch):
    hooks, _now, snapshots, services, requests, _health = setup
    original = proxy._pooled_request
    calls = []

    def request(*args, **kwargs):
        calls.append(json.loads(kwargs["body"]))
        if len(calls) == 1:
            raise OSError("response lost after accepted POST")
        return original(*args, **kwargs)

    monkeypatch.setattr(proxy, "_pooled_request", request)
    result = play(hooks, command_id="retained-command")
    assert result["ok"] and not services and len(snapshots) == 1
    assert calls[0] == calls[1] and calls[0]["command_id"] == "retained-command"
    assert result["prepare"]["action"] == "reuse"
    assert result["proxy_timing_ms"]["request_count"] == 2


@pytest.mark.parametrize("failure", [BrokenPipeError(32, "Broken pipe"), ConnectionResetError("reset")])
def test_closed_keepalive_retries_same_id_without_heavy_audit_or_voice_restart(setup, monkeypatch, failure):
    hooks, _now, snapshots, services, _requests, _health = setup
    original = proxy._pooled_request
    attempts = []
    def request(*args, **kwargs):
        attempts.append(json.loads(kwargs["body"]))
        if len(attempts) == 1:
            raise failure
        return original(*args, **kwargs)
    monkeypatch.setattr(proxy, "_pooled_request", request)
    result = play(hooks, command_id="retain-on-epipe")
    assert result["ok"] and len(attempts) == 2
    assert attempts[0] == attempts[1]
    assert not snapshots and not services
    assert result["proxy_timing_ms"]["repair"] == 0


def test_closed_keepalive_status_retries_without_restart(setup, monkeypatch):
    hooks, _now, snapshots, services, _requests, _health = setup
    original = proxy._pooled_request
    attempts = []
    def request(*args, **kwargs):
        attempts.append(1)
        if len(attempts) == 1:
            raise BrokenPipeError(32, "Broken pipe")
        return original(*args, **kwargs)
    monkeypatch.setattr(proxy, "_pooled_request", request)
    result = proxy.proxy_music_agent({"action": "status"}, max_output_bytes=4096, hooks=hooks)
    assert result["ok"] and len(attempts) == 2
    assert not services and not snapshots


def test_missing_agent_starts_once_and_generates_one_id_for_both_attempts(setup, monkeypatch):
    hooks, _now, snapshots, services, _requests, health = setup
    health["available"] = False
    calls = []

    def request(*_args, **kwargs):
        calls.append(json.loads(kwargs["body"]))
        if len(calls) == 1:
            raise ConnectionRefusedError()
        return {"ok": True}, {}

    monkeypatch.setattr(proxy, "_pooled_request", request)
    assert play(hooks)["ok"]
    assert services == ["start"] and len(snapshots) == 1
    assert calls[0] == calls[1] and len(calls[0]["command_id"]) == 32


@pytest.mark.parametrize("command_id", [None, "", "  "])
def test_empty_legacy_id_is_replaced_before_sending(setup, command_id):
    hooks, _now, _snapshots, _services, requests, _health = setup
    assert play(hooks, command_id=command_id)["ok"]
    encoded = json.loads(requests[0][3]["body"])
    assert len(encoded["command_id"]) == 32


def test_failed_retry_does_not_loop_services(setup, monkeypatch):
    hooks, _now, snapshots, services, _requests, health = setup
    health["available"] = False
    calls = []

    def request(*_args, **_kwargs):
        calls.append(1)
        raise ConnectionRefusedError("still stopped")

    monkeypatch.setattr(proxy, "_pooled_request", request)
    result = play(hooks)
    assert not result["ok"] and len(calls) == 2
    assert services == ["start"] and len(snapshots) == 1


def http_error(code, payload=None, headers=None):
    payload = payload or {"ok": False, "error": "unauthorized"}
    error = urllib.error.HTTPError("http://127.0.0.1/command", code, "failure", {}, io.BytesIO(json.dumps(payload).encode()))
    error.agent_payload = payload
    error.agent_headers = headers or {}
    return error


@pytest.mark.parametrize("code", [400, 401, 403, 404, 503])
def test_http_errors_do_not_restart_for_auth_or_user_errors(setup, monkeypatch, code):
    hooks, _now, snapshots, services, _requests, _health = setup

    def request(*_args, **_kwargs):
        raise http_error(code)

    monkeypatch.setattr(proxy, "_pooled_request", request)
    result = play(hooks)
    assert not result["ok"] and f"HTTP {code}" in result["error"]
    assert not services and not snapshots


def test_legacy_prepare_voice_repairs_only_unsupported_stale_action(setup, monkeypatch):
    hooks, _now, snapshots, services, _requests, health = setup
    health["runtime_version"] = "0.3.82"
    calls = []

    def request(*_args, **kwargs):
        calls.append(json.loads(kwargs["body"]))
        if len(calls) == 1:
            raise http_error(400, {"error": "ValueError: ação do Music Agent não suportada", "status": {"version": "0.3.82", "discord_ready": True}})
        return {"ok": True, "prepared": True}, {"x-music-agent-version": "0.3.83"}

    monkeypatch.setattr(proxy, "_pooled_request", request)
    result = proxy.proxy_music_agent({"action": "prepare_voice", "guild_id": 123, "voice_channel_id": 456}, max_output_bytes=4096, hooks=hooks)
    assert result["ok"] and services == ["restart"] and len(snapshots) == 1
    assert calls[0] == calls[1]


def test_old_agent_generic_failed_mutation_is_not_replayed(setup, monkeypatch):
    hooks, _now, snapshots, services, _requests, _health = setup

    def request(*_args, **_kwargs):
        raise http_error(400, {"error": "playback failed after changing state", "status": {"version": "0.3.82"}})

    monkeypatch.setattr(proxy, "_pooled_request", request)
    assert not play(hooks)["ok"]
    assert not snapshots and not services


@pytest.mark.parametrize("metadata", [{"x-music-agent-discord-ready": "0"}, {}])
def test_explicit_discord_readiness_is_preserved_even_when_ok_true(setup, monkeypatch, metadata):
    hooks, *_rest = setup
    monkeypatch.setattr(proxy, "_pooled_request", lambda *_args, **_kwargs: ({"ok": True, "discord_ready": False}, metadata))
    result = play(hooks)
    assert result["ok"] and not result["available"]


def test_zero_ttl_preserves_explicit_full_audit_mode(setup, monkeypatch):
    hooks, _now, snapshots, services, _requests, _health = setup
    monkeypatch.setenv("MUSIC_AGENT_COMMAND_READY_TTL_SECONDS", "0")
    assert play(hooks)["ok"] and play(hooks)["ok"]
    assert len(snapshots) == 2 and not services


def test_status_forwards_conditional_compact_query_without_preflight(setup):
    hooks, _now, snapshots, services, requests, _health = setup
    result = proxy.proxy_music_agent({"action": "get_state", "guild_id": 123, "known_revision": "v2 a"}, max_output_bytes=4096, hooks=hooks)
    assert result["ok"] and not snapshots and not services
    assert requests[0][2] == "/health?guild_id=123&compact=1&known_revision=v2+a"
    assert requests[0][3]["method"] == "GET" and requests[0][3]["body"] is None


def test_unconfigured_bot_never_sends_command(setup, monkeypatch):
    hooks, _now, snapshots, services, requests, _health = setup
    for name in ("MUSIC_AGENT_BOT_TOKEN", "DISCORD_TOKEN", "BOT_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    assert not play(hooks)["ok"]
    assert not snapshots and not services and not requests


def test_version_read_reuses_fingerprint_and_changes_after_atomic_update(tmp_path, monkeypatch):
    module = tmp_path / "runtime_telefone/ponte_worker/proxy.py"
    module.parent.mkdir(parents=True)
    server = module.parent.parent / "agente/servidor.py"
    server.parent.mkdir()
    server.write_text('AGENT_VERSION = "0.3.83"\n')
    monkeypatch.setattr(proxy, "__file__", str(module))
    monkeypatch.setattr(proxy, "_VERSION_CACHE", None)
    assert proxy._installed_version() == "0.3.83"
    original = Path.read_text
    reads = []
    monkeypatch.setattr(Path, "read_text", lambda self, **kwargs: reads.append(self) or original(self, **kwargs))
    assert proxy._installed_version() == "0.3.83" and not reads
    replacement = server.with_suffix(".next")
    replacement.write_text('AGENT_VERSION = "0.3.84"\n')
    replacement.replace(server)
    assert proxy._installed_version() == "0.3.84" and len(reads) == 1


def test_real_http_keepalive_reuses_authenticated_socket_and_does_not_replay(monkeypatch):
    accepted = []

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self):
            accepted.append((self.client_address, self.headers.get("Authorization"), json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
            raw = b'{"ok":true}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("X-Music-Agent-Version", "0.3.83")
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *_args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(proxy, "_HTTP_POOL", {})
    try:
        for index in range(2):
            data, headers = proxy._pooled_request("127.0.0.1", server.server_port, "/command", method="POST", headers={"Authorization": "Bearer unit-token", "Content-Type": "application/json"}, body=json.dumps({"command_id": str(index)}).encode(), timeout=2, max_bytes=4096)
            assert data["ok"] and headers["x-music-agent-version"] == "0.3.83"
        assert len(accepted) == 2 and accepted[0][0] == accepted[1][0]
        assert [item[1] for item in accepted] == ["Bearer unit-token", "Bearer unit-token"]
        assert [item[2]["command_id"] for item in accepted] == ["0", "1"]
    finally:
        for idle in proxy._HTTP_POOL.values():
            for _expires, connection in idle:
                connection.close()
        proxy._HTTP_POOL.clear()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
