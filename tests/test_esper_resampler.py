from __future__ import annotations

import hashlib
import io
import json
import math
import os
from pathlib import Path
import signal
import struct
import subprocess
import sys
import time
import types
import wave
from concurrent.futures import ThreadPoolExecutor
import threading

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy/esper-utau/termux/resampler.py"


@pytest.fixture
def bridge():
    module = types.ModuleType("esper_resampler_test")
    module.__file__ = str(SCRIPT)
    exec(compile(SCRIPT.read_bytes(), str(SCRIPT), "exec"), module.__dict__)
    return module


def pcm(*, silent=False, seconds=.45, rate=44100, channels=1, width=2):
    target = io.BytesIO()
    with wave.open(target, "wb") as writer:
        writer.setparams((channels, width, rate, 0, "NONE", "not compressed"))
        frames = round(seconds * rate)
        if width == 2:
            source = b"".join(struct.pack("<h", 0 if silent else int(7000 * math.sin(2 * math.pi * 220 * i / rate)))
                              for i in range(frames))
        else:
            source = b"\x80" * frames * width
        writer.writeframes(source * channels)
    return target.getvalue()


def frq():
    count = 80
    return b"FREQ0003" + struct.pack("<id16si", 256, 220.0, b"\0" * 16, count) + struct.pack("<dd", 220, .1) * count


def arguments(source, target):
    return [str(source), str(target), "C4", "100", "", "0", "240.25", "30", "-400", "100", "15", "!140", "AA#100#"]


@pytest.fixture
def fake_runtime(tmp_path, bridge, monkeypatch):
    root = tmp_path / "runtime"
    release = root / "releases" / bridge.RELEASE
    release.mkdir(parents=True)
    original = tmp_path / "original-bank"
    output = tmp_path / "output"
    original.mkdir()
    output.mkdir()
    source, target = original / "recording.wav", output / "rendered.wav"
    source.write_bytes(pcm())
    monkeypatch.setattr(bridge, "runtime_info", lambda *_args, **_kwargs: {
        "release": release, "engine_sha256": bridge.ENGINE_SHA256,
        "config_sha256": bridge.CONFIG_SHA256, "engine_elf_arm64": True})
    monkeypatch.setattr(bridge.shutil, "which", lambda name: "/fake/proot-distro")
    calls = []
    private_before = []

    def run(command, job, deadline):
        calls.append(command)
        binds = dict(item.split(":", 1) for item in command[4:11:2])
        copied = next(Path(host) for host, guest in binds.items() if guest == "/opt/esper-source")
        assert (copied / "source.wav").read_bytes() == source.read_bytes()
        private_before.append({name: (copied / name).read_bytes()
                               for name in ("source.wav", "source.esp", "source_wav.frq")
                               if (copied / name).exists()})
        (copied / "source.esp").write_bytes(b"analysis")
        (copied / "source_wav.frq").write_bytes(frq())
        (job / "output.wav").write_bytes(pcm(seconds=.241))
        return "", ""

    monkeypatch.setattr(bridge, "run_guest", run)
    return types.SimpleNamespace(root=root, release=release, original=original, source=source, target=target,
                                 cache=root / "cache", calls=calls, run=run, private_before=private_before)


def synthesize(bridge, runtime, **updates):
    options = dict(root=runtime.root, cache=runtime.cache, container="voicepeak-arm64", timeout=10)
    options.update(updates)
    return bridge.synthesize(arguments(runtime.source, runtime.target), **options)


