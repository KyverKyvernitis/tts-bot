"""WORLDLINE selection and audio identity through the real phone facade."""
import sys
from types import SimpleNamespace

import pytest

from test_phone_worker_tts_lifecycle import audio, tts  # noqa: F401


def install_renderers(monkeypatch, *, fail_worldline=False):
    created = []

    def create(backend):
        def renderer(**kwargs):
            created.append(backend)
            if backend == "worldline-r" and fail_worldline:
                raise RuntimeError("adaptador ausente")
            return SimpleNamespace(backend=backend, **kwargs)
        return renderer

    monkeypatch.setitem(sys.modules, "teto_renderer", SimpleNamespace(
        TetoRenderer=create("utau"), WorldlineRenderer=create("worldline-r")))
    return created


@pytest.mark.parametrize("setting", ("worldline-r", "worldline_r", " WORLDLINE-R "))
def test_native_backend_selects_only_worldline_and_reuses_instance(tts, monkeypatch, setting):
    created = install_renderers(monkeypatch)
    monkeypatch.setenv("PHONE_WORKER_TETO_BACKEND", setting)
    renderer = tts.get_teto_renderer()
    assert tts.worker._teto_backend() == renderer.backend == "worldline-r"
    assert renderer.resource_guard is tts.worker._teto_resource_snapshot
    assert tts.get_teto_renderer() is renderer and created == ["worldline-r"]


def test_backend_transition_drops_previous_renderer_and_cached_status(tts, monkeypatch):
    created = install_renderers(monkeypatch)
    old = tts.get_teto_renderer()
    monkeypatch.setenv("PHONE_WORKER_TETO_BACKEND", "worldline-r")
    native = tts.get_teto_renderer()
    assert native is not old
    monkeypatch.setenv("PHONE_WORKER_TETO_BACKEND", "voicepeak")
    migrated = tts.get_teto_renderer()
    assert migrated is not native and migrated is not old
    assert created == ["utau", "worldline-r", "utau"]


@pytest.mark.parametrize("setting", (
    "PHONE_WORKER_WORLDLINE_CONTAINER", "PHONE_WORKER_WORLDLINE_LIBRARY",
    "PHONE_WORKER_TETO_VOICEBANK_DIR", "PHONE_WORKER_TETO_ENGLISH_VOICEBANK_DIR",
    "PHONE_WORKER_TETO_VOICEBANK_MODE", "PHONE_WORKER_TETO_BASE_PITCH",
    "PHONE_WORKER_TETO_SPEECH_RATE", "PHONE_WORKER_TETO_VELOCITY",
    "PHONE_WORKER_TETO_MODULATION", "PHONE_WORKER_TETO_FLAGS",
    "PHONE_WORKER_TETO_MIN_ALIASES", "PHONE_WORKER_TETO_STATUS_CACHE_SECONDS",
))
def test_renderer_settings_recreate_instead_of_reusing_old_bank_or_runtime(tts, monkeypatch, setting):
    created = install_renderers(monkeypatch)
    monkeypatch.setenv("PHONE_WORKER_TETO_BACKEND", "worldline-r")
    before = tts.get_teto_renderer()
    monkeypatch.setenv(setting, "changed-value")
    after = tts.get_teto_renderer()
    assert after is not before and tts.get_teto_renderer() is after
    assert created == ["worldline-r", "worldline-r"]


def test_pairing_and_retired_provider_secrets_do_not_enter_renderer_identity(tts, monkeypatch):
    created = install_renderers(monkeypatch)
    monkeypatch.setenv("PHONE_WORKER_TETO_BACKEND", "worldline-r")
    renderer = tts.get_teto_renderer()
    identity = tts.worker._teto_renderer_config_fingerprint()
    for name in ("CORE_WORKER_PAIRING_TOKEN", "PHONE_WORKER_TOKEN",
                 "PHONE_WORKER_TETO_AUTH_TOKEN", "PHONE_WORKER_VOICEPEAK_COMMAND",
                 "PHONE_WORKER_VOICEPEAK_URL"):
        monkeypatch.setenv(name, "secret-or-retired-value")
    assert tts.worker._teto_renderer_config_fingerprint() == identity
    assert tts.get_teto_renderer() is renderer and created == ["worldline-r"]


