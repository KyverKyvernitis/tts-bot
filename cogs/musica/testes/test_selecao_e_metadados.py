from __future__ import annotations


from pathlib import Path

from cogs.musica.agente_telefone import selecao
from cogs.musica.agente_telefone.configuracao import ConfiguracaoSelecaoWorker
from cogs.musica.metadados.modelos import ApiTrackCandidate, UrlProfile
from cogs.musica.metadados.normalizacao import (
    clean_metadata_title,
    compact_key,
    parse_iso8601_duration,
    title_quality_score,
    unique_queries,
)


def _cfg() -> ConfiguracaoSelecaoWorker:
    return ConfiguracaoSelecaoWorker(
        worker_only=True,
        agente_ativo=True,
        exigir_turbo=True,
        papeis_obrigatorios=frozenset({"phone-worker"}),
        capacidades_obrigatorias=frozenset({"ffmpeg", "ffprobe"}),
        versao_minima_agente="0.3.8",
        maximo_sessoes=2,
        bootstrap_ao_tocar=True,
        cache_segundos=0.0,
    )


def _worker_sem_lavalink() -> dict:
    return {
        "worker_id": "telefone-1",
        "name": "Telefone 1",
        "enabled": True,
        "online": True,
        "profile": "turbo",
        "roles": ["phone-worker"],
        "capabilities": ["ffmpeg", "ffprobe"],
        "status": {
            "profile": "turbo",
            "music_agent": {
                "ok": True,
                "discord_ready": True,
                "version": "0.3.8",
                "guilds": {},
            },
        },
    }


def test_worker_sem_music_node_lavalink_pode_ser_selecionado(monkeypatch) -> None:
    monkeypatch.setattr(selecao, "carregar_configuracao_selecao", _cfg)
    monkeypatch.setattr(selecao, "carregar_workers_publicos", lambda: [_worker_sem_lavalink()])
    escolhido = selecao._select_music_worker_uncached()
    assert escolhido.available is True
    assert escolhido.worker_id == "telefone-1"
    assert "music_node" not in (escolhido.worker or {}).get("status", {})


def test_fallback_configurado_nao_inventa_lavalink(monkeypatch) -> None:
    monkeypatch.setattr(selecao, "carregar_configuracao_selecao", _cfg)
    monkeypatch.setattr(selecao, "_phone_worker_base_url", lambda: "http://telefone:8766")
    monkeypatch.setattr(selecao.config, "PHONE_WORKER_TOKEN", "token", raising=False)
    monkeypatch.setattr(selecao.config, "PHONE_WORKER_HOST", "telefone", raising=False)
    monkeypatch.setattr(
        selecao,
        "consultar_saude_worker_configurado",
        lambda *_: {
            "ok": True,
            "profile": "turbo",
            "music_agent": {
                "ok": True,
                "discord_ready": True,
                "version": "0.3.8",
                "guilds": {},
            },
        },
    )
    escolhido = selecao._configured_phone_worker_selection()
    assert escolhido is not None and escolhido.available
    status = dict((escolhido.worker or {}).get("status", {}))
    assert "music_agent" in status
    assert "music_node" not in status
    assert "music-lavalink" not in set((escolhido.worker or {}).get("capabilities", []))


def test_modelos_e_normalizacao_de_metadados_estao_separados() -> None:
    faixa = ApiTrackCandidate(title="Música (Official Video)", artist="Artista")
    assert faixa.key == "artista musica"
    assert clean_metadata_title("Faixa | Spotify") == "Faixa"
    assert compact_key("Ártista - Música [Official Audio]") == "artista musica"
    assert parse_iso8601_duration("PT3M12S") == 192.0
    assert unique_queries("A  B", "a b", "Outra") == ["A B", "Outra"]
    assert title_quality_score("Faixa official audio", channel="Artista - Topic") > 0

    perfil = UrlProfile(raw="x", canonical="x", host="", is_url=False)
    assert perfil.is_url is False


def test_selecao_nao_possui_gate_de_music_node_lavalink() -> None:
    texto = __import__("pathlib").Path(selecao.__file__).read_text(encoding="utf-8")
    assert "MUSIC_WORKER_LAVALINK" not in texto
    assert "MUSIC_WORKER_REQUIRE_MUSIC_NODE_STATUS" not in texto
    assert "music_node_offline" not in texto


def test_resolucao_do_phone_worker_nao_transporta_estado_de_player_lavalink() -> None:
    arquivo = Path(__file__).resolve().parents[1] / "agente_telefone" / "resolucao.py"
    texto = arquivo.read_text(encoding="utf-8")
    for proibido in ("lavalink_query", "lavalink_resolved", "lavalink_playable", "lavalink_encoded"):
        assert proibido not in texto


