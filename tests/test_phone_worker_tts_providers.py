"""Provider contracts through the real facade, without external assets or services."""
from concurrent.futures import ThreadPoolExecutor
import asyncio
import builtins
import hashlib
import io
import sys
import threading
from types import SimpleNamespace

import pytest

from test_phone_worker_tts_lifecycle import audio, tts  # noqa: F401


def standard(tts, engine="teto", *, body=None, timeout=7, limit=1024, raw=False):
    return tts.handler._synthesize_standard_tts_bytes(
        {"text": " teto ", "cache_mode": "bypass", **(body or {})}, engine=engine,
        roles=["cache-worker"], capabilities=["tts-synth"], logs=[], started=100.0,
        max_audio_bytes=limit, timeout=timeout, raw_response=raw)


@pytest.mark.parametrize("raw", [False, True])
def test_teto_parameters_metadata_and_exact_audio_limit(tts, monkeypatch, raw):
    captured = []
    rendered = {"audio": bytearray(b"a" * 1024), "audio_format": " WAV ",
                "voicebank": "Test Teto", "voicebank_fingerprint": "bank-sha",
                "rendered_phonemes": 3, "missing_phonemes": ["x"], "private": "not projected"}

    def synthesize(text, **kwargs):
        assert tts.worker._HEAVY_RESOURCE_LOCK.locked()
        captured.append((text, kwargs))
        return rendered

    monkeypatch.setattr(tts.worker, "_get_teto_renderer", lambda: SimpleNamespace(synthesize=synthesize))
    result = standard(tts, raw=raw)
    assert captured == [("teto", {"timeout_seconds": 7.0, "max_audio_bytes": 1024})]
    assert not tts.worker._HEAVY_RESOURCE_LOCK.locked()
    assert audio(result) == b"a" * 1024 and result["audio_format"] == "wav"
    assert result["sha256"] == hashlib.sha256(audio(result)).hexdigest()
    assert result["teto_voicebank"] == "Test Teto" and result["teto_fingerprint"] == "bank-sha"
    assert result["teto_rendered_phonemes"] == 3 and result["teto_missing_phonemes"] == ["x"]
    assert result["timing_ms"]["teto_render"] == 0
    assert result["logs"] == ["teto voicebank=Test Teto rendered=3 missing=1"]
    assert "private" not in result and "audio" not in rendered
    assert not tts.executor.jobs and not tts.root.exists()


def test_teto_busy_lock_rejects_before_renderer_load(tts, monkeypatch):
    monkeypatch.setattr(tts.worker, "_get_teto_renderer", lambda: pytest.fail("busy renderer loaded"))
    with tts.worker._HEAVY_RESOURCE_LOCK:
        with pytest.raises(RuntimeError, match="recurso pesado ocupado por build ou manutenção"):
            standard(tts)
        assert tts.worker._HEAVY_RESOURCE_LOCK.locked()


@pytest.mark.parametrize("stage", ["load", "render", "normalize"])
def test_teto_failure_releases_shared_lock(tts, monkeypatch, stage):
    error = ValueError("controlled " + stage)

    def fail(*args, **kwargs):
        raise error

    if stage == "load":
        monkeypatch.setattr(tts.worker, "_get_teto_renderer", fail)
    elif stage == "render":
        monkeypatch.setattr(tts.worker, "_get_teto_renderer", lambda: SimpleNamespace(synthesize=fail))
    else:
        monkeypatch.setattr(tts.handler, "_normalize_tts_cache_format", fail)
    with pytest.raises(ValueError) as caught:
        standard(tts)
    assert caught.value is error and not tts.worker._HEAVY_RESOURCE_LOCK.locked()
    assert not tts.executor.jobs and not tts.root.exists()


