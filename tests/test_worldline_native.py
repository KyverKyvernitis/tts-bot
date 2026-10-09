from __future__ import annotations

from array import array
import importlib.util
import json
import math
import os
from pathlib import Path
import struct
import subprocess
from types import SimpleNamespace
import wave

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "deploy/termux/phone-worker/teto_renderer/worldline_native.py"
spec = importlib.util.spec_from_file_location("worldline_phrase_native", SCRIPT)
assert spec and spec.loader
native = importlib.util.module_from_spec(spec)
spec.loader.exec_module(native)


def source_wav(path, *, rate=44100, channels=1, width=2, frames=44100, silent=False):
    with wave.open(str(path), "wb") as target:
        target.setparams((channels, width, rate, 0, "NONE", "not compressed"))
        if width == 2:
            values = array("h", (0 if silent else int(9000 * math.sin(2 * math.pi * 220 * i / rate)
                                                      + 4000 * math.sin(2 * math.pi * 440 * i / rate)) for i in range(frames)))
            if native.sys.byteorder != "little":
                values.byteswap()
            target.writeframes(values.tobytes() * channels)
        else:
            target.writeframes(b"\x80" * frames * channels * width)


def request(**updates):
    value = {"sample_file": "source.wav", "tone": 60, "offset_ms": 100,
             "required_length_ms": 300, "consonant_ms": 80, "cutoff_ms": -500,
             "position_ms": 0, "length_ms": 300}
    value.update(updates)
    return value


def job_data(*requests, duration=300):
    count = math.ceil(duration / 10) + 1
    return {"schema_version": 1, "sample_rate": 44100, "max_output_seconds": 3,
            "requests": list(requests or [request()]),
            "curves": {key: [value] * count for key, value in zip(native.CURVE_NAMES, (260, .5, .5, .5, 1))}}


def write_job(tmp_path, data):
    path = tmp_path / "job.json"
    path.write_text(json.dumps(data))
    return path, tmp_path / "out.wav"


@pytest.fixture
def job(tmp_path):
    source_wav(tmp_path / "source.wav")
    return write_job(tmp_path, job_data())


@pytest.fixture
def pinned_library():
    value = os.environ.get("WORLDLINE_TEST_LIBRARY")
    if not value or not Path(value).is_file():
        pytest.skip("set WORLDLINE_TEST_LIBRARY to the pinned native OpenUtau 0.1.565 library")
    return Path(value)


def test_request_abi_is_exactly_the_pinned_64_bit_layout():
    assert native.ctypes.sizeof(native.SynthRequest) == 144
    assert native.SynthRequest.con_vel.offset == 40
    assert native.SynthRequest.pitch_bend.offset == 112
    assert native.SynthRequest.flag_Mv.offset == 140


def test_job_loads_decoded_source_and_validates_model_grid(job):
    prepared = native.load_job(*job)
    assert prepared["expected_frames"] == 13231
    assert prepared["curve_length"] == 31
    assert prepared["requests"][0]["sample_frames"] == 44100
    assert prepared["requests"][0]["samples"][0] == 0


def test_cpp_rounding_uses_away_from_zero_instead_of_python_ties():
    assert native._round_frame(5) == 1
    assert native._round_frame(25) == 3


@pytest.mark.parametrize("updates,match", [
    ({"tone": True}, "tone"), ({"tone": 128}, "tone"),
    ({"offset_ms": -1}, "offset"), ({"offset_ms": float("nan")}, "finite"),
    ({"offset_ms": 600, "cutoff_ms": -500}, "exceeds"),
    ({"offset_ms": 100, "cutoff_ms": 950}, "empty"),
    ({"cutoff_ms": -10}, "two complete"),
    ({"consonant_ms": 600}, "consonant exceeds"),
    ({"length_ms": 301}, "required_length"),
    ({"skip_ms": 50}, "required_length"),
    ({"position_ms": 2990}, "duration exceeds"),
    ({"fade_in_ms": 301}, "fade durations"),
    ({"volume": float("inf")}, "finite"),
    ({"sample_file": "../outside.wav"}, "stay inside"),
    ({"sample_file": "/outside.wav"}, "stay inside"),
    ({"sample_file": "subdir/source.wav"}, "directly"),
])
def test_unsafe_oto_or_phrase_requests_are_rejected_before_native_calls(tmp_path, updates, match):
    source_wav(tmp_path / "source.wav")
    with pytest.raises(native.WorldlineNativeError, match=match):
        native.load_job(*write_job(tmp_path, job_data(request(**updates))))


def test_submillisecond_trim_never_erases_beyond_source_samples(tmp_path):
    source_wav(tmp_path / "source.wav", frames=44111)
    data = job_data(request(offset_ms=0, cutoff_ms=0))
    # ceil(1000.249)/10 => 100 complete frames, safely inside 44111 samples.
    assert native.load_job(*write_job(tmp_path, data))["requests"][0]["sample_frames"] == 44111


