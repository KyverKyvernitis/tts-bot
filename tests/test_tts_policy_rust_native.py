"""Differential contracts for the opt-in native policy and actual bot admission.

Build with `cargo build -p vinculo_fluxo_tts` before running this module.
The ordinary Python suite can still run without a compiled extension.
"""
from __future__ import annotations

import asyncio
import json
import os
import importlib.util
import random
from pathlib import Path

import pytest

from cogs.tts import performance, policy_rust, ttv
from cogs.tts.runtime import split_text as python_split
from cogs.tts.utils.ffmpeg import compose_audio_filters
from test_ttv_preferences import settings  # shared isolated Mongo fixture


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def native(monkeypatch):
    from cogs.tts.fluxo_rust import _carregar_nucleo
    required = os.environ.get("TTS_NATIVE_REQUIRED", "0").strip() == "1"
    try:
        module = _carregar_nucleo()
    except (ImportError, OSError):
        if required:
            raise
        pytest.skip("build/install the native extension before native contract tests")
    if not hasattr(module, "NativePendingQueue"):
        if required:
            raise RuntimeError("the required native extension lacks NativePendingQueue")
        pytest.skip("rebuild the native extension with pending queue support")
    monkeypatch.setattr(policy_rust, "_native", lambda: module)
    return module


def outcome(function, *args, **kwargs):
    try:
        return "ok", function(*args, **kwargs)
    except (ValueError, RuntimeError) as error:
        return type(error).__name__, str(error)


def compare(monkeypatch, function, *args, **kwargs):
    monkeypatch.delenv("TTS_BACKEND", raising=False)
    monkeypatch.setenv("TTS_POLICY_RUST", "0")
    if hasattr(function, "cache_clear"):
        function.cache_clear()
    expected = outcome(function, *args, **kwargs)
    monkeypatch.setenv("TTS_POLICY_RUST", "1")
    if hasattr(function, "cache_clear"):
        function.cache_clear()
    actual = outcome(function, *args, **kwargs)
    assert actual == expected, (args, kwargs, expected, actual)


def test_ttv_current_catalog_and_controls_match_python(native, monkeypatch):
    for value in [None, "", "Teto", "kasane_teto", "pururu-purpura", "unknown", 1]:
        compare(monkeypatch, ttv.normalize_voice_id, value)
    for value in [None, "", "pt", "PT_br", "en_US", "ja", True]:
        compare(monkeypatch, ttv.normalize_language, value)
    for value in [None, "", "-0", "−2,5 st", "3 semitons", "1.25", "9", True,
                  float("nan"), float("inf")]:
        compare(monkeypatch, ttv.normalize_pitch, value)
    for value in [None, "", "0,75", 0.5, 1.07, 1.5, "2", True, float("nan")]:
        compare(monkeypatch, ttv.normalize_speech_rate, value)
    for values in [{}, {"ttv_voice_id": "kasane-teto", "ttv_language": "en"},
                   {"ttv_pitch_semitones": "bad", "teto_pitch_semitones": "-2.5"},
                   {"ttv_voice_id": "missing"}, {"ttv_voice_id": "pururu-purpura", "ttv_language": "en"}]:
        compare(monkeypatch, ttv.resolve_preferences, values, default_pitch=1.5)
    for personal, server in [({}, {}), ({}, {"ttv_voice_id": "pururu-purpura", "ttv_language": "en"}),
                             ({"ttv_pitch_semitones": 0}, {"ttv_pitch_semitones": -2}),
                             ({"ttv_voice_id": "missing", "ttv_speech_rate": "bad"}, {})]:
        compare(monkeypatch, ttv.inherit_server_preferences, personal, server, default_pitch=-1.0)


def test_frozen_worker_cache_keys(native):
    fixture = json.loads((ROOT / "tests/fixtures/tts_policy_contract.json").read_text())
    for case in fixture["cache_keys"]:
        assert policy_rust.call("standard_cache_key", body=case["body"], engine=case["engine"],
                                teto_fingerprint=fixture["teto_fingerprint"],
                                teto_base_pitch=fixture["teto_base_pitch"]) == case["key"]


