from __future__ import annotations

from pathlib import Path


RAIZ = Path(__file__).resolve().parents[3]
MUSICA = RAIZ / "cogs" / "musica"


def test_comandos_de_reproducao_possuem_rota_para_music_agent() -> None:
    texto = (MUSICA / "modulo.py").read_text(encoding="utf-8")
    for acao in ("play", "pause", "resume", "skip", "stop", "shuffle"):
        assert f'"{acao}"' in texto
    assert "_send_music_agent_control" in texto
    assert 'music_agent_command(\n                            "play"' in texto


def test_servico_do_phone_worker_expoe_selecao_resolucao_e_controle() -> None:
    texto = (MUSICA / "agente_telefone" / "servico.py").read_text(encoding="utf-8")
    contratos = (
        "select_music_worker",
        "ensure_music_worker_available",
        "resolve_music_tracks_on_worker",
        "music_agent_command",
        "music_agent_status",
    )
    for contrato in contratos:
        assert f"def {contrato}" in texto or f"async def {contrato}" in texto


def test_lavalink_de_reproducao_permanece_apenas_no_legado_durante_migracao() -> None:
    lavalink_legado = MUSICA / "legado" / "motores" / "lavalink.py"
    assert lavalink_legado.is_file()
    fora_legado = [
        p.relative_to(MUSICA).as_posix()
        for p in MUSICA.rglob("*.py")
        if "legado" not in p.parts and "testes" not in p.parts and "play_lavalink_track" in p.read_text(encoding="utf-8")
    ]
    assert fora_legado == []