def test_concurrent_teto_uses_one_nonblocking_heavy_lock(tts, monkeypatch):
    entered, finish = threading.Event(), threading.Event()

    def synthesize(*args, **kwargs):
        entered.set()
        assert finish.wait(3), "test did not release renderer"
        return {"audio": b"first"}

    monkeypatch.setattr(tts.worker, "_get_teto_renderer", lambda: SimpleNamespace(synthesize=synthesize))
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(standard, tts)
        try:
            assert entered.wait(3)
            with pytest.raises(RuntimeError, match="recurso pesado ocupado"):
                standard(tts)
        finally:
            finish.set()
        assert audio(first.result(timeout=3)) == b"first"
    assert not tts.worker._HEAVY_RESOURCE_LOCK.locked()


@pytest.mark.parametrize("data", [None, b"", b"x" * 1025])
def test_teto_rejects_empty_or_oversized_audio_before_publication(tts, monkeypatch, data):
    monkeypatch.setattr(tts.worker, "_get_teto_renderer",
        lambda: SimpleNamespace(synthesize=lambda *a, **kw: {"audio": data}))
    with pytest.raises(RuntimeError, match="áudio|grande demais"):
        standard(tts, body={"cache_mode": "refresh"})
    assert not tts.worker._HEAVY_RESOURCE_LOCK.locked()
    assert not tts.executor.jobs and not tts.root.exists()


def test_teto_cache_hit_bypasses_busy_renderer(tts, monkeypatch):
    body = {"text": "cached", "cache_mode": "prefer", "cache_key": "a" * 64}
    cold = standard(tts, body=body)
    monkeypatch.setattr(tts.worker, "_get_teto_renderer", lambda: pytest.fail("cache hit loaded renderer"))
    with tts.worker._HEAVY_RESOURCE_LOCK:
        warm = standard(tts, body=body)
    assert warm["cache_hit"] and audio(warm) == audio(cold)
    assert len(tts.calls) == 1 and len(tts.executor.jobs) == 1


def test_warm_teto_adapter_observes_rebound_renderer_lock_clock_and_formatter(tts, monkeypatch):
    standard(tts)
    lock = threading.Lock()
    clock = iter([100.010, 100.027, 100.030])

    def synthesize(*args, **kwargs):
        assert lock.locked()
        return {"audio": b"rebound", "audio_format": "wav"}

    monkeypatch.setattr(tts.worker, "_HEAVY_RESOURCE_LOCK", lock)
    monkeypatch.setattr(tts.worker, "_get_teto_renderer", lambda: SimpleNamespace(synthesize=synthesize))
    monkeypatch.setattr(tts.worker, "time", SimpleNamespace(monotonic=lambda: next(clock)))
    monkeypatch.setattr(tts.handler, "_normalize_tts_cache_format", lambda value: "ogg")
    result = standard(tts)
    assert audio(result) == b"rebound" and result["audio_format"] == "ogg"
    assert result["timing_ms"]["teto_render"] == 17 and result["worker_total_ms"] == 30
    assert not lock.locked()


def test_teto_singleton_is_lazy_shared_and_uses_live_guard_inputs(tts, monkeypatch):
    created = []

    def renderer(**kwargs):
        created.append(kwargs)
        return SimpleNamespace(**kwargs)

    monkeypatch.setitem(sys.modules, "teto_renderer", SimpleNamespace(TetoRenderer=renderer))
    assert tts.worker._TETO_RENDERER is None and not created
    with ThreadPoolExecutor(max_workers=8) as pool:
        renderers = list(pool.map(lambda _: tts.get_teto_renderer(), range(32)))
    assert len(created) == 1 and all(r is renderers[0] for r in renderers)
    assert created[0]["resource_guard"] is tts.worker._teto_resource_snapshot
    monkeypatch.setattr(tts.worker, "_safe_telemetry", lambda *a: {})
    monkeypatch.setattr(tts.worker, "_core_job_runtime_snapshot", lambda: {})
    monkeypatch.setattr(tts.worker, "_available_memory_mb", lambda: 1500)
    assert renderers[0].resource_guard()["ok"]
    monkeypatch.setattr(tts.worker, "_available_memory_mb", lambda: 200)
    assert not renderers[0].resource_guard()["ok"]
    monkeypatch.setenv("PHONE_WORKER_TETO_MIN_FREE_MEMORY_MB", "128")
    assert renderers[0].resource_guard()["ok"]
    assert tts.worker._TETO_RENDERER_ERROR == ""


