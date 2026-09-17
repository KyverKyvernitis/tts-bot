"""Bounded in-memory HTTP responses; no sockets, remote jobs or private credentials."""
import importlib.util
import io
import json
from pathlib import Path
import urllib.error

import pytest

PHONE = Path(__file__).resolve().parents[1] / "deploy/termux/phone-worker/phone_worker.py"
CLIENTS = ["_get_json_url", "_post_json_url", "_get_local_json_url", "_post_local_json_url", "_download_url_to_file"]


class Reply(io.BytesIO):
    status = 202

    def __init__(self, data, *, fail=False):
        super().__init__(data)
        self.fail = fail
        self.read_sizes = []

    def read(self, size=-1):
        self.read_sizes.append(size)
        if self.fail:
            raise OSError("interrupted body")
        return super().read(size)


@pytest.fixture
def client(monkeypatch):
    spec = importlib.util.spec_from_file_location("phone_http_test", PHONE)
    worker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(worker)
    events = []
    monkeypatch.setattr(worker, "_remember_core_worker_network_ok", lambda: events.append("ok"))
    monkeypatch.setattr(worker, "_remember_core_worker_network_error", lambda exc: events.append(type(exc).__name__))
    return worker, events


def invoke(worker, name, tmp_path, **kwargs):
    url = "https://vps.invalid/control"
    if name == "_download_url_to_file":
        return worker._download_url_to_file(url, tmp_path / "download.bin", **kwargs)
    if "post" in name:
        return getattr(worker, name)(url, {"name": "ação"}, **kwargs)
    return getattr(worker, name)(url, **kwargs)


@pytest.mark.parametrize("name", CLIENTS)
def test_success_closes_response_preserves_request_and_network_scope(client, monkeypatch, tmp_path, name):
    worker, events = client
    reply = Reply(b'{"ok":true}')
    requests = []
    def open_request(req, *, timeout):
        requests.append((req, timeout))
        return reply
    monkeypatch.setattr(worker.urllib.request, "urlopen", open_request)
    result = invoke(worker, name, tmp_path, timeout=0)
    req, timeout = requests[0]
    assert timeout == 1.0 and req.get_method() == ("POST" if "post" in name else "GET")
    if "post" in name:
        assert req.data == '{"name":"ação"}'.encode()
        assert req.get_header("Content-type") == "application/json; charset=utf-8"
    if name != "_download_url_to_file":
        assert result == (202, {"ok": True}) and req.get_header("Accept") == "application/json"
    else:
        assert result["ok"] and result["bytes"] == len(b'{"ok":true}')
        assert (tmp_path / "download.bin").read_bytes() == b'{"ok":true}'
    assert reply.closed and events == ([] if "local" in name else ["ok"])


@pytest.mark.parametrize("name", CLIENTS)
@pytest.mark.parametrize("read_failure", [False, True])
def test_http_error_closes_body_even_on_read_failure(client, monkeypatch, tmp_path, name, read_failure):
    worker, events = client
    body = Reply(b'{"ok":false,"error":"rejected"}', fail=read_failure)
    error = urllib.error.HTTPError("https://vps.invalid/control", 403, "forbidden", {}, body)
    def fail(*a, **kw):
        raise error
    monkeypatch.setattr(worker.urllib.request, "urlopen", fail)
    target = tmp_path / "download.bin"
    target.write_bytes(b"previous valid download")
    target.with_suffix(".bin.tmp").write_bytes(b"incomplete")
    try:
        if read_failure:
            with pytest.raises(OSError, match="interrupted body"):
                invoke(worker, name, tmp_path)
        else:
            result = invoke(worker, name, tmp_path)
            if name == "_download_url_to_file":
                assert result["status"] == 403 and not result["ok"]
            else:
                assert result == (403, {"ok": False, "error": "rejected"})
        assert body.closed
        assert body.read_sizes == [8192 if name == "_download_url_to_file" else 16384]
        assert events == ([] if "local" in name else ["OSError" if read_failure else "ok"])
        assert target.read_bytes() == b"previous valid download"
        if name == "_download_url_to_file":
            assert not target.with_suffix(".bin.tmp").exists()
    finally:
        error.close()


@pytest.mark.parametrize("name", CLIENTS)
def test_network_failure_propagates_without_false_success(client, monkeypatch, tmp_path, name):
    worker, events = client
    failure = urllib.error.URLError("DNS unavailable")
    def fail(*a, **kw):
        raise failure
    monkeypatch.setattr(worker.urllib.request, "urlopen", fail)
    with pytest.raises(urllib.error.URLError) as caught:
        invoke(worker, name, tmp_path)
    assert caught.value is failure and events == ([] if "local" in name else ["URLError"])
    assert not (tmp_path / "download.bin.tmp").exists()


@pytest.mark.parametrize("name", CLIENTS[:4])
@pytest.mark.parametrize(("raw", "expected"), [(b"", {}), (b"[]", "não é objeto"),
    (b"broken", "JSON"), (b'{"message":"\xff"}', {"message": "\ufffd"})])
def test_json_body_contracts(client, monkeypatch, tmp_path, name, raw, expected):
    worker, _ = client
    reply = Reply(raw)
    monkeypatch.setattr(worker.urllib.request, "urlopen", lambda *a, **kw: reply)
    status, result = invoke(worker, name, tmp_path)
    assert status == 202 and reply.closed
    if isinstance(expected, dict):
        assert result == expected
    else:
        assert result["ok"] is False and expected in result["error"]


@pytest.mark.parametrize("name", CLIENTS[:4])
def test_success_body_is_bounded_and_oversize_not_parsed(client, monkeypatch, tmp_path, name):
    worker, _ = client
    limit = 1024 * 1024 if name == "_post_json_url" else 32
    kwargs = {} if name == "_post_json_url" else {"max_bytes": limit}
    monkeypatch.setenv("CORE_WORKER_JSON_RESPONSE_MAX_BYTES", "1")
    reply = Reply(b" " * (limit + 20))
    monkeypatch.setattr(worker.urllib.request, "urlopen", lambda *a, **kw: reply)
    status, data = invoke(worker, name, tmp_path, **kwargs)
    assert status == 202 and not data["ok"] and "grande demais" in data["error"]
    assert reply.closed and reply.read_sizes == [limit + 1]


def test_core_client_uses_live_auth_and_skips_network_without_configuration(client, monkeypatch):
    worker, _ = client
    calls = []
    def open_request(req, **kw):
        calls.append(req)
        return Reply(b"{}")
    monkeypatch.setattr(worker.urllib.request, "urlopen", open_request)
    monkeypatch.delenv("CORE_WORKER_VPS_URL", raising=False)
    monkeypatch.delenv("CORE_WORKER_BASE_URL", raising=False)
    monkeypatch.delenv("CORE_WORKER_TOKEN", raising=False)
    assert worker._post_core_worker_json("/heartbeat", {})[0] == 0 and not calls
    monkeypatch.setenv("CORE_WORKER_BASE_URL", " https://vps.invalid/// ")
    for token in ["first-test-token", "second-test-token"]:
        monkeypatch.setenv("CORE_WORKER_TOKEN", token)
        assert worker._post_core_worker_json("/heartbeat", {"ok": True}) == (202, {})
        assert calls[-1].get_header("Authorization") == "Bearer " + token
        assert calls[-1].full_url == "https://vps.invalid/heartbeat"
        assert json.loads(calls[-1].data) == {"ok": True}
