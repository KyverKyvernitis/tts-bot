"""Regression checks: keep the Termux worker, retire the separate Android APK."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_public_app_endpoints_removed():
    source = (ROOT / "webserver.py").read_text(encoding="utf-8")
    assert '@app.post("/core-worker/enroll/apk-child")' not in source
    assert '@app.post("/core-worker/app/publish")' not in source
    assert '@app.get("/core-worker/app/latest.json")' not in source
    assert '@app.post("/core-worker/app/jobs/fetch")' not in source
    assert '@app.post("/core-worker/app/fcm-token")' not in source
    for route in ('/core-worker/pair', '/core-worker/heartbeat', '/core-worker/jobs/poll', '/core-worker/agent/latest'):
        assert route in source


def test_phone_worker_keeps_music_and_tts_without_apk_jobs():
    source = (ROOT / "deploy/termux/phone-worker/phone_worker.py").read_text(encoding="utf-8")
    assert '"voice_agent_play_tts"' in source
    assert '"music_agent_command"' in source
    assert '"worker_update"' in source
    assert 'elif kind == "apk_build_debug"' not in source
    assert 'elif kind == "apk_publish_last"' not in source
    assert '_start_apk_child_auto_enrollment()' not in source


def test_automation_only_updates_termux():
    source = (ROOT / "scripts/core-worker-automation.py").read_text(encoding="utf-8")
    assert 'def queue_agent_updates(' in source
    assert 'def queue_boot_repairs(' in source
    assert 'def queue_apk_build(' not in source
    assert '"queue-apk-build"' not in source
    assert "queue_apk_build(" not in source[source.index("def after_update("): ]


def test_retired_android_source_removed():
    assert not any(p.is_file() for p in (ROOT / "android/core-worker-app").rglob("*"))
    assert not (ROOT / "deploy/termux/phone-worker/apk_identity.py").exists()


def test_removed_apk_scripts_are_not_installed_by_termux():
    for rel in (
        "deploy/termux/phone-worker/install.sh",
        "deploy/termux/phone-worker/bootstrap-phone-worker.sh",
        "scripts/sync-phone-worker.sh",
    ):
        source = (ROOT / rel).read_text(encoding="utf-8")
        assert "accept-core-worker-on-device.sh" not in source
        assert "apk_identity.py" not in source
