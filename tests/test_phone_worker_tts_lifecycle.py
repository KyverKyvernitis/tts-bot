"""Exercise the real TTS facade with local cache files and controlled providers."""
import base64
import copy
import hashlib
import importlib.util
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

PHONE = Path(__file__).resolve().parents[1] / "deploy/termux/phone-worker/phone_worker.py"


class DeferredExecutor:
    def __init__(self):
        self.jobs = []

    def submit(self, callback):
        self.jobs.append(callback)

    def drain(self):
        while self.jobs:
            self.jobs.pop(0)()


@pytest.fixture
def tts(monkeypatch, tmp_path):
    for name in list(os.environ):
        if name.startswith(("PHONE_WORKER_TTS", "PHONE_WORKER_TETO", "PHONE_WORKER_ANDROID_TTS")):
            monkeypatch.delenv(name)
    monkeypatch.setenv("CORE_WORKER_PROFILE", "turbo")
    monkeypatch.setenv("CORE_WORKER_ROLES", "cache-worker")
    monkeypatch.setenv("CORE_WORKER_CAPABILITIES", "cache-worker,tts-synth")
    monkeypatch.setenv("CORE_WORKER_ID", "phone-tts-fixture")
    monkeypatch.setenv("PHONE_WORKER_TTS_CACHE_DIR", str(tmp_path / "cache"))
    spec = importlib.util.spec_from_file_location("phone_tts_lifecycle", PHONE)
    worker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(worker)
    handler = object.__new__(worker.WorkerHandler)
    handler.server = SimpleNamespace(job_timeout=30, max_output_bytes=4096, max_body_bytes=8192)
    worker.time = SimpleNamespace(monotonic=lambda: 100.0, time=lambda: 1000.0)
    executor = DeferredExecutor()
    monkeypatch.setattr(worker, "_TTS_CACHE_MAINTENANCE_EXECUTOR", executor)
    calls = []
    deps = {"edge_tts": True, "gtts": True, "android_native_tts": True, "teto_tts": True,
            "teto": {"fingerprint": "fixture-bank"}}
    monkeypatch.setattr(worker, "_turbo_dependency_snapshot", lambda: dict(deps))
    teto_status = worker._teto_status
    get_teto_renderer = worker._get_teto_renderer
    monkeypatch.setattr(worker, "_teto_status", lambda: dict(deps["teto"]))
    native_raw_request = worker._android_tts_raw_request
    native_json_request = worker._android_tts_json_request

    def synthesize(**kwargs):
        calls.append(dict(kwargs))
        return b"audio:" + kwargs["engine"].encode()

    def native(path, **kwargs):
        calls.append({"engine": "android_native", "path": path, **kwargs})
        return b"audio:android_native", {"audio_format": "wav", "voice": "native-fixture", "android_synth_ms": 5}

    def teto(text, **kwargs):
        calls.append({"engine": "teto", "text": text, **kwargs})
        return {"audio": b"audio:teto", "audio_format": "wav", "voicebank": "fixture-bank"}

    monkeypatch.setattr(worker, "_tts_transport_module", lambda: SimpleNamespace(synthesize_bytes=synthesize))
    monkeypatch.setattr(worker, "_android_tts_raw_request", native)
    monkeypatch.setattr(worker, "_android_tts_json_request", lambda *a, **kw: pytest.fail("unexpected native JSON request"))
    monkeypatch.setattr(worker, "_get_teto_renderer", lambda: SimpleNamespace(synthesize=teto))
    return SimpleNamespace(worker=worker, handler=handler, calls=calls, executor=executor, deps=deps,
                           root=tmp_path / "cache", native_raw_request=native_raw_request,
                           native_json_request=native_json_request, teto_status=teto_status,
                           get_teto_renderer=get_teto_renderer)


def audio(result):
    return result["_raw_audio"] if "_raw_audio" in result else base64.b64decode(result["data_b64"])