def test_failed_native_constructor_never_reuses_utau_and_retries_when_module_arrives(tts, monkeypatch):
    created = install_renderers(monkeypatch, fail_worldline=True)
    old = tts.get_teto_renderer()
    monkeypatch.setenv("PHONE_WORKER_TETO_BACKEND", "worldline-r")
    with pytest.raises(RuntimeError, match="adaptador ausente"):
        tts.get_teto_renderer()
    assert tts.worker._TETO_RENDERER is None and created == ["utau", "worldline-r"]
    retried = install_renderers(monkeypatch)
    assert tts.get_teto_renderer() is not old
    assert retried == ["worldline-r"] and not tts.worker._TETO_RENDERER_ERROR


def test_worldline_and_utau_never_share_whole_phrase_cache_or_identity_metadata(tts, monkeypatch):
    rendered = []

    def synthesize(text, **kwargs):
        backend = tts.worker._teto_backend()
        rendered.append(backend)
        return {"audio": backend.encode(), "audio_format": "wav", "backend": backend,
                "renderer_version": "native-phrase-v1" if backend == "worldline-r" else "utau-v1"}

    monkeypatch.setattr(tts.worker, "_get_teto_renderer", lambda: SimpleNamespace(synthesize=synthesize))
    body = {"engine": "teto", "text": "Olá Teto.", "teto_pitch_semitones": -1.5}
    utau = tts.handler._task_tts_agent_synthesize(body)
    monkeypatch.setenv("PHONE_WORKER_TETO_BACKEND", "worldline-r")
    tts.deps["teto"].update(backend="worldline-r", renderer_version="native-phrase-v1")
    native = tts.handler._task_tts_agent_synthesize(body)
    warm = tts.handler._task_tts_agent_synthesize(body)
    assert not utau["cache_hit"] and not native["cache_hit"] and warm["cache_hit"]
    assert audio(utau) == b"utau" and audio(native) == audio(warm) == b"worldline-r"
    assert rendered == ["utau", "worldline-r"]
    assert warm["teto_backend"] == native["teto_backend"] == "worldline-r"
    assert warm["teto_renderer_version"] == native["teto_renderer_version"] == "native-phrase-v1"
    assert warm["teto_pitch_offset_semitones"] == -1.5
    monkeypatch.setenv("PHONE_WORKER_TETO_BACKEND", "utau")
    tts.deps["teto"].update(backend="utau", renderer_version="utau-v1")
    restored = tts.handler._task_tts_agent_synthesize(body)
    assert restored["cache_hit"] and audio(restored) == b"utau"


@pytest.mark.parametrize("setting", ("PHONE_WORKER_WORLDLINE_CONTAINER", "PHONE_WORKER_WORLDLINE_LIBRARY",
                                    "PHONE_WORKER_TETO_VOICEBANK_MODE", "PHONE_WORKER_TETO_SPEECH_RATE"))
def test_worldline_settings_change_cache_even_when_voicebank_fingerprint_matches(tts, monkeypatch, setting):
    monkeypatch.setenv("PHONE_WORKER_TETO_BACKEND", "worldline-r")
    body = {"engine": "teto", "text": "Teste da configuração."}
    first = tts.handler._task_tts_agent_synthesize(body)
    warm = tts.handler._task_tts_agent_synthesize(body)
    monkeypatch.setenv(setting, "changed-value")
    changed = tts.handler._task_tts_agent_synthesize(body)
    assert not first["cache_hit"] and warm["cache_hit"] and not changed["cache_hit"]
    assert first["cache_key"] != changed["cache_key"] and len(tts.calls) == 2


def test_worldline_renderer_revision_changes_cache_without_changing_bank(tts, monkeypatch):
    monkeypatch.setenv("PHONE_WORKER_TETO_BACKEND", "worldline-r")
    body = {"engine": "teto", "text": "Teste da revisão."}
    tts.deps["teto"]["renderer_version"] = "one"
    first = tts.handler._task_tts_agent_synthesize(body)
    tts.deps["teto"]["renderer_version"] = "two"
    changed = tts.handler._task_tts_agent_synthesize(body)
    assert not changed["cache_hit"] and changed["cache_key"] != first["cache_key"]


