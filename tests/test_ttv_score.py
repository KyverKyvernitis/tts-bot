"""Real SMF fixtures exercise musical timing, selection and parser boundaries."""
from __future__ import annotations

import json
import random

import pytest

from cogs.tts.score import ScoreError, inspect_midi_tracks, parse_midi


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
    previous = 0
    output = b""
    for tick, event in events:
        output += vlq(tick - previous) + event
        previous = tick
    output += vlq((previous if end is None else end) - previous) + meta(47)
    return b"MTrk" + len(output).to_bytes(4, "big") + output


def raw_track(data):
    return b"MTrk" + len(data).to_bytes(4, "big") + data


def midi(*tracks, format_number=None, ppq=480, header_extension=b""):
    if format_number is None:
        format_number = 0 if len(tracks) == 1 else 1
    header = format_number.to_bytes(2, "big") + len(tracks).to_bytes(2, "big") + ppq.to_bytes(2, "big")
    header += header_extension
    return b"MThd" + len(header).to_bytes(4, "big") + header + b"".join(tracks)


def note(pitch=60, start=0, end=480, *, channel=0, velocity=100):
    return [(start, bytes((144 | channel, pitch, velocity))), (end, bytes((128 | channel, pitch, 0)))]


def test_tempo_map_crossing_note_and_rests_preserve_original_timeline():
    conductor = track([
        (0, meta(81, (500_000).to_bytes(3, "big"))),
        (480, meta(81, (1_000_000).to_bytes(3, "big"))),
        (960, meta(81, (250_000).to_bytes(3, "big"))),
    ])
    melody = track(note(60, 0, 960) + note(64, 1440, 1920), end=2160, name="Vocal")
    assert parse_midi(midi(conductor, melody), lyrics="a mor") == {
        "version": 1, "track_index": 1, "duration_ms": 2125, "lyrics_source": "message",
        "notes": [
            {"midi_note": 60, "start_ms": 0, "duration_ms": 1500, "velocity": 100},
            {"midi_note": 64, "start_ms": 1750, "duration_ms": 250, "velocity": 100},
        ],
    }


def test_fractional_tick_rounding_has_no_accumulated_drift():
    score = parse_midi(midi(track(note(60, 0, 1) + note(62, 1, 2) + note(64, 2, 3)), ppq=3))
    assert [(n["start_ms"], n["duration_ms"]) for n in score["notes"]] == [(0, 167), (167, 166), (333, 167)]
    assert score["duration_ms"] == 500


def test_global_conductor_wins_over_conflicting_track_tempos():
    conductor = track([(0, meta(81, b"\x07\xa1\x20")), (480, meta(81, b"\x0f\x42\x40"))])
    melody = track([(0, meta(81, b"\x00\x00\x01"))] + note(60, 0, 960))
    assert parse_midi(midi(conductor, melody))["notes"][0]["duration_ms"] == 1500


def test_last_same_tick_conductor_tempo_and_tempo_in_nonfirst_track():
    empty = track()
    melody = track([(0, meta(81, b"\x07\xa1\x20")), (0, meta(81, b"\x03\xd0\x90"))] + note())
    assert parse_midi(midi(empty, melody))["duration_ms"] == 250


def test_running_status_zero_velocity_note_off_and_single_byte_channels():
    events = [
        (0, b"\xc0\x03"), (0, b"\x04"), (0, b"\xd0\x06"), (0, b"\x07"),
        (0, b"\x90\x3c\x60"), (480, b"\x3c\x00"),
        (480, b"\x40\x50"), (960, b"\x40\x00"),
    ]
    score = parse_midi(midi(track(events)))
    assert [(n["midi_note"], n["velocity"]) for n in score["notes"]] == [(60, 96), (64, 80)]


