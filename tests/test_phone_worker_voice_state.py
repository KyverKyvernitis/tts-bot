"""Voice state contracts with temporary persistence and a controlled clock."""
import importlib.util
import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest

PHONE = Path(__file__).resolve().parents[1] / "deploy/termux/phone-worker/phone_worker.py"
PUBLIC_RECORD = {"guild_id": "1", "channel_id": "11", "source": "tts", "state": "connected_dry_run",
    "created_at_ms": 1000, "started_at_ms": 2000, "updated_at_ms": 3000, "expires_at_ms": 60000,
    "session_id": "private-session", "voice_token": "private-voice-token", "endpoint": "voice.invalid",
    "voice_owner": "worker", "allow_connection_probe": True, "probe_authorized": True, "lease_id": "test-lease",
    "discord_voice": {"connected": True, "session_id_present": True, "voice_token_present": True,
                      "endpoint_present": True, "endpoint_host": "voice.invalid"}}


@pytest.fixture
def voice(monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location("phone_voice_state_test", PHONE)
    worker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(worker)
    clock = [10_000]
    monkeypatch.setattr(worker, "_voice_agent_now_ms", lambda: clock[0])
    monkeypatch.setenv("PHONE_WORKER_VOICE_AGENT_STATE_FILE", str(tmp_path / "voice-state.json"))
    for key in ["ENABLED", "SHARED_SESSION_ENABLED", "HANDOFF_ENABLED", "TRANSFER_CONTROL_ENABLED",
                "CONNECTION_DRY_RUN_ENABLED", "SESSION_TTL_SECONDS", "HANDOFF_TTL_SECONDS", "TRANSFER_LEASE_TTL_SECONDS"]:
        monkeypatch.delenv("PHONE_WORKER_VOICE_AGENT_" + key, raising=False)
    worker.threading = SimpleNamespace(Thread=lambda **kw: pytest.fail("uncontrolled voice thread"))
    return worker, clock


def request(guild=1, **extra):
    return {"guild_id": guild, "channel_id": guild + 10, "bot_user_id": 99,
            "discord_voice_handoff": {"session_id": "private-session", "endpoint": "voice.invalid",
                                      "voice_token": "private-voice-token"}, **extra}


def test_session_persistence_contains_metadata_only_and_handoff_stays_in_memory(voice):
    worker, _ = voice
    body = request(discord_voice={"connected": True, "session_id_present": True, "voice_token_present": True,
        "endpoint_present": True, "endpoint_host": "voice.invalid", "session_id": "private-session",
        "voice_token": "private-voice-token", "token": "private-bot-token"})
    registered = worker._voice_agent_register_session(body)
    path = worker._voice_agent_state_file()
    before = path.read_bytes()
    handoff = worker._voice_agent_register_handoff(body)
    assert path.read_bytes() == before and path.stat().st_mode & 0o777 == 0o600
    assert registered["registered"] and registered["session"]["connected"]
    assert handoff["handoff"]["complete"] and handoff["handoff"]["voice_owner"] == "vps"
    for secret in ["private-session", "private-voice-token", "private-bot-token"]:
        assert secret not in before.decode() and secret not in json.dumps((registered, handoff))
    assert worker._VOICE_AGENT_HANDOFF_MEMORY["1"]["voice_token"] == "private-voice-token"


@pytest.mark.parametrize(("kind", "ttl", "expected"), [("session", 1, 30), ("session", 9999, 900),
    ("handoff", 1, 10), ("handoff", 9999, 180), ("transfer", 1, 10), ("transfer", 9999, 180)])
def test_registration_ttl_limits(voice, kind, ttl, expected):
    worker, clock = voice
    fn = {"session": worker._voice_agent_register_session, "handoff": worker._voice_agent_register_handoff,
          "transfer": worker._voice_agent_prepare_transfer}[kind]
    result = fn(request(expires_in_seconds=ttl))
    assert result[kind]["ttl_seconds"] == expected
    assert result[kind]["guild_id"] == "1" and result[kind]["channel_id"] == "11"
    clock[0] += expected * 1000
    summary = getattr(worker, "_voice_agent_" + kind + "_summary")()
    assert summary[kind + "_count"] == 0


@pytest.mark.parametrize(("fn", "flag"), [("register_session", "ENABLED"), ("register_session", "SHARED_SESSION_ENABLED"),
    ("register_handoff", "HANDOFF_ENABLED"), ("prepare_transfer", "TRANSFER_CONTROL_ENABLED"),
    ("start_connection_probe", "CONNECTION_DRY_RUN_ENABLED")])
def test_disabled_features_do_not_create_state_or_threads(voice, monkeypatch, fn, flag):
    worker, _ = voice
    monkeypatch.setenv("PHONE_WORKER_VOICE_AGENT_" + flag, "false")
    with pytest.raises(RuntimeError, match="desativad"):
        getattr(worker, "_voice_agent_" + fn)(request())
    assert not worker._voice_agent_state_file().exists()
    assert worker._VOICE_AGENT_SESSION_MEMORY is None and not worker._VOICE_AGENT_HANDOFF_MEMORY
    assert not worker._VOICE_AGENT_TRANSFER_MEMORY and not worker._VOICE_AGENT_CONNECTION_MEMORY


@pytest.mark.parametrize("kind", ["session", "handoff", "connection", "transfer"])
def test_public_projection_honors_explicit_zero_clock(voice, kind):
    worker, _ = voice
    raw = {"guild_id": "1", "created_at_ms": 0, "updated_at_ms": 0, "started_at_ms": 0, "expires_at_ms": 10_000}
    result = getattr(worker, "_voice_agent_public_" + kind)(raw, now_ms=0)
    assert result["age_seconds"] == 0.0
    if kind != "connection":
        assert result["ttl_seconds"] == 10.0


@pytest.mark.parametrize("kind", ["session", "handoff", "connection", "transfer"])
def test_summary_puts_just_updated_record_first_and_counts_before_limit(voice, kind):
    worker, clock = voice
    records = {str(i): {"guild_id": str(i), "started_at_ms": clock[0] - 1000,
                        "updated_at_ms": clock[0] if i == 2 else clock[0] - 1000} for i in (1, 2)}
    if kind == "session":
        worker._VOICE_AGENT_SESSION_MEMORY = {"sessions": records}
    else:
        setattr(worker, "_VOICE_AGENT_" + kind.upper() + "_MEMORY", records)
    summary = getattr(worker, "_voice_agent_" + kind + "_summary")(limit=1)
    assert summary[kind + "_count"] == 2 and len(summary[kind + "s"]) == 1
    assert summary["last_" + kind]["guild_id"] == "2"
    selected = getattr(worker, "_voice_agent_" + kind + "_summary")(guild_id=1)
    assert selected[kind + "_count"] == 1 and selected["last_" + kind]["guild_id"] == "1"


@pytest.mark.parametrize("kind", ["sessions", "handoffs", "transfers"])
def test_pruning_handles_partial_records_without_losing_valid_entries(voice, kind):
    worker, clock = voice
    records = {"expired": {"expires_at_ms": clock[0]}, "future": {"expires_at_ms": clock[0] + 1},
               "indefinite": {"expires_at_ms": 0}, "invalid": ["corrupt"]}
    if kind == "sessions":
        worker._VOICE_AGENT_SESSION_MEMORY = {"sessions": records}
        result = worker._voice_agent_prune_sessions()["sessions"]
    else:
        setattr(worker, "_VOICE_AGENT_" + kind[:-1].upper() + "_MEMORY", records)
        result = getattr(worker, "_voice_agent_prune_" + kind)()
    assert set(result) == {"future", "indefinite"}
    assert set(records) == {"future", "indefinite"}


def test_explicit_empty_session_state_does_not_load_or_prune_runtime(voice):
    worker, _ = voice
    live = {"sessions": {"live": {"expires_at_ms": 999999}}}
    worker._VOICE_AGENT_SESSION_MEMORY = live
    empty = {}
    assert worker._voice_agent_prune_sessions(empty) is empty
    assert empty == {"sessions": {}} and worker._VOICE_AGENT_SESSION_MEMORY is live


@pytest.mark.parametrize("prepared", [True, False])
def test_transfer_requires_confirmation_then_grants_and_releases_owner(voice, prepared):
    worker, _ = voice
    worker._voice_agent_register_handoff(request())
    if prepared:
        worker._voice_agent_prepare_transfer(request())
    with pytest.raises(RuntimeError, match="confirmação"):
        worker._voice_agent_begin_transfer(request())
    result = worker._voice_agent_begin_transfer(request(confirm_transfer=True))
    assert result["started"] and result["transfer"]["voice_owner"] == "worker"
    assert result["transfer"]["probe_authorized"] and result["handoff_ready"]
    assert worker._VOICE_AGENT_HANDOFF_MEMORY["1"]["voice_owner"] == "worker"
    released = worker._voice_agent_release_transfer(request(reason="done"))
    assert released["released"] and released["transfer"]["voice_owner"] == "vps"
    assert not worker._VOICE_AGENT_HANDOFF_MEMORY["1"]["allow_connection_probe"]
    blocked = worker._voice_agent_start_connection_probe(request(allow_probe=True))
    assert blocked["blocked"] and not blocked["started"]


def test_expired_transfer_revokes_its_handoff_probe_permission(voice):
    worker, clock = voice
    worker._voice_agent_register_handoff(request())
    handoff = worker._VOICE_AGENT_HANDOFF_MEMORY["1"]
    handoff.update(voice_owner="worker", transport_owner="worker", allow_connection_probe=True,
                   connection_policy="worker_ownership_granted_explicit_transfer")
    worker._VOICE_AGENT_TRANSFER_MEMORY["1"] = {"guild_id": "1", "voice_owner": "worker",
                                               "probe_authorized": True, "expires_at_ms": clock[0]}
    assert worker._voice_agent_prune_transfers() == {}
    assert handoff["voice_owner"] == "vps" and not handoff["allow_connection_probe"]
    blocked = worker._voice_agent_start_connection_probe(request(allow_probe=True))
    assert blocked["blocked"] and not blocked["started"]


@pytest.mark.parametrize("value", ["false", "0", "off"])
def test_negative_text_flag_does_not_confirm_voice_transfer(voice, value):
    worker, _ = voice
    worker._voice_agent_register_handoff(request())
    with pytest.raises(RuntimeError, match="confirmação"):
        worker._voice_agent_begin_transfer(request(confirm_transfer=value))
    assert worker._VOICE_AGENT_HANDOFF_MEMORY["1"]["voice_owner"] == "vps"


@pytest.mark.parametrize("value", ["false", "0", "off"])
def test_negative_text_flag_does_not_authorize_probe(voice, value):
    worker, _ = voice
    registered = worker._voice_agent_register_handoff(request(voice_owner="worker", allow_probe=value))
    assert not registered["handoff"]["allow_connection_probe"]
    assert worker._voice_agent_start_connection_probe(request(allow_probe=value))["blocked"]


@pytest.mark.parametrize("value", [True, "true", "1", "sim"])
def test_affirmative_confirmation_alias_remains_supported(voice, value):
    worker, _ = voice
    worker._voice_agent_register_handoff(request())
    assert worker._voice_agent_begin_transfer(request(confirm=value))["started"]


@pytest.mark.parametrize(("confirmation", "granted"), [("false", False), (True, True)])
def test_direct_tts_keeps_proxy_contract_and_respects_transfer_flag(voice, monkeypatch, confirmation, granted):
    worker, _ = voice
    calls = []
    handler = object.__new__(worker.WorkerHandler)
    monkeypatch.setattr(handler, "_task_music_agent_proxy", lambda body: calls.append(body) or {"ok": True, "engine": "fixture"})
    monkeypatch.setattr(worker, "_music_agent_snapshot", lambda: {})
    monkeypatch.setattr(worker, "_tts_agent_snapshot", lambda: {})
    monkeypatch.setattr(worker, "_voice_agent_snapshot", lambda **kw: {"ok": True})
    result = handler._task_voice_agent_play_tts({"guild_id": 1, "channel_id": 2, "audio_b64": "fixture-audio",
                                              "confirm_transfer": confirmation})
    assert result["ok"] and len(calls) == 1
    assert calls[0]["audio_b64"] == "fixture-audio" and calls[0]["action"] == "voice_tts"
    assert calls[0]["voice_channel_id"] == 2 and calls[0]["timeout_seconds"] == 30.0
    assert bool(worker._VOICE_AGENT_TRANSFER_MEMORY) is granted


def test_clear_session_is_scoped_and_durable(voice):
    worker, _ = voice
    for guild in (1, 2):
        worker._voice_agent_register_session(request(guild))
    assert not worker._voice_agent_clear_session({})["cleared"]
    assert worker._voice_agent_clear_session({"guild_id": 1})["cleared"]
    assert worker._voice_agent_session_summary()["active_guilds"] == ["2"]
    worker._VOICE_AGENT_SESSION_MEMORY = None
    assert worker._voice_agent_session_summary()["active_guilds"] == ["2"]
    assert worker._voice_agent_clear_session({"all": True})["cleared"]
    assert json.loads(worker._voice_agent_state_file().read_text())["sessions"] == {}


def test_parallel_sessions_keep_live_owner_and_persist_complete_state(voice):
    worker, _ = voice
    lock = worker._VOICE_AGENT_SESSION_LOCK
    state = worker._voice_agent_load_state()
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda guild: worker._voice_agent_register_session(request(guild)), range(1, 33)))
    assert all(result["registered"] for result in results)
    assert worker._VOICE_AGENT_SESSION_LOCK is lock and worker._VOICE_AGENT_SESSION_MEMORY is state
    assert len(state["sessions"]) == 32
    assert len(json.loads(worker._voice_agent_state_file().read_text())["sessions"]) == 32


