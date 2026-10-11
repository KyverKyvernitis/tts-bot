"""Two TTV characters retain separate assets, admission, controls and caches."""
from concurrent.futures import ThreadPoolExecutor
import copy
import os
import sys
from types import SimpleNamespace

import pytest

from test_phone_worker_tts_lifecycle import audio, tts  # noqa: F401


def voice_row(voice_id, *, ready=True):
    return {"id": voice_id, "ready": ready, "available": ready, "enabled": ready,
            "supports_personal_rate": True, "languages": ["pt-BR", "en-US"],
            "fingerprint": voice_id + "-bank", "backend": "utau",
            "language_fingerprints": {language: voice_id + "-" + language
                                      for language in ("pt-BR", "en-US")},
            "voicebank_profile": "english-cvvc", "renderer_version": voice_id + "-renderer"}


@pytest.fixture
def two_voices(tts, monkeypatch):
    rows = [voice_row(voice_id) for voice_id in ("kasane-teto", "hatsune-miku")]
    tts.deps["teto"].update({"ttv_voices": rows})
    calls = []

    def renderer(voice_id):
        def synthesize(text, **controls):
            calls.append({"voice_id": voice_id, "text": text, **controls})
            language = controls.get("reading_language", "pt-BR")
            payload = (voice_id + "|" + language + "|" + str(controls["pitch_offset_semitones"])
                       + "|" + str(controls.get("speech_rate", 1.0))).encode()
            return {"audio": payload, "audio_format": "wav", "voice_id": voice_id,
                    "voicebank": voice_id, "voicebank_fingerprint": voice_id + "-bank",
                    "pitch_offset_semitones": controls["pitch_offset_semitones"],
                    "speech_rate": controls.get("speech_rate", 1.0), "reading_language": language,
                    "voicebank_profile": "english-cvvc", "renderer_version": voice_id + "-renderer"}
        return SimpleNamespace(synthesize=synthesize)

    monkeypatch.setattr(tts.worker, "_get_teto_renderer", lambda: renderer("kasane-teto"))
    monkeypatch.setattr(tts.worker, "_get_miku_renderer", lambda: renderer("hatsune-miku"))
    return SimpleNamespace(**vars(tts), rows=rows, voice_calls=calls)


@pytest.mark.parametrize("raw", [False, True])
@pytest.mark.parametrize("language", ["pt-BR", "en-US"])
@pytest.mark.parametrize(("pitch", "rate"), [(0, 1.0), (-4, .5), (2, 1.25), (4, 1.5)])
def test_same_client_key_cannot_mix_characters_or_effective_controls(two_voices, raw, language, pitch, rate):
    state = two_voices
    base = {"engine": "teto", "text": "Hello, Miku.", "cache_key": "a" * 64,
            "ttv_pitch_semitones": pitch, "ttv_speech_rate": rate, "ttv_language": language}
    original = copy.deepcopy(base)
    results = {}
    for voice_id in ("kasane-teto", "hatsune-miku"):
        body = {**base, "ttv_voice_id": voice_id}
        cold = state.handler._task_tts_agent_synthesize(body, raw_response=raw)
        warm = state.handler._task_tts_agent_synthesize(body, raw_response=raw)
        assert not cold["cache_hit"] and warm["cache_hit"]
        assert audio(cold) == audio(warm)
        for result in (cold, warm):
            assert result["ttv_voice_id"] == voice_id
            assert result["ttv_pitch_semitones"] == pitch
            assert result["ttv_speech_rate"] == rate
            assert result["ttv_language"] == language
            assert result["selected_engine"] == "teto"
            assert result["teto_renderer_version"] == voice_id + "-renderer"
        results[voice_id] = warm
    assert results["kasane-teto"]["cache_key"] != results["hatsune-miku"]["cache_key"]
    assert audio(results["kasane-teto"]) != audio(results["hatsune-miku"])
    assert [call["voice_id"] for call in state.voice_calls] == ["kasane-teto", "hatsune-miku"]
    assert base == original


@pytest.mark.parametrize("voice_id", ["kasane-teto", "hatsune-miku"])
def test_unavailable_character_rejects_before_cache_even_when_other_is_ready(two_voices, monkeypatch, voice_id):
    state = two_voices
    selected = next(row for row in state.rows if row["id"] == voice_id)
    selected.update({"ready": False, "available": False, "last_error": "missing voice assets"})
    monkeypatch.setattr(state.handler, "_find_tts_cache_file", lambda *a: pytest.fail("unavailable cache read"))
    with pytest.raises(RuntimeError, match="missing voice assets"):
        state.handler._task_tts_agent_synthesize({"engine": "teto", "text": "Hello.", "ttv_voice_id": voice_id})
    assert not state.voice_calls


