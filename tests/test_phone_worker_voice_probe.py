"""Probe lifecycle with scheduled callbacks and in-memory Voice WS/UDP boundaries."""
import importlib.util
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

PHONE = Path(__file__).resolve().parents[1] / "deploy/termux/phone-worker/phone_worker.py"


@pytest.fixture
def probe(monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location("phone_voice_probe_test", PHONE)
    worker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(worker)
    clock, scheduled = [10000], []
    monkeypatch.setattr(worker, "_voice_agent_now_ms", lambda: clock[0])
    monkeypatch.setenv("PHONE_WORKER_VOICE_AGENT_STATE_FILE", str(tmp_path / "state.json"))
    for key in ["ENABLED", "HANDOFF_ENABLED", "TRANSFER_CONTROL_ENABLED", "CONNECTION_DRY_RUN_ENABLED"]:
        monkeypatch.setenv("PHONE_WORKER_VOICE_AGENT_" + key, "true")
    class Thread:
        def __init__(self, **kwargs):
            self.options = kwargs
        def start(self):
            scheduled.append(self)
        def run(self):
            self.options["target"](**self.options["kwargs"])
    worker.threading = SimpleNamespace(Thread=Thread)
    worker._voice_agent_register_handoff({"guild_id": 1, "channel_id": 2, "bot_user_id": 3,
        "session_id": "private-session", "endpoint": "voice.invalid", "voice_token": "private-voice-token"})
    worker._voice_agent_prepare_transfer({"guild_id": 1, "channel_id": 2})
    worker._voice_agent_begin_transfer({"guild_id": 1, "channel_id": 2, "confirm_transfer": True})
    async def completed(*a, **kw):
        return {"guild_id": "1", "state": "connected_dry_run", "connected_once": True}
    worker._test_probe_coroutine = worker._voice_agent_probe_connection_async
    monkeypatch.setattr(worker, "_voice_agent_probe_connection_async", completed)
    return worker, clock, scheduled


@pytest.mark.parametrize("delay", [0, 49])
def test_just_started_probe_is_not_scheduled_twice(probe, delay):
    worker, clock, scheduled = probe
    assert worker._voice_agent_start_connection_probe({"guild_id": 1})["started"]
    clock[0] += delay
    result = worker._voice_agent_start_connection_probe({"guild_id": 1})
    assert not result["started"] and result["state"] == "connection_probe_already_running"
    assert len(scheduled) == 1


def test_concurrent_requests_schedule_one_probe(probe):
    worker, _, scheduled = probe
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: worker._voice_agent_start_connection_probe({"guild_id": 1}), range(32)))
    assert sum(result["started"] for result in results) == 1 and len(scheduled) == 1


@pytest.mark.parametrize("cancel", ["clear_one", "clear_all", "handoff", "replacement", "release", "expiry"])
def test_cancel_before_dispatch_prevents_work_and_late_success(probe, monkeypatch, cancel):
    worker, clock, scheduled = probe
    calls = []
    async def completed(*a, **kw):
        calls.append("ran")
        return {"guild_id": "1", "state": "connected_dry_run", "connected_once": True}
    monkeypatch.setattr(worker, "_voice_agent_probe_connection_async", completed)
    worker._voice_agent_start_connection_probe({"guild_id": 1})
    if cancel == "clear_one":
        worker._voice_agent_clear_connection({"guild_id": 1})
    elif cancel == "clear_all":
        worker._voice_agent_clear_connection({"all": True})
    elif cancel == "handoff":
        worker._voice_agent_clear_handoff({"guild_id": 1})
    elif cancel == "replacement":
        worker._voice_agent_register_handoff({**worker._VOICE_AGENT_HANDOFF_MEMORY["1"], "voice_token": "new-test-token"})
    elif cancel == "release":
        worker._voice_agent_release_transfer({"guild_id": 1})
    else:
        clock[0] += 46000
    scheduled[0].run()
    assert calls == []
    assert not worker._voice_agent_connection_summary()["connection_ready"]
    if cancel.startswith("clear"):
        assert worker._voice_agent_connection_summary()["connection_count"] == 0


@pytest.mark.parametrize("failure", [False, True])
def test_cleared_connection_is_not_resurrected_by_late_result(probe, monkeypatch, failure):
    worker, _, scheduled = probe
    async def completed(*a, **kw):
        worker._voice_agent_clear_connection({"guild_id": 1})
        if failure:
            raise OSError("late failure")
        return {"guild_id": "1", "state": "connected_dry_run", "connected_once": True}
    monkeypatch.setattr(worker, "_voice_agent_probe_connection_async", completed)
    worker._voice_agent_start_connection_probe({"guild_id": 1})
    scheduled[0].run()
    assert worker._voice_agent_connection_summary()["connection_count"] == 0