def test_failed_persistence_keeps_memory_available(voice, monkeypatch):
    worker, _ = voice
    def fail(*args):
        raise OSError("replace unavailable")
    monkeypatch.setattr(worker.os, "replace", fail)
    assert worker._voice_agent_register_session(request())["registered"]
    assert worker._voice_agent_session_summary()["session_count"] == 1


def test_public_wire_contract_matches_corrected_monolith(voice):
    worker, _ = voice
    expected = json.loads((PHONE.parents[3] / "tests/fixtures/voice_public_contract.json").read_text())
    actual = {kind: getattr(worker, "_voice_agent_public_" + kind)(PUBLIC_RECORD, now_ms=10000)
              for kind in ("session", "handoff", "connection", "transfer")}
    assert actual == expected


def test_voice_module_one_owner_and_live_facade_callbacks(voice, monkeypatch):
    worker, _ = voice
    with ThreadPoolExecutor(max_workers=8) as pool:
        modules = list(pool.map(lambda _: worker._phone_worker_voice_state_module(), range(32)))
    assert all(module is modules[0] for module in modules)
    assert Path(modules[0].__file__) == PHONE.parent / "phone_worker_runtime/voice_state.py"
    original_lock = worker._VOICE_AGENT_SESSION_LOCK
    worker._VOICE_AGENT_HANDOFF_MEMORY = {"7": {"guild_id": "7"}}
    monkeypatch.setattr(worker, "_voice_agent_public_handoff", lambda raw, **kw:
        {"guild_id": raw["guild_id"], "age_seconds": 0, "complete": True})
    summary = worker._voice_agent_handoff_summary()
    assert summary["handoff_guilds"] == ["7"] and summary["handoff_ready"]
    assert worker._VOICE_AGENT_SESSION_LOCK is original_lock
    calls = []
    monkeypatch.setattr(worker, "_voice_agent_int", lambda value, default=0: calls.append(value) or default)
    worker._voice_agent_public_transfer({"expires_at_ms": "not-an-int"})
    assert "not-an-int" in calls