def test_bridge_renders_from_private_copy_and_never_mounts_original_voicebank(bridge, fake_runtime, monkeypatch):
    runtime = fake_runtime
    notices = runtime.original / "terms.txt"
    notices.write_text("Preserve these terms")
    original_frq = runtime.source.with_name("recording_wav.frq")
    original_frq.write_bytes(frq())
    before = {path.name: path.read_bytes() for path in runtime.original.iterdir()}
    monkeypatch.setenv("PHONE_WORKER_TETO_VOICEBANK_DIR", str(runtime.original))
    environment = dict(os.environ)
    result = synthesize(bridge, runtime)
    assert result["ok"] and result["wav_verified"]
    assert not result["original_frq_copied"]
    assert result["pitch_analysis_mode"] == "esper-native"
    assert result["analysis_cache_version"] == "native-pitch-v1"
    assert result["implementation_id"] == hashlib.sha256(SCRIPT.read_bytes()).hexdigest()
    assert runtime.private_before[0] == {"source.wav": runtime.source.read_bytes()}
    assert not result["original_voicebank_changed"]
    assert runtime.target.read_bytes() == pcm(seconds=.241)
    assert {path.name: path.read_bytes() for path in runtime.original.iterdir()} == before
    assert dict(os.environ) == environment
    assert not any(str(runtime.original) in part for part in runtime.calls[0])
    assert "DOTNET_GCHeapHardLimit=40000000" in runtime.calls[0]
    assert "DOTNET_gcServer=0" in runtime.calls[0]
    assert result["gc_settings_requested"] == bridge.GC_ENVIRONMENT
    assert runtime.calls[0][-7] == "241"  # integer LENGTH, rounded up
    directory = Path(result["analysis_directory"])
    assert directory / "source.esp" != runtime.source.with_suffix(".esp")
    assert (directory / "source.esp").read_bytes() == b"analysis"
    assert (directory / "source_wav.frq").read_bytes() == frq()
    assert list((runtime.cache / "jobs").iterdir()) == []


def test_real_esper_initialization_under_limited_virtual_address_space(bridge, tmp_path):
    configured = os.environ.get("ESPER_TEST_X64_BINARY")
    if not configured or sys.platform != "linux" or bridge.platform.machine().lower() not in {"x86_64", "amd64"}:
        pytest.skip("requires pinned official ESPER Linux x64 binary for the GC reservation regression")
    import resource

    binary = Path(configured)
    assert hashlib.sha256(binary.read_bytes()).hexdigest() == "d613a715b451d9fff4dfa902e3fa55e3431a595833c61c369622405d21020eee"
    assert hashlib.sha256((binary.parent / "esper-config.ini").read_bytes()).hexdigest() == bridge.CONFIG_SHA256
    source = tmp_path / "synthetic.wav"
    source.write_bytes(pcm(seconds=.5))
    environment = {key: value for key, value in os.environ.items()
                   if not key.casefold().startswith(("dotnet_gc", "complus_gc"))}
    environment.update(LANG="C", LC_ALL="C", DOTNET_SYSTEM_GLOBALIZATION_INVARIANT="1",
                       DOTNET_BUNDLE_EXTRACT_BASE_DIR=str(tmp_path / "dotnet"))

    def restrict_virtual_space():
        # Reproduces failed GC reservation without exhausting physical RAM.
        limit = 16 * 1024 ** 3
        resource.setrlimit(resource.RLIMIT_AS, (limit, limit))

    output = tmp_path / "probe.wav"
    command = [str(binary), str(source), str(output), "C4", "100", "", "0", "240", "30",
               "-400", "100", "0", "!140", "AA#100#"]
    failed = subprocess.run(command, env=environment, capture_output=True, timeout=20,
                            preexec_fn=restrict_virtual_space)
    assert failed.returncode == 137
    assert b"GC heap initialization failed" in failed.stderr and b"0x8007000E" in failed.stderr
    assert not output.exists()
    environment.update(bridge.GC_ENVIRONMENT)
    fixed = subprocess.run(command, env=environment, capture_output=True, timeout=20,
                           preexec_fn=restrict_virtual_space)
    assert fixed.returncode == 0, fixed.stderr.decode(errors="replace")
    evidence = bridge.pcm_evidence(output.read_bytes(), output=True)
    assert evidence["wav_verified"] and evidence["sample_rate"] == 44100
    assert .08 <= evidence["duration_seconds"] <= .35


