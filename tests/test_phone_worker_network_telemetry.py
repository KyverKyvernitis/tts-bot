"""Network telemetry contracts with controlled clocks, commands and connections."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

PHONE = Path(__file__).resolve().parents[1] / "deploy/termux/phone-worker/phone_worker.py"


@pytest.fixture
def network(monkeypatch):
    spec = importlib.util.spec_from_file_location("phone_network_telemetry_test", PHONE)
    worker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(worker)
    clock = SimpleNamespace(now=10.0, perf=20.0, wall=100.0)
    worker.time = SimpleNamespace(monotonic=lambda: clock.now, perf_counter=lambda: clock.perf, time=lambda: clock.wall)
    worker.shutil = SimpleNamespace(which=lambda name: None)
    monkeypatch.setattr(worker, "_core_worker_auth_parts", lambda: ("https://192.0.2.1:9443/base", "private-token", "phone-id"))
    calls = []

    class Connection:
        def __enter__(self):
            clock.perf += 0.020
            return self

        def __exit__(self, *args):
            calls.append("closed")

    def connect(address, *, timeout):
        calls.append((address, timeout))
        return Connection()

    worker.socket = SimpleNamespace(create_connection=connect)
    monkeypatch.setattr(worker, "_run_json_command", lambda *a, **kw: {})
    monkeypatch.setattr(worker, "_heartbeat_configured", lambda: False)
    monkeypatch.setattr(worker.urllib.request, "urlopen", lambda *a, **kw: pytest.fail("unexpected HTTP probe"))
    return worker, clock, calls


def test_tcp_ping_cache_ttl_result_copies_and_connection_cleanup(network, monkeypatch):
    worker, clock, calls = network
    first = worker._vps_tcp_ping_snapshot()
    assert first == {"available": True, "source": "tcp_connect", "host_masked": "192.0.x.x", "port": 9443,
                     "reachable": True, "ping_ms": 20.0, "latency_ms": 20.0, "vps_ping_ms": 20.0}
    first["ping_ms"] = 999
    clock.now += 6.0
    cached = worker._vps_tcp_ping_snapshot()
    assert cached["cached"] and cached["ping_ms"] == 20.0
    cached["reachable"] = False
    assert worker._vps_tcp_ping_snapshot()["reachable"]
    assert calls == [(("192.0.2.1", 9443), 2.5), "closed"]
    clock.now += 0.001
    assert "cached" not in worker._vps_tcp_ping_snapshot()
    assert len(calls) == 4
    replacement = {}
    monkeypatch.setattr(worker, "_PING_CACHE", replacement)
    assert "cached" not in worker._vps_tcp_ping_snapshot()
    assert set(replacement) == {"192.0.2.1:9443"} and len(calls) == 6


def test_ping_cache_key_tracks_live_auth_host_and_port(network, monkeypatch):
    worker, _, calls = network
    worker._vps_tcp_ping_snapshot()
    monkeypatch.setattr(worker, "_core_worker_auth_parts", lambda: ("https://192.0.2.1:9443/new-path", "new-token", "new-id"))
    assert worker._vps_tcp_ping_snapshot()["cached"]
    monkeypatch.setattr(worker, "_core_worker_auth_parts", lambda: ("http://198.51.100.1", "", ""))
    assert worker._vps_tcp_ping_snapshot()["port"] == 80
    assert calls[-2] == (("198.51.100.1", 80), 2.5)


@pytest.mark.parametrize(("url", "source"), [("", "not_configured"), ("https://host:bad", "invalid_url"), ("relative", "missing_host")])
def test_invalid_or_absent_ping_url_does_not_connect(network, monkeypatch, url, source):
    worker, _, calls = network
    monkeypatch.setattr(worker, "_core_worker_auth_parts", lambda: (url, "", ""))
    result = worker._vps_tcp_ping_snapshot()
    assert not result["available"] and not result["reachable"] and result["source"] == source
    assert calls == [] and worker._PING_CACHE == {}


@pytest.mark.parametrize(("timeout", "expected"), [(0, 0.3), (0.2, 0.3), (2.5, 2.5)])
def test_failed_ping_is_cached_with_existing_timeout_and_ttl_floors(network, monkeypatch, timeout, expected):
    worker, clock, calls = network

    def fail(address, *, timeout):
        calls.append((address, timeout))
        raise TimeoutError("controlled timeout")

    monkeypatch.setattr(worker.socket, "create_connection", fail)
    result = worker._vps_tcp_ping_snapshot(timeout=timeout, cache_ttl=0)
    assert result["available"] and not result["reachable"] and "TimeoutError" in result["error"]
    assert "ping_ms" not in result and calls[0][1] == expected
    clock.now += 0.5
    assert worker._vps_tcp_ping_snapshot(timeout=timeout, cache_ttl=0)["cached"]
    assert len(calls) == 1
    clock.now += 0.001
    assert "cached" not in worker._vps_tcp_ping_snapshot(timeout=timeout, cache_ttl=0)
    assert len(calls) == 2


@pytest.mark.parametrize(("host", "connected", "state"), [("192.0.2.1", False, "no-cli"),
                                                          ("100.64.1.2", True, "app/vpn"), ("device.ts.net", True, "app/vpn")])
def test_tailscale_without_cli_keeps_existing_endpoint_inference_without_http(network, monkeypatch, host, connected, state):
    worker, _, calls = network
    monkeypatch.setattr(worker, "_core_worker_auth_parts", lambda: ("https://" + host, "", ""))
    result = worker._tailscale_snapshot()
    assert result["connected"] is connected and result["state"] == state
    assert result["via_vps_url"] is connected and not result["cli_available"]
    assert "vps_reachable" not in result and calls == []


def test_tailscale_cli_limits_normalization_and_masking(network, monkeypatch):
    worker, _, _ = network
    worker.shutil.which = lambda name: "/fixture/tailscale"
    commands = []

    def command(argv, **kwargs):
        commands.append((argv, kwargs))
        if argv[1] == "ip":
            return 0, "100.64.12.34\n100.64.56.78", ""
        return 0, json.dumps({"BackendState": "Running", "Self": {"HostName": "phone", "Online": True}, "Peer": {"a": {}, "b": {}}}), ""

    monkeypatch.setattr(worker, "_run_text_command", command)
    result = worker._tailscale_snapshot()
    assert result["connected"] and result["online"] and result["state"] == "Running"
    assert result["ip_masked"] == "100.64.x.x" and result["peers"] == 2 and result["hostname"] == "phone"
    assert commands == [(["tailscale", "ip", "-4"], {"timeout": 2.5, "max_bytes": 4096}),
                        (["tailscale", "status", "--json"], {"timeout": 3.5, "max_bytes": 65536})]
    assert "private-token" not in json.dumps(result)


@pytest.mark.parametrize("body", ['{"ok":false}', "invalid-json"])
def test_explicit_health_probe_preserves_http_bounds_and_response_cleanup(network, monkeypatch, body):
    worker, clock, _ = network
    calls = []

    class Response:
        status = 200

        def __enter__(self):
            return self

        def read(self, size):
            calls.append(size)
            clock.wall += 0.050
            return body.encode()

        def __exit__(self, *args):
            calls.append("closed")

    def open_url(request, *, timeout):
        assert request.full_url == "https://192.0.2.1:9443/base/health"
        assert request.get_method() == "GET" and timeout == 4.0
        assert request.headers == {"Accept": "application/json"}
        return Response()

    monkeypatch.setattr(worker.urllib.request, "urlopen", open_url)
    result = worker._tailscale_snapshot(probe_vps=True)
    assert result["vps_reachable"] and result["vps_status"] == 200 and result["vps_latency_ms"] == 50.0
    assert result.get("vps_health_ok") is (False if body.startswith("{") else None)
    assert calls == [4096, "closed"]


@pytest.mark.parametrize(("wifi", "kind", "source"), [({}, "unknown", "inferred"),
    ({"error": "permission denied"}, "unknown", "inferred"),
    ({"ssid": "<unknown ssid>", "rssi": "bad"}, "wifi", "termux-api"),
    ({"ssid": "local wifi", "rssi": "-55"}, "wifi", "termux-api")])
def test_network_composes_partial_sources_without_explicit_http_probe(network, monkeypatch, wifi, kind, source):
    worker, _, _ = network
    calls = []

    def json_command(argv, **kwargs):
        calls.append((argv, kwargs))
        return wifi

    monkeypatch.setattr(worker, "_run_json_command", json_command)
    result = worker._network_snapshot()
    assert result["type"] == kind and result["source"] == source
    assert result["vps_ping_ms"] == result["ping_ms"] == 20.0 and result["vps_reachable"]
    assert calls == [(["termux-wifi-connectioninfo"], {"timeout": 2.0})]
    if wifi.get("ssid") == "local wifi":
        assert result["name"] == "local wifi" and result["rssi"] == -55
    else:
        assert "name" not in result and "rssi" not in result


def test_network_uses_replaced_facade_dependencies_and_preserves_partial_failures(network, monkeypatch):
    worker, _, _ = network
    worker._network_snapshot()
    monkeypatch.setattr(worker, "_heartbeat_configured", lambda: True)
    monkeypatch.setattr(worker, "_tailscale_snapshot", lambda **kw: {"connected": True, "state": "late", "cli_available": True})

    def broken_ping():
        raise OSError("probe unavailable")

    monkeypatch.setattr(worker, "_vps_tcp_ping_snapshot", broken_ping)
    result = worker._network_snapshot()
    assert result["type"] == "connected" and result["tailscale_state"] == "late" and result["tailscale_cli"]
    assert not result["vps_reachable"] and not result["vps_ping_available"]
    assert "probe unavailable" in result["vps_ping_error"]
