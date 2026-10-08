"""VOICEPEAK contract with a fake executable, never proprietary engine assets."""
from __future__ import annotations

import io
import json
import os
from pathlib import Path
import sys
import wave

import pytest

WORKER_DIR = Path(__file__).resolve().parents[1] / "deploy" / "termux" / "phone-worker"
if str(WORKER_DIR) not in sys.path:
    sys.path.insert(0, str(WORKER_DIR))
from phone_worker_runtime.tts_providers import VoicepeakRenderer
from teto_renderer.phonemizer import prepare_voicepeak_text


@pytest.fixture
def voicepeak_assets(tmp_path, monkeypatch):
    for name in tuple(os.environ):
        if name.startswith("PHONE_WORKER_VOICEPEAK_") or name in {"PHONE_WORKER_TETO_ENABLED", "PHONE_WORKER_TETO_MAX_CHARACTERS", "PHONE_WORKER_TETO_MAX_AUDIO_SECONDS", "VOICEPEAK_TOKEN"}:
            monkeypatch.delenv(name, raising=False)
    command = tmp_path / "Fake VOICEPEAK CLI"
    state = tmp_path / "state.json"
    log = tmp_path / "args.jsonl"
    state.write_text(json.dumps({"narrators": ["重音テト"], "help": "VOICEPEAK fake-engine-1", "emotions": ["happy", "sasayaki"]}))
    command.write_text(f"#!{sys.executable}\n" + '''import json, pathlib, sys, time, wave
root = pathlib.Path(__file__).parent
state = json.loads((root / "state.json").read_text())
args = sys.argv[1:]
with (root / "args.jsonl").open("a") as log:
    log.write(json.dumps(args, ensure_ascii=False) + "\\n")
time.sleep(state.get("delay", 0))
if args == ["--list-narrator"]:
    print("\\n".join(state.get("narrators", [])), file=sys.stderr if state.get("stderr") else sys.stdout)
elif args == ["--help"]:
    print(state["help"], file=sys.stderr if state.get("stderr") else sys.stdout)
elif args[:1] == ["--list-emotion"]:
    print("\\n".join(state.get("emotions", [])), file=sys.stderr if state.get("stderr") else sys.stdout)
else:
    text = args[args.index("-s") + 1]
    assert len(text) <= 140
    out = pathlib.Path(args[args.index("-o") + 1])
    if state.get("broken"):
        out.write_bytes(b"bad wav")
    elif state.get("oversize"):
        out.write_bytes(b"x" * state["oversize"])
    else:
        frames = state.get("frames", len(text) * 20)
        with wave.open(str(out), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(state.get("rate", 24000))
            wav.writeframes(b"\\x00\\x00" * frames)
''', encoding="utf-8")
    command.chmod(0o755)
    monkeypatch.setenv("PHONE_WORKER_TETO_ENABLED", "true")
    monkeypatch.setenv("PHONE_WORKER_VOICEPEAK_COMMAND", str(command))

    def configure(**values):
        current = json.loads(state.read_text())
        current.update(values)
        state.write_text(json.dumps(current))

    def calls():
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    return command, configure, calls


def test_native_exact_teto_narrator_and_real_cli_arguments(voicepeak_assets, monkeypatch):
    command, configure, calls = voicepeak_assets
    monkeypatch.setenv("PHONE_WORKER_VOICEPEAK_SPEED", "125")
    monkeypatch.setenv("PHONE_WORKER_VOICEPEAK_PITCH", "30")
    monkeypatch.setenv("PHONE_WORKER_VOICEPEAK_EMOTION", "happy=25,sasayaki=50")
    renderer = VoicepeakRenderer()
    status = renderer.status()
    assert status["ready"] and status["narrator"] == "重音テト"
    assert status["backend"] == "voicepeak" and status["source"] == "native"
    assert status["phonemizer_version"] == "ptbr-kana-v1"
    rendered = renderer.synthesize("こんにちは！")
    args = next(args for args in calls() if "-s" in args)
    assert args[:4] == ["-s", "こんにちは！", "-n", "重音テト"]
    assert args[6:] == ["--speed", "125", "--pitch", "30", "-e", "happy=25,sasayaki=50"]
    assert rendered["audio"].startswith(b"RIFF")
    assert rendered["voicebank_profile"] == "voicepeak-ja"
    assert rendered["renderer_fingerprint"] == renderer.fingerprint()
    assert rendered["experimental_pronunciation"] is False
    assert str(command) not in str(status)


@pytest.mark.parametrize("narrators", (["別の声"], ["重音テト Power"], ["foo重音テト"]))
def test_narrator_absence_never_uses_another_voice(voicepeak_assets, narrators):
    _, configure, calls = voicepeak_assets
    configure(narrators=narrators)
    renderer = VoicepeakRenderer()
    assert not renderer.status()["ready"]
    with pytest.raises(RuntimeError, match="声|voz licenciada"):
        renderer.synthesize("こんにちは")
    assert not any("-s" in args for args in calls())


