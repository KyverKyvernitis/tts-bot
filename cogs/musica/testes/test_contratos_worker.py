from __future__ import annotations

from pathlib import Path


RAIZ = Path(__file__).resolve().parents[3]
MUSICA = RAIZ / "cogs" / "musica"


def test_comandos_de_reproducao_possuem_rota_para_music_agent() -> None:
    modulo = (MUSICA / "modulo.py").read_text(encoding="utf-8")
    tocar = (MUSICA / "comandos" / "tocar.py").read_text(encoding="utf-8")
    for acao in ("play", "pause", "resume", "skip", "stop", "shuffle"):
        assert f'"{acao}"' in modulo
    assert "_send_music_agent_control" in modulo
    assert 'music_agent_command(\n                            "play"' in tocar
    assert "class Music(FluxoTocar, commands.Cog)" in modulo


def test_phone_worker_esta_separado_por_responsabilidade() -> None:
    pasta = MUSICA / "agente_telefone"
    esperados = {
        "comandos.py",
        "modelos.py",
        "resolucao.py",
        "selecao.py",
        "servico.py",
        "utilitarios.py",
    }
    assert esperados <= {p.name for p in pasta.iterdir() if p.is_file()}

    contratos = {
        "selecao.py": ("select_music_worker", "ensure_music_worker_available"),
        "resolucao.py": ("resolve_music_tracks_on_worker",),
        "comandos.py": ("music_agent_command", "music_agent_status"),
    }
    for nome, funcoes in contratos.items():
        texto = (pasta / nome).read_text(encoding="utf-8")
        for funcao in funcoes:
            assert f"def {funcao}" in texto or f"async def {funcao}" in texto

    fachada = (pasta / "servico.py").read_text(encoding="utf-8")
    for funcao in ("select_music_worker", "resolve_music_tracks_on_worker", "music_agent_command", "music_agent_status"):
        assert f'"{funcao}"' in fachada


def test_lavalink_de_reproducao_permanece_apenas_no_legado_durante_migracao() -> None:
    lavalink_legado = MUSICA / "legado" / "motores" / "lavalink.py"
    assert lavalink_legado.is_file()
    fora_legado = [
        p.relative_to(MUSICA).as_posix()
        for p in MUSICA.rglob("*.py")
        if "legado" not in p.parts and "testes" not in p.parts and "play_lavalink_track" in p.read_text(encoding="utf-8")
    ]
    assert fora_legado == []
