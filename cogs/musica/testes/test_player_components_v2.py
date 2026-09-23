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
    assert 'PLAYER_STATUS_ANIMATED_EMOJI = "<a:loading:1510065277868445796>"' in source
    assert 'PLAYER_STATUS_PLAYING_EMOJI = "<a:circulando:1551635281738858670>"' in source
    assert 'PLAYER_QUEUE_FINISHED_EMOJI = "<:Barra:1548838704850800712>"' in source
    assert "bar.add_item(media=PLAYER_BAR_URL" in build
    assert 'discord.ui.TextDisplay(f"**{status_emoji} {status_title}**")' in build
    assert "discord.ui.Thumbnail(status_" not in build
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



def test_painel_refinado_prioriza_faixa_barra_fila_e_controles() -> None:
    source = COMPONENTS.read_text(encoding="utf-8")
    build = _method_source(COMPONENTS, "MusicPlayerView", "_build")

    assert 'discord.ui.TextDisplay(f"**{status_emoji} {status_title}**")' in build
    assert 'discord.ui.TextDisplay(f"**{status_title}**")' not in build
    assert "accessory=discord.ui.Thumbnail(status_" not in build
    assert '"-# Use os controles abaixo para controlar o player."' not in build

    # Ordem semântica: dados da faixa -> barra animada -> fila -> controles.
    track_pos = build.index("_player_track_text(state, current)")
    bar_pos = build.index("bar.add_item(media=PLAYER_BAR_URL")
    queue_pos = build.index("_queue_preview_text(state, limit=4)")
    controls_pos = build.index("discord.ui.ActionRow(back, pause, skip, stop, queue_button)")
    assert track_pos < bar_pos < queue_pos < controls_pos

    # O refinamento é puramente de renderização e continua sem polling/I/O novo.
    assert "await " not in build
    for forbidden in ("music_agent_status(", "resolve_music_tracks_on_worker(", "yt_dlp", "aiohttp", "requests."):
        assert forbidden not in build


def test_textos_do_player_sao_mais_compactos_no_mobile() -> None:
    source = COMPONENTS.read_text(encoding="utf-8")

    assert 'lines = [f"### {title}"' in source
    assert 'metadata = [duration, f"{source_emoji} {source_label}"]' in source
    assert 'header = f"**Fila** · {total_text} {count_label}"' in source
    assert 'marker = "▶" if selected_position == position else f"{position}."' in source
    assert 'f"{position:02d}"' not in source[source.index("def _queue_preview_text"):source.index("def _idle_player_text")]


def test_status_inline_nao_usa_spinner_de_loading_para_tocando_e_fim_usa_barra() -> None:
    source = COMPONENTS.read_text(encoding="utf-8")
    presentation = source[source.index("def _player_status_presentation"):source.index("def _player_track_text")]

    assert 'return "Tocando Agora", PLAYER_STATUS_PLAYING_EMOJI' in presentation
    assert 'return "Tocando Agora", PLAYER_STATUS_ANIMATED_EMOJI' not in presentation
    assert 'return "Fila concluída", PLAYER_QUEUE_FINISHED_EMOJI' in presentation
    assert 'return "As músicas acabaram", "✅"' not in presentation


def test_estado_ocioso_nao_repete_bloco_de_fila_vazia() -> None:
    build = _method_source(COMPONENTS, "MusicPlayerView", "_build")

    assert "if current is not None or queue:" in build
    queue_add = 'container.add_item(discord.ui.TextDisplay(_queue_preview_text(state, limit=4)))'
    assert queue_add in build
    assert build.index("if current is not None or queue:") < build.index(queue_add)