def test_private_recording_cache_reuses_source_and_changes_when_content_changes(bridge, fake_runtime):
    runtime = fake_runtime
    first = synthesize(bridge, runtime)
    copied = Path(first["analysis_directory"]) / "source.wav"
    stamp = copied.stat().st_mtime_ns
    second = synthesize(bridge, runtime)
    assert second["source_cache_key"] == first["source_cache_key"]
    assert copied.stat().st_mtime_ns == stamp
    runtime.source.write_bytes(pcm(seconds=.44))
    third = synthesize(bridge, runtime)
    assert third["source_cache_key"] != first["source_cache_key"]
    assert copied.read_bytes() == pcm()


def test_original_frq_changes_do_not_replace_native_analysis_or_modify_original(bridge, fake_runtime):
    runtime = fake_runtime
    first = synthesize(bridge, runtime)
    original_frq = runtime.source.with_name("recording_wav.frq")
    original_frq.write_bytes(b"another engine's private FRQ format")
    second = synthesize(bridge, runtime)
    assert first["source_cache_key"] == second["source_cache_key"]
    assert not second["original_frq_copied"]
    assert original_frq.read_bytes() == b"another engine's private FRQ format"
    assert runtime.private_before[1]["source_wav.frq"] == frq()
    assert runtime.private_before[1]["source.esp"] == b"analysis"


def test_symbolic_original_frq_is_ignored_and_never_followed_by_engine(bridge, fake_runtime):
    runtime = fake_runtime
    external = runtime.original.parent / "external.frq"
    external.write_bytes(frq())
    runtime.source.with_name("recording_wav.frq").symlink_to(external)
    result = synthesize(bridge, runtime)
    assert result["ok"] and not result["original_frq_copied"]
    assert external.read_bytes() == frq()
    assert runtime.source.with_name("recording_wav.frq").is_symlink()
    assert "source_wav.frq" not in runtime.private_before[0]


@pytest.mark.parametrize("original_frq", [None, b"existing legacy FRQ"])
def test_pre_fix_analysis_cache_is_preserved_and_never_reused(bridge, fake_runtime, original_frq):
    runtime = fake_runtime
    if original_frq is not None:
        runtime.source.with_name("recording_wav.frq").write_bytes(original_frq)
    identity = hashlib.sha256()
    for part in (bridge.ENGINE_SHA256.encode(), bridge.CONFIG_SHA256.encode(), runtime.source.read_bytes(),
                 original_frq or b"no-original-frq"):
        identity.update(len(part).to_bytes(8, "little"))
        identity.update(part)
    old_directory = runtime.cache / "sources" / identity.hexdigest()
    old_directory.mkdir(parents=True)
    (old_directory / "source.wav").write_bytes(runtime.source.read_bytes())
    (old_directory / "source.esp").write_bytes(b"contaminated legacy pitch")
    before = {p.name: p.read_bytes() for p in old_directory.iterdir()}
    result = synthesize(bridge, runtime)
    assert result["source_cache_key"] != identity.hexdigest()
    assert Path(result["analysis_directory"]) != old_directory
    assert runtime.private_before[0] == {"source.wav": runtime.source.read_bytes()}
    assert {p.name: p.read_bytes() for p in old_directory.iterdir()} == before


@pytest.mark.parametrize("requested,effective", [(100, 50), (200, 50), (20, 10)])
def test_native_volume_leaves_headroom_before_upstream_pcm16_conversion(bridge, fake_runtime, requested, effective):
    values = arguments(fake_runtime.source, fake_runtime.target)
    values[9] = str(requested)
    result = bridge.synthesize(values, root=fake_runtime.root, cache=fake_runtime.cache,
                               container="voicepeak-arm64", timeout=10)
    assert float(fake_runtime.calls[0][-4]) == effective
    assert result["requested_volume_percent"] == requested
    assert result["native_volume_percent"] == effective


@pytest.mark.parametrize("kind", ["source.wav", "source.esp", "source_wav.frq"])
def test_tampered_private_cache_cannot_redirect_engine_writes(bridge, fake_runtime, kind):
    runtime = fake_runtime
    result = synthesize(bridge, runtime)
    victim = runtime.original / "victim.txt"
    victim.write_bytes(b"unchanged")
    entry = Path(result["analysis_directory"]) / kind
    entry.unlink()
    entry.symlink_to(victim)
    runtime.calls.clear()
    with pytest.raises(bridge.EsperError, match="regular"):
        synthesize(bridge, runtime)
    assert victim.read_bytes() == b"unchanged" and runtime.calls == []


