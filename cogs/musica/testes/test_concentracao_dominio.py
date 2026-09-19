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


def test_ferramenta_spotify_mora_exclusivamente_na_cog() -> None:
    implementacao = _texto("cogs/musica/ferramentas/gerar_token_spotify.py")
    assert "accounts.spotify.com/api/token" in implementacao
    assert not (ROOT / "scripts/generate_spotify_refresh_token.py").exists()


def test_testes_puramente_musicais_nao_ficam_na_raiz_de_tests() -> None:
    antigos = (
        "test_music_agent_async_transitions.py",
        "test_music_agent_lifecycle.py",
        "test_music_diagnostics_repo_root.py",
    )
    for nome in antigos:
        assert not (ROOT / "tests" / nome).exists(), nome

    assert (ROOT / "cogs/musica/testes/runtime_telefone/test_music_agent_async_transitions.py").is_file()
    assert (ROOT / "cogs/musica/testes/runtime_telefone/test_music_agent_lifecycle.py").is_file()
    assert (ROOT / "cogs/musica/testes/diagnostico/test_repo_root.py").is_file()


def test_stub_antigo_diagnostico_musica_foi_removido() -> None:
    assert not (ROOT / "utility/commands/diagnostico_musica.py").exists()


def test_core_worker_apk_nao_desliga_phone_worker_de_musica(monkeypatch) -> None:
    import importlib.util

    monkeypatch.setenv("CORE_WORKER_APK_REPLACES_TERMUX", "true")
    monkeypatch.delenv("MUSIC_AGENT_ENABLED", raising=False)
    monkeypatch.delenv("MUSIC_WORKER_ONLY_ENABLED", raising=False)
    monkeypatch.delenv("MUSIC_WORKER_REQUIRE_TURBO", raising=False)
    monkeypatch.delenv("MUSIC_BACKEND", raising=False)

    caminho = ROOT / "cogs/musica/configuracao.py"
    spec = importlib.util.spec_from_file_location("_musica_config_isolada_teste", caminho)
    assert spec is not None and spec.loader is not None
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)

    assert modulo.MUSIC_AGENT_ENABLED is True
    assert modulo.MUSIC_WORKER_ONLY_ENABLED is True
    assert modulo.MUSIC_WORKER_REQUIRE_TURBO is True
    assert modulo.MUSIC_BACKEND == "worker"
    assert "CORE_WORKER_APK_REPLACES_TERMUX" not in caminho.read_text(encoding="utf-8")


def test_configuracao_da_musica_enxerga_phone_worker_direto_sem_flag_legado(monkeypatch) -> None:
    import importlib.util

    monkeypatch.setenv("PHONE_WORKER_HOST", "100.64.0.10")
    monkeypatch.setenv("PHONE_WORKER_PORT", "8766")
    monkeypatch.setenv("PHONE_WORKER_SCHEME", "http")
    monkeypatch.setenv("PHONE_WORKER_TOKEN", "segredo")
    monkeypatch.setenv("PHONE_WORKER_ENABLED", "false")
    monkeypatch.delenv("MUSIC_PHONE_WORKER_DIRECT_ENABLED", raising=False)

    caminho = ROOT / "cogs/musica/configuracao.py"
    spec = importlib.util.spec_from_file_location("_musica_config_phone_worker_teste", caminho)
    assert spec is not None and spec.loader is not None
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)

    assert modulo.PHONE_WORKER_HOST == "100.64.0.10"
    assert modulo.PHONE_WORKER_TOKEN == "segredo"
    assert modulo.PHONE_WORKER_ENABLED is True
    assert modulo.MUSIC_PHONE_WORKER_DIRECT_ENABLED is True


def test_roteador_importa_painel_do_modulo_interface_atual() -> None:
    texto = _texto("cogs/musica/legado/roteador_audio.py")
    assert "from ..interface.componentes import build_player_embeds, MusicPlayerView" in texto
    assert "from .ui import build_player_embeds, MusicPlayerView" not in texto