def test_worker_pure_wrappers_preserve_callbacks_and_frozen_cache_contract(native, monkeypatch):
    path = ROOT / "deploy/termux/phone-worker/phone_worker_runtime/tts_policy.py"
    spec = importlib.util.spec_from_file_location("native_worker_policy_contract", path)
    worker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(worker)
    original_import = worker.importlib.import_module

    def import_native(name, *args, **kwargs):
        if name in {"tts_fluxo_experimental._nucleo_fluxo_tts", "_nucleo_fluxo_tts"}:
            return native
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(worker.importlib, "import_module", import_native)
    for operation, values in {
        "normalize_engine": [None, "", "google-cloud", "edge_tts", "native", "kasane-teto"],
        "normalize_edge_rate": ["−20％", "9", "bad", "½", "²", "１２"],
        "normalize_edge_pitch": ["—5 hz", "9", "bad", "½"],
        "normalize_gtts_language": [None, "", "pt_BR", "en_US"],
        "normalize_cache_format": [None, ".wav", "opus", "bad"],
        "normalize_ttv_voice_id": ["kasane-teto", "pururu-purpura", "unknown", None],
        "normalize_ttv_language": ["pt", "en_US", "ja", None, True],
        "normalize_ttv_speech_rate": [1, 0.5, "1.07", False, "2", float("nan")],
        "normalize_teto_pitch_semitones": ["+4.9", "−2,5 st", None, "0.25", "0.75", float("inf")],
        "sanitize_cache_key": ["A" * 64, "shørt", "ab/cd-ef_0123456789"],
    }.items():
        function = getattr(worker, operation)
        for value in values:
            compare(monkeypatch, function, value)
    for body in [{}, {"ttv_speech_rate": False}, {"ttv_score": {}, "ttv_pitch_semitones": "3.5", "ttv_speech_rate": 0.5},
                 {"ttv_voice_id": "pururu-purpura", "ttv_language": "en-US"}]:
        compare(monkeypatch, worker.effective_ttv_settings, body, default_speech_rate=0.5)
    fixture = json.loads((ROOT / "tests/fixtures/tts_policy_contract.json").read_text())
    monkeypatch.setenv("TTS_POLICY_RUST", "1")
    for case in fixture["cache_keys"]:
        assert worker.standard_cache_key(case["body"], engine=case["engine"],
                                         sanitize_key=worker.sanitize_cache_key,
                                         normalize_rate=worker.normalize_edge_rate,
                                         normalize_pitch=worker.normalize_edge_pitch,
                                         normalize_language=worker.normalize_gtts_language,
                                         teto_fingerprint=fixture["teto_fingerprint"],
                                         teto_base_pitch=fixture["teto_base_pitch"]) == case["key"]
    # Deliberate caller rebindings must influence the identity even in Rust mode.
    body = {"engine": "edge", "text": "live callbacks"}
    kwargs = dict(engine="edge", sanitize_key=worker.sanitize_cache_key,
                  normalize_rate=lambda _: "custom-rate", normalize_pitch=lambda _: "custom-pitch",
                  normalize_language=lambda _: "custom-language", teto_fingerprint="", teto_base_pitch="")
    compare(monkeypatch, worker.standard_cache_key, body, **kwargs)


def test_complete_rust_backend_fails_closed_and_rejects_nonfinite_values(native, monkeypatch):
    monkeypatch.setenv("TTS_BACKEND", "rust")
    with pytest.raises(ValueError, match="passos"):
        ttv.normalize_pitch(float("nan"))
    with pytest.raises(ValueError, match="velocidade"):
        ttv.normalize_speech_rate(float("inf"))

    def missing_native():
        raise ImportError("missing artifact")

    monkeypatch.setattr(policy_rust, "_native", missing_native)
    with pytest.raises(RuntimeError, match="extensão nativa"):
        ttv.normalize_voice_id(None)


def test_markup_and_hash_differential(native, monkeypatch):
    samples = ["olá", "<olá mundo>", "a~~mor~!", "olá | mundo", "olá~", "/cantar olá~",
               r"olá \~", "`olá~`", "```olá~", '"olá~"', "“olá~”", "olá || mundo", "~oi",
               "https://example.org/a~", "<@123> olá~", "<:emoji:123> olá~", "a < b", "a>b",
               r"a \| b", "a | ~", "a ~mor", "a >", "a>>", "olá" * 90 + "~"]
    randomizer = random.Random(42)
    alphabet = ["a", "olá", "mundo", "~", "|", "<", ">", "`", "\\~", '"', "."]
    samples += [" ".join(randomizer.choices(alphabet, k=5)) for _ in range(100)]
    for text in samples:
        compare(monkeypatch, performance.singing_request, text)
        for singing in (False, True):
            compare(monkeypatch, performance.parse_performance_text, text, singing=singing)
    for segments in [None, [], [{"text": "olá", "hold": True, "pause_after_ms": 0}]]:
        compare(monkeypatch, performance.performance_hash, text=" olá   mundo ",
                score={"version": 1, "notes": [{"midi_note": 60, "duration_ms": 1000}]}, segments=segments)


def test_split_and_audio_filter_differential(native, monkeypatch):
    for text in ["", "Olá mundo. Outro & texto", "ação " * 40, "x" * 500,
                 "&<á> " * 100, "one; two! three? four: five."]:
        for escaped in (False, True):
            for limit in (1, 3, 16, 160):
                for parts in (1, 2, 4):
                    expected = python_split(text, escaped_utf8=escaped, limit=limit, max_parts=parts)
                    assert policy_rust.call("split_text", text=text, escaped_utf8=escaped,
                                            limit=limit, max_parts=parts) == expected
    for options, effects in [("-vn -b:a 96k", ""), ("-vn -af volume=0.5", "aecho=1"),
                             ("-af old -filter:a:0='volume=0.2' -ac 2", "atempo=1.1"),
                             ('-metadata "title=canção de teste" -af volume=0.8', "volume=0.9"),
                             ("-af", "volume=1")]:
        compare(monkeypatch, compose_audio_filters, options, effects)