def test_invalid_engine_output_never_replaces_previous_output(bridge, fake_runtime, monkeypatch):
    runtime = fake_runtime
    old = pcm(seconds=.1)
    runtime.target.write_bytes(old)

    def invalid(_command, job, _deadline):
        (job / "output.wav").write_bytes(pcm(silent=True))
        return "", ""

    monkeypatch.setattr(bridge, "run_guest", invalid)
    with pytest.raises(bridge.EsperError, match="silencioso"):
        synthesize(bridge, runtime)
    assert runtime.target.read_bytes() == old
    assert list((runtime.cache / "jobs").iterdir()) == []


def test_output_symlinks_and_paths_in_voicebank_are_rejected(bridge, fake_runtime, monkeypatch):
    runtime = fake_runtime
    victim = runtime.original / "victim.wav"
    victim.write_bytes(pcm())
    runtime.target.symlink_to(victim)
    with pytest.raises(bridge.EsperError, match="regular"):
        synthesize(bridge, runtime)
    assert victim.read_bytes() == pcm()
    runtime.target.unlink()
    monkeypatch.setenv("PHONE_WORKER_TETO_VOICEBANK_DIR", str(runtime.original))
    nested = runtime.original / "other"
    nested.mkdir()
    with pytest.raises(bridge.EsperError, match="voicebank original"):
        bridge.synthesize(arguments(runtime.source, nested / "output.wav"), root=runtime.root,
                          cache=runtime.cache, container="voicepeak-arm64", timeout=10)
    assert runtime.calls == []


@pytest.mark.parametrize("data", [pcm(silent=True), pcm(channels=2), pcm(width=1), b"fake WAV"])
def test_invalid_original_wav_is_rejected_before_guest_execution(bridge, fake_runtime, data):
    fake_runtime.source.write_bytes(data)
    with pytest.raises(bridge.EsperError):
        synthesize(bridge, fake_runtime)
    assert fake_runtime.calls == [] and not fake_runtime.target.exists()


@pytest.mark.parametrize("index,value", [(6,"nan"), (6,"20001"), (3,"0"), (11,"!nan"),
                                         (12,"AA#99999#"), (12,"AA#bad#"), (12,"A"), (4,"$(bad)")])
def test_unbounded_or_malformed_arguments_fail_before_native_execution(bridge, index, value):
    args = arguments("source.wav", "out.wav")
    args[index] = value
    with pytest.raises(bridge.EsperError):
        bridge.validate_arguments(args)


def test_cli_preserves_utau_positions_empty_flags_negative_cutoff_and_decimal_length(bridge, tmp_path, monkeypatch, capsys):
    original = arguments(tmp_path / "bank/source.wav", tmp_path / "output/test.wav")
    original[6] = "137.4"
    original[8] = "-418.001"
    calls = []

    def synthesize(values, **options):
        calls.append((values, options))
        validated = bridge.validate_arguments(values)
        assert len(validated) == 13
        assert validated[4] == "" and validated[6] == "138"
        assert validated[8] == "-418.001" and validated[12] == "AA#100#"
        return {"ok": True, "length": validated[6]}

    monkeypatch.setattr(bridge, "synthesize", synthesize)
    assert bridge.main(["--root", str(tmp_path / "runtime"), "--container", "voicepeak-arm64", *original]) == 0
    assert calls[0][0] == original
    assert calls[0][1]["container"] == "voicepeak-arm64"
    assert json.loads(capsys.readouterr().out) == {"ok": True, "length": "138"}


@pytest.mark.parametrize("identifier", ["", "not-a-sha256", "a" * 63, "a" * 65, "0" * 64])
def test_cli_implementation_guard_fails_before_rendering_or_creating_cache(bridge, tmp_path, monkeypatch, capsys,
                                                                        identifier):
    calls = []
    monkeypatch.setattr(bridge, "synthesize", lambda *a, **kw: calls.append((a, kw)))
    root = tmp_path / "runtime"
    assert bridge.main(["--root", str(root), "--implementation-id", identifier,
                        *arguments(tmp_path / "source.wav", tmp_path / "out.wav")]) == 1
    report = json.loads(capsys.readouterr().err)
    assert not report["ok"] and "implementation-id" in report["error"]
    assert calls == [] and not root.exists()


