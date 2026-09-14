from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
DISCORD_DIR = ROOT / "updater" / "discord"

_ORDEM = (
    "constantes.py",
    "cartoes.py",
    "preparacao.py",
    "progresso.py",
    "controles.py",
    "eventos.py",
    "integracao.py",
)


def caminhos_fontes_discord() -> tuple[Path, ...]:
    return tuple(DISCORD_DIR / nome for nome in _ORDEM)


def ler_fonte_discord() -> str:
    """Visão textual expandida usada pelos testes legados do updater."""
    partes: list[str] = []
    for caminho in caminhos_fontes_discord():
        partes.append(caminho.read_text(encoding="utf-8"))
    # O bot fica no fim para preservar os delimitadores históricos usados por
    # testes que encerravam blocos do updater em métodos gerais do BotLocal.
    partes.append((ROOT / "bot.py").read_text(encoding="utf-8"))
    return "\n\n".join(partes)
