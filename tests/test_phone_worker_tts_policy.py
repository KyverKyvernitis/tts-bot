"""Policy compatibility vectors captured from the original round-20 facade."""
import copy
from concurrent.futures import ThreadPoolExecutor
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest

from test_phone_worker_tts_lifecycle import PHONE, tts

CONTRACT = json.loads((Path(__file__).parent / "fixtures/tts_policy_contract.json").read_text())


@pytest.mark.parametrize("case", CONTRACT["cache_keys"])
def test_cache_keys_match_checkpoint20_contract(tts, monkeypatch, case):
    monkeypatch.setenv("PHONE_WORKER_TETO_BASE_PITCH", CONTRACT["teto_base_pitch"])
    monkeypatch.setattr(tts.worker, "_teto_status", lambda: {"fingerprint": CONTRACT["teto_fingerprint"]})
    before = copy.deepcopy(case["body"])
    assert tts.handler._tts_agent_standard_cache_key(case["body"], engine=case["engine"]) == case["key"]
    assert case["body"] == before


@pytest.mark.parametrize(("method", "raw", "expected"), [
    ("edge_rate", "−20％", "-20%"), ("edge_rate", "+010 %", "+010%"),
    ("edge_rate", "1.5", "+0%"), ("edge_rate", None, "+0%"),
    ("edge_pitch", "—5 hz", "-5Hz"), ("edge_pitch", "10HZ", "+10Hz"),
    ("edge_pitch", "1.5Hz", "+0Hz"), ("edge_pitch", "", "+0Hz"),
    ("gtts_language", "PT_br", "pt"), ("gtts_language", " en_US ", "en-us"),
    ("gtts_language", "", "pt"),
])
def test_text_normalization_preserves_provider_inputs(tts, method, raw, expected):
    assert getattr(tts.handler, "_normalize_tts_" + method)(raw) == expected


@pytest.mark.parametrize(("raw", "expected"), [(".WAVE", "wav"), ("opus", "ogg"), ("ogg", "ogg"),
    ("pcm", "mp3"), (None, "mp3")])
def test_cache_format_aliases(tts, raw, expected):
    assert tts.handler._normalize_tts_cache_format(raw) == expected


def test_cache_key_sanitizer_limits_and_invalid_fallback(tts):
    key = "A" * 120
    assert tts.handler._sanitize_tts_cache_key(key) == "a" * 96
    assert tts.handler._sanitize_tts_cache_key(" /ABCDEF_01234567-89/ ") == "abcdef_01234567-89"
    with pytest.raises(RuntimeError, match="inválida/curta"):
        tts.handler._sanitize_tts_cache_key("too-short")


def test_engine_order_honors_live_preference_and_explicit_teto(tts, monkeypatch):
    available = ["teto", "android_native", "edge", "gtts", "piper"]
    body = {"engine": "google", "fallback_engine": "edge_tts"}
    monkeypatch.setenv("PHONE_WORKER_TTS_AGENT_ENGINE", "native")
    assert tts.handler._tts_agent_engine_order(body, available) == ["android_native", "gtts", "edge"]
    monkeypatch.setenv("PHONE_WORKER_TTS_AGENT_ENGINE", "edge_tts")
    assert tts.handler._tts_agent_engine_order(body, available) == ["edge", "gtts", "android_native"]
    assert tts.handler._tts_agent_engine_order({"engine": "utau"}, available) == ["teto", "gtts", "android_native", "edge"]
    assert tts.handler._tts_agent_engine_order({"engine": "utau"}, ["edge"]) == ["edge"]
    assert tts.handler._tts_agent_engine_order(body, ["piper"]) == []
    assert body == {"engine": "google", "fallback_engine": "edge_tts"}


def test_engine_advertisement_and_default_preference_keep_teto_opt_in(tts, monkeypatch):
    available = tts.worker._tts_agent_available_engines(tts.deps)
    assert available == ["teto", "android_native", "edge", "gtts"]
    assert tts.worker._tts_agent_preferred_engine(available) == "android_native"
    assert tts.worker._tts_agent_preferred_engine(["teto"]) == ""
    monkeypatch.setenv("PHONE_WORKER_TTS_AGENT_ENGINE", "utau")
    assert tts.worker._tts_agent_preferred_engine(available) == "teto"
    assert tts.worker._tts_agent_available_engines({"piper": True}) == []


