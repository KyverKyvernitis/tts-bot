from __future__ import annotations

from pathlib import Path

from updater.testes.fonte_core import caminho_fonte_core

ROOT = Path(__file__).resolve().parents[2]
UPDATER = caminho_fonte_core()
INSTALLER = ROOT / "updater" / "sistema" / "instalar.sh"


def test_path_units_watch_only_pending_candidate_jsons() -> None:
    canonical = ROOT / "updater" / "sistema" / "bot-updater.path"
    assert not (ROOT / "deploy" / "systemd" / "bot-updater.path").exists()
    assert not (ROOT / "deploy" / "systemd" / "vps" / "bot-updater.path").exists()
    text = canonical.read_text(encoding="utf-8")
    assert "PathExistsGlob=/home/ubuntu/bot-update-staging/candidates/queue/pending/*.json" in text
    assert "Unit=bot-updater.service" in text
    assert "WantedBy=paths.target" in text
    assert "PathChanged=" not in text


def test_installer_requires_complete_canonical_overlay() -> None:
    text = INSTALLER.read_text(encoding="utf-8")
    assert 'required_updater_template' in text
    assert 'template obrigatório ausente' in text
    assert 'Ponte 47a' not in text
    assert 'Ponte da Wave 47a' not in text


def test_path_enablement_follows_updater_timer_maintenance_policy() -> None:
    text = INSTALLER.read_text(encoding="utf-8")
    assert 'set_updater_trigger_state bot-updater.timer "$UPDATER_TIMER_WAS_ENABLED" "$UPDATER_TIMER_WAS_ACTIVE"' in text
    assert 'set_updater_trigger_state bot-updater.path "$UPDATER_PATH_WAS_ENABLED" "$UPDATER_PATH_WAS_ACTIVE"' in text
    assert "timer mantido como fallback" in text


def test_future_updater_overlay_and_classifier_include_path_unit() -> None:
    text = UPDATER.read_text(encoding="utf-8")
    assert "updater/sistema/*.path" in text
    assert not (ROOT / "deploy" / "systemd" / "bot-updater.path").exists()
    assert not (ROOT / "deploy" / "systemd" / "vps" / "bot-updater.path").exists()
    overlay = text[text.index("build_vps_systemd_template_overlay() {") : text.index("\n\ndeploy_vps_systemd_units() {")]
    assert "bot-updater.path" in overlay