@pytest.mark.parametrize("failure", ["import", "construct"])
def test_teto_failed_initialization_retries_and_clears_error(tts, monkeypatch, failure):
    def fail(**kwargs):
        raise ValueError("constructor failed")

    monkeypatch.setitem(sys.modules, "teto_renderer",
        None if failure == "import" else SimpleNamespace(TetoRenderer=fail))
    with pytest.raises(RuntimeError, match="renderer Teto indisponível") as caught:
        tts.get_teto_renderer()
    assert caught.value.__cause__ is not None
    assert tts.worker._TETO_RENDERER is None and tts.worker._TETO_RENDERER_ERROR
    recovered = object()
    monkeypatch.setitem(sys.modules, "teto_renderer", SimpleNamespace(TetoRenderer=lambda **kw: recovered))
    assert tts.get_teto_renderer() is recovered and tts.worker._TETO_RENDERER_ERROR == ""


def test_disabled_teto_status_does_not_load_renderer_or_probe_resources(tts, monkeypatch):
    monkeypatch.setattr(tts.worker, "_get_teto_renderer", lambda: pytest.fail("disabled renderer loaded"))
    monkeypatch.setattr(tts.worker, "_teto_resource_snapshot", lambda: pytest.fail("disabled resource probe"))
    result = tts.teto_status(force=True)
    assert not result["enabled"] and not result["ready"] and not result["available"]
    assert result["last_error"] == "PHONE_WORKER_TETO_ENABLED=false"
    assert result["resources"] == {"ok": False, "reason": "engine desativada"}


@pytest.mark.parametrize("failed", [False, True])
def test_enabled_teto_status_preserves_force_and_reports_resources_on_failure(tts, monkeypatch, failed):
    monkeypatch.setenv("PHONE_WORKER_TETO_ENABLED", "true")
    called = []

    def status(**kwargs):
        called.append(kwargs)
        if failed:
            raise ValueError("no assets")
        return {"ready": True, "voicebank": "fixture"}

    monkeypatch.setattr(tts.worker, "_get_teto_renderer", lambda: SimpleNamespace(status=status))
    monkeypatch.setattr(tts.worker, "_teto_resource_snapshot", lambda: {"ok": False, "reason": "busy"})
    result = tts.teto_status(force=True)
    assert called == [{"force": True}] and result["enabled"]
    assert result["ready"] != failed and result["resources"] == {"ok": False, "reason": "busy"}
    if failed:
        assert result["last_error"] == "ValueError: no assets"


@pytest.fixture
def resource(tts, monkeypatch):
    state = {"memory": 1200, "battery": {"level": 25, "temperature_c": 43}, "job": {}}
    monkeypatch.setattr(tts.worker, "_available_memory_mb", lambda: state["memory"])
    monkeypatch.setattr(tts.worker, "_safe_telemetry", lambda *a: state["battery"])
    monkeypatch.setattr(tts.worker, "_core_job_runtime_snapshot", lambda: state["job"])
    return state


@pytest.mark.parametrize("job", ["apk_build_debug", "apk_build", "worker_update", "self_update",
    "boot_repair", "audio_convert", "media_convert", "maintenance_heavy"])
def test_teto_resource_guard_blocks_every_heavy_job(tts, resource, job):
    resource["job"] = {"active_type": " " + job.upper() + " "}
    result = tts.worker._teto_resource_snapshot()
    assert not result["ok"] and result["reason"] == "tarefa pesada ativa: " + job
    assert result["active_heavy_job"] == job