def test_missing_miku_advertisement_never_inherits_ready_teto(tts, monkeypatch):
    monkeypatch.setattr(tts.handler, "_find_tts_cache_file", lambda *a: pytest.fail("missing voice cache read"))
    with pytest.raises(RuntimeError, match="Miku"):
        tts.handler._task_tts_agent_synthesize({"engine": "teto", "text": "Hello.", "ttv_voice_id": "hatsune-miku"})
    assert not tts.calls


def test_wrong_native_character_is_rejected_before_cache_publication(two_voices, monkeypatch):
    state = two_voices
    monkeypatch.setattr(state.worker, "_get_miku_renderer", lambda: SimpleNamespace(
        synthesize=lambda *a, **kw: {"audio": b"wrong-character", "audio_format": "wav", "voice_id": "kasane-teto"}))
    monkeypatch.setattr(state.handler, "_store_tts_agent_standard_cache",
                        lambda **kw: pytest.fail("wrong character was cached"))
    with pytest.raises(RuntimeError, match="outra vocaloid"):
        state.handler._task_tts_agent_synthesize({"engine": "teto", "text": "Hello.", "ttv_voice_id": "hatsune-miku"})


def test_direct_voice_prebuild_forwards_miku_and_all_personal_controls(two_voices, monkeypatch):
    state = two_voices
    state.worker.time.perf_counter = lambda: 100.0
    monkeypatch.setattr(state.worker, "_voice_agent_prune_transfers", lambda: {})
    monkeypatch.setattr(state.worker, "_voice_agent_set_connection", lambda *a, **kw: dict(kw))
    monkeypatch.setattr(state.worker, "_music_agent_snapshot", lambda: {})
    monkeypatch.setattr(state.worker, "_tts_agent_snapshot", lambda: {})
    monkeypatch.setattr(state.worker, "_voice_agent_snapshot", lambda **kw: {})
    forwarded = []
    monkeypatch.setattr(state.handler, "_task_music_agent_proxy",
                        lambda body: forwarded.append(body) or {"ok": True, "engine": "teto"})
    body = {"guild_id": 1, "channel_id": 2, "engine": "teto", "text": "Hello, Miku.",
            "ttv_voice_id": "hatsune-miku", "ttv_language": "en-US",
            "ttv_pitch_semitones": 2.0, "ttv_speech_rate": 1.25}
    assert state.handler._task_voice_agent_play_tts(body)["ok"]
    assert len(state.voice_calls) == len(forwarded) == 1
    assert state.voice_calls[0]["voice_id"] == "hatsune-miku"
    for control in ("ttv_voice_id", "ttv_language", "ttv_pitch_semitones", "ttv_speech_rate"):
        assert forwarded[0][control] == body[control]
    assert forwarded[0]["prebuilt_audio"] and forwarded[0]["engine"] == "teto"


def test_only_changed_voice_and_language_fingerprint_invalidates_cache(two_voices):
    state = two_voices
    body = {"engine": "teto", "text": "Hello.", "ttv_voice_id": "hatsune-miku", "ttv_language": "en-US"}
    miku_en = state.handler._tts_agent_standard_cache_key(body, engine="teto")
    miku_pt = state.handler._tts_agent_standard_cache_key({**body, "ttv_language": "pt-BR"}, engine="teto")
    teto_en = state.handler._tts_agent_standard_cache_key({**body, "ttv_voice_id": "kasane-teto"}, engine="teto")
    state.rows[1]["language_fingerprints"]["en-US"] = "updated-miku-english"
    assert state.handler._tts_agent_standard_cache_key(body, engine="teto") != miku_en
    assert state.handler._tts_agent_standard_cache_key({**body, "ttv_language": "pt-BR"}, engine="teto") == miku_pt
    assert state.handler._tts_agent_standard_cache_key({**body, "ttv_voice_id": "kasane-teto"}, engine="teto") == teto_en