@pytest.mark.parametrize("engine", ["gtts", "edge", "android_native", "teto"])
@pytest.mark.parametrize("raw", [False, True])
def test_cold_then_warm_preserves_bytes_and_defers_pruning(tts, monkeypatch, engine, raw):
    body = {"engine": engine, "text": " Olá, cache. ", "cache_key": "c" * 64,
            "voice": "fixture-voice", "language": "pt-BR", "rate": "15%", "pitch": "-2Hz"}
    original = copy.deepcopy(body)
    pruned = []
    monkeypatch.setattr(tts.handler, "_prune_tts_cache", lambda **kw: pruned.append(kw))
    cold = tts.handler._task_tts_agent_synthesize(body, raw_response=raw)
    assert cold["ok"] and not cold["cache_hit"] and len(tts.calls) == 1
    assert pruned == [] and len(tts.executor.jobs) == 1
    warm = tts.handler._task_tts_agent_synthesize(body, raw_response=raw)
    assert warm["ok"] and warm["cache_hit"] and len(tts.calls) == 1
    assert warm["selected_engine"] == cold["selected_engine"] == engine
    assert warm["cache_key"] == cold["cache_key"]
    assert audio(warm) == audio(cold) == ("audio:" + engine).encode()
    assert warm["sha256"] == cold["sha256"] == hashlib.sha256(audio(warm)).hexdigest()
    assert warm["worker_synth_ms"] == 0 and warm["worker_version"] == tts.worker.PHONE_WORKER_VERSION
    assert ("_raw_audio" in warm) == raw and ("data_b64" in warm) != raw
    assert tts.worker._TTS_AGENT_ACTIVE == 0 and tts.worker._TTS_AGENT_TOTAL == 2
    assert tts.worker._TTS_AGENT_FAILED == 0 and body == original
    tts.executor.drain()
    assert len(pruned) == 1 and pruned[0]["protected"].is_file()
    assert not tts.worker._TTS_CACHE_MAINTENANCE_RUNNING


@pytest.mark.parametrize(("alias", "canonical"), [("google", "gtts"), ("google_tts", "gtts"),
    ("googlecloud", "gtts"), ("google-cloud", "gtts"), ("gcloud", "gtts"), ("edge_tts", "edge"),
    ("android", "android_native"), ("android_tts", "android_native"), ("native", "android_native"),
    ("native_android", "android_native"), ("android-native", "android_native"), ("kasane_teto", "teto")])
def test_primary_engine_alias_keeps_inputs_and_provided_cache(tts, monkeypatch, alias, canonical):
    body = {"engine": alias, "text": "Teste.", "cache_key": "primary-cache-key",
            "voice": "primary", "language": "pt-BR", "rate": "1", "pitch": "1",
            "fallback_voice": "fallback", "fallback_language": "en", "fallback_rate": "2", "fallback_pitch": "2"}
    original = copy.deepcopy(body)
    delegated = []
    monkeypatch.setattr(tts.handler, "_synthesize_standard_tts_bytes",
                        lambda payload, **kw: delegated.append((payload, kw)) or {"ok": True})
    assert tts.handler._task_tts_agent_synthesize(body)["ok"]
    assert len(delegated) == 1 and delegated[0][0] == original
    assert delegated[0][1]["engine"] == canonical and body == original


def test_real_fallback_changes_inputs_and_key_once_without_double_counting(tts, monkeypatch):
    body = {"engine": "edge", "fallback_engine": "gtts", "text": "Fallback.", "cache_key": "e" * 64,
            "language": "pt", "fallback_language": "en", "voice": "primary", "fallback_voice": "fallback"}
    original = copy.deepcopy(body)
    attempts = []

    def attempt(payload, **kwargs):
        attempts.append((dict(payload), kwargs))
        if kwargs["engine"] == "edge":
            raise RuntimeError("controlled provider failure")
        return {"ok": True, "selected_engine": kwargs["engine"]}

    monkeypatch.setattr(tts.handler, "_synthesize_standard_tts_bytes", attempt)
    result = tts.handler._task_tts_agent_synthesize(body)
    assert result["selected_engine"] == "gtts" and len(attempts) == 2
    assert attempts[0][0] == body and "cache_key" not in attempts[1][0]
    assert attempts[1][0]["language"] == "en" and attempts[1][0]["voice"] == "fallback"
    assert body == original and tts.worker._TTS_AGENT_ACTIVE == 0
    assert tts.worker._TTS_AGENT_TOTAL == 1 and tts.worker._TTS_AGENT_FAILED == 0


@pytest.mark.parametrize(("mode", "hit", "store"), [("prefer", True, False), ("only", True, False),
    ("no_store", True, False), ("refresh", False, True), ("bypass", False, False),
    ("off", False, False), ("false", False, False), ("disabled", False, False), ("none", False, False)])
def test_cache_modes_keep_existing_read_write_contract(tts, mode, hit, store):
    tts.root.mkdir()
    path = tts.root / ("a" * 64 + ".mp3")
    path.write_bytes(b"original-cache")
    result = tts.handler._task_tts_agent_synthesize({"engine": "gtts", "text": "Modo.", "cache_key": "a" * 64, "cache_mode": mode})
    assert result["cache_hit"] == hit and len(tts.calls) == int(not hit)
    assert path.read_bytes() == (b"audio:gtts" if store else b"original-cache")
    assert len(tts.executor.jobs) == int(store)


@pytest.mark.parametrize("flag", ["PHONE_WORKER_TTS_AGENT_CACHE_ENABLED", "PHONE_WORKER_TTS_AGENT_STANDARD_CACHE_ENABLED"])
def test_disabled_cache_does_not_compute_key_or_touch_files(tts, monkeypatch, flag):
    monkeypatch.setenv(flag, "false")
    monkeypatch.setattr(tts.handler, "_tts_agent_standard_cache_key", lambda *a, **kw: pytest.fail("disabled cache key"))
    result = tts.handler._task_tts_agent_synthesize({"engine": "gtts", "text": "Sem cache."})
    assert not result["cache_hit"] and not tts.root.exists() and not tts.executor.jobs
    assert len(tts.calls) == 1


