"""Cold readiness probes share bank parsing without weakening validation."""
from __future__ import annotations

import array
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import shlex
import sys
import threading
from types import SimpleNamespace
import wave

import pytest

WORKER = Path(__file__).resolve().parents[1] / "deploy" / "termux" / "phone-worker"
if str(WORKER) not in sys.path:
    sys.path.insert(0, str(WORKER))

from teto_renderer import renderer as renderer_module
from teto_renderer.errors import TetoRendererError
from teto_renderer.renderer import TetoRenderer
from teto_renderer.voicebank import VoicebankIndex


VOICES = ("hatsune-miku", "anna-nyui")


@pytest.fixture
def make_renderer(tmp_path, monkeypatch):
    # Status only checks ffmpeg's presence; these tests never execute audio tools.
    which = renderer_module.shutil.which
    monkeypatch.setattr(renderer_module.shutil, "which",
                        lambda command: sys.executable if command == "ffmpeg" else which(command))

    def create(voice_id, **overrides):
        bank = tmp_path / voice_id / "bank"
        bank.mkdir(parents=True, exist_ok=True)
        aliases = ["i:_D4", "I_D4", "m i:_D4"] if voice_id == "hatsune-miku" else ["aB3", "a", "aA4"]
        with wave.open(str(bank / "source.wav"), "wb") as target:
            target.setparams((1, 2, 44100, 0, "NONE", ""))
            target.writeframes(array.array("h", [900] * 13230).tobytes())
        (bank / "oto.ini").write_text(
            "".join(f"source.wav={alias},0,20,-300,20,10\n" for alias in aliases))
        if voice_id == "anna-nyui":
            (bank / "prefix.map").write_text("C4\t\tB3\nE4\t\t\nA4\t\tA4\n")
        environment = {
            "PHONE_WORKER_TETO_ENABLED": "true",
            "PHONE_WORKER_TETO_VOICEBANK_DIR": str(bank),
            "PHONE_WORKER_TETO_ENGLISH_VOICEBANK_DIR": str(bank),
            "PHONE_WORKER_TETO_VOICEBANK_MODE": "standard" if voice_id == "kasane-teto" else "english",
            "PHONE_WORKER_TETO_ALIAS_SUFFIX": "_D4" if voice_id == "hatsune-miku" else "",
            "PHONE_WORKER_TETO_MIN_ALIASES": "1",
            "PHONE_WORKER_TETO_ENGLISH_MIN_ALIASES": "1",
            "PHONE_WORKER_TETO_RESAMPLER_COMMAND": shlex.quote(sys.executable),
            "PHONE_WORKER_TETO_FRAGMENT_CACHE_DIR": str(tmp_path / voice_id / "fragments"),
            "PHONE_WORKER_TETO_STATUS_CACHE_SECONDS": "1",
        }
        environment.update(overrides)
        return TetoRenderer(environment=environment, voice_id=voice_id)

    return create


@pytest.mark.parametrize("voice_id", VOICES)
def test_eight_cold_status_calls_share_one_completed_probe(make_renderer, monkeypatch, voice_id):
    renderer = make_renderer(voice_id)
    original_load = VoicebankIndex.load
    original_primary = renderer._load_index
    load_started = threading.Event()
    release_load = threading.Event()
    all_attempting = threading.Barrier(9)
    parse_calls = []
    primary_calls = []

    def load(*args, **kwargs):
        parse_calls.append(kwargs)
        load_started.set()
        assert release_load.wait(timeout=5), "test did not release the cold bank load"
        return original_load(*args, **kwargs)

    def primary():
        primary_calls.append(True)
        return original_primary()

    def probe():
        all_attempting.wait(timeout=5)
        return renderer.status()

    monkeypatch.setattr(VoicebankIndex, "load", staticmethod(load))
    monkeypatch.setattr(renderer, "_load_index", primary)
    with ThreadPoolExecutor(max_workers=8) as pool:
        pending = [pool.submit(probe) for _ in range(8)]
        try:
            all_attempting.wait(timeout=5)
            assert load_started.wait(timeout=5)
            # Give all released callers a scheduling turn while the first load
            # is in flight. This is shorter than the production cache TTL.
            threading.Event().wait(0.05)
        finally:
            release_load.set()
        results = [future.result(timeout=5) for future in pending]

    assert len(parse_calls) == 1
    assert len(primary_calls) == 1
    assert all(result == results[0] for result in results)
    assert all(result["ready"] and result["available"] and result["ok"] for result in results)
    assert results[0]["voice_id"] == voice_id
    assert results[0]["aliases"] == 3
    assert len({id(result) for result in results}) == 8


@pytest.mark.parametrize("voice_id", VOICES)
@pytest.mark.parametrize("first", ("primary", "english"))
def test_primary_and_english_share_the_same_bank_in_either_order(make_renderer, monkeypatch, voice_id, first):
    renderer = make_renderer(voice_id)
    original_load = VoicebankIndex.load
    calls = []

    def load(*args, **kwargs):
        calls.append(kwargs)
        return original_load(*args, **kwargs)

    monkeypatch.setattr(VoicebankIndex, "load", staticmethod(load))
    loaders = {"primary": renderer._load_index, "english": renderer._load_english_index}
    initial = loaders[first]()
    shared = loaders["english" if first == "primary" else "primary"]()
    assert shared is initial
    assert len(calls) == 1
    assert calls[0]["case_sensitive"] is True
    assert calls[0].get("pitch_map", False) is (voice_id == "anna-nyui")