def test_percussion_never_becomes_lyrics_or_melody_even_in_format_zero():
    events = note(35, 0, 480, channel=9) + note(38, 0, 480, channel=9) + note(60, 0, 480)
    score = parse_midi(midi(track(events)))
    assert len(score["notes"]) == 1 and score["notes"][0]["midi_note"] == 60
    drums_only = midi(track(note(35, channel=9)))
    assert inspect_midi_tracks(drums_only)[0]["note_count"] == 0
    with pytest.raises(ScoreError, match="monofônica"):
        parse_midi(drums_only)


def test_lyric_associated_melody_wins_over_more_notes_and_named_accompaniment():
    bass = track(note(48, 0, 120) + note(50, 120, 240) + note(52, 240, 360), name="lead guitar")
    vocal = track([(0, meta(5, b"a"))] + note(60, 0, 480), name="track 2")
    assert parse_midi(midi(bass, vocal))["track_index"] == 1


@pytest.mark.parametrize("name", ["Melody", "VOCALS", "Lead synth", "Voz principal", "Melódia", "ボーカル"])
def test_unique_named_melody_is_selected_without_count_heuristic(name):
    accompaniment = track(note(48, 0, 120) + note(50, 120, 240) + note(52, 240, 360))
    melodic = track(note(), name=name)
    assert parse_midi(midi(accompaniment, melodic))["track_index"] == 1


def test_ambiguous_tracks_offer_indices_and_explicit_choice_preserves_track():
    data = midi(track(note(60), name="Piano"), track(note(64), name="Flute"))
    with pytest.raises(ScoreError) as raised:
        parse_midi(data)
    error = raised.value
    assert error.code == "ambiguous_track"
    assert [item["index"] for item in error.tracks] == [0, 1]
    assert "0: Piano" in str(error) and "1: Flute" in str(error) and "Escolha" in str(error)
    assert parse_midi(data, track_index=1)["notes"][0]["midi_note"] == 64
    assert not any(item["recommended"] for item in inspect_midi_tracks(data))


def test_inspection_lists_conductor_and_polyphonic_accompaniment():
    data = midi(track(), track(note(48) + note(52), name="Piano"), track(note(), name="Vocal"))
    details = inspect_midi_tracks(data)
    assert [(item["index"], item["note_count"], item["monophonic"], item["recommended"]) for item in details] == [
        (0, 0, False, False), (1, 2, False, False), (2, 1, True, True),
    ]


def test_polyphonic_vocal_cannot_silently_turn_into_bass():
    data = midi(track(note(48), name="Bass"), track(note(60) + note(64), name="Vocal"))
    with pytest.raises(ScoreError, match="faixas 1.*sobrepostas"):
        parse_midi(data)
    with pytest.raises(ScoreError, match="faixa 1.*sobrepostas"):
        parse_midi(data, track_index=1)


@pytest.mark.parametrize("index", [-1, 2, "1", True, 0.0])
def test_invalid_explicit_track_index(index):
    with pytest.raises(ScoreError, match="índice"):
        parse_midi(midi(track(note()), track()), track_index=index)


def test_timed_embedded_lyrics_are_preserved_and_unmarked_notes_are_melismas():
    melody = track([
        (0, meta(5, "\\a-".encode())), (960, meta(5, "mor".encode())),
    ] + note(60, 0, 480) + note(62, 480, 960) + note(64, 960, 1440))
    score = parse_midi(midi(melody))
    assert score["lyrics_source"] == "midi"
    assert [n["lyric"] for n in score["notes"]] == ["a-", "~", "mor"]
    override = parse_midi(midi(melody), lyrics="outro texto")
    assert override["lyrics_source"] == "message"
    assert not any("lyric" in n for n in override["notes"])


def test_separate_lyric_track_selects_corresponding_melody_and_cp1252():
    lyric = track([(480, meta(5, "Olá".encode("cp1252")))])
    accompaniment = track(note(48, 0, 360))
    melody = track(note(60, 0, 480) + note(62, 480, 960) + note(64, 960, 1440))
    score = parse_midi(midi(lyric, accompaniment, melody))
    assert score["track_index"] == 2 and score["lyrics_source"] == "midi"
    assert [n["lyric"] for n in score["notes"]] == ["a", "Olá", "~"]