@pytest.mark.parametrize("initial", ["missing", "incomplete", "syntax_error"])
def test_voice_module_late_arrival_keeps_bootstrap_control_available(voice, monkeypatch, tmp_path, initial):
    worker, _ = voice
    lone = tmp_path / "phone_worker.py"
    shutil.copyfile(PHONE, lone)
    monkeypatch.setattr(worker, "__file__", str(lone))
    target = tmp_path / "phone_worker_runtime/voice_state.py"
    target.parent.mkdir()
    if initial != "missing":
        target.write_text("partial = True\n" if initial == "incomplete" else "broken !!!")
    worker._VOICE_AGENT_HANDOFF_MEMORY = {"1": {"guild_id": "1", "expires_at_ms": 99999}}
    for _ in range(2):
        with pytest.raises((RuntimeError, SyntaxError)):
            worker._voice_agent_handoff_summary()
        assert worker._PHONE_WORKER_VOICE_STATE_MODULE is None
        assert "1" in worker._VOICE_AGENT_HANDOFF_MEMORY
    monkeypatch.setattr(worker, "_phone_worker_source_hash", lambda: "a" * 64)
    monkeypatch.setattr(worker, "_default_worker_name", lambda: "isolated fixture")
    payload = worker._core_worker_payload(host="0.0.0.0", port=8766)
    assert payload["health"]["control_plane_alive"] and payload["health"]["payload_mode"] == "bootstrap"
    shutil.copyfile(PHONE.parent / "phone_worker_runtime/voice_state.py", target)
    assert worker._voice_agent_handoff_summary()["handoff_guilds"] == ["1"]
    assert Path(worker._phone_worker_voice_state_module().__file__) == target


