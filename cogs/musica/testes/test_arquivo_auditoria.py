"""Auditoria persistente e fallback conservador para fontes do arquivo."""
from __future__ import annotations

import asyncio

import pytest

from cogs.musica.busca import arquivo
from cogs.musica.nucleo.modelos import MusicTrack
from cogs.musica.runtime_telefone.agente.correspondencia import CatalogNoMatchError
from cogs.musica.runtime_telefone.agente.validade_stream import ArchiveMixin, _archive_download_reason


PAGE = "https://heavenpierceher.bandcamp.com/track/disgrace-humiliation"
VIDEO = "https://www.youtube.com/watch?v=AbCdEfGh123"


def _track(title="Heaven Pierce Her - Disgrace. Humiliation.", identifier="one", *, webpage=""):
    return MusicTrack(title=title, display_title=title, display_uploader="Heaven Pierce Her",
                      duration=110, webpage_url=webpage or f"https://open.spotify.com/track/{identifier}",
                      original_url=f"https://open.spotify.com/track/{identifier}",
                      requester_id=1, source="Spotify")


def _setup(tmp_path, monkeypatch):
    monkeypatch.setattr(arquivo, "_db_path", lambda: tmp_path / "escolhas.sqlite3")
    arquivo.set_channel(1, 2, kind="forum")


