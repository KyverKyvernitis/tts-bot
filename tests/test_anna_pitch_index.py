"""Authored UTAU registers are selected after the lexical alias is planned."""
from __future__ import annotations

import os
from pathlib import Path
import sys

import pytest

WORKER = Path(__file__).resolve().parents[1] / "deploy/termux/phone-worker"
if str(WORKER) not in sys.path:
    sys.path.insert(0, str(WORKER))

from teto_renderer.errors import TetoVoicebankError
from teto_renderer.voicebank import VoicebankIndex


def _bank(tmp_path, aliases=("taB3", "ta", "taA4", "aB3", "a", "aA4", "aW")):
    bank = tmp_path / "bank"
    bank.mkdir()
    lines = []
    for number, alias in enumerate(aliases):
        name = f"sample-{number}.wav"
        (bank / name).write_bytes(b"bank recording " + alias.encode())
        lines.append(f"{name}={alias},0,30,-300,20,10")
    (bank / "oto.ini").write_text("\n".join(lines))
    (bank / "prefix.map").write_text(
        "C3\t\tB3\nC4\t\tB3\nD4\t\tB3\nD#4\t\t\nE4\t\t\nG4\t\t\n"
        "G#4\t\tA4\nA4\t\tA4\nA#5\t\tA4\nB5\t\tW\nC6\t\tW\n")
    return bank


def test_rendered_note_uses_authored_recording_and_keeps_its_cache_identity(tmp_path):
    bank = _bank(tmp_path)
    index = VoicebankIndex.load(bank, minimum_aliases=1, case_sensitive=True, pitch_map=True)
    for pitch, expected in (("C4", "taB3"), ("E4", "ta"), ("A4", "taA4")):
        selected = index.resolve(("ta",), pitch=pitch)
        assert selected.alias == expected
        assert selected.wav_path.read_bytes() == b"bank recording " + expected.encode()
        assert expected in selected.cache_identity()
    assert len({index.resolve(("ta",), pitch=pitch).cache_identity()
                for pitch in ("C4", "E4", "A4")}) == 3


def test_inventory_probe_does_not_freeze_low_register_when_base_cue_is_missing(tmp_path):
    bank = _bank(tmp_path, aliases=("taB3", "taA4"))
    index = VoicebankIndex.load(bank, minimum_aliases=1, pitch_map=True)
    lexical = index.resolve(("ta",))
    assert lexical is not None and lexical.alias == "ta"
    assert index.resolve((lexical.alias,), pitch="A4").alias == "taA4"
    assert index.resolve(("taB3",), pitch="A4").alias == "taA4"


def test_missing_whistle_consonant_uses_nearest_available_full_voice(tmp_path):
    bank = _bank(tmp_path)
    index = VoicebankIndex.load(bank, minimum_aliases=1, pitch_map=True)
    assert index.resolve(("a",), pitch="C6").alias == "aW"
    assert index.resolve(("ta",), pitch="C6").alias == "taA4"
    assert index.resolve(("missing",), pitch="C6") is None


def test_missing_low_recording_falls_back_to_middle_before_high(tmp_path):
    bank = _bank(tmp_path, aliases=("ta", "taA4"))
    index = VoicebankIndex.load(bank, minimum_aliases=1, pitch_map=True)
    assert index.resolve(("ta",), pitch="C3").alias == "ta"


def test_exact_phoneme_candidate_precedes_a_different_recording_in_target_register(tmp_path):
    bank = _bank(tmp_path, aliases=("taB3", "daA4"))
    index = VoicebankIndex.load(bank, minimum_aliases=1, pitch_map=True)
    assert index.resolve(("ta", "da"), pitch="A4").alias == "taB3"


def test_bank_mapping_is_opt_in_and_preserves_case_distinctions(tmp_path):
    bank = _bank(tmp_path, aliases=("EB3", "EA4", "eB3", "eA4"))
    plain = VoicebankIndex.load(bank, minimum_aliases=1, case_sensitive=True)
    mapped = VoicebankIndex.load(bank, minimum_aliases=1, case_sensitive=True, pitch_map=True)
    assert plain.resolve(("E",)) is None
    assert mapped.resolve(("E",), pitch="C4").alias == "EB3"
    assert mapped.resolve(("e",), pitch="A4").alias == "eA4"
    assert mapped.fingerprint != plain.fingerprint


def test_affixes_do_not_strip_an_unrelated_numeric_phoneme(tmp_path):
    bank = _bank(tmp_path, aliases=("A4", "3", "3B3", "3A4"))
    index = VoicebankIndex.load(bank, minimum_aliases=1, case_sensitive=True, pitch_map=True)
    assert index.resolve(("A4",)).alias == "A4"
    assert index.resolve(("3",), pitch="A4").alias == "3A4"


def test_authored_prefix_and_suffix_are_both_applied(tmp_path):
    bank = _bank(tmp_path, aliases=("soft ta_low", "strong ta_high"))
    (bank / "prefix.map").write_text("C3\tsoft \t_low\nC5\tstrong \t_high\n")
    index = VoicebankIndex.load(bank, minimum_aliases=1, pitch_map=True)
    assert index.resolve(("ta",)).alias == "ta"
    assert index.resolve(("ta",), pitch="C3").alias == "soft ta_low"
    assert index.resolve(("soft ta_low",), pitch="C5").alias == "strong ta_high"


def test_pitch_map_contents_invalidate_bank_even_when_stamp_is_unchanged(tmp_path):
    bank = _bank(tmp_path)
    source = bank / "prefix.map"
    old = source.stat()
    first = VoicebankIndex.load(bank, minimum_aliases=1, pitch_map=True)
    source.write_text(source.read_text().replace("C4\t\tB3", "C4\t\tA4"))
    os.utime(source, ns=(old.st_atime_ns, old.st_mtime_ns))
    second = VoicebankIndex.load(bank, minimum_aliases=1, pitch_map=True)
    assert first.fingerprint != second.fingerprint
    assert first.resolve(("ta",), pitch="C4").alias == "taB3"
    assert second.resolve(("ta",), pitch="C4").alias == "taA4"


@pytest.mark.parametrize("contents", ["", "not a map", "C99\t\tA4\n"])
def test_invalid_map_fails_before_rendering(tmp_path, contents):
    bank = _bank(tmp_path)
    (bank / "prefix.map").write_text(contents)
    with pytest.raises(TetoVoicebankError, match="notas válidas"):
        VoicebankIndex.load(bank, minimum_aliases=1, pitch_map=True)


def test_map_outside_bank_is_not_read(tmp_path):
    bank = _bank(tmp_path)
    (bank / "prefix.map").unlink()
    external = tmp_path / "other.map"
    external.write_text("C4\t\tB3\n")
    (bank / "prefix.map").symlink_to(external)
    with pytest.raises(TetoVoicebankError, match="fora da pasta"):
        VoicebankIndex.load(bank, minimum_aliases=1, pitch_map=True)


def test_invalid_render_pitch_is_not_silently_mapped(tmp_path):
    index = VoicebankIndex.load(_bank(tmp_path), minimum_aliases=1, pitch_map=True)
    with pytest.raises(TetoVoicebankError, match="nota inválida"):
        index.resolve(("ta",), pitch="not a pitch")