def test_miku_factory_is_concurrent_singleton_without_global_bank_mutation(tts, monkeypatch, tmp_path):
    monkeypatch.setenv("PHONE_WORKER_TETO_ENABLED", "false")
    monkeypatch.setenv("PHONE_WORKER_TETO_VOICEBANK_DIR", "/unchanged/teto")
    monkeypatch.setenv("PHONE_WORKER_TETO_ENGLISH_VOICEBANK_DIR", "/unchanged/teto-english")
    monkeypatch.setenv("PHONE_WORKER_TETO_BASE_PITCH", "F3")
    monkeypatch.setenv("PHONE_WORKER_TETO_FLAGS", "B100")
    monkeypatch.setenv("PHONE_WORKER_MIKU_ENABLED", "true")
    monkeypatch.setenv("PHONE_WORKER_MIKU_VOICEBANK_DIR", str(tmp_path / "miku"))
    monkeypatch.setenv("PHONE_WORKER_MIKU_RESAMPLER_COMMAND", "python /installed/.esper-utau/bin/resampler.py")
    before = dict(os.environ)
    created = []
    def construct(**kwargs):
        created.append(kwargs)
        return SimpleNamespace(**kwargs)
    monkeypatch.setitem(sys.modules, "teto_renderer", SimpleNamespace(TetoRenderer=construct))
    with ThreadPoolExecutor(max_workers=8) as pool:
        renderers = list(pool.map(lambda _: tts.worker._get_ttv_renderer("hatsune-miku"), range(32)))
    assert len(created) == 1 and all(renderer is renderers[0] for renderer in renderers)
    assert os.environ == before
    environment = created[0]["environment"]
    assert created[0]["voice_id"] == "hatsune-miku"
    assert environment["PHONE_WORKER_TETO_ENABLED"] == "true"
    assert environment["PHONE_WORKER_TETO_VOICEBANK_DIR"] == str(tmp_path / "miku")
    assert environment["PHONE_WORKER_TETO_ENGLISH_VOICEBANK_DIR"] == str(tmp_path / "miku")
    assert environment["PHONE_WORKER_TETO_VOICEBANK_MODE"] == "english"
    assert environment["PHONE_WORKER_TETO_ALIAS_SUFFIX"] == "_D4"
    assert environment["PHONE_WORKER_TETO_BASE_PITCH"] == "C4"
    assert environment["PHONE_WORKER_TETO_FLAGS"] == ""
    assert "CORE_WORKER_TOKEN" not in environment
    monkeypatch.setenv("PHONE_WORKER_MIKU_ALIAS_SUFFIX", "_A3")
    assert tts.worker._get_ttv_renderer("hatsune-miku") is not renderers[0]
    assert len(created) == 2
    assert os.environ["PHONE_WORKER_TETO_VOICEBANK_DIR"] == "/unchanged/teto"


def test_miku_empty_paths_do_not_use_teto_default_bank(tts, monkeypatch):
    monkeypatch.delenv("PHONE_WORKER_MIKU_VOICEBANK_DIR", raising=False)
    monkeypatch.delenv("PHONE_WORKER_MIKU_ENGLISH_VOICEBANK_DIR", raising=False)
    monkeypatch.setenv("PHONE_WORKER_TETO_ENGLISH_VOICEBANK_DIR", "/installed/teto")
    environment = tts.worker._miku_renderer_environment()
    assert environment["PHONE_WORKER_TETO_VOICEBANK_DIR"] == ""
    assert environment["PHONE_WORKER_TETO_ENGLISH_VOICEBANK_DIR"].endswith("/voicebanks/hatsune-miku-english")
    assert "/installed/teto" not in environment.values()


def test_teto_controls_cannot_change_miku_environment_or_factory_identity(tts, monkeypatch):
    created = []
    monkeypatch.setitem(sys.modules, "teto_renderer", SimpleNamespace(
        TetoRenderer=lambda **kw: created.append(kw) or SimpleNamespace(**kw)))
    before = tts.worker._miku_renderer_environment()
    renderer = tts.worker._get_miku_renderer()
    other_settings = {
        "BACKEND": "worldline-r", "LENGTH_MODE": "post-consonant", "ALIAS_SUFFIX": "_F5",
        "STATUS_CACHE_SECONDS": "300", "FRAGMENT_CACHE_MB": "1", "MIN_ALIASES": "2",
        "ENGLISH_MIN_ALIASES": "3", "MAX_CHARACTERS": "50", "MAX_PHONEMES": "60",
        "MAX_AUDIO_SECONDS": "4", "RENDER_THREADS": "20", "FLAGS": "B100", "BASE_PITCH": "F3",
        "SPEECH_RATE": "1.5", "RESAMPLER_COMMAND": "unrelated-resampler",
    }
    for suffix, value in other_settings.items():
        monkeypatch.setenv("PHONE_WORKER_TETO_" + suffix, value)
    assert tts.worker._miku_renderer_environment() == before
    assert tts.worker._get_miku_renderer() is renderer and len(created) == 1
    assert before["PHONE_WORKER_TETO_LENGTH_MODE"] == "total"
    assert before["PHONE_WORKER_TETO_MIN_ALIASES"] == "500"
    assert before["PHONE_WORKER_TETO_ENGLISH_MIN_ALIASES"] == "500"
    assert before["PHONE_WORKER_TETO_FRAGMENT_CACHE_MB"] == "256"
    monkeypatch.setenv("PHONE_WORKER_MIKU_LENGTH_MODE", "post-consonant")
    assert tts.worker._miku_renderer_environment()["PHONE_WORKER_TETO_LENGTH_MODE"] == "post-consonant"
    assert tts.worker._get_miku_renderer() is not renderer and len(created) == 2


