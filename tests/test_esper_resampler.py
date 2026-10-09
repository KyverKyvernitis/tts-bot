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

    def run(command, job, deadline):
        calls.append(command)
        binds = dict(item.split(":", 1) for item in command[4:11:2])
        copied = next(Path(host) for host, guest in binds.items() if guest == "/opt/esper-source")
        assert (copied / "source.wav").read_bytes() == source.read_bytes()
        (copied / "source.esp").write_bytes(b"analysis")
        (copied / "source_wav.frq").write_bytes(frq())
        (job / "output.wav").write_bytes(pcm(seconds=.241))
        return "", ""

    monkeypatch.setattr(bridge, "run_guest", run)
    return types.SimpleNamespace(root=root, release=release, original=original, source=source, target=target,
                                 cache=root / "cache", calls=calls, run=run)


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
    assert result["original_frq_copied"]
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


def test_original_frq_changes_private_analysis_identity(bridge, fake_runtime):
    runtime = fake_runtime
    first = synthesize(bridge, runtime)
    runtime.source.with_name("recording_wav.frq").write_bytes(frq())
    second = synthesize(bridge, runtime)
    assert first["source_cache_key"] != second["source_cache_key"]
    assert second["original_frq_copied"]


def test_symbolic_frq_cannot_be_used_to_modify_original_bank(bridge, fake_runtime):
    runtime = fake_runtime
    external = runtime.original.parent / "external.frq"
    external.write_bytes(frq())
    runtime.source.with_name("recording_wav.frq").symlink_to(external)
    with pytest.raises(bridge.EsperError, match="regular"):
        synthesize(bridge, runtime)
    assert external.read_bytes() == frq() and runtime.calls == []


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