def test_cache_key_callbacks_and_teto_inputs_are_resolved_only_when_needed(tts, monkeypatch):
    calls = []
    monkeypatch.setattr(tts.worker, "_teto_status", lambda: calls.append("status") or {"fingerprint": "first"})
    monkeypatch.setattr(tts.handler, "_normalize_tts_edge_rate", lambda raw: "+88%")
    monkeypatch.setattr(tts.handler, "_normalize_tts_edge_pitch", lambda raw: "-77Hz")
    provided = {"engine": "edge", "text": "Texto.", "cache_key": "p" * 64}
    assert tts.handler._tts_agent_standard_cache_key(provided, engine="edge") == "p" * 64
    assert calls == []
    monkeypatch.setattr(tts.handler, "_sanitize_tts_cache_key", lambda raw: "reassigned-cache-key")
    assert tts.handler._tts_agent_standard_cache_key(provided, engine="edge") == "reassigned-cache-key"
    import hashlib
    assert tts.handler._tts_agent_standard_cache_key({"text": "Texto."}, engine="edge") == hashlib.sha256(
        b"tts-v2|edge|pt-BR-FranciscaNeural|+88%|-77Hz|Texto.").hexdigest()
    before = tts.handler._tts_agent_standard_cache_key(provided, engine="teto")
    assert calls == ["status"]
    monkeypatch.setenv("PHONE_WORKER_TETO_BASE_PITCH", "D4")
    after = tts.handler._tts_agent_standard_cache_key(provided, engine="teto")
    assert before != after and calls == ["status", "status"]


def test_policy_loads_once_concurrently_and_warm_calls_do_not_load_or_lock(tts, monkeypatch):
    worker = tts.worker
    assert worker._PHONE_WORKER_TTS_POLICY_MODULE is None
    original = importlib.util.spec_from_file_location
    imports = []

    def spec_for(*args, **kwargs):
        spec = original(*args, **kwargs)
        execute = spec.loader.exec_module
        def execute_once(module):
            imports.append(module)
            execute(module)
        spec.loader.exec_module = execute_once
        return spec

    monkeypatch.setattr(worker, "importlib", SimpleNamespace(util=SimpleNamespace(
        spec_from_file_location=spec_for, module_from_spec=importlib.util.module_from_spec)))
    with ThreadPoolExecutor(max_workers=8) as pool:
        loaded = list(pool.map(lambda _: worker._phone_worker_tts_policy_module(), range(32)))
    assert len(imports) == 1 and all(module is imports[0] for module in loaded)
    monkeypatch.setattr(worker, "Path", lambda *a: pytest.fail("warm policy touched source path"))
    monkeypatch.setattr(worker, "_PHONE_WORKER_TTS_POLICY_LOCK", None)
    for _ in range(10):
        assert worker._tts_agent_normalize_engine("edge_tts") == "edge"
        assert tts.handler._normalize_tts_edge_pitch("5") == "+5Hz"
    assert len(imports) == 1 and not tts.executor.jobs


@pytest.mark.parametrize("initial", ["missing", "incomplete", "syntax_error"])
def test_late_policy_arrival_preserves_minimal_control_and_retries(tts, monkeypatch, tmp_path, initial):
    worker = tts.worker
    phone = tmp_path / "late/phone_worker.py"
    phone.parent.mkdir()
    target = phone.parent / "phone_worker_runtime/tts_policy.py"
    target.parent.mkdir()
    monkeypatch.setattr(worker, "__file__", str(phone))
    if initial != "missing":
        target.write_text("normalize_engine = None\n" if initial == "incomplete" else "broken !!!")
    with pytest.raises((RuntimeError, SyntaxError)):
        worker._phone_worker_tts_policy_module()
    assert worker._PHONE_WORKER_TTS_POLICY_MODULE is None
    monkeypatch.setattr(worker, "_runtime_state_dir", lambda: tmp_path / "state")
    monkeypatch.setattr(worker, "_phone_worker_source_hash", lambda: "fixture-hash")
    monkeypatch.setattr(worker, "_default_worker_name", lambda: "isolated TTS fixture")
    payload = worker._core_worker_payload(host="0.0.0.0", port=8766)
    assert payload["health"]["control_plane_alive"] and payload["health"]["payload_mode"] == "bootstrap"
    assert not tts.calls and not tts.executor.jobs
    shutil.copy2(PHONE.parent / "phone_worker_runtime/tts_policy.py", target)
    assert worker._tts_agent_normalize_engine("google") == "gtts"
    assert worker._phone_worker_tts_policy_module().__file__ == str(target)


