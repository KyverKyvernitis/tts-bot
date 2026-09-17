from __future__ import annotations

import re
from pathlib import Path


RAIZ = Path(__file__).resolve().parents[3]
MUSICA = RAIZ / "cogs" / "musica"


def test_musica_e_um_pacote_carregavel_e_o_layout_antigo_sumiu() -> None:
    assert (MUSICA / "__init__.py").is_file()
    assert re.search(r"^async def setup\s*\(", (MUSICA / "__init__.py").read_text(), re.MULTILINE)
    assert not (RAIZ / "cogs" / "music.py").exists()
    antigo = RAIZ / "music_system"
    assert not antigo.exists() or not any(antigo.rglob("*.py"))


def test_estrutura_principal_usa_nomes_em_portugues() -> None:
    esperados = {
        "agente_telefone",
        "diagnostico",
        "interface",
        "legado",
        "metadados",
        "nucleo",
        "reproducao",
        "testes",
    }
    encontrados = {p.name for p in MUSICA.iterdir() if p.is_dir() and not p.name.startswith("__")}
    assert esperados <= encontrados


def test_codigo_python_nao_importa_o_antigo_music_system() -> None:
    arquivos = [RAIZ / "bot.py", RAIZ / "utility" / "commands" / "vps.py", *[p for p in MUSICA.rglob("*.py") if "testes" not in p.parts]]
    restos = []
    for arquivo in arquivos:
        texto = arquivo.read_text(encoding="utf-8")
        if "music_system" in texto:
            restos.append(str(arquivo.relative_to(RAIZ)))
    assert restos == []


def test_entrypoint_nao_contem_player_local() -> None:
    texto = (MUSICA / "modulo.py").read_text(encoding="utf-8")
    assert "music_agent_command" in texto
    assert "ensure_music_worker_available" in texto
    assert "FFmpegPCMAudio" not in texto
    assert "yt_dlp" not in texto


def test_codigo_de_extracao_local_esta_isolado_como_legado() -> None:
    assert (MUSICA / "legado" / "extrator_local.py").is_file()
    for arquivo in (MUSICA / "metadados").rglob("*.py"):
        texto = arquivo.read_text(encoding="utf-8")
        assert "YoutubeDL" not in texto
        assert "import yt_dlp" not in texto


def test_reproducao_nova_nao_contem_player_local_nem_lavalink() -> None:
    pasta = MUSICA / "reproducao"
    assert (pasta / "sincronizacao.py").is_file()
    for arquivo in pasta.rglob("*.py"):
        texto = arquivo.read_text(encoding="utf-8")
        assert "yt_dlp" not in texto
        assert "FFmpegPCMAudio" not in texto
        assert "LavalinkBackend" not in texto
        assert "play_lavalink_track" not in texto


def test_fachada_phone_worker_nao_volta_a_virar_monolito() -> None:
    servico = MUSICA / "agente_telefone" / "servico.py"
    assert len(servico.read_text(encoding="utf-8").splitlines()) < 100
    for relativo in ("modulo.py", "interface/componentes.py"):
        texto = (MUSICA / relativo).read_text(encoding="utf-8")
        assert "agente_telefone.servico" not in texto