@pytest.mark.parametrize(("memory", "battery", "ok"), [
    (1200, {"level": 25, "temperature_c": 43}, True),
    (1199, {"level": 25}, False), (None, {}, True), (None, None, True),
    (1200, {"level": 0}, False), (1200, {"temperature_c": 43.1}, False),
    (1200, {"level": "unknown", "temperature_c": "unknown"}, True),
    (1200, {"level": 0, "charging": True}, True),
    (1200, {"level": 0, "status": " FULL "}, True),
    (1200, {"level": 0, "status": "charging"}, True),
    (1200, {"level": 0, "status": "discharging"}, False),
])
def test_teto_resource_boundaries_and_missing_telemetry(tts, resource, memory, battery, ok):
    resource.update(memory=memory, battery=battery, job={"active_type": "light_task"})
    result = tts.worker._teto_resource_snapshot()
    assert result["ok"] == ok and result["memory_available_mb"] == memory
    assert result["minimum_memory_mb"] == 1200


def test_teto_guard_accumulates_reasons_in_order_and_reads_env_again(tts, resource, monkeypatch):
    resource.update(memory=127, battery={"level": 0, "temperature_c": 44, "charging": True})
    monkeypatch.setenv("PHONE_WORKER_TETO_ALLOW_LOW_BATTERY_WHEN_CHARGING", "false")
    with tts.worker._APK_BUILD_THREAD_LOCK:
        result = tts.worker._teto_resource_snapshot()
    assert result["reason"] == ("tarefa pesada ativa: apk_build; memória disponível baixa: 127 MB < 1200 MB; "
        "bateria aquecida: 44.0 °C > 43.0 °C; bateria baixa: 0% < 25%")
    monkeypatch.setenv("PHONE_WORKER_TETO_MIN_FREE_MEMORY_MB", "0")
    monkeypatch.setenv("PHONE_WORKER_TETO_MAX_BATTERY_TEMP_C", "0")
    monkeypatch.setenv("PHONE_WORKER_TETO_MIN_BATTERY_PERCENT", "-1")
    resource.update(memory=128, battery={"level": 0, "temperature_c": 30})
    assert tts.worker._teto_resource_snapshot()["ok"]


@pytest.fixture
def fallback(tts, monkeypatch):
    state = SimpleNamespace(calls=[], chunks=[{"type": "audio", "data": b"fallback"}],
        data=b"fallback", error=None, fail_stage="", closed=False)
    monkeypatch.setattr(tts.worker, "_tts_transport_module", lambda: None)

    class Edge:
        def __init__(self, **kwargs):
            state.calls.append({"provider": "edge", **kwargs})
            if state.fail_stage == "construct":
                raise state.error

        async def stream(self):
            try:
                for chunk in state.chunks:
                    yield chunk
                if state.fail_stage == "stream":
                    raise state.error
                if state.fail_stage == "timeout":
                    await asyncio.sleep(60)
            finally:
                state.closed = True

    class Gtts:
        def __init__(self, **kwargs):
            state.calls.append({"provider": "gtts", **kwargs})
            if state.fail_stage == "construct":
                raise state.error

        def write_to_fp(self, buffer):
            buffer.write(state.data)
            if state.fail_stage == "write":
                raise state.error

    monkeypatch.setitem(sys.modules, "edge_tts", SimpleNamespace(Communicate=Edge))
    monkeypatch.setitem(sys.modules, "gtts", SimpleNamespace(gTTS=Gtts))
    return state


@pytest.mark.parametrize("raw", [False, True])
def test_edge_fallback_concatenates_only_audio_and_preserves_parameters(tts, fallback, raw):
    fallback.chunks = [{"type": "WordBoundary", "data": b"ignored"}, {},
        {"type": "audio", "data": b""}, {"type": "audio", "data": b"a" * 512},
        {"type": "audio", "data": b"b" * 512}]
    result = standard(tts, "edge", raw=raw,
        body={"voice": " pt-BR-AntonioNeural ", "rate": "15%", "pitch": "-2Hz"})
    assert fallback.calls == [{"provider": "edge", "text": "teto", "voice": "pt-BR-AntonioNeural",
                               "rate": "+15%", "pitch": "-2Hz"}]
    assert audio(result) == b"a" * 512 + b"b" * 512 and result["audio_format"] == "mp3"
    assert result["sha256"] == hashlib.sha256(audio(result)).hexdigest()
    assert result["logs"] == ["edge voice=pt-BR-AntonioNeural rate=+15% pitch=-2Hz"]
    assert fallback.closed and not tts.executor.jobs


