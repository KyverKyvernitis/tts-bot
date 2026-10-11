"""Reading language reaches both native renderers without changing bank settings."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import sys
from unittest.mock import patch

import pytest

from test_phone_worker_tts_lifecycle import audio, tts  # noqa: F401
from test_ttv_renderer_personal_controls import fixture, frames

from phone_worker_runtime import tts_policy, tts_providers
from teto_renderer import TetoRenderer, WorldlineRenderer, renderer as renderer_module, worldline
from teto_renderer.errors import TetoConfigurationError


@pytest.mark.parametrize("language", ["", None, True, "ja", "pt-PT", "en-GB", "unknown"])
def test_unknown_language_rejected_before_cache_and_discovery(tts, monkeypatch, language):
    monkeypatch.setattr(tts.worker, "_teto_dependency_snapshot_fast", lambda: pytest.fail("discovery ran"))
    monkeypatch.setattr(tts.handler, "_find_tts_cache_file", lambda *a: pytest.fail("cache was read"))
    with pytest.raises(ValueError, match="idioma TTV"):
        tts.handler._task_tts_agent_synthesize({"engine": "teto", "text": "Hello.", "ttv_language": language})
    assert not tts.calls


def test_legacy_requests_always_keep_portuguese_adaptation():
    assert tts_policy.effective_ttv_settings({"language": "en-US"})["ttv_language"] == "pt-BR"
    assert tts_policy.effective_ttv_settings({"ttv_language": "en"})["ttv_language"] == "en-US"
    assert tts_policy.effective_ttv_settings({"ttv_language": "PT_br"})["ttv_language"] == "pt-BR"


def test_unknown_language_prebuilt_audio_fails_before_voice_lease(tts, monkeypatch):
    monkeypatch.setattr(tts.worker, "_voice_agent_set_connection", lambda *a, **kw: pytest.fail("voice lease changed"))
    monkeypatch.setattr(tts.handler, "_task_music_agent_proxy", lambda body: pytest.fail("unknown language played"))
    with pytest.raises(ValueError, match="idioma TTV"):
        tts.handler._task_voice_agent_play_tts({"engine": "teto", "ttv_language": "ja",
                                              "audio_b64": "d2F2ZQ==", "guild_id": 1, "channel_id": 2})


def test_english_bank_fingerprint_change_invalidates_worker_cache(tts):
    tts.deps["teto"]["ttv_language_fingerprints"] = {"en-US": "english-bank-original"}
    body = {"text": "Hello.", "ttv_language": "en-US", "cache_key": "c" * 64}
    english = tts.handler._tts_agent_standard_cache_key(body, engine="teto")
    portuguese = tts.handler._tts_agent_standard_cache_key({**body, "ttv_language": "pt-BR"}, engine="teto")
    tts.deps["teto"]["ttv_language_fingerprints"]["en-US"] = "english-bank-updated"
    assert tts.handler._tts_agent_standard_cache_key(body, engine="teto") != english
    assert tts.handler._tts_agent_standard_cache_key({**body, "ttv_language": "pt-BR"}, engine="teto") == portuguese


@pytest.mark.parametrize("raw", [False, True])
def test_language_separates_warm_cache_even_when_client_key_is_identical(tts, monkeypatch, raw):
    rendered = []

    def synthesize(text, **kwargs):
        language = kwargs.get("reading_language", "pt-BR")
        rendered.append(kwargs)
        return {"audio": language.encode(), "audio_format": "wav", "reading_language": language}

    class Renderer:
        def fingerprint(self, **kwargs):
            return "english-bank" if kwargs.get("reading_language") == "en-US" else "fixture-bank"

    renderer = Renderer()
    renderer.synthesize = synthesize
    monkeypatch.setattr(tts.worker, "_get_teto_renderer", lambda: renderer)
    body = {"engine": "teto", "text": "Hello, world.", "cache_key": "c" * 64,
            "ttv_voice_id": "kasane-teto", "ttv_speech_rate": 1.0}
    pt = tts.handler._task_tts_agent_synthesize({**body, "ttv_language": "pt-BR"}, raw_response=raw)
    en = tts.handler._task_tts_agent_synthesize({**body, "ttv_language": "en-US"}, raw_response=raw)
    en_hit = tts.handler._task_tts_agent_synthesize({**body, "ttv_language": "en"}, raw_response=raw)
    assert pt["cache_key"] != en["cache_key"] == en_hit["cache_key"]
    assert not pt["cache_hit"] and not en["cache_hit"] and en_hit["cache_hit"]
    assert audio(pt) == b"pt-BR" and audio(en) == audio(en_hit) == b"en-US"
    assert [kw["reading_language"] for kw in rendered] == ["pt-BR", "en-US"]
    assert pt["ttv_language"] == "pt-BR"
    assert en["ttv_language"] == en_hit["ttv_language"] == "en-US"


def test_legacy_provider_omits_new_kwarg_and_explicit_provider_passes_it():
    import threading

    calls = []

    class Renderer:
        def synthesize(self, text, **kwargs):
            calls.append(kwargs)
            return {"audio": b"wav", "audio_format": "wav"}

    controls = dict(text="Hello.", timeout=10, max_audio_bytes=1000, logs=[], stage_ms={},
                    heavy_lock=threading.Lock(), get_renderer=Renderer, monotonic=lambda: 0,
                    normalize_format=lambda value: value)
    tts_providers.synthesize_teto(**controls)
    tts_providers.synthesize_teto(**controls, reading_language="en-US")
    assert "reading_language" not in calls[0]
    assert calls[1]["reading_language"] == "en-US"
    assert "language=pt-BR" in controls["logs"][0]
    assert "language=en-US" in controls["logs"][1]


@pytest.mark.parametrize("renderer_class", [TetoRenderer, WorldlineRenderer])
def test_invalid_language_fails_before_assets(renderer_class):
    renderer = renderer_class()
    with patch.object(renderer, "_load_index") as load:
        with pytest.raises(ValueError, match="idioma TTV"):
            renderer.synthesize("Hello.", reading_language="ja")
        load.assert_not_called()


@pytest.mark.parametrize("renderer_class", [TetoRenderer, WorldlineRenderer])
def test_missing_english_bank_never_uses_standard_portuguese_fallback(renderer_class, tmp_path):
    env, digest, _ = fixture(tmp_path)
    env["PHONE_WORKER_TETO_ENGLISH_VOICEBANK_DIR"] = str(tmp_path / "missing-english")
    module = worldline if renderer_class is WorldlineRenderer else renderer_module
    with patch.dict(os.environ, env), patch.object(worldline, "ARM64_LIBRARY_SHA256", digest):
        renderer = renderer_class()
        with patch.object(module, "phonemize") as g2p:
            with pytest.raises(TetoConfigurationError, match="Inglês exige"):
                renderer.synthesize("Hello.", reading_language="en-US")
            g2p.assert_not_called()
        assert renderer.reading_languages() == ["pt-BR"]
        assert renderer._index_profile == "standard"


@pytest.mark.parametrize("renderer_class", [TetoRenderer, WorldlineRenderer])
def test_english_bank_and_phonemizer_are_request_local(renderer_class, tmp_path):
    env, digest, guest = fixture(tmp_path)
    english = tmp_path / "english"
    shutil.copytree(tmp_path / "bank", english)
    (english / "oto.ini").write_text("te.wav=て,1,20,0,20,10\nto.wav=と,1,20,0,20,10\n", encoding="utf-8")
    env.update(PHONE_WORKER_TETO_ENGLISH_VOICEBANK_DIR=str(english), PHONE_WORKER_TETO_ENGLISH_MIN_ALIASES="1")
    module = worldline if renderer_class is WorldlineRenderer else renderer_module
    original = module.phonemize
    calls = []

    def phonemize(text, **kwargs):
        calls.append({"language": kwargs.get("reading_language", "pt-BR"), "profile": kwargs["voicebank_profile"]})
        # PCM assembly is real; the synthetic bank has only these two aliases.
        return original("teto", max_moras=kwargs["max_moras"], resolve_alias=kwargs["resolve_alias"], voicebank_profile="standard")

    with patch.dict(os.environ, env), patch.object(worldline, "ARM64_LIBRARY_SHA256", digest), patch.object(module, "phonemize", side_effect=phonemize):
        renderer = renderer_class()
        if renderer_class is WorldlineRenderer:
            guest_patch = patch.object(renderer, "_guest_command", side_effect=lambda library, workdir=None: [sys.executable, str(guest), str(workdir)])
        else:
            resample = renderer._resample_note

            def resample_with_profile(**kwargs):
                expected = "english-cvvc" if kwargs["entry"].wav_path.parent == english else "standard"
                assert renderer._index_profile == expected, "executor lost request-local bank profile"
                if expected == "english-cvvc":
                    assert renderer._english_lead_ms(kwargs["entry"], kwargs["note"]) == 20
                return resample(**kwargs)

            guest_patch = patch.object(renderer, "_resample_note", side_effect=resample_with_profile)
        with guest_patch:
            fingerprint = renderer.fingerprint()
            before_env = dict(os.environ)
            pt = renderer.synthesize("Hello.", speech_rate=1, reading_language="pt-BR")
            en = renderer.synthesize("Hello.", speech_rate=1, reading_language="en-US")
            pt_again = renderer.synthesize("Hello.", speech_rate=1)
            assert calls == [{"language": "pt-BR", "profile": "standard"},
                             {"language": "en-US", "profile": "english-cvvc"},
                             {"language": "pt-BR", "profile": "standard"}]
            assert frames(pt) > 0 and frames(en) > 0
            assert pt["reading_language"] == pt_again["reading_language"] == "pt-BR"
            assert en["reading_language"] == "en-US"
            assert en["phonemizer_version"] == "en-us-cmudict-xsampa-cvvc-v2"
            assert pt["voicebank_fingerprint"] != en["voicebank_fingerprint"]
            assert pt["renderer_fingerprint"] == pt_again["renderer_fingerprint"] != en["renderer_fingerprint"]
            assert renderer.fingerprint() == fingerprint
            assert renderer._index_profile == "standard"
            assert os.environ == before_env
            assert renderer.reading_languages() == ["pt-BR", "en-US"]


def test_other_thread_keeps_default_profile_during_english_render(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    env, _, _ = fixture(tmp_path)
    english = tmp_path / "english"
    shutil.copytree(tmp_path / "bank", english)
    env.update(PHONE_WORKER_TETO_ENGLISH_VOICEBANK_DIR=str(english), PHONE_WORKER_TETO_ENGLISH_MIN_ALIASES="1")
    with patch.dict(os.environ, env):
        renderer = TetoRenderer()
        renderer._load_reading_index("en-US")
        assert renderer._index_profile == "english-cvvc"
        with ThreadPoolExecutor(max_workers=1) as pool:
            assert pool.submit(lambda: renderer._index_profile).result() == "standard"
        renderer._clear_reading_context()
        assert renderer._index_profile == "standard"