def test_worker_termux_fallback_com_music_agent_pronto_nao_precisa_ser_turbo(monkeypatch) -> None:
    cfg = ConfiguracaoSelecaoWorker(
        worker_only=True,
        agente_ativo=True,
        exigir_turbo=True,
        papeis_obrigatorios=frozenset({"phone-worker"}),
        capacidades_obrigatorias=frozenset({"ffmpeg", "ffprobe"}),
        versao_minima_agente="0.3.8",
        maximo_sessoes=2,
        bootstrap_ao_tocar=True,
        cache_segundos=0.0,
    )
    worker = {
        "worker_id": "telefone-fallback",
        "name": "Telefone fallback",
        "enabled": True,
        "online": True,
        "profile": "completo",
        "runtime_mode": "termux",
        "roles": ["phone-worker", "music", "music-agent"],
        "capabilities": ["phone-worker", "ffmpeg", "ffprobe", "music", "music-agent", "music-ytdlp"],
        "status": {
            "profile": "completo",
            "runtime_mode": "termux",
            "music_agent": {
                "ok": True,
                "available": True,
                "discord_ready": True,
                "version": "0.3.8",
                "guilds": {},
            },
        },
    }
    monkeypatch.setattr(selecao, "carregar_configuracao_selecao", lambda: cfg)
    monkeypatch.setattr(selecao, "carregar_workers_publicos", lambda: [worker])

    escolhido = selecao._select_music_worker_uncached()

    assert escolhido.available is True
    assert escolhido.worker_id == "telefone-fallback"
    assert escolhido.reason == "ok"


def test_worker_nao_turbo_so_bloqueia_bootstrap_do_agente(monkeypatch) -> None:
    cfg = ConfiguracaoSelecaoWorker(
        worker_only=True,
        agente_ativo=True,
        exigir_turbo=True,
        papeis_obrigatorios=frozenset({"phone-worker"}),
        capacidades_obrigatorias=frozenset({"ffmpeg", "ffprobe"}),
        versao_minima_agente="0.3.8",
        maximo_sessoes=2,
        bootstrap_ao_tocar=True,
        cache_segundos=0.0,
    )
    worker = {
        "worker_id": "telefone-fallback",
        "name": "Telefone fallback",
        "enabled": True,
        "online": True,
        "profile": "completo",
        "roles": ["phone-worker"],
        "capabilities": ["phone-worker", "ffmpeg", "ffprobe"],
        "status": {"profile": "completo", "music_agent": {}},
    }
    monkeypatch.setattr(selecao, "carregar_configuracao_selecao", lambda: cfg)
    monkeypatch.setattr(selecao, "carregar_workers_publicos", lambda: [worker])
    monkeypatch.setattr(selecao, "_configured_phone_worker_selection", lambda *args, **kwargs: None)

    escolhido = selecao._select_music_worker_uncached()

    assert escolhido.available is False
    assert "não_turbo_para_bootstrap" in escolhido.reason
    assert "Nenhum worker online" not in escolhido.message
    assert "worker online" in escolhido.message


def test_registry_da_musica_usa_o_mesmo_snapshot_publico_do_painel(monkeypatch) -> None:
    from cogs.musica.agente_telefone import registro
    from utility.commands import workers_registry

    worker = _worker_sem_lavalink()
    chamadas = []

    class RegistryFake:
        def snapshot(self, *, lock_timeout_seconds=None):
            chamadas.append(lock_timeout_seconds)
            return {
                "ok": True,
                "workers": [worker],
                "summary": {"online": 1, "runtime_online": 1},
            }

    fake = RegistryFake()
    monkeypatch.setattr(workers_registry, "get_core_workers_registry", lambda: fake)

    encontrados = registro.carregar_workers_publicos()

    assert encontrados == [worker]
    assert chamadas and chamadas[0] is not None


def test_snapshot_stale_ainda_preserva_worker_online(monkeypatch) -> None:
    from cogs.musica.agente_telefone import registro
    from utility.commands import workers_registry

    worker = _worker_sem_lavalink()

    class RegistryFake:
        def snapshot(self, *, lock_timeout_seconds=None):
            return {
                "ok": False,
                "stale": True,
                "error": "registry_lock_timeout",
                "workers": [worker],
                "summary": {"online": 1, "runtime_online": 1},
            }

    monkeypatch.setattr(workers_registry, "get_core_workers_registry", lambda: RegistryFake())

    encontrados = registro.carregar_workers_publicos()

    assert encontrados and encontrados[0]["online"] is True