def test_cli_accepts_matching_implementation_hash_as_cache_identity(bridge, tmp_path, monkeypatch, capsys):
    expected = hashlib.sha256(SCRIPT.read_bytes()).hexdigest()
    calls = []

    def render(values, **options):
        calls.append(values)
        return {"ok": True}

    monkeypatch.setattr(bridge, "synthesize", render)
    args = arguments(tmp_path / "source.wav", tmp_path / "out.wav")
    assert bridge.main(["--implementation-id", expected, *args]) == 0
    assert calls == [args] and json.loads(capsys.readouterr().out)["ok"]


@pytest.mark.parametrize("recording", ["_e+_he+_e+_e+_e+-.wav", "_ou+_hou+_ou+-.wav", "_lau+_lau+_l-.wav"])
def test_real_esper_native_pitch_avoids_original_frq_zero_frame_bug(bridge, fake_runtime, monkeypatch, recording):
    binary_path = os.environ.get("ESPER_TEST_X64_BINARY")
    archive_path = os.environ.get("TETO_ENGLISH_TEST_ARCHIVE")
    if not binary_path or not archive_path or sys.platform != "linux" or bridge.platform.machine().lower() not in {"x86_64", "amd64"}:
        pytest.skip("requires official ESPER Linux x64 and official Teto English archive for the FRQ regression")
    import statistics
    import zipfile

    binary = Path(binary_path)
    assert hashlib.sha256(binary.read_bytes()).hexdigest() == "d613a715b451d9fff4dfa902e3fa55e3431a595833c61c369622405d21020eee"
    assert hashlib.sha256((binary.parent / "esper-config.ini").read_bytes()).hexdigest() == bridge.CONFIG_SHA256
    archive = Path(archive_path)
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == "addb3ab9dbe3dce7cb40fe6ba0c93ab5814d02acf5b091d0a762de370b813d6e"

    with zipfile.ZipFile(archive) as bank:
        wav_entry = next(name for name in bank.namelist() if name.endswith("/" + recording))
        frq_entry = wav_entry[:-4] + "_wav.frq"
        recording_data, original_frq = bank.read(wav_entry), bank.read(frq_entry)
    runtime = fake_runtime
    runtime.source.write_bytes(recording_data)
    frq_file = runtime.source.with_name("recording_wav.frq")
    frq_file.write_bytes(original_frq)
    frame_count = struct.unpack_from("<i", original_frq, 36)[0]
    source_f0 = [struct.unpack_from("<d", original_frq, 40 + i * 16)[0] for i in range(frame_count)]
    expected_f0 = statistics.median(f for f in source_f0 if f > 0)
    assert any(f == 0 for f in source_f0) and 250 < expected_f0 < 300
    environment = os.environ.copy()
    environment.update(bridge.GC_ENVIRONMENT)
    environment.update(LANG="C", LC_ALL="C", DOTNET_SYSTEM_GLOBALIZATION_INVARIANT="1",
                       DOTNET_BUNDLE_EXTRACT_BASE_DIR=str(runtime.root / "dotnet"))

    def median_analysis_f0(path):
        analysis = path.read_bytes()
        assert struct.unpack_from("<I?", analysis) == (12, False)
        n_voiced, n_unvoiced, step, frames = struct.unpack_from("<HHii", analysis, 5)
        assert (n_voiced, n_unvoiced, step) == (33, 257, 256)
        width = 1 + 2 * n_voiced + n_unvoiced
        periods = [struct.unpack_from("<f", analysis, 17 + i * width * 4)[0] for i in range(frames)]
        return statistics.median(44100 / period for period in periods if period > 0)

    # Reproduce the upstream problem in a separate private source directory.
    legacy = runtime.root / "legacy"
    legacy.mkdir()
    (legacy / "source.wav").write_bytes(recording_data)
    (legacy / "source_wav.frq").write_bytes(original_frq)
    bad_args = arguments(legacy / "source.wav", legacy / "output.wav")
    bad_args[6], bad_args[9] = "241", "50"
    failed_analysis = subprocess.run([str(binary), *bad_args], env=environment, capture_output=True, timeout=20)
    assert failed_analysis.returncode == 0, failed_analysis.stderr.decode(errors="replace")
    wrong_f0 = median_analysis_f0(legacy / "source.esp")
    assert abs(wrong_f0 / expected_f0 - 1) > .6

    def real_native_run(command, job, deadline):
        mounts = dict(item.split(":", 1) for item in command[4:11:2])
        copied = next(Path(host) for host, guest in mounts.items() if guest == "/opt/esper-source")
        assert not (copied / "source_wav.frq").exists()
        engine_args = command[command.index("/opt/esper-engine/ESPER-Utau") + 1:]
        for host, guest in mounts.items():
            engine_args = [arg.replace(guest, host) for arg in engine_args]
        completed = subprocess.run([str(binary), *engine_args], env=environment, capture_output=True,
                                   timeout=max(.1, deadline - time.monotonic()))
        assert completed.returncode == 0, completed.stderr.decode(errors="replace")
        return completed.stdout.decode(), completed.stderr.decode()

    monkeypatch.setattr(bridge, "run_guest", real_native_run)
    result = synthesize(bridge, runtime, timeout=20)
    corrected_f0 = median_analysis_f0(Path(result["analysis_directory"]) / "source.esp")
    assert abs(corrected_f0 / expected_f0 - 1) < .08
    assert result["pitch_analysis_mode"] == "esper-native" and not result["original_frq_copied"]
    assert runtime.source.read_bytes() == recording_data and frq_file.read_bytes() == original_frq
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == "addb3ab9dbe3dce7cb40fe6ba0c93ab5814d02acf5b091d0a762de370b813d6e"