@pytest.mark.parametrize("teto_enabled", [False, True])
def test_status_advertises_each_voice_and_miku_alone_can_keep_ttv_ready(tts, monkeypatch, teto_enabled):
    monkeypatch.setenv("PHONE_WORKER_TETO_ENABLED", str(teto_enabled).lower())
    monkeypatch.setenv("PHONE_WORKER_MIKU_ENABLED", "true")
    def renderer(voice_id):
        return SimpleNamespace(status=lambda **kw: {"ok": True, "ready": True, "available": True,
                                                    "fingerprint": voice_id + "-bank", "ttv_languages": ["pt-BR", "en-US"]},
                               fingerprint=lambda **kw: voice_id + "-" + kw["reading_language"])
    monkeypatch.setattr(tts.worker, "_get_teto_renderer", lambda: renderer("kasane-teto"))
    monkeypatch.setattr(tts.worker, "_get_miku_renderer", lambda: renderer("hatsune-miku"))
    monkeypatch.setattr(tts.worker, "_teto_resource_snapshot", lambda: {"ok": True})
    state = tts.teto_status(force=True)
    rows = {row["id"]: row for row in state["ttv_voices"]}
    assert state["ready"] and state["enabled"] and state["available"]
    assert rows["hatsune-miku"]["ready"]
    assert rows["kasane-teto"]["ready"] == teto_enabled
    assert rows["hatsune-miku"]["fingerprint"] == "hatsune-miku-bank"
    assert rows["hatsune-miku"]["language_fingerprints"]["en-US"] == "hatsune-miku-en-US"
    assert rows["hatsune-miku"]["languages"] == ["pt-BR", "en-US"]


@pytest.mark.parametrize("teto_enabled", [False, True])
def test_invalid_teto_backend_cannot_block_healthy_miku(tts, monkeypatch, teto_enabled):
    monkeypatch.setenv("PHONE_WORKER_TETO_ENABLED", str(teto_enabled).lower())
    monkeypatch.setenv("PHONE_WORKER_TETO_BACKEND", "invalid-backend")
    monkeypatch.setenv("PHONE_WORKER_MIKU_ENABLED", "true")
    monkeypatch.setattr(tts.worker, "_get_teto_renderer", lambda: pytest.fail("invalid Teto loaded"))
    monkeypatch.setattr(tts.worker, "_get_miku_renderer", lambda: SimpleNamespace(
        status=lambda **kw: {"ok": True, "ready": True, "available": True,
                            "fingerprint": "miku-bank", "ttv_languages": ["pt-BR", "en-US"]},
        fingerprint=lambda **kw: "miku-" + kw["reading_language"]))
    monkeypatch.setattr(tts.worker, "_teto_resource_snapshot", lambda: {"ok": True})
    state = tts.teto_status(force=True)
    rows = {row["id"]: row for row in state["ttv_voices"]}
    assert state["ready"] and state["enabled"] and state["available"]
    assert not rows["kasane-teto"]["ready"] and not rows["kasane-teto"]["available"]
    assert rows["kasane-teto"]["enabled"] == teto_enabled
    assert "PHONE_WORKER_TETO_BACKEND" in rows["kasane-teto"]["last_error"]
    assert rows["hatsune-miku"]["ready"] and rows["hatsune-miku"]["available"]
    assert rows["hatsune-miku"]["backend"] == "utau"
    assert rows["hatsune-miku"]["languages"] == ["pt-BR", "en-US"]
