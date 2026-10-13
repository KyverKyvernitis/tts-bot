"""MIDI importer tests: real binary events, not mocked parser responses."""
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import random
import sys

import pytest


_PATH = Path(__file__).resolve().parents[1] / "deploy" / "ttv-cd" / "midi_extract.py"
_SPEC = importlib.util.spec_from_file_location("cd_midi_extract_test_module", _PATH)
module = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = module
_SPEC.loader.exec_module(module)
extract_midi = module.extract_midi
MidiExtractionError = module.MidiExtractionError


def vlq(value):
    parts = [value & 127]
    while value > 127:
        value >>= 7
        parts.append(128 | (value & 127))
    return bytes(reversed(parts))


def meta(kind, data=b""):
    return bytes((255, kind)) + vlq(len(data)) + data


def track(events=(), *, end=None, name=""):
    events = list(events)
    if name:
        events.insert(0, (0, meta(3, name.encode())))
    events.sort(key=lambda item: item[0])
    previous, output = 0, b""
    for tick, event in events:
        output += vlq(tick - previous) + event
        previous = tick
    output += vlq((previous if end is None else end) - previous) + meta(47)
    return raw_track(output)


def raw_track(data):
    return b"MTrk" + len(data).to_bytes(4, "big") + data


def midi(*tracks, format_number=None, ppq=480):
    if format_number is None:
        format_number = 0 if len(tracks) == 1 else 1
    header = format_number.to_bytes(2, "big") + len(tracks).to_bytes(2, "big") + ppq.to_bytes(2, "big")
    return b"MThd" + len(header).to_bytes(4, "big") + header + b"".join(tracks)


def note(pitch=60, start=0, end=480, *, channel=0, velocity=100):
    return [(start, bytes((144 | channel, pitch, velocity))), (end, bytes((128 | channel, pitch, 0)))]


def extract(tmp_path, data, **kwargs):
    path = tmp_path / "source.mid"
    path.write_bytes(data)
    result = extract_midi(path, **kwargs)
    assert_worker_contract(result["score"])
    return result


def assert_worker_contract(score):
    assert set(score) == {"version", "track_index", "duration_ms", "lyrics_source", "notes"}
    assert score["version"] == 1 and 0 <= score["track_index"] < 64
    assert 40 <= score["duration_ms"] <= 60_000
    assert 1 <= len(score["notes"]) <= 128
    previous = 0
    for item in score["notes"]:
        assert set(item) == {"midi_note", "start_ms", "duration_ms", "velocity", "lyric"}
        assert 36 <= item["midi_note"] <= 96
        assert 1 <= item["velocity"] <= 127
        assert previous <= item["start_ms"]
        assert item["duration_ms"] >= 40
        previous = item["start_ms"] + item["duration_ms"]
        assert previous <= score["duration_ms"]


def test_global_tempo_crossings_and_rests_have_original_timing(tmp_path):
    conductor = track([(0, meta(81, (500_000).to_bytes(3, "big"))),
                       (480, meta(81, (1_000_000).to_bytes(3, "big"))),
                       (960, meta(81, (250_000).to_bytes(3, "big")))])
    result = extract(tmp_path, midi(conductor, track(note(60, 0, 960) + note(64, 1440, 1920), name="Vocal")))
    assert [(n["start_ms"], n["duration_ms"]) for n in result["score"]["notes"]] == [(0, 1500), (1750, 250)]
    assert result["score"]["duration_ms"] == 2000
    assert result["extraction"]["rests"] == [{"start_ms": 1500, "duration_ms": 250}]
    assert result["extraction"]["vocal_status"] == "midi_indicated"
    assert result["extraction"]["vocal_transcription_confirmed"] is False


def test_overlapping_same_pitch_note_offs_pair_fifo_and_recover(tmp_path):
    data = midi(track([(0, b"\x90\x3c\x64"), (120, b"\x90\x3c\x50"),
                       (480, b"\x80\x3c\x00"), (600, b"\x80\x3c\x00")]))
    result = extract(tmp_path, data)
    # FIFO first ends at 480, second at 600; extraction clips the first at
    # the actual second onset (120), never overwrites the first onset.
    assert [(n["start_ms"], n["duration_ms"], n["velocity"]) for n in result["score"]["notes"]] == [(0, 125, 100), (125, 500, 80)]
    assert result["extraction"]["parser_conditions"]["same_pitch_overlaps_fifo"] == 1
    assert result["extraction"]["vocal_status"] == "ambiguous"


