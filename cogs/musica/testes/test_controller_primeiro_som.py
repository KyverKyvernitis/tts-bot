import asyncio
from collections import deque
import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


@pytest.fixture
def storage(tmp_path, monkeypatch):
    from cogs.musica.busca import memoria, arquivo
    monkeypatch.setattr(memoria, "_db_path", lambda: tmp_path / "learning.sqlite3")
    monkeypatch.setattr(arquivo, "_db_path", lambda: tmp_path / "archive.sqlite3")
    memoria.limpar_memoria_busca()
    arquivo.set_channel(1, 20, kind="forum")
    yield memoria, arquivo
    memoria.limpar_memoria_busca()


def track(title="Compass", slug="abcdefghijk"):
    from cogs.musica.nucleo.modelos import MusicTrack
    url = f"https://www.youtube.com/watch?v={slug}"
    return MusicTrack(title=title, webpage_url=url, original_url=url, duration=240, requester_id=8)


def test_archive_batch_reads_once_and_preserves_duplicates(storage, monkeypatch):
    _memory, archive = storage
    song = track()
    archive.record_play(song, "play-1")
    ref = {"guild_id": 1, "forum_id": 20, "channel_id": 40, "message_id": 30, "attachment_id": 9}
    archive.mark_result(archive.media_key(song), {"status": "done", "reference": ref, "presentation": 8})
    calls = []
    original = archive._db
    def counted():
        calls.append(1)
        return original()
    monkeypatch.setattr(archive, "_db", counted)
    result = archive.archived_many([song, track(slug="zzzzzzzzzzz"), song], guild_id=1)
    assert len(calls) == 1
    assert result[0][0] == ref and result[2] == result[0] and result[1] == ({}, "", "")
    assert archive.archived_many([song], guild_id=2) == [({}, "", "")]


def test_pending_learning_survives_restart_before_alias_work(storage):
    memory, _archive = storage
    ids = memory.enfileirar_lote_link_busca([track()])
    assert ids
    with memory._abrir_db() as db:
        assert db.execute("SELECT COUNT(*) FROM escolhas").fetchone()[0] == 0
    memory.recarregar_memoria_busca()
    assert memory.processar_lotes_link_pendentes() == 1
    assert memory.obter_escolha_busca("Compass").webpage_url == track().webpage_url
    assert len(memory.obter_pendencias_arquivamento()) == 1
    assert memory.processar_lotes_link_pendentes() == 0


def test_failed_alias_commit_keeps_learning_event(storage, monkeypatch):
    memory, _archive = storage
    memory.enfileirar_lote_link_busca([track()])
    original = memory._persistir_varias
    def failed(*args, **kwargs):
        raise sqlite3.OperationalError("disk temporarily unavailable")
    monkeypatch.setattr(memory, "_persistir_varias", failed)
    with pytest.raises(sqlite3.OperationalError):
        memory.processar_lotes_link_pendentes()
    with memory._abrir_db() as db:
        assert db.execute("SELECT COUNT(*) FROM escolhas_link_pendentes").fetchone()[0] == 1
    monkeypatch.setattr(memory, "_persistir_varias", original)
    assert memory.processar_lotes_link_pendentes() == 1


def test_old_learning_replay_does_not_replace_new_link(storage):
    memory, _archive = storage
    memory.enfileirar_lote_link_busca([track(slug="abcdefghijk")])
    memory.registrar_link_busca(track(slug="zzzzzzzzzzz"))
    memory.processar_lotes_link_pendentes()
    assert memory.obter_escolha_busca("Compass").webpage_url == track(slug="zzzzzzzzzzz").webpage_url


