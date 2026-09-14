from __future__ import annotations

from pathlib import Path

from updater.testes.fonte_core import ler_fonte_core


ROOT = Path(__file__).resolve().parents[2]
SISTEMA = ROOT / "updater" / "sistema"
SUDOERS = ROOT / "updater" / "sudoers"
INSTALADOR = SISTEMA / "instalar.sh"
FACHADA = ROOT / "scripts" / "install-vps-systemd-units.sh"

UNITS_UPDATER = (
    "tts-bot-updater.service",
    "tts-bot-updater.timer",
    "tts-bot-updater.path",
    "tts-bot-alert@.service",
)


def test_infraestrutura_canonica_fica_dentro_de_updater() -> None:
    assert INSTALADOR.is_file()
    assert (SISTEMA / "README.md").is_file()
    assert (SUDOERS / "tts-bot-updater-start").is_file()
    for nome in UNITS_UPDATER:
        assert (SISTEMA / nome).is_file()


def test_templates_canonicos_preservam_compatibilidade_legada() -> None:
    for nome in UNITS_UPDATER:
        canonico = (SISTEMA / nome).read_bytes()
        assert canonico == (ROOT / "deploy" / "systemd" / nome).read_bytes()
        assert canonico == (ROOT / "deploy" / "systemd" / "vps" / nome).read_bytes()

    assert (SUDOERS / "tts-bot-updater-start").read_bytes() == (
        ROOT / "deploy" / "sudoers.d" / "tts-bot-updater-start"
    ).read_bytes()


def test_instalador_legado_e_apenas_fachada() -> None:
    texto = FACHADA.read_text(encoding="utf-8")
    assert "updater/sistema/instalar.sh" in texto
    assert "install_units()" not in texto
    assert len(texto.splitlines()) < 10


def test_instalador_canonico_prefere_sistema_e_sudoers_do_updater() -> None:
    texto = INSTALADOR.read_text(encoding="utf-8")
    assert 'UPDATER_SYSTEM_DIR="${UPDATER_SYSTEM_DIR:-$REPO_DIR/updater/sistema}"' in texto
    assert 'UPDATER_SUDOERS_DIR="${UPDATER_SUDOERS_DIR:-$REPO_DIR/updater/sudoers}"' in texto
    assert 'is_updater_unit() {' in texto
    assert 'src_dir="$UPDATER_SUDOERS_DIR"' in texto
    assert 'TEMPLATE_DIR_EXPLICIT' in texto


def test_core_prefere_instalador_e_templates_canonicos() -> None:
    texto = ler_fonte_core()
    assert '$REPO_DIR/updater/sistema/instalar.sh' in texto
    assert 'updater/sistema/$rel' in texto
    assert 'updater/sistema/tts-bot-updater.service' in texto
    assert 'updater/sistema/tts-bot-updater.timer' in texto
    assert 'updater/sistema/tts-bot-updater.path' in texto
    assert 'updater/sistema/tts-bot-alert@.service' in texto
    assert 'updater/sudoers/*' in texto


def test_unit_canonica_executa_core_canonico() -> None:
    texto = (SISTEMA / "tts-bot-updater.service").read_text(encoding="utf-8")
    assert "ExecStart=/usr/bin/env bash /home/ubuntu/bot/updater/core/atualizar.sh" in texto
    assert "scripts/tts-bot-update.sh" not in texto