def test_polyphonic_upper_voice_keeps_rests_ignores_lower_interruption_and_reports_pedal(tmp_path):
    events = note(48, 0, 960) + note(72, 0, 480) + note(50, 240, 960)
    events += note(74, 960, 1440) + [(0, b"\xb0\x40\x7f"), (1440, b"\xb0\x40\x00")]
    result = extract(tmp_path, midi(track(events)))
    assert [n["midi_note"] for n in result["score"]["notes"]] == [72, 74]
    assert result["extraction"]["rests"] == [{"start_ms": 500, "duration_ms": 500}]
    assert "sustain_pedal_ignored_key_durations_preserved" in result["extraction"]["warnings"]
    assert "polyphonic_upper_voice_heuristic" in result["extraction"]["warnings"]


def test_polyphonic_vocal_name_beats_monophonic_bass(tmp_path):
    bass = track(note(48) + note(50, 480, 960), name="Bass")
    vocal = track(note(60) + note(64), name="Vocal")
    result = extract(tmp_path, midi(bass, vocal))
    assert result["score"]["track_index"] == 1
    assert result["extraction"]["vocal_status"] == "ambiguous"


def test_track_and_channel_selection_do_not_merge_accompaniment(tmp_path):
    data = midi(track(note(48, channel=0) + note(64, channel=1) + note(38, channel=9)),
                track(note(72, channel=2)))
    result = extract(tmp_path, data, track=0, channel=1)
    assert [n["midi_note"] for n in result["score"]["notes"]] == [64]
    assert result["extraction"]["channel"] == 1
    assert result["extraction"]["selection"] == "explicit"
    auto = extract(tmp_path, data)
    assert len(auto["extraction"]["candidates"]) == 3
    with pytest.raises(MidiExtractionError, match="percussão"):
        extract(tmp_path, data, channel=9)


def test_lyrics_track_selects_voice_and_only_aligned_lyrics_survive(tmp_path):
    lyrics = track([(0, meta(5, b"a")), (960, meta(5, b"mor"))])
    bass = track(note(48, 0, 120))
    vocal = track(note(60, 0, 480) + note(62, 480, 960) + note(64, 960, 1440))
    result = extract(tmp_path, midi(lyrics, bass, vocal))
    assert result["score"]["track_index"] == 2
    assert result["score"]["lyrics_source"] == "midi"
    assert [n["lyric"] for n in result["score"]["notes"]] == ["a", "~", "mor"]


def test_running_status_zero_velocity_and_one_byte_messages(tmp_path):
    events = [(0, b"\xc0\x03"), (0, b"\x04"), (0, b"\xd0\x06"), (0, b"\x07"),
              (0, b"\x90\x3c\x60"), (480, b"\x3c\x00"), (480, b"\x40\x50"), (960, b"\x40\x00")]
    result = extract(tmp_path, midi(track(events)))
    assert [(n["midi_note"], n["velocity"]) for n in result["score"]["notes"]] == [(60, 96), (64, 80)]
    assert result["extraction"]["program"] == 4


def test_bounded_excerpt_never_accelerates_long_arrangement(tmp_path):
    events = sum((note(60 + i % 12, i * 480, i * 480 + 360) for i in range(400)), [])
    result = extract(tmp_path, midi(track(events, name="Vocal")))
    assert result["extraction"]["source_duration_ms"] == 199875
    assert result["extraction"]["is_excerpt"]
    assert result["extraction"]["excerpt_reasons"] == ["worker_note_limit", "worker_duration_limit"]
    assert len(result["score"]["notes"]) == 120
    assert result["score"]["duration_ms"] == 59875
    assert all(n["duration_ms"] == 375 for n in result["score"]["notes"])
    assert all(b["start_ms"] - a["start_ms"] == 500 for a, b in zip(result["score"]["notes"], result["score"]["notes"][1:]))
    assert result["extraction"]["tempo_changed"] is False


def test_note_budget_is_hard_limit_without_stretch(tmp_path):
    events = sum((note(60, i * 96, i * 96 + 80) for i in range(300)), [])
    result = extract(tmp_path, midi(track(events)))
    assert len(result["score"]["notes"]) == 128
    assert result["score"]["duration_ms"] == 12783
    assert result["score"]["notes"][-1]["start_ms"] == 12700


def test_explicit_offset_preserves_initial_rest_drops_boundary_spanning_notes(tmp_path):
    data = midi(track(note(60, 0, 480) + note(64, 960, 1440) + note(67, 1920, 2400)))
    result = extract(tmp_path, data, start_ms=600, max_duration_ms=1000)
    assert [(n["start_ms"], n["duration_ms"]) for n in result["score"]["notes"]] == [(400, 500)]
    assert result["extraction"]["rests"] == [{"start_ms": 0, "duration_ms": 400}]
    assert result["extraction"]["excerpt_start_ms"] == 600