@pytest.mark.parametrize("rate,channels,width,match", [(48000,1,2,"PCM16"), (44100,2,2,"PCM16"), (44100,1,1,"PCM16")])
def test_only_decoded_normalized_wave_format_is_accepted(tmp_path, rate, channels, width, match):
    source_wav(tmp_path / "source.wav", rate=rate, channels=channels, width=width)
    with pytest.raises(native.WorldlineNativeError, match=match):
        native.load_job(*write_job(tmp_path, job_data()))


def test_silent_source_is_rejected(tmp_path):
    source_wav(tmp_path / "source.wav", silent=True)
    with pytest.raises(native.WorldlineNativeError, match="entirely silent"):
        native.load_job(*write_job(tmp_path, job_data()))


def test_source_symlink_cannot_escape_job_directory(tmp_path):
    outside = tmp_path / "other.wav"
    source_wav(outside)
    (tmp_path / "source.wav").symlink_to(outside)
    with pytest.raises(native.WorldlineNativeError, match="regular file"):
        native.load_job(*write_job(tmp_path, job_data()))


def test_output_stays_in_job_directory_and_cannot_replace_source(job):
    path, output = job
    for invalid in (output.parent.parent / output.name, path, output.parent / "source.wav"):
        with pytest.raises(native.WorldlineNativeError):
            native.load_job(path, invalid)


@pytest.mark.parametrize("mutation,match", [
    (lambda value: value.update(schema_version=True), "schema_version"),
    (lambda value: value.update(requests=[]), "between 1"),
    (lambda value: value.update(sample_rate=48000), "44100"),
    (lambda value: value["curves"].update(f0=[]), "every 10"),
    (lambda value: value["curves"].update(gender=[.5] * 32), "same length"),
    (lambda value: value["curves"]["gender"].__setitem__(0, -1), "finite"),
    (lambda value: value["curves"]["f0"].__setitem__(0, 10**400), "finite"),
    (lambda value: value.update(silence_intervals_ms=[[100, 50]]), "silence end"),
])
def test_malformed_or_unbounded_curves_are_rejected(tmp_path, mutation, match):
    source_wav(tmp_path / "source.wav")
    data = job_data()
    mutation(data)
    with pytest.raises(native.WorldlineNativeError, match=match):
        native.load_job(*write_job(tmp_path, data))


def test_requests_are_sorted_by_end_so_cpp_does_not_truncate_overlap(tmp_path):
    source_wav(tmp_path / "source.wav")
    data = job_data(request(position_ms=400), request(position_ms=0), duration=700)
    prepared = native.load_job(*write_job(tmp_path, data))
    assert [item["p4"] for item in prepared["requests"]] == [30, 70]


def test_aggregate_native_model_memory_is_bounded(tmp_path):
    source_wav(tmp_path / "source.wav")
    data = job_data(*[request(required_length_ms=120000, length_ms=300) for _ in range(3)])
    data["max_output_seconds"] = 120
    with pytest.raises(native.WorldlineNativeError, match="memory budget"):
        native.load_job(*write_job(tmp_path, data))


@pytest.mark.parametrize("values", [[], [0.0] * 10, [1, float("nan")], [float("inf")]])
def test_invalid_native_output_cannot_be_reported_as_success(tmp_path, values):
    with pytest.raises(native.WorldlineNativeError):
        native._write_output(tmp_path / "out.wav", values, [])
    assert not (tmp_path / "out.wav").exists()


def test_pcm_writer_prevents_clipping_and_preserves_silence_intervals(tmp_path):
    output = tmp_path / "out.wav"
    result = native._write_output(output, [2.0] * 1000, [(100, 200)])
    assert result["gain"] == pytest.approx(.49)
    with wave.open(str(output), "rb") as source:
        assert source.getparams()[:3] == (1, 2, 44100)
        values = array("h", source.readframes(source.getnframes()))
    assert max(values) <= 32112
    assert list(values[100:200]) == [0] * 100


@pytest.fixture
def mock_pin(monkeypatch, tmp_path):
    monkeypatch.setattr(native, "inspect_library", lambda path: {"library_sha256": "pin"})
    return tmp_path / "lib.so"


def test_native_child_signal_does_not_crash_worker(mock_pin, monkeypatch):
    monkeypatch.setattr(native.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=-11, stdout="", stderr="segfault"))
    result = native.run_isolated(mock_pin)
    assert not result["ok"] and result["signal"] == 11
    assert result["stderr"] == "segfault"