def test_unaligned_embedded_lyrics_are_actionable_and_message_override_works():
    data = midi(track([(10, meta(5, b"hello"))]), track(note()))
    with pytest.raises(ScoreError, match="não coincide"):
        parse_midi(data)
    assert parse_midi(data, lyrics="hello")["lyrics_source"] == "message"


def test_conflicting_separate_lyric_tracks_are_not_chosen_arbitrarily():
    data = midi(track([(0, meta(5, b"one"))]), track([(0, meta(5, b"two"))]), track(note()))
    with pytest.raises(ScoreError, match="letras diferentes"):
        parse_midi(data)


def test_exact_60_seconds_with_leading_rest_is_allowed_and_trailing_rest_is_bounded():
    data = midi(track(note(60, 59500, 60000)), ppq=500)
    score = parse_midi(data)
    assert score["duration_ms"] == 60000 and score["notes"][0]["start_ms"] == 59500
    with pytest.raises(ScoreError, match="60 segundos"):
        parse_midi(midi(track(note(), end=60001), ppq=500))


def test_note_pitch_duration_and_count_boundaries():
    assert [n["midi_note"] for n in parse_midi(midi(track(note(36, 0, 40) + note(96, 40, 80)), ppq=500))["notes"]] == [36, 96]
    for pitch in (35, 97):
        with pytest.raises(ScoreError, match="36 e 96"):
            parse_midi(midi(track(note(pitch))))
    with pytest.raises(ScoreError, match="40 ms"):
        parse_midi(midi(track(note(60, 0, 39)), ppq=500))
    events = [event for index in range(128) for event in note(60, index * 100, (index + 1) * 100)]
    assert len(parse_midi(midi(track(events), ppq=500))["notes"]) == 128
    with pytest.raises(ScoreError, match="128 notas"):
        parse_midi(midi(track(events + note(60, 12800, 12900)), ppq=500))


def test_event_limit_counts_all_tracks_before_selection():
    events = b"\x00\xb0\x01\x01" * 49999 + b"\x00\xff\x2f\x00"
    # Exactly 50,000 events is valid structurally, but has no melodic note.
    with pytest.raises(ScoreError, match="monofônica"):
        parse_midi(midi(raw_track(events)))
    with pytest.raises(ScoreError, match="50.000 eventos"):
        parse_midi(midi(raw_track(events), track(note())))


def test_size_track_count_and_format_limits():
    with pytest.raises(ScoreError, match="512 KiB"):
        parse_midi(b"x" * (512 * 1024 + 1))
    with pytest.raises(ScoreError, match="64 faixas"):
        parse_midi(midi(*(track() for _ in range(65))))
    with pytest.raises(ScoreError, match="formato 2"):
        parse_midi(midi(track(note()), format_number=2))
    with pytest.raises(ScoreError, match="uma no formato 0"):
        parse_midi(midi(track(), track(note()), format_number=0))
    with pytest.raises(ScoreError, match="SMPTE"):
        parse_midi(midi(track(note()), ppq=0xE728))
    with pytest.raises(ScoreError, match="PPQ"):
        parse_midi(midi(track(note()), ppq=0))


def test_extended_header_and_sysex_are_skipped_without_affecting_timing():
    events = [(0, b"\xf0\x03\x01\x02\xf7"), (0, b"\xf7\x02\x01\x02")] + note()
    assert parse_midi(midi(track(events), header_extension=b"extra"))["duration_ms"] == 500


