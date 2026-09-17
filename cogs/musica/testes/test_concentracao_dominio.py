from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def _texto(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _nomes_atribuicoes_python(rel: str) -> set[str]:
    tree = ast.parse(_texto(rel), filename=rel)
    nomes: set[str] = set()
    for node in ast.walk(tree):
        targets = []
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        for target in targets:
            if isinstance(target, ast.Name):
                nomes.add(target.id)
    return nomes


def test_configuracao_musical_tem_fonte_de_verdade_na_cog() -> None:
    proibidos = (
        "MUSIC_",
        "LAVALINK_",
        "AUX_LAVALINK_",
        "SPOTIFY_",
        "DEEZER_",
        "SOUNDCLOUD_",
        "AUDIO_NODE_",
    )
    atribuicoes = _nomes_atribuicoes_python("config.py")
    assert not {nome for nome in atribuicoes if nome.startswith(proibidos)}
    assert "YOUTUBE_API_KEY" not in atribuicoes
    assert "cogs.musica" not in _texto("config.py")

    config_musica = _texto("cogs/musica/configuracao.py")
    assert "MUSIC_WORKER_ONLY_ENABLED" in config_musica
    assert "SPOTIFY_CLIENT_ID" in config_musica
    assert "LAVALINK_ENABLED" in config_musica


def test_bot_nao_implementa_ciclo_de_vida_da_musica() -> None:
    texto = _texto("bot.py")
    assert "AudioRouter(" not in texto
    assert "_music_startup_reconcile_task" not in texto
    assert "_run_music_startup_reconcile" not in texto
    assert "MUSIC_STARTUP_RESTORE_DELAY_SECONDS" not in texto
    assert "IntegracaoMusicaBot" in texto


def test_vps_nao_implementa_diagnostico_musical() -> None:
    texto = _texto("utility/commands/vps.py")
    assert "build_music_diagnostics_archive" not in texto
    assert "build_music_diagnostics_report" not in texto
    assert "build_music_diagnostics_emergency_report" not in texto
    assert "AudioRouter" not in texto
    assert "gerar_diagnostico_musical_vps" in texto


def test_tts_nao_acessa_audio_router_diretamente() -> None:
    for rel in ("cogs/tts/audio.py", "cogs/tts/cog.py", "cogs/tts/streaming.py"):
        texto = _texto(rel)
        assert '"audio_router"' not in texto
        assert "'audio_router'" not in texto
        assert "is_lavalink_active_for_guild" not in texto
        assert "should_route_tts_to_music_agent" not in texto
        assert "play_tts_via_music_agent" not in texto


def test_ferramenta_spotify_mora_na_cog() -> None:
    implementacao = _texto("cogs/musica/ferramentas/gerar_token_spotify.py")
    compat = _texto("scripts/generate_spotify_refresh_token.py")
    assert "accounts.spotify.com/api/token" in implementacao
    assert "cogs.musica.ferramentas.gerar_token_spotify" in compat
    assert "accounts.spotify.com/api/token" not in compat


def test_stub_antigo_diagnostico_musica_foi_removido() -> None:
    assert not (ROOT / "utility/commands/diagnostico_musica.py").exists()
