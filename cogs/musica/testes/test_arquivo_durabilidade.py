"""Fila durável e índice do fórum, sem cache permanente de áudio."""
from __future__ import annotations

import json

import pytest

from cogs.musica.arquivo_coordenador import ArchiveCoordinator
from cogs.musica.busca import arquivo, memoria
from cogs.musica.nucleo.modelos import MusicTrack


def _track(identifier="abcdefghijk", duration=1800):
    return MusicTrack(title="Artista - Faixa longa", display_title="Faixa longa",
                      display_uploader="Artista", source="YouTube", duration=duration,
                      webpage_url=f"https://www.youtube.com/watch?v={identifier}",
                      requester_id=1, queue_item_id=identifier)


def _setup(tmp_path, monkeypatch):
    path = tmp_path / "choices.sqlite3"
    monkeypatch.setattr(arquivo, "_db_path", lambda: path)
    monkeypatch.setattr(memoria, "_db_path", lambda: path)
    memoria.limpar_memoria_busca()
    arquivo.set_channel(1, 2, kind="forum")


def _ref(**extras):
    return {"guild_id": 1, "forum_id": 2, "channel_id": 3,
            "message_id": 4, "attachment_id": 5, **extras}


def test_aprendizagem_e_observacao_aceitam_faixa_maior_que_dez_minutos(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    track = _track(duration=7200)
    assert arquivo.register_learned_batch([(memoria._track_payload(track), 100)]) == 1
    assert arquivo.pending()["track"]["duration"] == 7200
    monkeypatch.setattr(memoria, "faixa_aprendida", lambda _track: True)
    manager = ArchiveCoordinator(object())
    manager.observe(1, track, {"position_ms": 0, "playback_token": 1}, confirmed=True)
    with arquivo._db() as db:
        assert db.execute("SELECT tocadas FROM arquivo_musicas").fetchone() == (1,)
    for bad in (float("nan"), float("inf"), -1):
        assert not arquivo.record_play(_track(duration=bad), f"bad:{bad}")


def test_fila_atende_antigas_e_retry_pronto_antes_das_recém_chegadas(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    clock = [100.0]
    monkeypatch.setattr(arquivo.time, "time", lambda: clock[0])
    oldest = _track("oldoldold01")
    arquivo.register_learned_batch([(memoria._track_payload(oldest), clock[0])])
    clock[0] += 1
    newer = _track("newnewnew01")
    arquivo.register_learned_batch([(memoria._track_payload(newer), clock[0])])
    key = arquivo.media_key(oldest)
    assert arquivo.pending()["key"] == key
    arquivo.mark_result(key, {"status": "failed", "reason": "download_timeout"})
    arquivo.mark_result(arquivo.media_key(newer), {"status": "done", "presentation": 8, "reference": _ref()})
    retry_at = arquivo.failures()[0]["retry_at"]
    clock[0] = retry_at + 1
    latest = _track("newnewnew02")
    arquivo.register_learned_batch([(memoria._track_payload(latest), clock[0])])
    assert arquivo.pending()["key"] == key
    assert arquivo.pending()["retry"] is True


def test_retry_infinito_tem_backoff_limitado_e_estado_persistente(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    clock = [100.0]
    monkeypatch.setattr(arquivo.time, "time", lambda: clock[0])
    track = _track()
    key = arquivo.media_key(track)
    arquivo.record_play(track, "one")
    for attempt in range(20):
        arquivo.mark_attempt(key)
        arquivo.mark_result(key, {"status": "failed", "reason": "discord_http_error"})
        failure = arquivo.failures()[0]
        assert failure["state"] == "failed"
        assert failure["attempts"] == attempt + 1
        assert 0 < failure["retry_at"] - clock[0] <= 21600
        assert arquivo.pending() is None
        clock[0] = failure["retry_at"] + 1
    # Nova conexão lê o job sem depender de nenhuma fila Python.
    assert arquivo.pending()["key"] == key


def test_outbox_recupera_reinicio_e_commit_sem_ack_e_nao_duplica_post(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    track = _track()
    assert memoria.registrar_selecao_busca("minha música", track)
    with arquivo._learned_lock:
        arquivo._new_learned.clear()
    real_ack = memoria.confirmar_pendencia_arquivamento

    def failing_ack(*_args, **_kwargs):
        raise RuntimeError("reinício antes de confirmar")

    monkeypatch.setattr(memoria, "confirmar_pendencia_arquivamento", failing_ack)
    with pytest.raises(RuntimeError, match="reinício"):
        arquivo.flush_learned()
    assert len(memoria.obter_pendencias_arquivamento()) == 1
    assert arquivo.counts()["learned"] == 1
    key = arquivo.media_key(track)
    arquivo.mark_result(key, {"status": "done", "presentation": 8, "reference": _ref()})
    monkeypatch.setattr(memoria, "confirmar_pendencia_arquivamento", real_ack)
    assert arquivo.flush_learned() == 1
    assert not memoria.obter_pendencias_arquivamento()
    assert arquivo.counts()["learned"] == 1
    assert arquivo.archived(track)[0] == _ref()
    assert arquivo.pending() is None


def test_lookup_url_usa_alias_e_preserva_metadata_oficial_e_segmentos(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    track = _track()
    track.original_url = "https://open.spotify.com/track/official"
    assert arquivo.register_learned_batch([(memoria._track_payload(track), 100)]) == 1
    segments = [{"order": 0, "offset_seconds": 0, "duration": 1800, **_ref()}]
    reference = _ref(manifest_attachment_id=7, segments=segments)
    arquivo.mark_result(arquivo.media_key(track), {"status": "done", "presentation": 8,
                                                "reference": reference, "emoji": "🎵"})
    for url in (track.original_url, "https://youtu.be/abcdefghijk?t=10"):
        resolved = arquivo.archived_track_for_url(url, requester_id=42, requester_name="Usuário", guild_id=1)
        assert resolved is not None
        assert resolved.display_title == "Faixa longa" and resolved.display_uploader == "Artista"
        assert resolved.duration == 1800 and resolved.archive_ref == reference
        assert resolved.requester_id == 42 and resolved.requester_name == "Usuário"
        assert resolved.stream_url == "" and resolved.source_emoji == "🎵"
    assert arquivo.archived_track_for_url(track.original_url, guild_id=99) is None
    assert arquivo.archived_track_for_url("https://open.spotify.com/track/unknown", guild_id=1) is None


def test_referencia_parcial_persiste_para_retry_sem_ser_tocavel(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    track = _track()
    key = arquivo.media_key(track)
    arquivo.record_play(track, "one")
    arquivo.mark_result(key, {"status": "failed", "reason": "discord_http_error", "reference": _ref()})
    assert arquivo.archived(track) == ({}, "", "")
    assert arquivo.archived_track_for_url(track.webpage_url, guild_id=1) is None
    retry_at = arquivo.failures()[0]["retry_at"]
    monkeypatch.setattr(arquivo.time, "time", lambda: retry_at + 1)
    assert arquivo.pending()["reference"] == _ref()
    wrong_segment = {"order": 0, **_ref(channel_id=999)}
    arquivo.mark_result(key, {"status": "done", "presentation": 8,
                             "reference": _ref(segments=[wrong_segment])})
    assert arquivo.archived(track) == ({}, "", "")
    assert arquivo.failures()[0]["reason"] == "invalid_reference"


def test_auditoria_e_falhas_navegam_alem_de_mil_sem_perder_historico(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    track = _track()
    key = arquivo.media_key(track)
    arquivo.record_play(track, "one")
    with arquivo._db() as db:
        db.executemany("INSERT INTO arquivo_eventos(chave, ocorrido_em, evento) VALUES (?, ?, ?)",
                       ((key, i, "teste") for i in range(2100)))
        db.executemany("INSERT INTO arquivo_musicas(chave, track_json, estado, ultima_tentativa_em) VALUES (?, ?, 'failed', 10)",
                       ((f"{i:032x}", json.dumps({"title": str(i)})) for i in range(1100)))
        arquivo._audit(db, key, "novo")
    assert len(arquivo.history(key, 8, offset=1100)) == 8
    first = arquivo.history(key, 8)
    second = arquivo.history(key, 8, before_id=first[-1]["id"])
    assert not {event["id"] for event in first} & {event["id"] for event in second}
    with arquivo._db() as db:
        assert db.execute("SELECT COUNT(*) FROM arquivo_eventos").fetchone()[0] > 2000
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert len(arquivo.failures(8, offset=1050)) == 8
    first = arquivo.failures(8)
    cursor = arquivo.parse_failure_cursor(arquivo.failure_cursor(first[-1]))
    second = arquivo.failures(8, after=cursor)
    assert not {entry["key"] for entry in first} & {entry["key"] for entry in second}


def test_upgrade_mesma_thread_nao_agenda_remocao_do_audio_validado(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    track = _track()
    key = arquivo.media_key(track)
    arquivo.record_play(track, "one")
    arquivo.mark_result(key, {"status": "done", "presentation": 7, "reference": _ref()})
    reference = _ref(manifest_attachment_id=9, segments=[{"order": 0, **_ref()}])
    arquivo.mark_result(key, {"status": "done", "presentation": 8, "reference": reference})
    assert arquivo.archived(track)[0] == reference
    assert arquivo.cleanup_pending() is None


def test_lookup_nao_confunde_ids_em_query_nem_caixa_youtube(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    track = _track("AbCdEfGhIjk")
    track.original_url = "https://music.apple.com/br/album/teste/123?i=111"
    arquivo.record_play(track, "one")
    arquivo.mark_result(arquivo.media_key(track), {"status": "done", "presentation": 8, "reference": _ref()})
    assert arquivo.archived_track_for_url(track.original_url, guild_id=1) is not None
    assert arquivo.archived_track_for_url("https://music.apple.com/br/album/teste/123?i=222", guild_id=1) is None
    assert arquivo.archived_track_for_url("https://www.youtube.com/watch?v=abcdefghijk", guild_id=1) is None


def test_alias_spotify_internacional_e_youtube_shorts_sem_rekey(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    track = _track("abcdefghijk")
    track.webpage_url = "https://www.youtube.com/shorts/abcdefghijk"
    track.original_url = "https://open.spotify.com/intl-pt/track/OriginalCaseID"
    key = arquivo.media_key(track)
    arquivo.record_play(track, "one")
    arquivo.mark_result(key, {"status": "done", "presentation": 8, "reference": _ref()})
    for url in ("https://open.spotify.com/track/OriginalCaseID", track.original_url,
                "https://www.youtube.com/watch?v=abcdefghijk"):
        resolved = arquivo.archived_track_for_url(url, guild_id=1)
        assert resolved is not None and resolved.archive_ref == _ref()
        assert arquivo.media_key(resolved) == key
    assert arquivo.archived_track_for_url("https://open.spotify.com/track/originalcaseid", guild_id=1) is None
    wrong = _track("abcdefghijk")
    wrong.webpage_url = "https://open.spotify.com/intl-pt/track/originalcaseid"
    assert arquivo.archived(wrong) == ({}, "", "")


@pytest.mark.asyncio
async def test_job_longo_continua_enquanto_agente_responde_working(tmp_path, monkeypatch):
    import asyncio
    from cogs.musica import arquivo_coordenador as module

    _setup(tmp_path, monkeypatch)
    track = _track(duration=7200)
    arquivo.record_play(track, "one")
    real_pending = arquivo.pending
    real_sleep = asyncio.sleep
    polls = 0
    pending_calls = 0

    class Bot:
        async def wait_until_ready(self):
            pass

    def once(**kwargs):
        nonlocal pending_calls
        pending_calls += 1
        if pending_calls > 1:
            raise asyncio.CancelledError
        return real_pending(**kwargs)

    async def status(**_kwargs):
        return {"available": True, "version": "0.3.82"}

    async def command(action, **_kwargs):
        nonlocal polls
        if action == "archive_enqueue":
            return {"ok": True, "status": "queued"}
        polls += 1
        if polls <= 100:
            return {"ok": True, "status": "working"}
        return {"ok": True, "status": "done", "presentation": 8, "reference": _ref()}

    async def fast_sleep(_seconds):
        await real_sleep(0)

    monkeypatch.setattr(arquivo, "pending", once)
    monkeypatch.setattr(module, "music_agent_status", status)
    monkeypatch.setattr(module, "music_agent_command", command)
    monkeypatch.setattr(module.asyncio, "sleep", fast_sleep)
    with pytest.raises(asyncio.CancelledError):
        await ArchiveCoordinator(Bot())._run()
    assert polls == 101
    assert arquivo.archived(track)[0] == _ref()
    with arquivo._db() as db:
        assert db.execute("SELECT estado, falhas FROM arquivo_musicas").fetchone() == ("done", 0)


def test_url_de_fonte_confirmada_tambem_aponta_para_metadata_arquivada(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    track = _track()
    track.original_url = "https://open.spotify.com/track/official"
    key = arquivo.media_key(track)
    arquivo.record_play(track, "one")
    page = "https://artist.bandcamp.com/track/long-song"
    arquivo.set_source_override(key, page)
    arquivo.mark_result(key, {"status": "done", "presentation": 8, "reference": _ref(), "source_url": page})
    resolved = arquivo.archived_track_for_url(page, guild_id=1)
    assert resolved is not None
    assert resolved.display_title == "Faixa longa" and resolved.archive_ref == _ref()
