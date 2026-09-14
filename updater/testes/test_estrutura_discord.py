from __future__ import annotations

import ast
from pathlib import Path

from updater.testes.fonte_discord import caminhos_fontes_discord, ler_fonte_discord


ROOT = Path(__file__).resolve().parents[2]
BOT = ROOT / "bot.py"
DISCORD_DIR = ROOT / "updater" / "discord"


def test_integracao_discord_fica_fora_do_bot_py() -> None:
    source = BOT.read_text(encoding="utf-8")
    assert "class BotLocal(IntegracaoDiscordUpdaterMixin, commands.Bot):" in source
    assert "inicializar_integracao_updater(self)" in source
    assert "def _zip_update_render_card_text" not in source
    assert "def _handle_zip_update_message" not in source
    assert "async def on_interaction" not in source
    assert len(source.splitlines()) < 1500


def test_modulos_discord_canonicos_existem_e_compilam() -> None:
    esperados = {
        "__init__.py",
        "constantes.py",
        "cartoes.py",
        "preparacao.py",
        "progresso.py",
        "controles.py",
        "eventos.py",
        "integracao.py",
    }
    assert esperados <= {p.name for p in DISCORD_DIR.glob("*.py")}
    for path in caminhos_fontes_discord():
        ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def test_fonte_expandida_preserva_contratos_visuais_e_dispatch() -> None:
    source = ler_fonte_discord()
    assert "def _zip_update_render_card_text" in source
    assert "async def _handle_zip_update_message" in source
    assert "async def _dispatch_updater_candidate" in source
    assert "async def _start_zip_update_rollback_flow" in source
    assert 'UPDATE_EMOJI_PROGRESS_TITLE = "<a:areia:1496606578395189473>"' in source


def test_mudancas_na_integracao_discord_exigem_reinicio_do_bot() -> None:
    from updater.testes.fonte_core import ler_fonte_core

    fonte = ler_fonte_core()
    bloco = fonte[fonte.index("classify_changed_files() {") : fonte.index("fast_reload_modules_for_changed_files() {")]
    assert "updater/discord/*" in bloco
    assert "updater/utilitarios/*" in bloco