@pytest.mark.parametrize(("body", "voice"), [({}, "pt-BR-FranciscaNeural"),
    ({"fallback_voice": " en-US-AriaNeural "}, "en-US-AriaNeural"),
    ({"voice": " ", "fallback_voice": "en-US-AriaNeural"}, "pt-BR-FranciscaNeural")])
def test_edge_fallback_voice_defaults_match_received_behavior(tts, fallback, body, voice):
    standard(tts, "edge", body=body)
    assert fallback.calls[0] == {"provider": "edge", "text": "teto", "voice": voice,
                                 "rate": "+0%", "pitch": "+0Hz"}


def test_edge_timeout_cancels_and_closes_partial_stream_without_caching(tts, fallback):
    fallback.fail_stage = "timeout"
    with pytest.raises(TimeoutError):
        standard(tts, "edge", timeout=0.01, body={"cache_mode": "refresh"})
    assert fallback.closed and len(fallback.calls) == 1
    assert not tts.executor.jobs and not tts.root.exists()


@pytest.mark.parametrize(("engine", "stage"), [("edge", "construct"), ("edge", "stream"),
    ("gtts", "construct"), ("gtts", "write")])
def test_fallback_provider_error_propagates_without_publishing_partial_audio(tts, fallback, engine, stage):
    fallback.fail_stage, fallback.error = stage, ValueError("partial provider failure")
    with pytest.raises(ValueError) as caught:
        standard(tts, engine, body={"cache_mode": "refresh"})
    assert caught.value is fallback.error
    assert not tts.executor.jobs and not tts.root.exists()
    if stage == "stream":
        assert fallback.closed


@pytest.mark.parametrize("engine", ["edge", "gtts"])
@pytest.mark.parametrize("size", [0, 1025])
def test_fallback_audio_validation_runs_before_publication(tts, fallback, engine, size):
    fallback.data = b"a" * size
    fallback.chunks = [{"type": "audio", "data": fallback.data}]
    with pytest.raises(RuntimeError, match="engine não gerou áudio|áudio grande demais: 1025 bytes"):
        standard(tts, engine, body={"cache_mode": "refresh"})
    assert not tts.executor.jobs and not tts.root.exists()


@pytest.mark.parametrize("raw", [False, True])
@pytest.mark.parametrize(("timeout", "expected"), [(2, (2, 2)), (5, (3.5, 5)), (12, (3.5, 8.0))])
def test_gtts_fallback_timeouts_language_and_exact_audio_limit(tts, fallback, raw, timeout, expected):
    fallback.data = b"g" * 1024
    result = standard(tts, "gtts", raw=raw, timeout=timeout,
        body={"language": "pt_BR", "fallback_language": "en", "tld": "com.br"})
    # The inline fallback has never forwarded tld. The shared transport still does.
    assert fallback.calls == [{"provider": "gtts", "text": "teto", "lang": "pt", "timeout": expected}]
    assert audio(result) == fallback.data and result["audio_format"] == "mp3"
    assert result["logs"] == ["gtts language=pt"]


@pytest.mark.parametrize(("body", "language"), [({}, "pt"),
    ({"fallback_language": "EN_us"}, "en-us"), ({"language": "FR"}, "fr")])
def test_gtts_fallback_language_defaults(tts, fallback, body, language):
    standard(tts, "gtts", body=body)
    assert fallback.calls[0]["lang"] == language