def test_old_probe_cannot_overwrite_new_generation(probe, monkeypatch):
    worker, clock, scheduled = probe
    async def completed(handoff, **kw):
        return {"guild_id": "1", "state": handoff["fixture_result"]}
    monkeypatch.setattr(worker, "_voice_agent_probe_connection_async", completed)
    worker._voice_agent_start_connection_probe({"guild_id": 1})
    scheduled[0].options["kwargs"]["handoff"]["fixture_result"] = "old-result"
    clock[0] += 9000
    worker._voice_agent_start_connection_probe({"guild_id": 1})
    scheduled[1].options["kwargs"]["handoff"]["fixture_result"] = "new-result"
    scheduled[1].run()
    scheduled[0].run()
    assert worker._voice_agent_connection_summary()["last_connection"]["state"] == "new-result"


def test_thread_start_failure_does_not_leave_probe_running(probe):
    worker, _, _ = probe
    original = worker.threading.Thread
    class FailedThread(original):
        def start(self):
            raise OSError("thread unavailable")
    worker.threading.Thread = FailedThread
    with pytest.raises(OSError, match="thread unavailable"):
        worker._voice_agent_start_connection_probe({"guild_id": 1})
    status = worker._voice_agent_connection_summary()
    assert status["connection_probing_count"] == 0
    assert status["last_connection"]["state"] == "connection_failed"
    worker.threading.Thread = original
    assert worker._voice_agent_start_connection_probe({"guild_id": 1})["started"]


@pytest.mark.parametrize("cancel_on_receive", ["none", "clear", "release", "expiry", "retry_on_hello"])
def test_real_probe_loop_closes_ws_and_honors_cancellation(probe, monkeypatch, cancel_on_receive):
    worker, clock, scheduled = probe
    monkeypatch.setattr(worker, "_voice_agent_probe_connection_async", worker._test_probe_coroutine)
    sent, udp_calls = [], []
    class WS:
        def __init__(self):
            self.closed = False
            self.messages = [{"op": 8, "d": {"heartbeat_interval": 1000}},
                {"op": 2, "d": {"ssrc": 123, "ip": "192.0.2.1", "port": 9000, "modes": ["fixture"]}}]
        async def __aenter__(self): return self
        async def __aexit__(self, *a): self.closed = True
        async def send_json(self, data): sent.append(data)
        async def receive(self, **kw):
            if cancel_on_receive == "clear":
                worker._voice_agent_clear_connection({"guild_id": 1})
            elif cancel_on_receive == "release":
                worker._voice_agent_release_transfer({"guild_id": 1})
            elif cancel_on_receive == "expiry":
                clock[0] += 46000
            elif cancel_on_receive == "retry_on_hello" and len(self.messages) == 1:
                assert not worker._voice_agent_start_connection_probe({"guild_id": 1})["started"]
            return SimpleNamespace(type=1, data=json.dumps(self.messages.pop(0)))
        async def close(self, **kw): self.closed = True
    ws = WS()
    class Session:
        def __init__(self, **kw): self.options = kw
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        def ws_connect(self, url, **kw):
            assert url == "wss://voice.invalid/?v=4" and kw["heartbeat"] is None
            return ws
    fake = SimpleNamespace(ClientTimeout=lambda **kw: kw, ClientSession=Session,
                           WSMsgType=SimpleNamespace(TEXT=1, ERROR=2, CLOSED=3, CLOSE=4))
    monkeypatch.setitem(sys.modules, "aiohttp", fake)
    monkeypatch.setattr(worker, "_voice_agent_udp_discovery_probe", lambda **kw: udp_calls.append(kw) or {"attempted": True, "ok": True})
    worker._voice_agent_start_connection_probe({"guild_id": 1})
    scheduled[0].run()
    assert ws.closed
    status = worker._voice_agent_connection_summary()
    if cancel_on_receive in {"clear", "release", "expiry"}:
        assert not status["connection_ready"] and not udp_calls
        if cancel_on_receive == "clear":
            assert status["connection_count"] == 0
        assert [message["op"] for message in sent] == [0]
    else:
        assert status["connection_ready"] and status["last_connection"]["closed_after_probe"]
        assert [message["op"] for message in sent] == [0, 3] and len(udp_calls) == 1
        assert udp_calls[0]["ssrc"] == 123
        assert "private-voice-token" not in json.dumps(status)
