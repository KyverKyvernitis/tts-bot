from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
COMPONENTS = ROOT / "interface" / "componentes.py"
ROUTER = ROOT / "legado" / "roteador_audio.py"
QUEUE_COMMAND = ROOT / "comandos" / "fila.py"


def _class_node(path: Path, name: str) -> tuple[str, ast.ClassDef]:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == name:
            return source, node
    raise AssertionError(f"classe {name!r} não encontrada em {path}")


def _class_source(path: Path, name: str) -> str:
    source, node = _class_node(path, name)
    return ast.get_source_segment(source, node) or ""


def _method_source(path: Path, class_name: str, method_name: str) -> str:
    source, node = _class_node(path, class_name)
    for child in node.body:
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and child.name == method_name:
            return ast.get_source_segment(source, child) or ""
    raise AssertionError(f"método {class_name}.{method_name} não encontrado")


def test_painel_principal_e_fila_usam_layoutview_components_v2() -> None:
    music_player = _class_source(COMPONENTS, "MusicPlayerView")
    queue_view = _class_source(COMPONENTS, "QueueView")
    confirm_view = _class_source(COMPONENTS, "QueueConfirmView")

    assert "class MusicPlayerView(discord.ui.LayoutView)" in music_player
    assert "class QueueView(discord.ui.LayoutView)" in queue_view
    assert "class QueueConfirmView(discord.ui.LayoutView)" in confirm_view

    for component in (
        "discord.ui.Container",
        "discord.ui.Section",
        "discord.ui.TextDisplay",
        "discord.ui.Thumbnail",
        "discord.ui.Separator",
        "discord.ui.MediaGallery",
        "discord.ui.ActionRow",
    ):
        assert component in music_player

    assert "discord.Embed" not in music_player
    assert "embed=" not in music_player
    assert "embeds=" not in music_player


def test_animacoes_existentes_sao_preservadas_sem_polling_extra() -> None:
    source = COMPONENTS.read_text(encoding="utf-8")
    build = _method_source(COMPONENTS, "MusicPlayerView", "_build")

    assert 'PLAYER_STATUS_ANIMATED_URL = "https://i.ibb.co/QXtk5VB/neon-circle.gif"' in source
    assert "bar.add_item(media=PLAYER_BAR_URL" in build
    assert "discord.ui.Thumbnail(status_icon" in build
    assert "await " not in build
    for forbidden in ("music_agent_status(", "resolve_music_tracks_on_worker(", "yt_dlp", "aiohttp", "requests."):
        assert forbidden not in build


def test_update_panel_envia_apenas_layoutview_e_migra_embed_antigo() -> None:
    source = ROUTER.read_text(encoding="utf-8")
    tree = ast.parse(source)
    update_source = ""
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "update_panel":
            update_source = ast.get_source_segment(source, node) or ""
            break
    assert update_source
    assert "embeds = build_player_embeds(state)" not in update_source
    assert "await state.now_message.edit(content=None, embeds=[], attachments=[], view=view)" in update_source
    assert "await channel.send(view=view, silent=True)" in update_source
    assert "channel.send(embeds=" not in update_source


def test_comando_fila_nao_envia_embed_legado() -> None:
    source = QUEUE_COMMAND.read_text(encoding="utf-8")
    assert "from ..interface.componentes import QueueView" in source
    assert "build_queue_embed" not in source
    assert "view=QueueView(" in source