@pytest.mark.parametrize("voice_id", VOICES)
def test_concurrent_primary_and_english_loaders_parse_once(make_renderer, monkeypatch, voice_id):
    renderer = make_renderer(voice_id)
    original_load = VoicebankIndex.load
    started = threading.Event()
    release = threading.Event()
    barrier = threading.Barrier(9)
    calls = []

    def load(*args, **kwargs):
        calls.append(True)
        started.set()
        assert release.wait(timeout=5), "test did not release the cold bank load"
        return original_load(*args, **kwargs)

    def get_index(number):
        barrier.wait(timeout=5)
        return (renderer._load_index if number % 2 else renderer._load_english_index)()

    monkeypatch.setattr(VoicebankIndex, "load", staticmethod(load))
    with ThreadPoolExecutor(max_workers=8) as pool:
        pending = [pool.submit(get_index, number) for number in range(8)]
        try:
            barrier.wait(timeout=5)
            assert started.wait(timeout=5)
            threading.Event().wait(0.05)
        finally:
            release.set()
        indexes = [future.result(timeout=5) for future in pending]
    assert len(calls) == 1
    assert all(index is indexes[0] for index in indexes)


@pytest.mark.parametrize("first", ("primary", "english"))
def test_shared_bank_reuse_preserves_the_other_loader_minimum(make_renderer, monkeypatch, first):
    minimums = {
        "PHONE_WORKER_TETO_MIN_ALIASES": "1" if first == "primary" else "4",
        "PHONE_WORKER_TETO_ENGLISH_MIN_ALIASES": "1" if first == "english" else "4",
    }
    renderer = make_renderer("kasane-teto", **minimums)
    original_load = VoicebankIndex.load
    calls = []

    def load(*args, **kwargs):
        calls.append(kwargs["minimum_aliases"])
        return original_load(*args, **kwargs)

    monkeypatch.setattr(VoicebankIndex, "load", staticmethod(load))
    loaders = {"primary": renderer._load_index, "english": renderer._load_english_index}
    assert loaders[first]().alias_count == 3
    with pytest.raises(TetoRendererError, match="poucos aliases"):
        loaders["english" if first == "primary" else "primary"]()
    assert calls == [1, 4]
    if first == "english":
        assert renderer.status()["ready"] is False


@pytest.mark.parametrize("voice_id", VOICES)
@pytest.mark.parametrize("unavailable", ("missing", "disabled"))
def test_missing_or_disabled_bank_never_reports_ready(make_renderer, monkeypatch, tmp_path, voice_id, unavailable):
    settings = ({"PHONE_WORKER_TETO_ENABLED": "false"} if unavailable == "disabled" else {
        "PHONE_WORKER_TETO_VOICEBANK_DIR": str(tmp_path / "missing"),
        "PHONE_WORKER_TETO_ENGLISH_VOICEBANK_DIR": str(tmp_path / "missing"),
    })
    renderer = make_renderer(voice_id, **settings)

    def unexpected_load(*args, **kwargs):
        pytest.fail("a disabled or absent bank must not be parsed")

    monkeypatch.setattr(VoicebankIndex, "load", staticmethod(unexpected_load))
    barrier = threading.Barrier(8)

    def probe():
        barrier.wait(timeout=5)
        return renderer.status()

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: probe(), range(8)))
    results.append(renderer.status(force=True))
    assert all(result["ready"] is False and result["ok"] is False
               and result["available"] is False for result in results)
    assert all(result["last_error"] for result in results)


@pytest.mark.parametrize("voice_id", VOICES)
def test_status_cache_lifetime_starts_after_slow_bank_load(make_renderer, monkeypatch, voice_id):
    renderer = make_renderer(voice_id)
    clock = [100.0]
    monkeypatch.setattr(renderer_module, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    original_load = VoicebankIndex.load
    original_primary = renderer._load_index
    parses = []
    probes = []

    def slow_load(*args, **kwargs):
        parses.append(True)
        clock[0] += 20.0  # A bank scan can outlast the entire status cache TTL.
        return original_load(*args, **kwargs)

    def primary():
        probes.append(True)
        return original_primary()

    monkeypatch.setattr(VoicebankIndex, "load", staticmethod(slow_load))
    monkeypatch.setattr(renderer, "_load_index", primary)
    completed = renderer.status()
    assert completed["ready"] is True
    assert renderer._last_status_at == 120.0
    assert renderer.status() == completed
    clock[0] += 0.9
    assert renderer.status() == completed
    assert len(probes) == 1
    clock[0] += 0.2
    assert renderer.status()["ready"] is True
    assert len(probes) == 2
    assert len(parses) == 1