def test_sem_correspondencia_fica_visivel_sem_retry_e_fonte_nova_reabre(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    track = _track()
    key = arquivo.media_key(track)
    arquivo.record_play(track, "1")
    revision = "0.3.81:2026.08.19"
    arquivo.mark_attempt(key, source=track.webpage_url, revision=revision)
    arquivo.mark_result(key, {"status": "unavailable", "reason": "no_match", "agent_revision": revision})
    assert arquivo.pending() is None
    assert arquivo.counts()["unavailable"] == 1 and arquivo.counts()["unposted"] == 0
    assert arquivo.failures()[0]["reason"] == "no_match"
    assert [event["event"] for event in arquivo.history(key)[:2]] == ["unavailable", "tentativa"]
    assert arquivo.reopen_on_revision(revision) == 0

    # Uma reprodução real trouxe uma URL pública nova. O stream temporário
    # jamais vai ao banco e a identidade original da faixa permanece Spotify.
    arquivo.record_play(_track(webpage=VIDEO), "2")
    pending = arquivo.pending()
    assert pending["key"] == key and pending["source_known"] == VIDEO
    with arquivo._db() as db:
        raw = db.execute("SELECT track_json, fonte_estavel FROM arquivo_musicas WHERE chave=?", (key,)).fetchone()
    assert raw[1] == VIDEO and "spotify.com/track/one" in raw[0]


def test_link_manual_canonico_e_auditoria_sem_query(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    track = _track()
    key = arquivo.media_key(track)
    arquivo.record_play(track, "1")
    arquivo.set_source_override(key, PAGE + "?utm_source=friend#comment")
    assert arquivo.pending()["source_override"] == PAGE
    assert arquivo.history(key)[0]["source"] == PAGE
    with pytest.raises(ValueError):
        arquivo.set_source_override(key, "https://heavenpierceher.bandcamp.com/track/")


def test_retries_transitorios_tem_limite_e_nova_revisao_reabre(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    track = _track()
    key = arquivo.media_key(track)
    arquivo.record_play(track, "1")
    for attempt in range(5):
        arquivo.mark_attempt(key, revision="0.3.81:2026.08.19")
        arquivo.mark_result(key, {"status": "failed", "reason": "download_timeout",
                                  "agent_revision": "0.3.81:2026.08.19"})
        assert arquivo.failures()[0]["attempts"] == attempt + 1
    assert arquivo.failures()[0]["state"] == "paused"
    assert arquivo.pending() is None
    assert arquivo.reopen_on_revision("0.3.81:2026.08.19") == 0
    assert arquivo.reopen_on_revision("0.3.82:2026.08.19") == 1
    assert arquivo.pending()["key"] == key
    arquivo.mark_result(key, {"status": "unavailable", "reason": "no_match",
                              "agent_revision": "0.3.82:2026.08.19"})
    arquivo.requeue(key)
    assert arquivo.pending()["key"] == key


def test_banda_confirmada_descobre_slug_mas_so_persiste_apos_validacao(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    first = _track()
    key = arquivo.media_key(first)
    arquivo.record_play(first, "1")
    arquivo.set_source_override(key, PAGE)
    reference = {"guild_id": 1, "forum_id": 2, "channel_id": 3,
                 "message_id": 4, "attachment_id": 5}
    arquivo.mark_result(key, {"status": "done", "reference": reference,
                              "presentation": 7, "source_url": PAGE,
                              "agent_revision": "0.3.81:2026.08.19"})
    assert arquivo.archived(first)[0] == reference

    second = _track("Heaven Pierce Her - Disgrace. Humiliation.", "two")
    second_key = arquivo.media_key(second)
    arquivo.record_play(second, "2")
    candidate = arquivo.bandcamp_candidate(arquivo._track_payload(second))
    assert candidate == PAGE
    assert not arquivo.queue_discovered_source(second_key, "https://evil.bandcamp.com/track/disgrace-humiliation")
    assert arquivo.pending()["source_override"] == ""
    arquivo.mark_result(second_key, {"status": "unavailable", "reason": "no_match"})
    assert arquivo.queue_discovered_source(second_key, candidate)
    assert arquivo.pending()["source_override"] == PAGE
    assert any(event["event"] == "fonte_descoberta" for event in arquivo.history(second_key))


@pytest.mark.asyncio
async def test_agent_preserva_motivo_da_busca_vazia_e_nao_vaza_link_assinado():
    logs = []

    class Worker(ArchiveMixin):
        def __init__(self):
            self._archive_init()

        async def _archive_one(self, item):
            raise CatalogNoMatchError("nenhuma fonte encontrada corresponde à faixa solicitada")

        def log(self, event, **kwargs):
            logs.append((event, kwargs))

    worker = Worker()
    body = {"archive_key": "a" * 32, "guild_id": 1, "archive_channel_id": 2,
            "track": {"webpage_url": "https://open.spotify.com/track/one", "duration": 110}}
    await worker.cmd_archive_enqueue(body)
    await worker._archive_queue.join()
    result = await worker.cmd_archive_status(body)
    assert result["status"] == "unavailable" and result["reason"] == "no_match"
    assert logs[-1][1]["reason"] == "no_match"
    signed = b"ERROR: HTTP Error 403: https://t4.bcbits.com/stream?token=secret"
    assert _archive_download_reason(signed, bandcamp=True) == "source_http_403"
    assert "secret" not in str(logs)


@pytest.mark.asyncio
async def test_resolucao_sem_url_tocavel_e_classificada_como_sem_fonte(tmp_path):
    class Worker(ArchiveMixin):
        states = {}
        cookies_file = ""
        js_runtimes = ""
        _archive_active = "a" * 32

        async def resolve_track(self, _query, *, track_meta, body, priority):
            from types import SimpleNamespace
            assert body["guild_id"] == 1 and priority == 20
            return SimpleNamespace(webpage_url=track_meta["webpage_url"], duration=110)

        def _query_from_track_meta(self, _track):
            return "faixa aprendida"

    track = {"webpage_url": _track().webpage_url, "duration": 110}
    item = {"key": "a" * 32, "guild_id": 1, "track": track, "emoji": "🎵"}
    with pytest.raises(CatalogNoMatchError):
        await Worker()._archive_download(item, tmp_path)


@pytest.mark.asyncio
async def test_comandos_de_auditoria_exibem_motivo_e_restringem_ao_dono(tmp_path, monkeypatch):
    from cogs.musica.comandos.configuracoes import FluxoConfiguracoes

    _setup(tmp_path, monkeypatch)
    track = _track()
    key = arquivo.media_key(track)
    arquivo.record_play(track, "1")
    arquivo.mark_attempt(key, source="https://t4.bcbits.com/stream?token=segredo",
                         revision="0.3.81:2026.08.19")
    arquivo.mark_result(key, {"status": "unavailable", "reason": "no_match",
                              "agent_revision": "0.3.81:2026.08.19"})

    class Context:
        author = object()
        messages = []

        async def reply(self, message, **_kwargs):
            self.messages.append(message)

    class Bot:
        owner = False

        async def is_owner(self, _author):
            return self.owner

    context = Context()
    bot = Bot()
    settings = FluxoConfiguracoes()
    settings.bot = bot
    await settings._run_musicarquivo(context, "falhas")
    assert not context.messages

    bot.owner = True
    await settings._run_musicarquivo(context, "falhas")
    assert key in context.messages[-1] and "nenhuma fonte corresponde" in context.messages[-1]
    await settings._run_musicarquivo(context, f"auditoria {key}")
    assert "tentativa" in context.messages[-1] and "no_match" not in context.messages[-1]
    assert "token=segredo" not in "\n".join(context.messages)


@pytest.mark.asyncio
async def test_coordenador_usa_dominio_confirmado_apos_no_match(tmp_path, monkeypatch):
    from cogs.musica import arquivo_coordenador as coordinator
    from cogs.musica import arquivo_fonte

    _setup(tmp_path, monkeypatch)
    first = _track()
    first_key = arquivo.media_key(first)
    arquivo.record_play(first, "first")
    arquivo.set_source_override(first_key, PAGE)
    arquivo.mark_result(first_key, {"status": "done", "source_url": PAGE, "presentation": 7,
                                   "reference": {"guild_id": 1, "forum_id": 2, "channel_id": 3,
                                                 "message_id": 4, "attachment_id": 5}})
    later = _track(identifier="later")
    later_key = arquivo.media_key(later)
    arquivo.record_play(later, "later")

    class Bot:
        async def wait_until_ready(self):
            pass

    sent = []
    real_pending = arquivo.pending
    pending_calls = 0
    real_sleep = asyncio.sleep

    def pending(**kwargs):
        nonlocal pending_calls
        pending_calls += 1
        if pending_calls > 2:
            raise asyncio.CancelledError
        return real_pending(**kwargs)

    async def status(**_kwargs):
        return {"available": True, "version": "0.3.81", "archive_resolver_revision": "0.3.81:2026.08.19"}

    async def command(action, **kwargs):
        sent.append((action, kwargs))
        if action == "archive_enqueue":
            return {"ok": True, "status": "queued"}
        if len([x for x in sent if x[0] == "archive_enqueue"]) == 1:
            return {"status": "unavailable", "reason": "no_match"}
        return {"status": "done", "presentation": 7, "source_url": PAGE,
                "reference": {"guild_id": 1, "forum_id": 2, "channel_id": 8,
                              "message_id": 9, "attachment_id": 10}}

    def resolve(url, metadata):
        assert url == PAGE and metadata["display_uploader"] == "Heaven Pierce Her"
        return {"webpage_url": PAGE, "stream_url": "https://t4.bcbits.com/stream?token=ephemeral",
                "title": later.title, "duration": 110, "source": "Bandcamp"}

    async def sleep(_seconds):
        await real_sleep(0)

    monkeypatch.setattr(coordinator, "music_agent_status", status)
    monkeypatch.setattr(coordinator, "music_agent_command", command)
    monkeypatch.setattr(arquivo, "pending", pending)
    monkeypatch.setattr(arquivo_fonte, "resolve_bandcamp_source", resolve)
    monkeypatch.setattr(coordinator.asyncio, "sleep", sleep)
    with pytest.raises(asyncio.CancelledError):
        await coordinator.ArchiveCoordinator(Bot())._run()
    enqueues = [kwargs for action, kwargs in sent if action == "archive_enqueue"]
    assert len(enqueues) == 2 and enqueues[0]["archive_source"] is None
    assert enqueues[1]["archive_source"]["webpage_url"] == PAGE
    assert arquivo.archived(later)[0]["message_id"] == 9
    assert arquivo.failures() == []