@pytest.mark.parametrize("payload, match", [
    (b"\x00\x3c\x60", "running status"),
    (b"\x00\x90\x3c\x80", "dados inválidos"),
    (b"\x00\xf1\x00", "sistema"),
    (b"\x00\xff\x80\x00", "metadado"),
    (b"\x00\xff\x51\x03\x00\x00\x00", "andamento"),
    (b"\x00\xff\x51\x02\x00\x01", "andamento"),
    (b"\x00\x90\x3c\x60\x00\xff\x01\x00\x01\x3c\x00", "running status"),
    (b"\x00\x90\x3c\x60\x00\xf0\x00\x01\x3c\x00", "running status"),
    (b"\x81\x80\x80\x80\x00", "VLQ"),
    (b"\x00\x80\x3c\x00", "não foi iniciada"),
    (b"\x00\x90\x3c\x60\x00\x90\x3c\x60", "repete uma nota"),
    (b"\x00\x90\x3c\x60\x00\x80\x3c\x00", "duração positiva"),
    (b"\x00\x90\x3c\x60", "sem encerramento"),
])
def test_malformed_events_are_rejected(payload, match):
    with pytest.raises(ScoreError, match=match):
        parse_midi(midi(raw_track(payload + b"\x00\xff\x2f\x00")))


def test_truncated_files_missing_end_and_extra_data_are_rejected():
    valid = midi(track(note()))
    for length in range(len(valid)):
        with pytest.raises(ScoreError):
            parse_midi(valid[:length])
    with pytest.raises(ScoreError, match="fim da faixa"):
        parse_midi(midi(raw_track(b"\x00\xb0\x01\x01")))
    with pytest.raises(ScoreError, match="fim da faixa"):
        parse_midi(midi(raw_track(b"\x00\xff\x2f\x00\x00")))
    with pytest.raises(ScoreError, match="dados extras"):
        parse_midi(valid + b"extra")


def test_metadata_limits_and_nontext_lyrics():
    with pytest.raises(ScoreError, match="128 caracteres"):
        parse_midi(midi(track([(0, meta(5, b"a" * 129))] + note())))
    with pytest.raises(ScoreError, match="mais de uma letra"):
        parse_midi(midi(track([(0, meta(5, b"a")), (0, meta(5, b"b"))] + note())))
    with pytest.raises(ScoreError, match="texto"):
        parse_midi(midi(track(note())), lyrics=None)
    with pytest.raises(ScoreError, match="bytes"):
        parse_midi(bytearray(midi(track(note()))))


def test_embedded_missing_lyric_after_rest_remains_logical_continuation():
    # This lets the renderer reattack only the preceding vowel after the rest; explicit
    # message holds are distinguished by lyrics_source and must not erase it.
    melody = track([(0, meta(5, b"ta"))] + note(60, 0, 480) + note(64, 960, 1440))
    score = parse_midi(midi(melody))
    assert score["lyrics_source"] == "midi"
    assert [n["lyric"] for n in score["notes"]] == ["ta", "~"]
    assert score["notes"][1]["start_ms"] - score["notes"][0]["duration_ms"] == 500


def test_malformed_body_mutations_only_raise_score_errors_or_return_bounded_scores():
    # Keep the real header intact so mutations exercise channel/meta parsing,
    # declared track lengths and VLQs rather than only rejecting magic bytes.
    original = midi(track([(0, meta(5, b"a"))] + note(60, 0, 480) + note(64, 960, 1440)))
    seed = random.Random(20261011)
    for _ in range(400):
        altered = bytearray(original)
        for unused in range(seed.randrange(1, 5)):
            altered[seed.randrange(14, len(altered))] = seed.randrange(256)
        try:
            score = parse_midi(bytes(altered))
        except ScoreError:
            continue
        assert score["version"] == 1 and 40 <= score["duration_ms"] <= 60000
        assert 1 <= len(score["notes"]) <= 128
        previous_end = 0
        for event in score["notes"]:
            assert 36 <= event["midi_note"] <= 96 and 1 <= event["velocity"] <= 127
            assert event["start_ms"] >= previous_end and event["duration_ms"] >= 40
            previous_end = event["start_ms"] + event["duration_ms"]
            assert previous_end <= score["duration_ms"]
        assert json.loads(json.dumps(score, allow_nan=False)) == score