def test_settingsdb_uses_existing_cache_and_preserves_invalid_values(native, monkeypatch, settings):
    snapshots = [({}, {}), ({"ttv_pitch_semitones": 0, "advanced_reverb_level": 2}, {}),
                 ({"engine": "edge", "ttv_voice_id": "unknown", "ttv_language": "ja"}, {}),
                 ({"ttv_voice_id": "pururu-purpura"}, {"ttv_language": "en-US"}),
                 ({"teto_pitch_semitones": "-2.5", "ttv_speech_rate": 1.07}, {"gtts_prefix": "!"})]
    for user, guild in snapshots:
        settings.user_cache[(1, 2)] = {"tts": user}
        settings.guild_cache[1] = {"tts_defaults": guild}
        monkeypatch.setenv("TTS_POLICY_RUST", "0")
        monkeypatch.delenv("TTS_BACKEND", raising=False)
        settings._resolved_tts_cache.clear()
        expected = settings.resolve_tts(1, 2)
        monkeypatch.setenv("TTS_POLICY_RUST", "1")
        settings._resolved_tts_cache.clear()
        actual = settings.resolve_tts(1, 2)
        assert actual == expected
        assert settings._resolved_tts_cache[(1, 2)] == expected
    settings.coll.update_one.assert_not_awaited()


def test_melody_validation_identity_and_vowel_spans_match_python(native, monkeypatch):
    from copy import deepcopy
    from cogs.tts import melodies
    entry = {"duration_ms": 20_000, "notes": [
        {"midi_note": 60, "start_ms": 0, "duration_ms": 10_000, "velocity": 100},
        {"midi_note": 62, "start_ms": 10_000, "duration_ms": 10_000, "velocity": 100},
    ]}
    for mutate in [None, ("duration_ms", True), ("duration_ms", 60_001), ("notes", [])]:
        current = deepcopy(entry)
        if mutate:
            current[mutate[0]] = mutate[1]
        compare(monkeypatch, melodies._validate_score, current)
    overlap = deepcopy(entry)
    overlap["notes"][1]["start_ms"] = 9999
    compare(monkeypatch, melodies._validate_score, overlap)
    compare(monkeypatch, melodies.catalog_score, entry)
    compare(monkeypatch, melodies.score_sha256, melodies.catalog_score(entry))
    for score in [None, {}, {"notes": []}, entry, overlap,
                  {"notes": [{"midi_note": 60, "start_ms": 0, "duration_ms": 50},
                             {"midi_note": 62, "start_ms": 100, "duration_ms": 50}]}]:
        compare(monkeypatch, melodies.minimum_singing_syllables, score)
    compare(monkeypatch, melodies.fit_arrangement_lyrics, "olá", None,
            {"parts": [{"role": "lead", "notes": entry["notes"]}]})
    compare(monkeypatch, melodies.fit_arrangement_lyrics, "olá", None, {"parts": []})


@pytest.mark.asyncio
async def test_real_enqueue_groups_dedup_eviction_and_generation(native, monkeypatch):
    from test_tts_runtime import Probe, item, tts_audio
    monkeypatch.setenv("TTS_POLICY_RUST", "1")
    monkeypatch.setattr(tts_audio, "TTS_QUEUE_MAXSIZE", 3)
    owner = Probe()
    first, second, third = item("part a"), item("part b"), item("third")
    first.request_id = second.request_id = "same-message"
    assert await owner._enqueue_tts_items(1, [first, second, third]) == (True, 0, False)
    state = owner._get_state(1)
    assert state.queue.native_policy
    assert state.queue._unfinished_tasks == 3
    assert await owner._enqueue_tts_item(1, item("third")) == (False, 0, True)
    new = [item("new a"), item("new b")]
    assert await owner._enqueue_tts_items(1, new) == (True, 2, False)
    assert [entry.text for entry in state.queue._queue] == ["third", "new a", "new b"]
    assert state.queue._unfinished_tasks == 3
    assert len(state.pending_signatures) == 3
    state.accepting = False
    state.generation += 1
    assert state.queue._domain.generation == 1
    assert not state.queue._domain.accepting
    assert await owner._enqueue_tts_item(1, item("blocked")) == (False, 0, False)
    while not state.queue.empty():
        state.queue.get_nowait()
        state.queue.task_done()
    await asyncio.wait_for(state.queue.join(), 1)
    assert state.pending_signatures == {}
    state.accepting = True
    assert await owner._enqueue_tts_item(1, item("new generation")) == (True, 0, False)
    current = state.queue.get_nowait()
    assert current.generation == 1
    state.queue.task_done()


@pytest.mark.asyncio
async def test_native_queue_cancelled_waiter_does_not_consume_next_item(native, monkeypatch):
    monkeypatch.setenv("TTS_POLICY_RUST", "1")
    from test_tts_runtime import Probe, item
    owner = Probe()
    state = owner._get_state(1)
    assert state.queue.native_policy
    waiter = asyncio.create_task(state.queue.get())
    await asyncio.sleep(0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert await owner._enqueue_tts_item(1, item("survives")) == (True, 0, False)
    assert (await state.queue.get()).text == "survives"
    state.queue.task_done()
    await asyncio.wait_for(state.queue.join(), 1)
