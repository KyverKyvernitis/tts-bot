from __future__ import annotations

import ast
from pathlib import Path

from cogs.musica import configuracao as config


ROOT = Path(__file__).resolve().parents[1]


def _class_source(path: Path, class_name: str) -> str:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            return ast.get_source_segment(source, node) or ""
    raise AssertionError(f"classe {class_name!r} não encontrada em {path}")


def test_emoji_youtube_tem_fonte_unica_em_configuracao() -> None:
    assert config.MUSIC_SOURCE_EMOJIS["youtube"] == "<:YouTube:1502490543891021827>"
    assert config.MUSIC_SOURCE_EMOJI_FALLBACK == "🎵"

    router_source = (ROOT / "legado" / "roteador_audio.py").read_text(encoding="utf-8")
    assert "MUSIC_SOURCE_EMOJIS = config.MUSIC_SOURCE_EMOJIS" in router_source
    assert '"youtube": "<:YouTube:1502490543891021827>"' not in router_source


def test_seletor_de_busca_usa_emoji_youtube_sem_fetch() -> None:
    components_path = ROOT / "interface" / "componentes.py"
    source = components_path.read_text(encoding="utf-8")
    search_select_source = _class_source(components_path, "SearchSelect")

    assert 'YOUTUBE_SEARCH_OPTION_EMOJI = config.MUSIC_SOURCE_EMOJIS.get("youtube")' in source
    assert "emoji=YOUTUBE_SEARCH_OPTION_EMOJI" in search_select_source
    assert 'emoji="🎵"' not in search_select_source
    assert "fetch_emoji" not in search_select_source
    assert "fetch_emojis" not in search_select_source
    assert "await " not in search_select_source.split("async def callback", 1)[0]
