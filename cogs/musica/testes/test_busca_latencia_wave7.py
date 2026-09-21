from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def _selection_block(text: str, marker: str) -> str:
    start = text.index(marker)
    end = text.index("                return", start) + len("                return")
    return text[start:end]


def test_comando_inicia_prefetch_antes_de_enviar_menu_discord() -> None:
    text = (ROOT / "cogs/musica/comandos/tocar.py").read_text(encoding="utf-8")
    block = _selection_block(text, "            if should_open_selection:")
    assert block.index("self._schedule_music_agent_prefetch(") < block.index("await self._reply(")
    assert 'prefetch_kind="selection"' in text


def test_modal_inicia_prefetch_antes_de_enviar_menu_discord() -> None:
    text = (ROOT / "cogs/musica/interface/componentes.py").read_text(encoding="utf-8")
    marker = "        if should_open_selection:"
    start = text.index(marker, text.index("class AddSongModal"))
    end = text.index("            return", start) + len("            return")
    block = text[start:end]
    assert block.index("_schedule_agent_prefetch(") < block.index("await interaction.followup.send(")
    assert 'prefetch_kind="selection"' in text


def test_worker_selection_prefetch_tem_prioridades_configuraveis() -> None:
    text = (ROOT / "cogs/musica/runtime_telefone/agente/servidor.py").read_text(encoding="utf-8")
    assert "MUSIC_AGENT_SELECTION_PREFETCH_IDLE_PRIORITY" in text
    assert "MUSIC_AGENT_SELECTION_PREFETCH_ACTIVE_PRIORITY" in text
