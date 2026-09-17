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
        "comandos",
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
    modulo = (MUSICA / "modulo.py").read_text(encoding="utf-8")
    tocar = (MUSICA / "comandos" / "tocar.py").read_text(encoding="utf-8")
    assert "class Music(FluxoTocar, commands.Cog)" in modulo
    assert "music_agent_command" in tocar
    assert "ensure_music_worker_available" in modulo
    for texto in (modulo, tocar):
        assert "FFmpegPCMAudio" not in texto
        assert "yt_dlp.YoutubeDL" not in texto




def test_fluxo_tocar_esta_modularizado_fora_do_cog_principal() -> None:
    modulo = (MUSICA / "modulo.py").read_text(encoding="utf-8")
    tocar = (MUSICA / "comandos" / "tocar.py").read_text(encoding="utf-8")
    assert "class FluxoTocar" in tocar
    assert "async def _run_play" in tocar
    assert "async def _run_play" not in modulo
    assert len(modulo.splitlines()) < 500

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


def test_fila_monitor_e_sincronizacao_saem_do_roteador_legado() -> None:
    musica = Path(__file__).resolve().parents[1]
    legado = (musica / "legado" / "roteador_audio.py").read_text(encoding="utf-8")
    fila = (musica / "nucleo" / "fila.py").read_text(encoding="utf-8")
    monitor = (musica / "agente_telefone" / "monitor.py").read_text(encoding="utf-8")
    sync = (musica / "reproducao" / "sincronizacao.py").read_text(encoding="utf-8")

    assert "async def obter_proxima_faixa" in fila
    assert "def registrar_historico" in fila
    assert "def iniciar_monitor_music_agent" in monitor
    assert "async def sincronizar_estado_agente" in sync
    assert "return await sincronizar_estado_agente(" in legado
    assert "iniciar_monitor_music_agent(" in legado

    arquitetura_nova = fila + monitor + sync
    for proibido in ("yt_dlp", "FFmpegPCMAudio", "LavalinkBackend.play"):
        assert proibido not in arquitetura_nova