def test_prefetch_especulativo_padrao_limita_a_um_resultado():
    from cogs.musica import configuracao
    assert configuracao.MUSIC_AGENT_PREFETCH_TOP_RESULTS == 1


def test_roteador_legado_carrega_extrator_e_backends_sob_demanda() -> None:
    texto = _texto("cogs/musica/legado/roteador_audio.py")
    tree = ast.parse(texto)
    imports_top_level = []
    for node in tree.body:
        if isinstance(node, ast.ImportFrom):
            imports_top_level.append((node.module or "", {alias.name for alias in node.names}))
    assert not any(module.endswith("extrator_local") and "MusicExtractor" in names for module, names in imports_top_level)
    assert not any(module.endswith("motores") and "MusicBackendManager" in names for module, names in imports_top_level)
    assert "self._extractor = None" in texto
    assert "self._backends = None" in texto
    assert "def extractor(self):" in texto
    assert "def backends(self):" in texto


def test_worker_only_nao_materializa_extrator_so_para_classificar_url() -> None:
    texto = _texto("cogs/musica/comandos/tocar.py")
    assert "not input_profile.is_url and len(batch.tracks) > 1" in texto
    assert "not self.router.extractor.looks_like_url(query) and len(batch.tracks) > 1" not in texto


def test_runtime_musical_do_phone_worker_tem_fonte_canonica_na_cog() -> None:
    assert (ROOT / "cogs/musica/runtime_telefone/agente/configuracao.py").is_file()
    assert (ROOT / "cogs/musica/runtime_telefone/agente/ciclo_vida.py").is_file()
    assert (ROOT / "cogs/musica/runtime_telefone/agente/utilitarios.py").is_file()
    assert (ROOT / "cogs/musica/runtime_telefone/agente/estado.py").is_file()
    assert (ROOT / "cogs/musica/runtime_telefone/agente/mixer_pcm.py").is_file()
    assert (ROOT / "cogs/musica/runtime_telefone/agente/resolucao.py").is_file()
    assert (ROOT / "cogs/musica/runtime_telefone/agente/reproducao.py").is_file()
    assert (ROOT / "cogs/musica/runtime_telefone/agente/tts.py").is_file()
    assert (ROOT / "cogs/musica/runtime_telefone/agente/servidor.py").is_file()
    assert (ROOT / "cogs/musica/runtime_telefone/termux/integracao-worker.sh").is_file()
    assert (ROOT / "cogs/musica/runtime_telefone/termux/iniciar-agente-musica.sh").is_file()
    assert (ROOT / "cogs/musica/runtime_telefone/termux/musica.env.example").is_file()
    legado = ROOT / "deploy/termux/phone-worker/music_agent_runtime"
    assert not (legado / "__init__.py").exists()
    assert not (legado / "lifecycle.py").exists()

    assert not (ROOT / "deploy/termux/phone-worker/music_agent.py").exists()
    assert not (ROOT / "deploy/termux/phone-worker/start-phone-music-agent.sh").exists()
    agente = _texto("cogs/musica/runtime_telefone/agente/servidor.py")
    assert "cogs.musica.runtime_telefone.agente.configuracao" in agente
    assert "cogs.musica.runtime_telefone.agente.ciclo_vida" in agente
    assert "cogs.musica.runtime_telefone.agente.reproducao" in agente
    assert "cogs.musica.runtime_telefone.agente.tts" in agente
    assert "from music_agent_runtime" not in agente