@pytest.mark.asyncio
async def test_prepare_voice_overlaps_metadata_and_failure_cancels_lease(storage, monkeypatch):
    from cogs.musica.comandos import tocar
    started, release = asyncio.Event(), asyncio.Event()
    sent = []
    async def command(action, **kwargs):
        sent.append((action, kwargs))
        if action == "prepare_voice":
            started.set()
        return {"ok": True, "cancelled": True}
    async def metadata(*args, **kwargs):
        await asyncio.wait_for(started.wait(), timeout=1)
        await release.wait()
        raise ValueError("metadata failed")
    class Flow(tocar.FluxoTocar):
        def _music_error_message(self, exc):
            return str(exc)
        async def _voice_channel_from_ctx(self, ctx):
            return SimpleNamespace(id=50)
        async def _reply(self, *args, **kwargs):
            return None
    flow = Flow()
    flow._archived_url_batch = AsyncMock(return_value=None)
    flow.router = SimpleNamespace(music_worker_only_enabled=lambda: True,
        current_music_operation_generation=lambda guild: 0,
        ensure_music_worker_available=AsyncMock(return_value=SimpleNamespace(available=True)),
        extractor=SimpleNamespace(extract=metadata))
    monkeypatch.setattr(tocar, "music_agent_command", command)
    ctx = SimpleNamespace(guild=SimpleNamespace(id=1), channel=SimpleNamespace(id=60),
        author=SimpleNamespace(id=8, display_name="Pessoa"), message=None)
    play = asyncio.create_task(flow._run_play(ctx, "https://open.spotify.com/playlist/knownplaylist"))
    await asyncio.wait_for(started.wait(), timeout=1)
    assert not play.done()
    release.set()
    await play
    await asyncio.gather(*flow.router._music_fast_start_tasks)
    assert [action for action, _ in sent] == ["prepare_voice", "cancel_prepare_voice"]
    assert sent[0][1]["prepare_id"] == sent[1][1]["prepare_id"]


class Response:
    def __init__(self, chunks, *, status=200, headers=None):
        self.chunks = deque(chunks)
        self.status, self.headers, self.reason = status, headers or {}, "ok"
        self.content = self
    async def read(self, size):
        return self.chunks.popleft() if self.chunks else b""
    async def __aenter__(self):
        return self
    async def __aexit__(self, *args):
        pass


@pytest.mark.asyncio
async def test_html_pool_reads_all_chunks_caches_and_revalidates(monkeypatch):
    from cogs.musica.metadados.provedores_api import MusicApiProviders
    api = MusicApiProviders()
    calls = []
    replies = deque([Response([b"<html>", b"playlist", b"</html>"], headers={"ETag": "revision1"}), Response([], status=304)])
    def request(url, **kwargs):
        calls.append((url, kwargs))
        return replies.popleft()
    monkeypatch.setattr(api, "_http_session_persistente", AsyncMock(return_value=SimpleNamespace(get=request)))
    url = "https://open.spotify.com/embed/playlist/abcdefghijk"
    assert await api._to_thread_text(url) == "<html>playlist</html>"
    assert await api._to_thread_text(url) == "<html>playlist</html>"
    assert len(calls) == 1
    key, entry = next(iter(api._spotify_public_html_cache.items()))
    api._spotify_public_html_cache[key] = (0.0, *entry[1:])
    assert await api._to_thread_text(url) == "<html>playlist</html>"
    assert calls[1][1]["headers"]["If-None-Match"] == "revision1"


@pytest.mark.asyncio
async def test_json_pool_reads_split_response(monkeypatch):
    from cogs.musica.metadados.provedores_api import MusicApiProviders
    api = MusicApiProviders()
    response = Response([b'{"tracks":', b'[1, 2]', b'}'])
    session = SimpleNamespace(request=lambda *args, **kwargs: response)
    monkeypatch.setattr(api, "_http_session_persistente", AsyncMock(return_value=session))
    assert await api._to_thread_json("https://example.test") == {"tracks": [1, 2]}


@pytest.mark.asyncio
async def test_html_response_budget_rejects_oversized_without_cache(monkeypatch):
    from cogs.musica.metadados.provedores_api import MusicApiProviders
    api = MusicApiProviders()
    session = SimpleNamespace(get=lambda *args, **kwargs: Response([b"1234", b"5678"]))
    monkeypatch.setattr(api, "_http_session_persistente", AsyncMock(return_value=session))
    with pytest.raises(ValueError, match="orçamento"):
        await api._to_thread_text("https://open.spotify.com/playlist/abc", max_bytes=7)
    assert not api._spotify_public_html_cache