@pytest.mark.parametrize("engine", ["edge", "gtts"])
@pytest.mark.parametrize("error_type", [ModuleNotFoundError, RuntimeError])
def test_fallback_import_error_is_bounded_chained_and_can_retry(tts, fallback, monkeypatch, engine, error_type):
    original = builtins.__import__
    provider = "edge_tts" if engine == "edge" else "gtts"
    error = error_type("x" * 400)

    def import_provider(name, *args, **kwargs):
        if name == provider:
            raise error
        return original(name, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(builtins, "__import__", import_provider)
        with pytest.raises(RuntimeError, match="não instalado no worker") as caught:
            standard(tts, engine)
    assert caught.value.__cause__ is error and len(str(caught.value)) < 220
    assert not fallback.calls
    assert audio(standard(tts, engine)) == b"fallback" and len(fallback.calls) == 1


@pytest.mark.parametrize("engine", ["edge", "gtts"])
def test_shared_transport_precedes_lazy_fallback_and_preserves_all_arguments(tts, monkeypatch, engine):
    monkeypatch.setitem(sys.modules, "edge_tts", None)
    monkeypatch.setitem(sys.modules, "gtts", None)
    result = standard(tts, engine, body={"voice": " raw voice ", "language": "EN_us",
        "rate": "3", "pitch": "-5", "tld": "com.br"})
    assert tts.calls == [{"engine": engine, "text": "teto", "voice": " raw voice ",
        "language": "en-us", "rate": "+3%", "pitch": "-5Hz", "tld": "com.br", "timeout": 7, "max_bytes": 1024}]
    assert audio(result) == b"audio:" + engine.encode()
    assert result["logs"] == [engine + " transporte compartilhado"]


@pytest.mark.parametrize("engine", ["edge", "gtts"])
def test_fallback_cache_hit_does_not_import_provider_again(tts, fallback, monkeypatch, engine):
    body = {"cache_mode": "prefer", "cache_key": "b" * 64}
    cold = standard(tts, engine, body=body)
    monkeypatch.setitem(sys.modules, "edge_tts", None)
    monkeypatch.setitem(sys.modules, "gtts", None)
    warm = standard(tts, engine, body=body)
    assert warm["cache_hit"] and audio(warm) == audio(cold) == b"fallback"
    assert len(fallback.calls) == 1 and len(tts.executor.jobs) == 1


def test_failed_edge_fallback_allows_gtts_with_balanced_admission_and_fallback_inputs(tts, fallback, monkeypatch):
    fallback.fail_stage, fallback.error = "stream", ValueError("edge failed")
    tts.deps.update(teto_tts=False, android_native_tts=False)
    result = tts.handler._task_tts_agent_synthesize({"engine": "edge", "text": "fallback",
        "language": "pt", "fallback_language": "en", "cache_mode": "bypass"})
    assert result["selected_engine"] == "gtts" and audio(result) == b"fallback"
    assert [call["provider"] for call in fallback.calls] == ["edge", "gtts"]
    assert fallback.calls[1]["lang"] == "en" and fallback.closed
    assert tts.worker._TTS_AGENT_TOTAL == 1 and tts.worker._TTS_AGENT_ACTIVE == 0
    assert tts.worker._TTS_AGENT_FAILED == 0


@pytest.mark.parametrize("engine", ["edge", "gtts"])
def test_warm_fallback_adapter_observes_rebound_codecs_and_asyncio(tts, fallback, monkeypatch, engine):
    standard(tts, engine)
    fallback.calls.clear()
    called = []

    def buffer():
        called.append("buffer")
        return io.BytesIO()

    def wait_for(coro, *, timeout):
        called.append(("timeout", timeout))
        return asyncio.wait_for(coro, timeout=timeout)

    def run(coro):
        called.append("run")
        return asyncio.run(coro)

    monkeypatch.setattr(tts.worker, "io", SimpleNamespace(BytesIO=buffer))
    monkeypatch.setattr(tts.worker, "asyncio", SimpleNamespace(run=run, wait_for=wait_for))
    monkeypatch.setattr(tts.handler, "_normalize_tts_edge_rate", lambda value: "+77%")
    monkeypatch.setattr(tts.handler, "_normalize_tts_edge_pitch", lambda value: "-9Hz")
    monkeypatch.setattr(tts.handler, "_normalize_tts_gtts_language", lambda value: "it")
    assert audio(standard(tts, engine)) == b"fallback"
    if engine == "edge":
        assert called == [("timeout", 7), "run", "buffer"]
        assert fallback.calls[0]["rate"] == "+77%" and fallback.calls[0]["pitch"] == "-9Hz"
    else:
        assert called == ["buffer"] and fallback.calls[0]["lang"] == "it"