def test_voice_module_import_and_projection_have_no_runtime_effects(tmp_path):
    code = r'''
import importlib.util, os, socket, subprocess, sys, threading
from unittest.mock import patch
sys.dont_write_bytecode = True
def audit(event, args):
    if event == "open" and ((isinstance(args[1], str) and any(c in args[1] for c in "wa+"))
                            or (isinstance(args[2], int) and args[2] & (os.O_WRONLY | os.O_RDWR))):
        raise AssertionError("write during voice import/projection")
sys.addaudithook(audit)
before = dict(os.environ)
with patch.object(threading.Thread, "start", side_effect=AssertionError("thread")), \
     patch.object(subprocess, "Popen", side_effect=AssertionError("process")), \
     patch.object(socket.socket, "connect", side_effect=AssertionError("connect")), \
     patch.object(socket.socket, "bind", side_effect=AssertionError("bind")):
    spec = importlib.util.spec_from_file_location("isolated_voice", sys.argv[1])
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    record = {"guild_id": "1", "expires_at_ms": 1000}
    before_record = dict(record)
    data = module.public_session(record, now_ms=0, int_value=lambda value, default: int(value) if value is not None else default)
    assert data["ttl_seconds"] == 1.0 and record == before_record
assert dict(os.environ) == before
assert "phone_worker" not in sys.modules and "aiohttp" not in sys.modules and "music_agent" not in sys.modules
'''
    result = subprocess.run([sys.executable, "-S", "-c", code, str(PHONE.parent / "phone_worker_runtime/voice_state.py")],
        cwd=tmp_path, env=os.environ.copy(), capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr
