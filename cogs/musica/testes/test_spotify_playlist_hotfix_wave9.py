from __future__ import annotations

from types import SimpleNamespace

import pytest

from cogs.musica.reproducao import controle_remoto


@pytest.mark.asyncio
async def test_skip_remoto_nao_cancela_refill_da_playlist(monkeypatch: pytest.MonkeyPatch) -> None:
    cancelled: list[str] = []

    class Router:
        def cancel_pending_music_operations(self, guild_id: int, *, reason: str):
            cancelled.append(reason)

    async def command(action: str, **kwargs):
        assert action == "skip"
        return {"ok": True, "state": {}}

    monkeypatch.setattr(controle_remoto, "music_agent_command", command)
    await controle_remoto.enviar_controle_remoto(Router(), "skip", guild_id=123)

    assert cancelled == []


@pytest.mark.asyncio
async def test_stop_remoto_cancela_pipeline_uma_unica_vez(monkeypatch: pytest.MonkeyPatch) -> None:
    cancelled: list[str] = []

    class Router:
        def cancel_pending_music_operations(self, guild_id: int, *, reason: str):
            cancelled.append(reason)

    async def command(action: str, **kwargs):
        assert action == "stop"
        return {"ok": True, "state": {}}

    monkeypatch.setattr(controle_remoto, "music_agent_command", command)
    await controle_remoto.enviar_controle_remoto(Router(), "stop", guild_id=123)

    assert cancelled == ["agent_stop"]


def test_interface_e_comando_nao_fazem_pre_cancelamento_duplicado() -> None:
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    ui = (root / "interface" / "componentes.py").read_text(encoding="utf-8")
    base = (root / "comandos" / "base.py").read_text(encoding="utf-8")

    assert 'cancel_pending_music_operations(self.guild_id, reason=f"agent_{action}")' not in ui
    assert 'cancel_pending_music_operations(ctx.guild.id, reason=f"agent_{action}")' not in base


def test_config_padrao_buffer_inicial_igual_a_janela() -> None:
    from cogs.musica import configuracao as config

    assert config.MUSIC_PLAYLIST_STARTUP_SIZE == config.MUSIC_PLAYLIST_WINDOW_SIZE
    assert config.MUSIC_PLAYLIST_STARTUP_SIZE >= 5
