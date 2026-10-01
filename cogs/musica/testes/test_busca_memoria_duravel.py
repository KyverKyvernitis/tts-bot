from __future__ import annotations

import json
import sqlite3

import pytest

from cogs.musica import configuracao as config
from cogs.musica.busca import memoria
from cogs.musica.nucleo.modelos import MusicTrack


@pytest.fixture(autouse=True)
def memoria_isolada(tmp_path, monkeypatch):
    monkeypatch.setattr(memoria, "_db_path", lambda: tmp_path / "escolhas.sqlite3")
    monkeypatch.setattr(config, "MUSIC_SEARCH_CHOICE_MEMORY_MAX_ENTRIES", 2)
    monkeypatch.setattr(memoria, "_fts_disponivel", None)
    memoria.limpar_memoria_busca()
    yield
    memoria.limpar_memoria_busca()


def _track(title: str, slug: str) -> MusicTrack:
    return MusicTrack(title=title, webpage_url=f"https://youtube.test/{slug}",
                      original_url=f"https://youtube.test/{slug}", requester_id=1, source="youtube")


def _popular() -> MusicTrack:
    track = _track("Compass", "compass")
    memoria.registrar_link_busca(track, now=1)
    for idx in range(24):
        memoria.registrar_link_busca(_track(f"Outra Faixa {idx}", f"outra-{idx}"), now=idx + 2)
    return track


def test_evicao_e_restart_preservam_catalogo_e_limitam_ram():
    _popular()
    with memoria._abrir_db() as conn:
        total = conn.execute("SELECT COUNT(*) FROM escolhas").fetchone()[0]
    assert total > 24
    assert len(memoria._memoria) == 2
    assert memoria.chave_semantica_busca("compass") not in memoria._memoria

    memoria.recarregar_memoria_busca()
    assert len(memoria._memoria) == 2
    hit = memoria.obter_escolha_busca("compass", requester_id=77)
    assert hit is not None and hit.webpage_url == "https://youtube.test/compass"
    assert hit.requester_id == 77 and hit.stream_url == ""
    assert len(memoria._memoria) == 2
    with memoria._abrir_db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM escolhas").fetchone()[0] == total


@pytest.mark.parametrize("fts", [None, False])
def test_typo_e_lexical_consultam_indices_fora_ram(monkeypatch, fts):
    monkeypatch.setattr(memoria, "_fts_disponivel", fts)
    _popular()
    memoria.recarregar_memoria_busca()
    typo = memoria.obter_escolha_busca("composs")
    assert typo is not None and typo.webpage_url == "https://youtube.test/compass"
    memoria.recarregar_memoria_busca()
    lexical = memoria.obter_escolha_busca("compass orchestra")
    assert lexical is not None and lexical.webpage_url == "https://youtube.test/compass"
    assert memoria.obter_escolha_busca("composs live") is None


def test_link_fora_ram_mantem_prioridade_e_identidade():
    track = _popular()
    assert memoria.faixa_aprendida(track)
    assert not memoria.registrar_selecao_busca("compass", _track("Compass", "lyrics"))
    hit = memoria.obter_escolha_busca("compass")
    assert hit is not None and hit.webpage_url == track.webpage_url


def test_esquecimento_explicito_remove_escolha_que_so_estava_em_disco():
    _popular()
    assert memoria.chave_semantica_busca("compass") not in memoria._memoria
    assert memoria.esquecer_escolha_busca("compass")
    memoria.recarregar_memoria_busca()
    assert memoria.obter_escolha_busca("compass") is None
    with memoria._abrir_db() as conn:
        key = memoria.chave_semantica_busca("compass")
        assert conn.execute("SELECT 1 FROM escolhas_indice WHERE chave=?", (key,)).fetchone() is None
        assert conn.execute("SELECT 1 FROM escolhas_tokens WHERE chave=?", (key,)).fetchone() is None
        assert conn.execute("SELECT 1 FROM escolhas_trigramas WHERE chave=?", (key,)).fetchone() is None


def test_outbox_deduplica_aliases_sobrevive_restart_e_ack_preserva_update():
    memoria.registrar_link_busca(_track("Mili - Compass", "compass"))
    events = memoria.obter_pendencias_arquivamento()
    assert len(events) == 1
    key, track, stamp = events[0]
    memoria.recarregar_memoria_busca()
    assert memoria.obter_pendencias_arquivamento()[0][0] == key
    with memoria._abrir_db() as conn:
        memoria._enfileirar_arquivo(conn, track, stamp=stamp + 1)
    memoria.confirmar_pendencia_arquivamento(key, stamp)
    assert len(memoria.obter_pendencias_arquivamento()) == 1
    memoria.confirmar_pendencia_arquivamento(key, stamp + 1)
    assert memoria.obter_pendencias_arquivamento() == []