def test_publisher_do_worker_distribui_runtime_musical_a_partir_da_cog() -> None:
    texto = _texto("scripts/core-worker-automation.py")
    assert '"cogs/musica/runtime_telefone/agente/configuracao.py"' in texto
    assert '"cogs/musica/runtime_telefone/agente/ciclo_vida.py"' in texto
    assert '"cogs/musica/runtime_telefone/agente/reproducao.py"' in texto
    assert '"cogs/musica/runtime_telefone/agente/tts.py"' in texto
    assert '"cogs/musica/runtime_telefone/agente/servidor.py"' in texto
    assert '"cogs/musica/runtime_telefone/termux/iniciar-agente-musica.sh"' in texto
    assert '"music_agent_runtime/lifecycle.py"' not in texto
    assert '("music_agent.py",' not in texto
    assert '("start-phone-music-agent.sh",' not in texto


def test_entrypoint_music_agent_nao_reimplementa_estado_mixer_e_utilitarios() -> None:
    texto = _texto("cogs/musica/runtime_telefone/agente/servidor.py")
    assert "class AgentTrack:" not in texto
    assert "class GuildMusicState:" not in texto
    assert "class AgentMixedAudioSource" not in texto
    assert "def _select_stream_info(" not in texto
    assert "def _looks_like_url(" not in texto
    assert "cogs.musica.runtime_telefone.agente.estado" in texto
    assert "cogs.musica.runtime_telefone.agente.mixer_pcm" in texto
    assert "cogs.musica.runtime_telefone.agente.utilitarios" in texto
    assert "cogs.musica.runtime_telefone.agente.resolucao" in texto
    assert "cogs.musica.runtime_telefone.agente.reproducao" in texto
    assert "cogs.musica.runtime_telefone.agente.tts" in texto
    assert "def _resolve_with_ytdlp(" not in texto
    assert "async def resolve_track(" not in texto
    assert "async def _play_next(" not in texto
    assert "async def cmd_voice_tts(" not in texto
    assert "async def _recover_current_stream(" not in texto


def test_termux_generico_so_carrega_hooks_do_dominio_musical() -> None:
    supervisor = _texto("deploy/termux/phone-worker/start-phone-worker.sh")
    env_generico = _texto("deploy/termux/phone-worker/phone-worker.env.example")
    integracao = _texto("cogs/musica/runtime_telefone/termux/integracao-worker.sh")
    supervisor_musica = _texto("cogs/musica/runtime_telefone/termux/iniciar-agente-musica.sh")

    assert "load_music_runtime_hooks()" in supervisor
    assert "musica_ensure_agent_if_needed" in supervisor
    assert "MUSIC_AGENT_TOKEN=" not in env_generico
    assert "PHONE_WORKER_MUSIC_YTDLP" not in env_generico
    assert "musica_active_agent_start_command()" in integracao
    assert "cogs.musica.runtime_telefone.agente.servidor" in supervisor_musica
    assert not (ROOT / "deploy/termux/phone-worker/music_agent.py").exists()
    assert not (ROOT / "deploy/termux/phone-worker/start-phone-music-agent.sh").exists()


def test_phone_worker_nao_reimplementa_dominio_musical() -> None:
    ponte = ROOT / "cogs/musica/runtime_telefone/ponte_worker"
    for nome in (
        "configuracao.py",
        "streams.py",
        "resolucao.py",
        "proxy.py",
        "telemetria.py",
        "servico.py",
    ):
        assert (ponte / nome).is_file(), nome

    worker = _texto("deploy/termux/phone-worker/phone_worker.py")

    # O worker genérico só mantém adaptadores lazy. Implementação do domínio
    # de música precisa permanecer na ponte canônica dentro de cogs/musica.
    assert 'import yt_dlp' not in worker
    assert 'YoutubeDL(' not in worker
    assert '_MUSIC_STREAMS =' not in worker
    assert '_MUSIC_PCM_PREPARATIONS =' not in worker
    assert 'def _probe_local_lavalink_http(' not in worker
    assert 'def _ensure_phone_lavalink_started(' not in worker
    assert 'def _phone_lavalink_port(' not in worker
    assert '_phone_worker_music_bridge_module("streams")' in worker
    assert '_phone_worker_music_bridge_module("resolucao")' in worker
    assert '_phone_worker_music_bridge_module("proxy")' in worker
    assert '_phone_worker_music_bridge_module("telemetria")' in worker
    assert '_phone_worker_music_bridge_module("servico")' in worker

    resolucao = _texto("cogs/musica/runtime_telefone/ponte_worker/resolucao.py")
    streams = _texto("cogs/musica/runtime_telefone/ponte_worker/streams.py")
    telemetria = _texto("cogs/musica/runtime_telefone/ponte_worker/telemetria.py")
    servico = _texto("cogs/musica/runtime_telefone/ponte_worker/servico.py")
    assert "yt_dlp" in resolucao
    assert "_MUSIC_STREAMS" in streams
    assert "music_agent_snapshot" in telemetria
    assert "run_service_action" in servico