def test_module_and_handler_rebindings_remain_live(tts, monkeypatch):
    worker, handler = tts.worker, tts.handler
    module = worker._phone_worker_tts_policy_module()
    monkeypatch.setattr(module, "normalize_edge_rate", lambda raw: "+88%")
    assert handler._normalize_tts_edge_rate("input") == "+88%"
    monkeypatch.setattr(handler, "_normalize_tts_gtts_language", lambda raw: "late-language")
    monkeypatch.setattr(module, "standard_cache_key", lambda body, **kw: kw["normalize_language"](None))
    assert handler._tts_agent_standard_cache_key({"text": "Bindings."}, engine="gtts") == "late-language"
    monkeypatch.setattr(worker, "_TTS_AGENT_TOTAL", 7)
    monkeypatch.setattr(worker, "_TTS_AGENT_FAILED", 2)
    monkeypatch.setattr(worker, "_TTS_AGENT_ACTIVE", 1)
    monkeypatch.setattr(worker, "_TTS_AGENT_TOTAL_MS", 350)
    monkeypatch.setenv("PHONE_WORKER_TTS_AGENT_CONCURRENCY", "3")
    snapshot = worker._tts_agent_snapshot()
    assert (snapshot["total"], snapshot["failed"], snapshot["active"], snapshot["avg_synth_ms"], snapshot["concurrency_limit"]) == (7, 2, 1, 50, 3)


def test_pure_policy_import_and_execution_need_no_runtime_or_io(tmp_path):
    path = PHONE.parent / "phone_worker_runtime/tts_policy.py"
    code = r'''
import builtins, importlib.util, os, socket, subprocess, sys, threading
from unittest.mock import patch
sys.dont_write_bytecode = True
def forbidden(*a, **kw):
    raise AssertionError("pure policy touched runtime")
before = dict(os.environ)
with patch.object(threading.Thread, "start", forbidden), patch.object(socket.socket, "connect", forbidden), \
     patch.object(subprocess, "Popen", forbidden), patch.object(os, "getenv", forbidden):
    spec = importlib.util.spec_from_file_location("pure_tts_policy", sys.argv[1])
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    with patch.object(builtins, "open", forbidden):
        assert m.normalize_engine("google") == "gtts"
        assert m.available_engines({"gtts": True}) == ["gtts"]
        assert m.preferred_engine(["edge", "gtts"], requested="auto") == "edge"
        assert m.engine_order({"engine": "utau"}, ["teto", "edge"], preferred_default="edge") == ["teto", "edge"]
        assert m.normalize_edge_rate("1") == "+1%" and m.normalize_edge_pitch("2") == "+2Hz"
        assert m.normalize_gtts_language("pt-BR") == "pt"
        assert m.normalize_cache_format("opus") == "ogg"
        assert m.sanitize_cache_key("A" * 64) == "a" * 64
        assert m.cache_mode_allows_read({"cache_mode": "no_store"})
        assert not m.cache_mode_allows_store({"cache_mode": "no_store"})
        key = m.standard_cache_key({"text": "test"}, engine="gtts", sanitize_key=m.sanitize_cache_key,
            normalize_rate=m.normalize_edge_rate, normalize_pitch=m.normalize_edge_pitch,
            normalize_language=m.normalize_gtts_language, teto_fingerprint="unused", teto_base_pitch="C4")
        assert len(key) == 64
assert dict(os.environ) == before
assert not any(n.startswith(("phone_worker", "music_agent", "teto_renderer")) for n in sys.modules)
assert "tts_transport" not in sys.modules
'''
    result = subprocess.run([sys.executable, "-S", "-c", code, str(path)], cwd=tmp_path,
                            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("available", [True, False])
def test_startup_primes_policy_before_work_and_survives_missing_module(tts, monkeypatch, tmp_path, available):
    worker = tts.worker
    if not available:
        monkeypatch.setattr(worker, "__file__", str(tmp_path / "missing/phone_worker.py"))
    monkeypatch.setattr(worker, "_load_phone_worker_runtime_env", lambda: None)
    monkeypatch.setattr(worker, "_load_env_file", lambda: None)
    monkeypatch.setattr(worker, "_load_persisted_pending_core_job_results", lambda: None)
    called = []
    def heartbeat(**kwargs):
        assert (worker._PHONE_WORKER_TTS_POLICY_MODULE is not None) == available
        called.append(kwargs)
        return True
    monkeypatch.setattr(worker, "_send_core_worker_heartbeat_once", heartbeat)
    monkeypatch.setattr(sys, "argv", ["phone-worker", "--heartbeat-once"])
    assert worker.main() == 0 and len(called) == 1
    assert not tts.calls and not tts.executor.jobs
