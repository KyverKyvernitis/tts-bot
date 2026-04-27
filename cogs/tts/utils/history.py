"""Renderização do histórico de ações dos painéis de TTS.

As entradas são guardadas como string codificada e decodificadas no momento
de mostrar — assim cada viewer vê a frase ajustada (`Você trocou X` quando
é o dono, `Fulano trocou X` quando é outro). A última entrada vem em bold
pra ficar fácil identificar a mais recente.
"""
from __future__ import annotations

from typing import Callable


def quote_value(value: str) -> str:
    return f'"{value}"'


def render_history_entry(
    entry: str,
    *,
    decoder: Callable[[str], tuple[int, str, str] | None],
    public_panel_states: dict[int, dict] | None = None,
    viewer_user_id: int | None = None,
    message_id: int | None = None,
) -> str:
    decoded = decoder(entry)
    if not decoded:
        return str(entry or "")

    owner_id, actor_name, action_text = decoded
    # Painel público de outro user: mostra "Você (Fulano) X" só pra quem é o
    # dono dele — fica claro que a ação foi sobre o painel próprio mesmo
    # quando está num painel compartilhado.
    state = (public_panel_states or {}).get(message_id or 0, {}) if message_id else {}
    is_public_user_panel = bool(state and state.get("panel_kind") == "user")
    public_panel_owner_id = int(state.get("owner_id", 0) or 0) if state else 0

    if viewer_user_id == owner_id:
        if is_public_user_panel:
            if public_panel_owner_id == owner_id:
                return f"Você ({actor_name}) {action_text}"
            return f"{actor_name} {action_text}"
        return f"Você {action_text}"

    return f"{actor_name} {action_text}"


def format_history_entries(
    entries: list[str],
    *,
    decoder: Callable[[str], tuple[int, str, str] | None],
    public_panel_states: dict[int, dict] | None = None,
    viewer_user_id: int | None = None,
    message_id: int | None = None,
) -> str:
    entries = [str(x) for x in (entries or []) if str(x or "").strip()]
    if not entries:
        return ""
    lines = []
    for idx, entry in enumerate(entries):
        rendered = render_history_entry(
            entry,
            decoder=decoder,
            public_panel_states=public_panel_states,
            viewer_user_id=viewer_user_id,
            message_id=message_id,
        )
        # Backtick dentro do texto fica conflitando com o `code span` da linha,
        # então troca por aspas simples antes de embrulhar.
        safe = rendered.replace("`", "'")
        line = f"`{safe}`"
        if idx == len(entries) - 1:
            line = f"**{line}**"
        lines.append(line)
    return "\n".join(lines)


def format_status_history_entries(
    entries: list[str],
    *,
    decoder: Callable[[str], tuple[int, str, str] | None],
    public_panel_states: dict[int, dict] | None = None,
    viewer_user_id: int | None = None,
) -> str:
    # Versão compacta usada no embed de status: só as 2 últimas entradas,
    # com bullets em vez de code spans.
    entries = [str(x) for x in (entries or []) if str(x or "").strip()]
    if not entries:
        return ""
    lines = []
    recent_entries = entries[-2:]
    for idx, entry in enumerate(recent_entries):
        rendered = render_history_entry(
            entry,
            decoder=decoder,
            public_panel_states=public_panel_states,
            viewer_user_id=viewer_user_id,
            message_id=None,
        )
        safe = rendered.replace("`", "'")
        line = f"• {safe}"
        if idx == len(recent_entries) - 1:
            line = f"**{line}**"
        lines.append(line)
    return "\n".join(lines)