def test_real_esper_headroom_prevents_pcm16_wrap_on_native_pitch_teto_fragment(bridge, tmp_path):
    binary_path = os.environ.get("ESPER_TEST_X64_BINARY")
    archive_path = os.environ.get("TETO_ENGLISH_TEST_ARCHIVE")
    if not binary_path or not archive_path or sys.platform != "linux" or bridge.platform.machine().lower() not in {"x86_64", "amd64"}:
        pytest.skip("requires official ESPER Linux x64 and official Teto English archive for the PCM16 regression")
    import zipfile

    binary, archive = Path(binary_path), Path(archive_path)
    assert hashlib.sha256(binary.read_bytes()).hexdigest() == "d613a715b451d9fff4dfa902e3fa55e3431a595833c61c369622405d21020eee"
    assert hashlib.sha256((binary.parent / "esper-config.ini").read_bytes()).hexdigest() == bridge.CONFIG_SHA256
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == "addb3ab9dbe3dce7cb40fe6ba0c93ab5814d02acf5b091d0a762de370b813d6e"
    source = tmp_path / "source.wav"
    with zipfile.ZipFile(archive) as bank:
        entry = next(name for name in bank.namelist() if name.endswith("/_lau+_lau+_l-.wav"))
        source.write_bytes(bank.read(entry))
    assert not source.with_name("source_wav.frq").exists()
    environment = os.environ.copy()
    environment.update(bridge.GC_ENVIRONMENT)
    environment.update(LANG="C", LC_ALL="C", DOTNET_SYSTEM_GLOBALIZATION_INVARIANT="1",
                       DOTNET_BUNDLE_EXTRACT_BASE_DIR=str(tmp_path / "dotnet"))
    rendered = {}
    for volume in (100, 50):
        output = tmp_path / f"volume-{volume}.wav"
        args = [str(source), str(output), "C4", "100", "B0", "649.335", "214", "123.332", "-366.668",
                str(volume), "15", "!140", "/5/6#7#/5/5/4/3/3/2/1/0/z/y/x/x/w/v/u/t/s/r/r/q/q/p#11#/o#9#"]
        result = subprocess.run([str(binary), *args], env=environment, capture_output=True, timeout=20)
        assert result.returncode == 0, result.stderr.decode(errors="replace")
        with wave.open(str(output), "rb") as reader:
            assert (reader.getframerate(), reader.getnchannels(), reader.getsampwidth()) == (44100, 1, 2)
            data = reader.readframes(reader.getnframes())
        rendered[volume] = struct.unpack("<" + str(len(data) // 2) + "h", data)
    full, safe = rendered[100], rendered[50]
    assert len(full) == len(safe)
    assert max(abs(value * 2) for value in safe) > 32767
    assert any(abs(a - b) > 40000 for a, b in zip(full, full[1:]))
    assert not any(abs(a - b) > 40000 for a, b in zip(safe, safe[1:]))
    rewrapped = [((value * 2 + 32768) % 65536) - 32768 for value in safe]
    assert max(abs(a - b) for a, b in zip(full, rewrapped)) <= 2


def test_deadline_from_phrase_is_respected_before_creating_guest(bridge, fake_runtime, monkeypatch):
    monkeypatch.setenv("PHONE_WORKER_ESPER_DEADLINE", str(time.monotonic() - 1))
    with pytest.raises(TimeoutError):
        synthesize(bridge, fake_runtime)
    assert fake_runtime.calls == [] and not fake_runtime.cache.exists()


def test_source_lock_wait_is_bounded_and_released(bridge, tmp_path):
    path = tmp_path / "lock"
    with bridge.source_lock(path, time.monotonic() + 1):
        with pytest.raises(TimeoutError):
            with bridge.source_lock(path, time.monotonic() + .04):
                pytest.fail("second renderer should wait for the same recording")
    with bridge.source_lock(path, time.monotonic() + 1):
        pass


def test_two_cold_bridges_can_create_shared_cache_directory_without_false_failure(bridge, tmp_path, monkeypatch):
    directory = tmp_path / "new-cache"
    original = Path.lstat
    both_missing = threading.Barrier(2)

    def synchronized_lstat(path):
        try:
            return original(path)
        except FileNotFoundError:
            if path == directory:
                both_missing.wait(timeout=2)
            raise

    monkeypatch.setattr(Path, "lstat", synchronized_lstat)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(bridge.real_directories, directory, create=True) for _ in range(2)]
        for result in futures:
            result.result(timeout=3)
    assert directory.is_dir() and not directory.is_symlink()


@pytest.fixture
def pinned_fixture(tmp_path, bridge, monkeypatch):
    monkeypatch.setattr(bridge.platform, "machine", lambda: "aarch64")
    root = tmp_path / "runtime"
    release = root / "releases" / bridge.RELEASE
    release.mkdir(parents=True)
    engine = bytearray(128)
    engine[:6] = b"\x7fELF\x02\x01"
    engine[18:20] = (183).to_bytes(2, "little")
    engine[64:] = bytes(range(64))
    (release / "ESPER-Utau").write_bytes(engine)
    (release / "ESPER-Utau").chmod(0o700)
    config = b"fixture config\n"
    (release / "esper-config.ini").write_bytes(config)
    monkeypatch.setattr(bridge, "ENGINE_BYTES", len(engine))
    monkeypatch.setattr(bridge, "ENGINE_SHA256", hashlib.sha256(engine).hexdigest())
    monkeypatch.setattr(bridge, "CONFIG_BYTES", len(config))
    monkeypatch.setattr(bridge, "CONFIG_SHA256", hashlib.sha256(config).hexdigest())
    cache = root / "cache"
    cache.mkdir()
    return root, cache, release


def test_release_pin_validates_elf_hash_and_rechecks_mutated_runtime(bridge, pinned_fixture):
    root, cache, release = pinned_fixture
    first = bridge.runtime_info(root, cache, deadline=time.monotonic() + 2)
    assert first["engine_elf_arm64"]
    assert bridge.runtime_info(root, cache, deadline=time.monotonic() + 2) == first
    engine = release / "ESPER-Utau"
    data = bytearray(engine.read_bytes())
    data[-1] ^= 1
    engine.write_bytes(data)
    with pytest.raises(bridge.EsperError, match="SHA256"):
        bridge.runtime_info(root, cache, deadline=time.monotonic() + 2)


def test_release_ini_pin_and_architecture_are_required(bridge, pinned_fixture, monkeypatch):
    root, cache, release = pinned_fixture
    config = release / "esper-config.ini"
    data = bytearray(config.read_bytes())
    data[0] ^= 1
    config.write_bytes(data)
    with pytest.raises(bridge.EsperError, match="config.ini"):
        bridge.runtime_info(root, cache, deadline=time.monotonic() + 2)
    monkeypatch.setattr(bridge.platform, "machine", lambda: "x86_64")
    with pytest.raises(bridge.EsperError, match="ARM64"):
        bridge.runtime_info(root, cache, deadline=time.monotonic() + 2)


def test_fake_proot_success_with_signal_report_is_failure(bridge, tmp_path):
    command = [sys.executable, "-c", "print('proot info: vpid 1: terminated with signal 11')"]
    with pytest.raises(bridge.EsperError, match="guest ESPER falhou"):
        bridge.run_guest(command, tmp_path, time.monotonic() + 3)


def test_guest_nonzero_exit_never_becomes_success(bridge, tmp_path):
    with pytest.raises(bridge.EsperError, match="code 7"):
        bridge.run_guest([sys.executable, "-c", "raise SystemExit(7)"], tmp_path, time.monotonic() + 3)


def alive(pid):
    try:
        state = Path(f"/proc/{pid}/stat").read_text().split()[2]
        return state != "Z"
    except FileNotFoundError:
        return False


@pytest.mark.parametrize("end", ["parent-pipe", "deadline"])
def test_supervisor_kills_guest_and_grandchild_when_parent_or_deadline_ends(bridge, tmp_path, end):
    pidfile = tmp_path / "pids.json"
    program = ("import subprocess,sys,os,time,json; from pathlib import Path; "
               "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(20)']); "
               f"Path({str(pidfile)!r}).write_text(json.dumps([os.getpid(),p.pid]));time.sleep(20)")
    request = tmp_path / "supervision.json"
    request.write_text(json.dumps({"command": [sys.executable, "-c", program],
                                   "deadline": time.monotonic() + (1.0 if end == "deadline" else 5)}))
    supervisor = subprocess.Popen([sys.executable, str(SCRIPT), "--_supervise", str(request)],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    pids = []
    try:
        deadline = time.monotonic() + 2
        while not pidfile.exists() and time.monotonic() < deadline:
            time.sleep(.02)
        assert pidfile.exists()
        pids = json.loads(pidfile.read_text())
        assert all(alive(pid) for pid in pids)
        if end == "parent-pipe":
            supervisor.stdin.close()
        supervisor.wait(timeout=3)
        assert supervisor.returncode == (125 if end == "parent-pipe" else 124)
        assert all(not alive(pid) for pid in pids)
    finally:
        if supervisor.stdin and not supervisor.stdin.closed:
            supervisor.stdin.close()
        if supervisor.poll() is None:
            supervisor.kill()
            supervisor.wait()
        for pid in pids:
            if alive(pid):
                os.kill(pid, signal.SIGKILL)


def test_probe_is_real_synthesis_call_and_does_not_claim_teto(bridge, fake_runtime, monkeypatch):
    runtime = fake_runtime
    recorded = []

    def render(args, **kwargs):
        recorded.append((args, kwargs, Path(args[0]).read_bytes()))
        assert bridge.pcm_evidence(recorded[-1][2], output=False)["wav_verified"]
        return {"ok": True, "output": args[1], "wav_verified": True}

    monkeypatch.setattr(bridge, "synthesize", render)
    result = bridge.probe(root=runtime.root, cache=runtime.cache, container="voicepeak-arm64", timeout=10)
    assert result["synthetic_render_verified"]
    assert not result["teto_synthesis_verified"] and not result["portuguese_speech_verified"]
    assert len(recorded[0][0]) == 13 and recorded[0][0][6] == "240"
    assert Path(recorded[0][0][0]).parent != Path(recorded[0][0][1]).parent
    assert not Path(recorded[0][0][0]).exists()