def test_artefatos_legados_de_musica_nao_ficam_espalhados_no_repositorio() -> None:
    proibidos = (
        "deploy/systemd/lavalink.service",
        "deploy/systemd/phone-lavalink-watch.service",
        "deploy/systemd/phone-lavalink-watch.timer",
        "deploy/systemd/vps/lavalink.service.disabled-reference",
        "deploy/systemd/vps/lavalink.service.d.disabled-reference/README.md",
        "deploy/systemd/vps/phone-lavalink-watch.service",
        "deploy/systemd/vps/phone-lavalink-watch.timer",
        "deploy/termux/phone-lavalink/phone-lavalink.env.example",
        "deploy/termux/phone-lavalink/start-phone-lavalink.sh",
        "deploy/termux/phone-lavalink/watch-phone-lavalink.sh",
        "scripts/phone-lavalink-watch.sh",
    )
    for rel in proibidos:
        assert not (ROOT / rel).exists(), rel

    # Snapshot histórico duplicado continha outro phone_worker/music_agent e
    # podia reintroduzir implementação musical fora do domínio canônico. O
    # updater remove arquivos, mas pode deixar diretórios vazios no disco.
    snapshot = ROOT / "tts-bot-main"
    assert not snapshot.exists() or not any(p.is_file() for p in snapshot.rglob("*"))


def test_aliases_do_antigo_phone_lavalink_nao_voltam_ao_runtime_generico() -> None:
    fontes = (
        "scripts/sync-phone-worker.sh",
        "scripts/phone-worker-client.py",
        "scripts/phone-worker-watch.sh",
        "deploy/termux/phone-worker/phone_worker.py",
        "updater/core/mudancas.sh",
        "updater/core/aplicacao.sh",
        "updater/core/atualizar.sh",
        "updater/core/progresso.sh",
        "updater/core/validacao.sh",
        "updater/sistema/instalar.sh",
    )
    proibidos = (
        "PHONE_LAVALINK",
        "AUX_LAVALINK",
        "phone-lavalink",
        "phone_lavalink",
        "PHONE_LAVALINK_WATCH_CHANGED",
    )
    for rel in fontes:
        texto = _texto(rel)
        for token in proibidos:
            assert token not in texto, f"{token} reapareceu em {rel}"


def test_updater_nao_pode_reativar_lavalink_local_da_vps() -> None:
    updater = "\n".join(
        _texto(rel)
        for rel in (
            "updater/core/mudancas.sh",
            "updater/core/aplicacao.sh",
            "updater/core/atualizar.sh",
            "updater/core/validacao.sh",
            "updater/sistema/instalar.sh",
        )
    )
    assert "VPS_LAVALINK_ENABLED" not in updater
    assert "INSTALL_LEGACY_VPS_LAVALINK" not in updater
    assert "--install-legacy-vps-lavalink" not in updater
    assert "deploy/systemd/lavalink.service" not in updater
    assert "systemctl enable lavalink.service" not in updater
    assert "systemctl restart lavalink.service" not in updater