def test_control_status_preserves_runtime_and_does_not_claim_real_synthesis(tts, monkeypatch):
    monkeypatch.setenv("PHONE_WORKER_TETO_ENABLED", "true")
    monkeypatch.setenv("PHONE_WORKER_TETO_BACKEND", "worldline-r")
    forced = []
    monkeypatch.setattr(tts.worker, "_get_teto_renderer", lambda: SimpleNamespace(
        status=lambda **kwargs: forced.append(kwargs) or {
            "ready": True, "runtime_verified": True, "teto_synthesis_verified": False,
            "portuguese_speech_verified": False, "renderer_version": "native-phrase-v1"}))
    monkeypatch.setattr(tts.worker, "_teto_resource_snapshot", lambda: {"ok": True})
    state = tts.teto_status(force=True)
    assert state["backend"] == "worldline-r" and state["ready"]
    assert state["runtime_verified"] and not state["teto_synthesis_verified"]
    assert not state["portuguese_speech_verified"] and forced == [{"force": True}]


@pytest.mark.parametrize("key_seconds", (0.75, 2.5))
def test_native_cache_status_cost_is_deducted_from_remaining_request_deadline(tts, monkeypatch, key_seconds):
    monkeypatch.setenv("PHONE_WORKER_TETO_BACKEND", "worldline-r")
    clock = [100.0]
    tts.worker.time.monotonic = lambda: clock[0]
    original_key = tts.handler._tts_agent_standard_cache_key

    def key(body, **kwargs):
        result = original_key(body, **kwargs)
        clock[0] += key_seconds
        return result

    monkeypatch.setattr(tts.handler, "_tts_agent_standard_cache_key", key)
    body = {"engine": "teto", "text": "Teste de prazo.", "timeout_seconds": 2}
    if key_seconds < 2:
        assert tts.handler._task_tts_agent_synthesize(body)["ok"]
        assert tts.calls[0]["timeout_seconds"] == 2 - key_seconds
    else:
        with pytest.raises(TimeoutError, match="prazo total"):
            tts.handler._task_tts_agent_synthesize(body)
        assert not tts.calls


def test_native_unavailable_remains_teto_only_even_with_another_fallback(tts, monkeypatch):
    monkeypatch.setenv("PHONE_WORKER_TETO_BACKEND", "worldline-r")
    tts.deps["teto"].update(ready=False, backend="worldline-r", last_error="biblioteca ausente")
    monkeypatch.setattr(tts.worker, "_turbo_dependency_snapshot",
                        lambda: pytest.fail("generic provider discovery used for Teto"))
    with pytest.raises(RuntimeError, match="biblioteca ausente"):
        tts.handler._task_tts_agent_synthesize({"engine": "teto", "text": "Oi.", "fallback_engine": "gtts"})
    assert not tts.calls


def test_update_policy_accepts_both_phrase_adapter_modules(tts, monkeypatch, tmp_path):
    monkeypatch.setenv("PHONE_WORKER_RELEASE_DIR", str(tmp_path))
    for name in ("worldline.py", "worldline_native.py"):
        target, mode = tts.worker._safe_update_target_path("teto_renderer/" + name)
        assert target == tmp_path / "teto_renderer" / name and mode == 0o644


def test_optional_installer_files_do_not_change_runtime_identity(tts, monkeypatch, tmp_path):
    monkeypatch.setenv("PHONE_WORKER_RELEASE_DIR", str(tmp_path))
    for target in tts.worker._WORKER_UPDATE_TARGETS:
        if target not in tts.worker._PHONE_WORKER_SOURCE_HASH_EXCLUDED:
            path, _mode = tts.worker._safe_update_target_path(target)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("runtime module " + target)
    missing_optional = tts.worker._phone_worker_source_hash()
    assert len(missing_optional) == 64
    for target in ("scripts/validate-teto-assets.py", "teto_renderer/OPENUTAU_LICENSE.txt",
                   "bootstrap-phone-worker.sh"):
        path, _mode = tts.worker._safe_update_target_path(target)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("optional installation asset")
    assert tts.worker._phone_worker_source_hash() == missing_optional