def test_conductor_tempo_is_global_and_conflicts_are_visible(tmp_path):
    conductor = track([(0, meta(81, b"\x07\xa1\x20")), (480, meta(81, b"\x0f\x42\x40"))])
    melody = track([(0, meta(81, b"\x00\x00\x01"))] + note(60, 0, 960))
    result = extract(tmp_path, midi(conductor, melody))
    assert result["score"]["duration_ms"] == 1500
    assert result["extraction"]["tempo_conductor_track"] == 0
    assert "conflicting_nonconductor_tempos_ignored" in result["extraction"]["warnings"]


def test_fractional_rounding_has_no_cumulative_drift(tmp_path):
    result = extract(tmp_path, midi(track(note(60, 0, 1) + note(62, 1, 2) + note(64, 2, 3)), ppq=3))
    assert [(n["start_ms"], n["duration_ms"]) for n in result["score"]["notes"]] == [(0, 167), (167, 166), (333, 167)]


def test_smpte_preserves_timing_and_ignores_tempo(tmp_path):
    division = ((256 - 25) << 8) | 40  # 1000 ticks/second
    result = extract(tmp_path, midi(track(note(60, 0, 500)), ppq=division))
    assert result["score"]["duration_ms"] == 500
    assert result["extraction"]["timing"] == "smpte"