def test_status_cache_and_configuration_change_invalidate_fingerprint(voicepeak_assets, monkeypatch):
    _, configure, calls = voicepeak_assets
    renderer = VoicepeakRenderer()
    first = renderer.fingerprint()
    renderer.status()
    assert len(calls()) == 2
    monkeypatch.setenv("PHONE_WORKER_VOICEPEAK_SPEED", "120")
    second = renderer.fingerprint()
    assert second != first and len(calls()) == 4
    configure(help="VOICEPEAK fake-engine-2")
    assert renderer.status(force=True)["fingerprint"] != second
    monkeypatch.setenv("PHONE_WORKER_VOICEPEAK_TEXT_MODE", "ptbr-kana")
    third = renderer.fingerprint()
    assert third != second
    monkeypatch.setenv("PHONE_WORKER_TETO_MAX_AUDIO_SECONDS", "1")
    fourth = renderer.fingerprint()
    assert fourth != third
    monkeypatch.setenv("PHONE_WORKER_TETO_MAX_CHARACTERS", "100")
    assert renderer.fingerprint() != fourth


def test_prepared_kana_is_chunked_without_truncation(voicepeak_assets, monkeypatch):
    _, configure, calls = voicepeak_assets
    monkeypatch.setenv("PHONE_WORKER_VOICEPEAK_TEXT_MODE", "ptbr-kana")
    original = "fax " * 45
    reading = prepare_voicepeak_text(original.strip(), mode="ptbr-kana")
    assert len(original) <= 180 and len(reading) > 140
    result = VoicepeakRenderer().synthesize(original)
    spoken = [args[args.index("-s") + 1] for args in calls() if "-s" in args]
    assert len(spoken) > 1 and all(len(chunk) <= 140 for chunk in spoken)
    assert "".join(spoken) == reading
    with wave.open(io.BytesIO(result["audio"])) as wav:
        assert wav.getnframes() == len(reading) * 20
    assert result["reading_text"] == reading and result["experimental_pronunciation"] is True


def test_text_is_passed_as_data_without_shell_expansion(voicepeak_assets, tmp_path):
    _, configure, calls = voicepeak_assets
    marker = tmp_path / "injected"
    text = f"$(touch {marker})"
    VoicepeakRenderer().synthesize(text)
    assert not marker.exists()
    assert text in next(args for args in calls() if "-s" in args)


def test_shared_deadline_includes_discovery_and_all_chunks(voicepeak_assets):
    _, configure, _ = voicepeak_assets
    configure(delay=0.2)
    with pytest.raises((TimeoutError, RuntimeError), match="prazo"):
        VoicepeakRenderer().synthesize("こんにちは", timeout_seconds=0.45)


@pytest.mark.parametrize("values,limit,error", [({"broken": True}, 4096, "WAV PCM"), ({"oversize": 5000}, 4096, "grande demais"), ({"frames": 5000}, 4096, "grande demais")])
def test_native_output_validation(voicepeak_assets, values, limit, error):
    _, configure, _ = voicepeak_assets
    configure(**values)
    with pytest.raises(RuntimeError, match=error):
        VoicepeakRenderer().synthesize("こんにちは", max_audio_bytes=limit)


def test_combined_duration_limit_is_checked(voicepeak_assets, monkeypatch):
    _, configure, _ = voicepeak_assets
    monkeypatch.setenv("PHONE_WORKER_TETO_MAX_AUDIO_SECONDS", "1")
    configure(frames=16000)
    with pytest.raises(RuntimeError, match="duração"):
        VoicepeakRenderer().synthesize("あ" * 180)