def test_native_child_timeout_does_not_crash_worker(mock_pin, monkeypatch):
    def slow(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"], stderr=b"slow")
    monkeypatch.setattr(native.subprocess, "run", slow)
    result = native.run_isolated(mock_pin, timeout=.01)
    assert not result["ok"] and result["timed_out"]


def test_native_child_success_requires_matching_hash_and_requested_render(mock_pin, monkeypatch, job):
    child = {"ok": True, "api_verified": True, "library_sha256": "wrong", "phrase_render_verified": True}
    monkeypatch.setattr(native.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=json.dumps(child), stderr=""))
    assert not native.run_isolated(mock_pin, *job)["ok"]
    child["library_sha256"] = "pin"
    child["phrase_render_verified"] = False
    assert not native.run_isolated(mock_pin, *job)["ok"]


@pytest.mark.parametrize("timeout", [0, -1, 121, float("nan")])
def test_invalid_timeout_never_runs_native_child(mock_pin, monkeypatch, timeout):
    monkeypatch.setattr(native.subprocess, "run", lambda *args, **kwargs: pytest.fail("child should not run"))
    assert "timeout" in native.run_isolated(mock_pin, timeout=timeout)["error"]


def test_actual_pinned_api_probe_is_isolated_and_does_not_claim_teto_speech(pinned_library):
    result = native.run_isolated(pinned_library)
    assert result["ok"] and result["api_verified"] and result["abi_verified"]
    assert result["library_hash_verified"] and result["phrase_adapter_available"]
    assert result["phrase_render_verified"] is False
    assert result["portuguese_speech_verified"] is False


def test_actual_native_two_notes_pitch_change_pause_and_reversed_ends(tmp_path, pinned_library):
    source_wav(tmp_path / "source.wav")
    data = job_data(request(position_ms=400), request(position_ms=0), duration=700)
    data["silence_intervals_ms"] = [[300, 400]]
    data["curves"]["f0"] = [220 if i < 35 else 330 for i in range(71)]
    path, output = write_job(tmp_path, data)
    result = native.run_isolated(pinned_library, path, output)
    assert result["ok"], result
    assert result["phrase_render_verified"] and result["frames"] == 30871
    assert result["request_count"] == 2 and result["voice_quality_verified"] is False
    with wave.open(str(output), "rb") as rendered:
        values = array("h", rendered.readframes(rendered.getnframes()))
    assert all(value == 0 for value in values[13230:17640])
    assert max(abs(value) for value in values[:13230]) > 100
    assert max(abs(value) for value in values[17640:]) > 100
    # A compact autocorrelation measures actual acoustic pitch, not just API input.
    def estimated_hz(begin):
        segment = values[begin:begin+4000]
        lags = range(110, 260)
        lag = max(lags, key=lambda lag: sum(segment[i] * segment[i+lag] for i in range(len(segment)-260)))
        return 44100 / lag
    assert estimated_hz(3000) == pytest.approx(220, abs=8)
    assert estimated_hz(22000) == pytest.approx(330, abs=12)


def test_actual_native_volume_changes_output_energy(tmp_path, pinned_library):
    source_wav(tmp_path / "source.wav")
    result = []
    for volume in (50, 100):
        path, output = write_job(tmp_path, job_data(request(volume=volume)))
        rendered = native.run_isolated(pinned_library, path, output)
        assert rendered["ok"], rendered
        result.append(rendered["rms"])
    assert result[1] > result[0] * 1.5


def test_actual_native_preserves_declared_terminal_pause(tmp_path, pinned_library):
    source_wav(tmp_path / "source.wav")
    data = job_data(duration=500)
    data["duration_ms"] = 500
    data["silence_intervals_ms"] = [[300, 500]]
    path, output = write_job(tmp_path, data)
    result = native.run_isolated(pinned_library, path, output)
    assert result["ok"], result
    assert result["frames"] == 22051
    with wave.open(str(output), "rb") as rendered:
        values = array("h", rendered.readframes(rendered.getnframes()))
    assert max(abs(value) for value in values[:13230]) > 100
    assert all(value == 0 for value in values[13230:])


@pytest.mark.parametrize("duration,match", [(290, "duration_ms"), (305, "10 ms phrase grid"), (4000, "duration_ms")])
def test_declared_duration_cannot_truncate_or_overrun_the_native_phrase(job, duration, match):
    path, output = job
    value = json.loads(path.read_text())
    value["duration_ms"] = duration
    path.write_text(json.dumps(value))
    with pytest.raises(native.WorldlineNativeError, match=match):
        native.load_job(path, output)


def test_native_runtime_ships_complete_mit_notice():
    notice = (SCRIPT.parents[3] / "worldline-r/LICENSE.openutau.txt").read_text().rstrip()
    assert notice in SCRIPT.read_text()