def test_note_names_octaves_hashes_and_reproducibility(tmp_path):
    data = midi(track(note(61, 0, 480) + note(84, 960, 1440)))
    one, two = extract(tmp_path, data), extract(tmp_path, data)
    assert one == two
    assert one["extraction"]["source_sha256"] == hashlib.sha256(data).hexdigest()
    canonical = json.dumps(one["score"], sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    assert one["extraction"]["score_sha256"] == hashlib.sha256(canonical).hexdigest()
    assert [(n["note"], n["octave"]) for n in one["extraction"]["note_names"]] == [("C#", 4), ("C", 6)]


def test_recovery_for_redundant_note_off_and_unclosed_note_is_visible(tmp_path):
    data = midi(track([(0, b"\x80\x3c\x00"), (0, b"\x90\x3c\x64")], end=480))
    result = extract(tmp_path, data)
    assert result["score"]["duration_ms"] == 500
    assert result["extraction"]["parser_conditions"]["unmatched_note_off"] == 1
    assert result["extraction"]["parser_conditions"]["unclosed_notes_at_track_end"] == 1


def test_all_notes_off_controller_closes_at_real_tick(tmp_path):
    result = extract(tmp_path, midi(track([(0, b"\x90\x3c\x64"), (480, b"\xb0\x7b\x00")], end=960)))
    assert result["score"]["duration_ms"] == 500
    assert result["extraction"]["parser_conditions"]["channel_all_notes_off"] == 1


@pytest.mark.parametrize("kwargs", [{"track": True}, {"track": -1}, {"track": 64}, {"track": 1},
                                    {"channel": 16}, {"channel": "1"}, {"start_ms": -1},
                                    {"start_ms": False}, {"max_notes": 129}, {"max_notes": 0},
                                    {"max_duration_ms": 60001}, {"max_duration_ms": 39}])
def test_invalid_selection_is_permanent_and_not_cooldown_worthy(tmp_path, kwargs):
    with pytest.raises(MidiExtractionError) as raised:
        extract(tmp_path, midi(track(note())), **kwargs)
    assert raised.value.permanent and raised.value.code == "invalid_selection"


@pytest.mark.parametrize("data", [b"no midi", midi(track(note()))[:-1],
                                 midi(raw_track(b"\x00\x3c\x64\x00\xff\x2f\x00")),
                                 midi(raw_track(b"\x00\xff\x51\x03\x00\x00\x00\x00\xff\x2f\x00")),
                                 midi(track(note()), format_number=2),
                                 midi(track(note())) + b"extra"])
def test_malformed_binary_fails_with_useful_error(tmp_path, data):
    with pytest.raises(MidiExtractionError):
        extract(tmp_path, data)


def test_out_of_range_and_micro_notes_are_reported_without_octave_shifts(tmp_path):
    data = midi(track(note(20, 0, 480) + note(60, 0, 10) + note(72, 480, 960)))
    result = extract(tmp_path, data)
    assert [n["midi_note"] for n in result["score"]["notes"]] == [72]
    assert result["extraction"]["reduction"]["out_of_range_notes_dropped"] == 1
    assert result["extraction"]["reduction"]["short_notes_dropped"] == 1


def test_no_playable_notes_is_permanent_source_error(tmp_path):
    with pytest.raises(MidiExtractionError) as raised:
        extract(tmp_path, midi(track(note(20))))
    assert raised.value.code == "no_melody"


def test_near_onset_upper_note_does_not_create_micro_note(tmp_path):
    result = extract(tmp_path, midi(track(note(60, 0, 480) + note(72, 10, 480))))
    assert [n["midi_note"] for n in result["score"]["notes"]] == [72]
    assert result["extraction"]["excerpt_start_ms"] == 10


def test_crossing_keeps_original_end_in_provenance(tmp_path):
    result = extract(tmp_path, midi(track(note(60, 0, 960) + note(72, 480, 960))))
    assert result["score"]["notes"][0]["duration_ms"] == 500
    assert result["extraction"]["note_names"][0]["source_end_ms"] == 1000
    assert result["extraction"]["note_names"][0]["retained_end_ms"] == 500


def test_conflicting_lyrics_are_ignored_and_ambiguity_is_visible(tmp_path):
    events = [(0, meta(5, b"a")), (0, meta(5, b"other"))] + note(60)
    result = extract(tmp_path, midi(track(events)))
    assert result["score"]["lyrics_source"] == "vowel"
    assert result["score"]["notes"][0]["lyric"] == "a"
    assert "conflicting_embedded_lyrics_ignored" in result["extraction"]["warnings"]
    assert result["extraction"]["vocal_status"] == "ambiguous"


def test_tempo_in_second_track_and_same_tick_last_change_wins(tmp_path):
    melody = track([(0, meta(81, (500_000).to_bytes(3, "big"))),
                    (0, meta(81, (250_000).to_bytes(3, "big")))] + note(60))
    result = extract(tmp_path, midi(track(), melody))
    assert result["score"]["duration_ms"] == 250
    assert result["extraction"]["tempo_conductor_track"] == 1


def test_auto_window_handles_individually_overlong_note_without_hiding_following_notes(tmp_path):
    data = midi(track(note(60, 0, 100_000) + note(62, 100_000, 100_480)))
    result = extract(tmp_path, data)
    assert len(result["score"]["notes"]) == 1
    assert result["score"]["notes"][0]["midi_note"] == 62
    assert result["score"]["duration_ms"] == 500


def test_neither_invalid_status_nor_missing_end_can_be_recovered_as_success(tmp_path):
    for data in [midi(raw_track(b"\x00\xf8")), midi(raw_track(b"\x00\x90\x3c\x64")),
                 midi(raw_track(b"\x00\x90\x3c\xff\x00\xff\x2f\x00"))]:
        with pytest.raises(MidiExtractionError):
            extract(tmp_path, data)


def test_seeded_arrangements_keep_worker_bounds_and_do_not_invent_timing(tmp_path):
    randomizer = random.Random(20261013)
    for _ in range(60):
        tracks = []
        for _ in range(randomizer.randint(1, 3)):
            events = [(0, meta(81, randomizer.randint(200_000, 900_000).to_bytes(3, "big")))]
            for _ in range(80):
                start = randomizer.randint(0, 60_000)
                events += note(randomizer.randint(30, 100), start, start + randomizer.randint(40, 2400),
                               channel=randomizer.choice([0, 1, 9]), velocity=randomizer.randint(1, 127))
            tracks.append(track(events))
        result = extract(tmp_path, midi(*tracks))
        assert result["extraction"]["tempo_changed"] is False
        for item, origin in zip(result["score"]["notes"], result["extraction"]["note_names"]):
            assert item["start_ms"] + result["extraction"]["excerpt_start_ms"] == origin["source_start_ms"]
            assert origin["retained_end_ms"] <= origin["source_end_ms"]


def test_two_indicated_vocal_streams_still_record_selection_ambiguity(tmp_path):
    data = midi(track(note(60), name="Vocal"), track(note(72), name="Voice"))
    automatic = extract(tmp_path, data)
    assert automatic["extraction"]["vocal_status"] == "ambiguous"
    assert automatic["extraction"]["selection_ambiguous"]
    explicit = extract(tmp_path, data, track=1)
    assert explicit["extraction"]["vocal_status"] == "midi_indicated"
    assert explicit["extraction"]["selection_ambiguous"] is False


def test_redundant_nonconductor_tempo_is_not_reported_as_conflict(tmp_path):
    conductor = track([(0, meta(81, (500_000).to_bytes(3, "big")))])
    melody = track([(480, meta(81, (500_000).to_bytes(3, "big")))] + note(60, 0, 960))
    result = extract(tmp_path, midi(conductor, melody))
    assert result["score"]["duration_ms"] == 1000
    assert "conflicting_nonconductor_tempos_ignored" not in result["extraction"]["warnings"]


def test_trailing_source_rest_is_explicitly_recorded_as_trimmed_excerpt(tmp_path):
    result = extract(tmp_path, midi(track(note(60) + [(1000, b"\xc0\x28")], end=1000)))
    assert result["score"]["duration_ms"] == 500
    assert result["extraction"]["selected_stream_duration_ms"] == 1042
    assert result["extraction"]["is_excerpt"] is True
    assert result["extraction"]["excerpt_reasons"] == ["source_silence_trimmed"]
    assert result["extraction"]["program_scope"] == "last_program_in_source_stream"