@pytest.mark.parametrize("name,value", [("PHONE_WORKER_VOICEPEAK_SPEED", "201"), ("PHONE_WORKER_VOICEPEAK_PITCH", "-301"), ("PHONE_WORKER_VOICEPEAK_EMOTION", "happy=101"), ("PHONE_WORKER_VOICEPEAK_EMOTION", "happy=10,happy=20"), ("PHONE_WORKER_VOICEPEAK_TEXT_MODE", "pt-br")])
def test_invalid_configuration_is_not_ready(voicepeak_assets, monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    status = VoicepeakRenderer().status()
    assert not status["ready"] and status["last_error"]


def test_unknown_emotion_not_silently_ignored(voicepeak_assets, monkeypatch):
    monkeypatch.setenv("PHONE_WORKER_VOICEPEAK_EMOTION", "angry=50")
    assert not VoicepeakRenderer().status()["ready"]


def test_native_resource_guard_and_busy_admission(voicepeak_assets):
    renderer = VoicepeakRenderer(resource_guard=lambda: {"ok": False, "reason": "memória baixa"})
    with pytest.raises(RuntimeError, match="memória baixa"):
        renderer.synthesize("こんにちは")
    with renderer._lock:
        with pytest.raises(RuntimeError, match="ocupado"):
            renderer.synthesize("こんにちは")
        assert renderer.status(force=True)["busy"]
    assert not renderer._lock.locked()


def test_missing_configuration_and_disabled_engine(voicepeak_assets, monkeypatch):
    monkeypatch.delenv("PHONE_WORKER_VOICEPEAK_COMMAND")
    renderer = VoicepeakRenderer()
    assert not renderer.status()["ready"]
    monkeypatch.setenv("PHONE_WORKER_TETO_ENABLED", "false")
    assert renderer.status()["last_error"] == "PHONE_WORKER_TETO_ENABLED=false"


@pytest.mark.parametrize("kwargs", ({"timeout_seconds": float("nan")}, {"timeout_seconds": float("inf")}, {"timeout_seconds": 0}, {"max_audio_bytes": 44}, {"pitch_offset_semitones": -1}))
def test_invalid_request_parameters(voicepeak_assets, kwargs):
    with pytest.raises(ValueError):
        VoicepeakRenderer().synthesize("こんにちは", **kwargs)


def test_status_respects_short_caller_budget(voicepeak_assets):
    import time
    _, configure, _ = voicepeak_assets
    configure(delay=0.2)
    started = time.monotonic()
    status = VoicepeakRenderer().status(timeout_seconds=0.05)
    assert not status["ready"] and "prazo" in status["last_error"]
    assert time.monotonic() - started < 0.5


def test_emulated_cli_can_use_a_longer_configured_inventory_budget(voicepeak_assets, monkeypatch):
    _, configure, _ = voicepeak_assets
    # Two cold CLI processes exceed the historical five-second status budget.
    configure(delay=2.6)
    monkeypatch.setenv("PHONE_WORKER_VOICEPEAK_STATUS_TIMEOUT_SECONDS", "10")
    assert VoicepeakRenderer().status()["ready"]


@pytest.mark.parametrize("budget", ("0", "61", "invalid"))
def test_status_budget_configuration_is_bounded(voicepeak_assets, monkeypatch, budget):
    monkeypatch.setenv("PHONE_WORKER_VOICEPEAK_STATUS_TIMEOUT_SECONDS", budget)
    with pytest.raises(ValueError, match="STATUS_TIMEOUT_SECONDS"):
        VoicepeakRenderer().status()


def test_model_revision_invalidates_cache_identity_behind_an_unchanged_launcher(voicepeak_assets, monkeypatch):
    renderer = VoicepeakRenderer()
    initial = renderer.fingerprint()
    monkeypatch.setenv("PHONE_WORKER_VOICEPEAK_CACHE_REVISION", "termux-v1222-teto-1")
    revised = renderer.fingerprint()
    assert revised != initial
    monkeypatch.setenv("PHONE_WORKER_VOICEPEAK_CACHE_REVISION", "termux-v1222-teto-2")
    assert renderer.fingerprint() != revised


def test_successful_cli_help_and_inventory_on_stderr_are_recognized(voicepeak_assets):
    _, configure, _ = voicepeak_assets
    configure(stderr=True)
    renderer = VoicepeakRenderer()
    status = renderer.status()
    assert status['ready'] and status['engine_version'] == 'VOICEPEAK fake-engine-1'
    first = status['fingerprint']
    configure(help='VOICEPEAK fake-engine-2')
    assert renderer.status(force=True)['fingerprint'] != first


@pytest.mark.skipif(os.name != 'posix', reason='PRoot/QEMU process groups are POSIX')
def test_timeout_stops_launcher_descendants_before_next_request(tmp_path):
    import time
    heartbeat = tmp_path / 'child-heartbeat'
    script = tmp_path / 'emulated-cli'
    child = ("import pathlib,time; p=pathlib.Path(" + repr(str(heartbeat)) + "); "
             "exec('while True:\\n p.write_text(str(time.monotonic()))\\n time.sleep(0.03)')")
    script.write_text('#!' + sys.executable + '\nimport subprocess,time\n'
                      + 'subprocess.Popen([' + repr(sys.executable) + ', "-c", ' + repr(child) + '])\n'
                      + 'time.sleep(20)\n')
    script.chmod(0o700)
    with pytest.raises(TimeoutError, match='prazo'):
        VoicepeakRenderer()._run(str(script), [], time.monotonic() + 0.4)
    assert heartbeat.is_file()
    stopped = heartbeat.read_text()
    time.sleep(0.15)
    assert heartbeat.read_text() == stopped


@pytest.mark.parametrize("narrator,ready", (("Kasane Teto", True), ("Teto", True), ("別の声", False)))
def test_only_explicit_teto_inventory_aliases_are_supported(voicepeak_assets, monkeypatch, narrator, ready):
    _, configure, _ = voicepeak_assets
    configure(narrators=[narrator])
    monkeypatch.setenv("PHONE_WORKER_VOICEPEAK_NARRATOR", narrator)
    assert VoicepeakRenderer().status()["ready"] is ready