def test_escolha_e_outbox_revertem_juntas_se_transacao_falhar(monkeypatch):
    def falhar(*args):
        raise sqlite3.OperationalError("falha controlada de escrita")

    monkeypatch.setattr(memoria, "_indexar_db", falhar)
    memoria.registrar_selecao_busca("compass", _track("Compass", "compass"))
    with memoria._abrir_db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM escolhas").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM escolhas_arquivo_outbox").fetchone()[0] == 0


def test_migracao_legado_indexa_e_recupera_outbox_uma_vez():
    track = _track("Compass", "compass")
    with sqlite3.connect(memoria._db_path()) as conn:
        conn.execute("CREATE TABLE escolhas(chave TEXT PRIMARY KEY, consulta TEXT NOT NULL, origem TEXT NOT NULL, "
                     "prioridade INTEGER NOT NULL, track_json TEXT NOT NULL, registrado_em REAL NOT NULL)")
        for query in ("compass", "mili compass"):
            conn.execute("INSERT INTO escolhas VALUES (?, ?, 'link', 100, ?, 1)",
                         (memoria.chave_semantica_busca(query), query, json.dumps(memoria._track_payload(track))))
    memoria.recarregar_memoria_busca()
    events = memoria.obter_pendencias_arquivamento()
    assert len(events) == 1
    assert memoria.obter_escolha_busca("composs") is not None
    key, _, stamp = events[0]
    memoria.confirmar_pendencia_arquivamento(key, stamp)
    memoria.recarregar_memoria_busca()
    assert memoria.obter_pendencias_arquivamento() == []


def test_busca_aproximada_respeita_orcamento_de_candidatos(monkeypatch):
    monkeypatch.setattr(config, "MUSIC_SEARCH_CHOICE_MEMORY_CANDIDATE_LIMIT", 16)
    monkeypatch.setattr(config, "MUSIC_SEARCH_CHOICE_MEMORY_MAX_ENTRIES", 100)
    for idx in range(80):
        memoria.registrar_selecao_busca(f"compass variant {idx}", _track(f"Compass Variant {idx}", str(idx)))
    candidates = memoria._candidatos_persistidos("compass variant", aproximada=True)
    assert len(candidates) <= 16


def test_falha_temporaria_na_migracao_nao_marca_pronto_nem_apaga_ram(monkeypatch):
    abrir = memoria._abrir_db

    def indisponivel():
        raise sqlite3.OperationalError("disco indisponível")

    monkeypatch.setattr(memoria, "_abrir_db", indisponivel)
    memoria.recarregar_memoria_busca()
    assert memoria._loaded is False
    memoria.registrar_selecao_busca("compass", _track("Compass", "compass"))
    assert memoria.obter_escolha_busca("compass") is not None
    assert memoria._loaded is False
    monkeypatch.setattr(memoria, "_abrir_db", abrir)
    monkeypatch.setattr(memoria, "_load_retry_after", 0.0)
    assert memoria.obter_escolha_busca("compass") is not None
    assert memoria._loaded is True


def test_lookup_reutiliza_schema_sem_ddl(monkeypatch):
    _popular()
    statements = []

    class ObservedConnection(memoria._MemoryConnection):
        def execute(self, sql, *args, **kwargs):
            statements.append(sql)
            return super().execute(sql, *args, **kwargs)

        def executescript(self, sql, *args, **kwargs):
            statements.append(sql)
            return super().executescript(sql, *args, **kwargs)

    monkeypatch.setattr(memoria, "_MemoryConnection", ObservedConnection)
    assert memoria.obter_escolha_busca("compass") is not None
    assert not any("CREATE" in sql or "journal_mode" in sql for sql in statements)


def test_ack_nao_perde_evento_se_relogio_repetir_timestamp():
    track = _track("Compass", "compass")
    memoria.registrar_link_busca(track)
    key, _, stamp = memoria.obter_pendencias_arquivamento()[0]
    with memoria._abrir_db() as conn:
        memoria._enfileirar_arquivo(conn, track, stamp=stamp)
    memoria.confirmar_pendencia_arquivamento(key, stamp)
    pending = memoria.obter_pendencias_arquivamento()
    assert len(pending) == 1 and pending[0][2] > stamp
