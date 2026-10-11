"""Miku uses its own immutable bank settings and recorded CVVC aliases."""
from __future__ import annotations

import array
from concurrent.futures import ThreadPoolExecutor
import io
import os
from pathlib import Path
import sys
import threading
import wave

import pytest

WORKER = Path(__file__).resolve().parents[1] / "deploy" / "termux" / "phone-worker"
sys.path.insert(0, str(WORKER)) if str(WORKER) not in sys.path else None

from teto_renderer import TetoRenderer, WorldlineRenderer
from teto_renderer.errors import TetoConfigurationError
from teto_renderer.phonemizer import phonemize
from teto_renderer.voicebank import VoicebankIndex


def bank(root: Path, aliases: list[str], *, value: int = 900) -> Path:
    root.mkdir(parents=True)
    with wave.open(str(root / "source.wav"), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(44100)
        wav.writeframes(array.array("h", [value] * 13230).tobytes())
    (root / "oto.ini").write_text("".join(f"source.wav={alias},0,20,-300,20,10\n" for alias in aliases))
    return root


def settings(tmp_path: Path, bank_path: Path, *, miku: bool) -> dict[str, str]:
    script = tmp_path / "copy_resampler.py"
    script.write_text("import shutil, sys\nshutil.copyfile(sys.argv[1],sys.argv[2])\n")
    return {
        "PHONE_WORKER_TETO_ENABLED": "true",
        "PHONE_WORKER_TETO_VOICEBANK_DIR": str(bank_path),
        "PHONE_WORKER_TETO_ENGLISH_VOICEBANK_DIR": str(bank_path),
        "PHONE_WORKER_TETO_VOICEBANK_MODE": "english" if miku else "standard",
        "PHONE_WORKER_TETO_ALIAS_SUFFIX": "_D4" if miku else "",
        "PHONE_WORKER_TETO_MIN_ALIASES": "1", "PHONE_WORKER_TETO_ENGLISH_MIN_ALIASES": "1",
        "PHONE_WORKER_TETO_RESAMPLER_COMMAND": f"{sys.executable} {script}",
        "PHONE_WORKER_TETO_FRAGMENT_CACHE_DIR": str(tmp_path / ("miku-cache" if miku else "teto-cache")),
        "PHONE_WORKER_TETO_BASE_PITCH": "D4" if miku else "C4",
        "PHONE_WORKER_TETO_SPEECH_RATE": "1", "PHONE_WORKER_TETO_MAX_AUDIO_SECONDS": "10",
    }


@pytest.mark.parametrize("suffix", ["D4", "_D4"])
def test_suffix_resolves_recorded_alias_without_changing_identity(tmp_path, suffix):
    path = bank(tmp_path / "bank", ["i:_D4", "I_D4", "m i:_D4"])
    index = VoicebankIndex.load(path, minimum_aliases=1, alias_suffix=suffix, case_sensitive=True)
    assert index.alias_count == 3
    assert index.resolve(("i:",)).alias == "i:_D4"
    assert index.resolve(("m i:",)).alias == "m i:_D4"
    assert index.resolve(("I",)).alias == "I_D4"
    assert index.resolve(("i",)) is None
    assert index.resolve(("i:_D4",)).cache_identity() == index.resolve(("i:",)).cache_identity()
    assert VoicebankIndex.load(path, minimum_aliases=1).fingerprint != index.fingerprint


@pytest.mark.parametrize("renderer_class", [TetoRenderer, WorldlineRenderer])
def test_explicit_environment_is_an_authoritative_snapshot(renderer_class, monkeypatch, tmp_path):
    monkeypatch.setenv("PHONE_WORKER_TETO_FLAGS", "B100")
    monkeypatch.setenv("PHONE_WORKER_TETO_ENGLISH_VOICEBANK_DIR", str(tmp_path / "teto"))
    env = {"PHONE_WORKER_TETO_FLAGS": "B0", "PHONE_WORKER_TETO_VOICEBANK_DIR": str(tmp_path / "miku")}
    renderer = renderer_class(environment=env, voice_id="hatsune-miku")
    env["PHONE_WORKER_TETO_FLAGS"] = "B100"
    env["PHONE_WORKER_TETO_VOICEBANK_DIR"] = str(tmp_path / "other")
    assert renderer._getenv("PHONE_WORKER_TETO_FLAGS") == "B0"
    assert renderer._english_voicebank_dir() == str(tmp_path / "miku")
    assert not renderer._env_bool("PHONE_WORKER_TETO_ENABLED")
    assert renderer._voicebank_mode() == "english"
    assert renderer._phonemizer_options() == {"inventory_profile": "miku-v4-english"}
    with pytest.raises(TetoConfigurationError, match="independente"):
        renderer_class(voice_id="hatsune-miku")


@pytest.mark.parametrize("language", ["pt-BR", "en-US"])
def test_missing_miku_bank_cannot_borrow_global_teto_bank(tmp_path, monkeypatch, language):
    teto = bank(tmp_path / "teto", ["e", "i", "u", "te", "tu"])
    monkeypatch.setenv("PHONE_WORKER_TETO_ENABLED", "true")
    monkeypatch.setenv("PHONE_WORKER_TETO_ENGLISH_VOICEBANK_DIR", str(teto))
    env = settings(tmp_path, tmp_path / "missing-miku", miku=True)
    renderer = TetoRenderer(environment=env, voice_id="hatsune-miku")
    with pytest.raises(TetoConfigurationError):
        renderer.synthesize("Hello", reading_language=language)
    assert renderer.fingerprint(reading_language=language) == "unavailable"


def test_miku_nucleus_cannot_be_replaced_by_initial_consonant(tmp_path):
    path = bank(tmp_path / "miku", ["Sil m_D4", "m i:_D4", "k u:_D4"])
    index = VoicebankIndex.load(path, minimum_aliases=1, alias_suffix="_D4", case_sensitive=True)
    moras = phonemize("Miku", voicebank_profile="english-cvvc", resolve_alias=index.resolve,
                     inventory_profile="miku-v4-english")
    assert [m.candidates[0] for m in moras] == ["m i:_D4", "k u:_D4"]
    assert all(m.role == "nucleus" for m in moras)


def test_vowel_mapping_does_not_change_teto_or_hide_portuguese_approximations(tmp_path):
    path = bank(tmp_path / "miku", ["Sil V_D4", "Sil O:_D4", "l V_D4", "e_D4"])
    index = VoicebankIndex.load(path, minimum_aliases=1, alias_suffix="_D4", case_sensitive=True)
    args = dict(voicebank_profile="english-cvvc", resolve_alias=index.resolve)
    ordinary = phonemize("Olá", **args)
    adapted = phonemize("Olá", **args, inventory_profile="miku-v4-english")
    assert any(index.resolve(m.candidates) is None for m in ordinary)
    assert [m.candidates[0] for m in adapted] == ["Sil O:_D4", "l V_D4"]
    assert all(m.coverage == "approximation" for m in adapted)


def test_concurrent_miku_and_teto_renders_keep_voice_bank_and_controls(tmp_path):
    miku_bank = bank(tmp_path / "miku", ["m i:_D4", "k u:_D4", "m I_D4", "k V_D4"], value=1200)
    teto_bank = bank(tmp_path / "teto", ["て", "と"], value=-1200)
    miku = TetoRenderer(environment=settings(tmp_path, miku_bank, miku=True), voice_id="hatsune-miku")
    teto = TetoRenderer(environment=settings(tmp_path, teto_bank, miku=False))
    before = dict(os.environ)
    barrier = threading.Barrier(2)

    def run(renderer, text, language, pitch, rate):
        barrier.wait(timeout=5)
        return renderer.synthesize(text, timeout_seconds=15, reading_language=language,
                                   pitch_offset_semitones=pitch, speech_rate=rate)

    with ThreadPoolExecutor(max_workers=2) as pool:
        m = pool.submit(run, miku, "Miku", "en-US", 0, 1)
        t = pool.submit(run, teto, "teto", "pt-BR", -2, .75)
        mr, tr = m.result(), t.result()
    assert mr["voice_id"] == "hatsune-miku" and tr["voice_id"] == "kasane-teto"
    assert mr["reading_language"] == "en-US" and tr["reading_language"] == "pt-BR"
    assert mr["pitch_offset_semitones"] == 0 and tr["pitch_offset_semitones"] == -2
    assert mr["speech_rate"] == 1 and tr["speech_rate"] == .75
    assert mr["voicebank_fingerprint"] != tr["voicebank_fingerprint"]
    assert mr["renderer_fingerprint"] != tr["renderer_fingerprint"]
    assert not mr["missing_phonemes"] and not tr["missing_phonemes"]
    assert mr["audio"] != tr["audio"]
    for result in [mr, tr]:
        with wave.open(io.BytesIO(result["audio"]), "rb") as wav:
            assert wav.getframerate() == 44100 and wav.getnframes() > 0
    assert os.environ == before


def test_resampler_receives_explicit_bridge_settings_without_global_mutation(tmp_path, monkeypatch):
    path = bank(tmp_path / "miku", ["m i:_D4", "k u:_D4"])
    env = settings(tmp_path, path, miku=True)
    probe = tmp_path / "child.json"
    env.update(PHONE_WORKER_ESPER_CONTAINER="miku-test-guest", PHONE_WORKER_ESPER_TIMEOUT="17",
               PHONE_WORKER_ESPER_DEADLINE="123456.0", CHILD_PROBE=str(probe))
    monkeypatch.setenv("PHONE_WORKER_ESPER_CONTAINER", "global-other-guest")
    monkeypatch.setenv("PHONE_WORKER_TETO_FLAGS", "global-other-voice")
    (tmp_path / "copy_resampler.py").write_text(
        "import json,os,shutil,sys\n"
        "from pathlib import Path\n"
        "Path(os.environ['CHILD_PROBE']).write_text(json.dumps({k:os.environ.get(k) for k in "
        "['PHONE_WORKER_ESPER_CONTAINER','PHONE_WORKER_ESPER_TIMEOUT','PHONE_WORKER_ESPER_DEADLINE','PHONE_WORKER_TETO_FLAGS']}))\n"
        "shutil.copyfile(sys.argv[1],sys.argv[2])\n"
    )
    before = dict(os.environ)
    renderer = TetoRenderer(environment=env, voice_id="hatsune-miku")
    result = renderer.synthesize("Miku", timeout_seconds=10)
    import json
    child = json.loads(probe.read_text())
    assert child == {"PHONE_WORKER_ESPER_CONTAINER": "miku-test-guest", "PHONE_WORKER_ESPER_TIMEOUT": "17",
                     "PHONE_WORKER_ESPER_DEADLINE": "123456.0", "PHONE_WORKER_TETO_FLAGS": None}
    assert result["rendered_phonemes"] == 2 and not result["missing_phonemes"]
    assert os.environ == before


@pytest.mark.parametrize("language,text", [
    ("pt-BR", "Olá. Eu sou a Miku. Como você está?"),
    ("pt-BR", "Brasil, trabalho, estranho, não, coração, amanhã, filha."),
    ("en-US", "Hello. I am Hatsune Miku. How are you?"),
    ("en-US", "Three strong words, world, vocal, thought, beautiful, goodbye."),
])
def test_optional_actual_miku_bank_inventory(language, text):
    path = Path(os.environ.get("MIKU_CVVC_TEST_BANK", "/path/not/configured"))
    if not path.is_dir():
        pytest.skip("set MIKU_CVVC_TEST_BANK to the separately downloaded English CVVC bank")
    index = VoicebankIndex.load(path, minimum_aliases=500, alias_suffix="_D4", case_sensitive=True)
    moras = phonemize(text, voicebank_profile="english-cvvc", reading_language=language,
                     inventory_profile="miku-v4-english", resolve_alias=index.resolve)
    assert index.alias_count >= 500
    assert moras
    assert all(index.resolve(m.candidates) is not None for m in moras)
    assert all(m.candidates[0].endswith("_D4") for m in moras)
    assert all(not m.candidates[0].startswith("Sil ") or not m.source_phonemes[:-1]
               for m in moras if m.role == "nucleus")
