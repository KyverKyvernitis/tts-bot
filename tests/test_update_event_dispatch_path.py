from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UPDATER = ROOT / "scripts" / "tts-bot-update.sh"
INSTALLER = ROOT / "scripts" / "install-vps-systemd-units.sh"


def test_path_units_watch_only_pending_candidate_jsons() -> None:
    root = ROOT / "deploy" / "systemd" / "tts-bot-updater.path"
    vps = ROOT / "deploy" / "systemd" / "vps" / "tts-bot-updater.path"
    assert root.read_text(encoding="utf-8") == vps.read_text(encoding="utf-8")
    text = root.read_text(encoding="utf-8")
    assert "PathExistsGlob=/home/ubuntu/bot-update-staging/candidates/queue/pending/*.json" in text
    assert "Unit=tts-bot-updater.service" in text
    assert "WantedBy=paths.target" in text
    assert "PathChanged=" not in text


def test_installer_bootstraps_path_even_when_old_updater_overlay_omits_it() -> None:
    text = INSTALLER.read_text(encoding="utf-8")
    assert '"$rel" == "tts-bot-updater.path"' in text
    assert '"$REPO_DIR/deploy/systemd/vps/$rel"' in text
    assert '"$REPO_DIR/deploy/systemd/$rel"' in text
    assert "tts-bot-updater.service tts-bot-updater.timer tts-bot-updater.path" in text


def test_path_enablement_follows_updater_timer_maintenance_policy() -> None:
    text = INSTALLER.read_text(encoding="utf-8")
    assert "disable --now tts-bot-updater.timer tts-bot-updater.path" in text
    assert "enable --now tts-bot-updater.path" in text
    assert "timer mantido como fallback" in text


def test_future_updater_overlay_and_classifier_include_path_unit() -> None:
    text = UPDATER.read_text(encoding="utf-8")
    assert "deploy/systemd/tts-bot-updater.path" in text
    assert "deploy/systemd/vps/tts-bot-updater.path" in text
    overlay = text[text.index("build_vps_systemd_template_overlay() {") : text.index("\n\ndeploy_vps_systemd_units() {")]
    assert "tts-bot-updater.path" in overlay
