from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
COMPONENTS = ROOT / "interface" / "componentes.py"


def _method_source(class_name: str, method_name: str) -> str:
    source = COMPONENTS.read_text(encoding="utf-8")
    tree = ast.parse(source)
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == class_name)
    method = next(
        node
        for node in cls.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == method_name
    )
    return ast.get_source_segment(source, method) or ""


def test_queue_pagination_edits_component_response_instead_of_ephemeral_message_route() -> None:
    redraw = _method_source("QueueView", "_redraw")

    # Queue aberta pelo botão do player é ephemeral. Depois de DEFERRED_MESSAGE_UPDATE,
    # a edição precisa usar o webhook da própria interação; Message.edit pode usar a
    # rota normal de canal e falhar para mensagens ephemeral.
    assert redraw.index("await interaction.response.defer()") < redraw.index("await self._prepare_page()")
    assert "await interaction.edit_original_response(" in redraw
    assert "await message.edit(" not in redraw
    assert "await interaction.message.edit(" not in redraw


def test_queue_navigation_is_serialized_and_transactional() -> None:
    source = COMPONENTS.read_text(encoding="utf-8")
    queue_cls = next(node for node in ast.parse(source).body if isinstance(node, ast.ClassDef) and node.name == "QueueView")
    init = next(node for node in queue_cls.body if isinstance(node, ast.FunctionDef) and node.name == "__init__")
    init_source = ast.get_source_segment(source, init) or ""
    redraw = _method_source("QueueView", "_redraw")
    previous = _method_source("QueueView", "previous_page")
    next_page = _method_source("QueueView", "next_page")

    assert "self._interaction_lock = asyncio.Lock()" in init_source
    assert "async with self._interaction_lock" in redraw
    assert "old_page = self.page" in redraw
    assert "self.page = old_page" in redraw
    assert "page_delta=-1" in previous
    assert "page_delta=1" in next_page


def test_queue_selection_uses_same_safe_redraw_path() -> None:
    callback = _method_source("QueueSelect", "callback")
    assert "view._redraw(interaction, selected_position=int(self.values[0]))" in callback
    assert "view.selected_position =" not in callback
