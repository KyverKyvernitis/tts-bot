from __future__ import annotations

from pathlib import Path

from updater.testes.fonte_core import caminho_fonte_core

ROOT = Path(__file__).resolve().parents[1]
UPDATER = caminho_fonte_core()
INSTALLER = ROOT / "updater" / "sistema" / "instalar.sh"


def test_path_units_watch_only_pending_candidate_jsons() -> None:
    canonical = ROOT / "updater" / "sistema" / "tts-bot-updater.path"
    assert not (ROOT / "deploy" / "systemd" / "tts-bot-updater.path").exists()
    assert not (ROOT / "deploy" / "systemd" / "vps" / "tts-bot-updater.path").exists()
    text = canonical.read_text(encoding="utf-8")
    assert "PathExistsGlob=/home/ubuntu/bot-update-staging/candidates/queue/pending/*.json" in text
    assert "Unit=tts-bot-updater.service" in text
    assert "WantedBy=paths.target" in text
    assert "PathChanged=" not in text


def test_installer_bootstraps_path_even_when_old_updater_overlay_omits_it() -> None:
    text = INSTALLER.read_text(encoding="utf-8")
    assert '"$rel" == "tts-bot-updater.path"' in text
    assert '"$UPDATER_SYSTEM_DIR/$rel"' in text
    assert '"$REPO_DIR/deploy/systemd/vps/$rel"' not in text
    assert '"$REPO_DIR/deploy/systemd/$rel"' not in text
    assert "tts-bot-updater.service tts-bot-updater.timer tts-bot-updater.path" in text


def test_path_enablement_follows_updater_timer_maintenance_policy() -> None:
    text = INSTALLER.read_text(encoding="utf-8")
    assert "disable --now tts-bot-updater.timer tts-bot-updater.path" in text
    assert "enable --now tts-bot-updater.path" in text
    assert "timer mantido como fallback" in text


def test_future_updater_overlay_and_classifier_include_path_unit() -> None:
    text = UPDATER.read_text(encoding="utf-8")
    assert "updater/sistema/tts-bot-updater.path" in text
    assert not (ROOT / "deploy" / "systemd" / "tts-bot-updater.path").exists()
    assert not (ROOT / "deploy" / "systemd" / "vps" / "tts-bot-updater.path").exists()
    overlay = text[text.index("build_vps_systemd_template_overlay() {") : text.index("\n\ndeploy_vps_systemd_units() {")]
    assert "tts-bot-updater.path" in overlay