@pytest.mark.parametrize("failure", [FileNotFoundError, PermissionError])
def test_cache_read_failure_keeps_requested_provider(tts, monkeypatch, failure):
    tts.root.mkdir()
    path = tts.root / ("f" * 64 + ".mp3")
    path.write_bytes(b"stale-cache")
    original = Path.read_bytes

    def read(p):
        if p == path:
            raise failure("controlled cache IO failure")
        return original(p)

    monkeypatch.setattr(Path, "read_bytes", read)
    result = tts.handler._task_tts_agent_synthesize({"engine": "edge", "text": "Cache indisponível.", "cache_key": "f" * 64})
    assert result["ok"] and result["selected_engine"] == "edge" and not result["cache_hit"]
    assert len(tts.calls) == 1 and tts.calls[0]["engine"] == "edge"


@pytest.mark.parametrize("stage", ["write", "replace"])
def test_direct_cache_store_failure_preserves_destination_and_cleans_temporary(tts, monkeypatch, stage):
    tts.root.mkdir()
    target = tts.root / ("d" * 64 + ".mp3")
    target.write_bytes(b"old-audio")
    original = Path.write_bytes

    def write(path, data):
        if ".tmp-" in path.name:
            original(path, b"partial")
            raise OSError("controlled write failure")
        return original(path, data)

    if stage == "write":
        monkeypatch.setattr(Path, "write_bytes", write)
    else:
        local_os = SimpleNamespace(**vars(os))
        local_os.replace = lambda *a: (_ for _ in ()).throw(OSError("controlled replace failure"))
        monkeypatch.setattr(tts.worker, "os", local_os)
    with pytest.raises(OSError):
        tts.handler._task_tts_cache_store({"cache_key": "d" * 64, "data_b64": base64.b64encode(b"new-audio").decode()})
    assert target.read_bytes() == b"old-audio" and list(tts.root.iterdir()) == [target]
    assert not tts.executor.jobs


def test_failed_synthesis_and_queue_rejection_keep_counters_balanced(tts, monkeypatch):
    monkeypatch.setattr(tts.handler, "_synthesize_standard_tts_bytes", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("failed")))
    with pytest.raises(RuntimeError, match="failed"):
        tts.handler._task_tts_agent_synthesize({"text": "Erro."})
    assert tts.worker._TTS_AGENT_ACTIVE == 0 and tts.worker._TTS_AGENT_TOTAL == 1
    assert tts.worker._TTS_AGENT_FAILED == 1
    tts.worker._TTS_AGENT_ACTIVE = tts.worker._tts_agent_queue_limit()
    with pytest.raises(RuntimeError, match="fila local cheia"):
        tts.handler._task_tts_agent_synthesize({"text": "Fila."})
    assert tts.worker._TTS_AGENT_ACTIVE == tts.worker._tts_agent_queue_limit()
    assert tts.worker._TTS_AGENT_TOTAL == 1 and tts.worker._TTS_AGENT_FAILED == 1


def test_maintenance_coalesces_pending_work_and_survives_callback_failure(tts):
    worker = tts.worker
    called = []
    def category(value):
        called.append(value)
    def broken():
        raise ValueError("controlled cleanup failure")
    assert worker._submit_tts_cache_maintenance(category, "obsolete")
    assert worker._submit_tts_cache_maintenance(category, "latest")
    assert worker._submit_tts_cache_maintenance(broken)
    assert len(tts.executor.jobs) == 1 and not called
    tts.executor.drain()
    assert called == ["latest"] and not worker._TTS_CACHE_MAINTENANCE_RUNNING
    assert not worker._TTS_CACHE_MAINTENANCE_PENDING


def test_rejected_maintenance_submission_allows_later_retry(tts, monkeypatch):
    worker = tts.worker
    rejected = SimpleNamespace(submit=lambda *a: (_ for _ in ()).throw(RuntimeError("shutdown")))
    monkeypatch.setattr(worker, "_TTS_CACHE_MAINTENANCE_EXECUTOR", rejected)
    called = []
    assert not worker._submit_tts_cache_maintenance(called.append, "discarded")
    assert not worker._TTS_CACHE_MAINTENANCE_RUNNING and not worker._TTS_CACHE_MAINTENANCE_PENDING
    monkeypatch.setattr(worker, "_TTS_CACHE_MAINTENANCE_EXECUTOR", tts.executor)
    assert worker._submit_tts_cache_maintenance(called.append, "retry")
    tts.executor.drain()
    assert called == ["retry"]
