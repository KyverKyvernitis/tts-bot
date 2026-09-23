from __future__ import annotations

from pathlib import Path


RAIZ = Path(__file__).resolve().parents[3]
MUSICA = RAIZ / "cogs" / "musica"


def test_comandos_ficam_fora_do_entrypoint_principal() -> None:
    modulo = (MUSICA / "modulo.py").read_text(encoding="utf-8")
    base = (MUSICA / "comandos" / "base.py").read_text(encoding="utf-8")
    controle = (MUSICA / "comandos" / "controle.py").read_text(encoding="utf-8")
    fila = (MUSICA / "comandos" / "fila.py").read_text(encoding="utf-8")
    configuracoes = (MUSICA / "comandos" / "configuracoes.py").read_text(encoding="utf-8")

    assert "class BaseComandosMusica" in base
    assert "class FluxoControle" in controle
    assert "class FluxoFila" in fila
    assert "class FluxoConfiguracoes" in configuracoes
    assert "async def _run_pause" in controle
    assert "async def _run_queue" in fila
    assert "async def _run_voicestatus" in configuracoes
    assert "async def _ensure_music_action_voice" not in modulo
    assert "async def _send_music_agent_control" not in modulo
    assert len(modulo.splitlines()) < 250


def test_fila_worker_remota_nao_fica_implementada_no_legado() -> None:
    remoto = (MUSICA / "reproducao" / "fila_remota.py").read_text(encoding="utf-8")
    legado = (MUSICA / "legado" / "roteador_audio.py").read_text(encoding="utf-8")

    for nome in ("embaralhar_fila_worker", "alternar_repeticao_worker", "voltar_historico_worker"):
        assert f"async def {nome}" in remoto
        assert f"{nome}(self" in legado

    for trecho in ("await embaralhar(", "await alternar_repeticao(", "await anterior("):
        assert trecho not in legado

    for proibido in ("yt_dlp", "FFmpegPCMAudio", "play_lavalink_track"):
        assert proibido not in remoto


def test_controlador_de_fila_worker_muta_a_fila_remota_em_vez_do_player_local() -> None:
    remoto = (MUSICA / "reproducao" / "fila_remota.py").read_text(encoding="utf-8")
    legado = (MUSICA / "legado" / "roteador_audio.py").read_text(encoding="utf-8")
    servidor = (MUSICA / "runtime_telefone" / "agente" / "servidor.py").read_text(encoding="utf-8")

    for nome in (
        "tocar_posicao_fila_worker",
        "mover_item_fila_worker",
        "remover_item_fila_worker",
        "limpar_fila_worker",
    ):
        assert f"async def {nome}" in remoto
        assert f"{nome}(self" in legado

    for action in ("queue_play_now", "queue_move", "queue_remove", "queue_clear"):
        assert action in servidor

    assert "replace_queue local ignorado em sessão remota" in legado
