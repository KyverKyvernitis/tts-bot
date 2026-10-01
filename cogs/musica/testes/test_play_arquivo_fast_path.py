"""A faixa já arquivada dispensa o provedor, inclusive em links Spotify."""
from types import SimpleNamespace
import asyncio
from unittest.mock import AsyncMock

import pytest


@pytest.fixture
def catalogo(tmp_path, monkeypatch):
    from cogs.musica.busca import arquivo

    monkeypatch.setattr(arquivo, "_db_path", lambda: tmp_path / "escolhas.sqlite3")
    arquivo.set_channel(1, 20, kind="forum")
    return arquivo


def _track():
    from cogs.musica.nucleo.modelos import MusicTrack

    return MusicTrack(
        title="Faixa conhecida", uploader="Artista", duration=240,
        webpage_url="https://www.youtube.com/watch?v=abcdefghijk",
        original_url="https://open.spotify.com/track/knowntrack",
        requester_id=7, source="YouTube", extractor="worker-ytdlp",
    )


def _publicar(arquivo):
    track = _track()
    arquivo.record_play(track, "play-1")
    ref = {"guild_id": 1, "forum_id": 20, "channel_id": 40, "message_id": 30, "attachment_id": 9}
    arquivo.mark_result(arquivo.media_key(track), {
        "status": "done", "reference": ref, "presentation": 7, "emoji": "🎵",
    })
    return ref


def _flow(monkeypatch):
    from cogs.musica.comandos import tocar

    class Flow(tocar.FluxoTocar):
        async def _voice_channel_from_ctx(self, ctx):
            return SimpleNamespace(id=50)

        async def _reply(self, *args, **kwargs):
            pytest.fail("O teste deve terminar após enviar o comando ao agente")

    flow = Flow()
    flow.router = SimpleNamespace(
        music_worker_only_enabled=lambda: True,
        current_music_operation_generation=lambda guild_id: 0,
        ensure_music_worker_available=AsyncMock(return_value=SimpleNamespace(available=True)),
        extractor=SimpleNamespace(extract=AsyncMock(side_effect=AssertionError("provedor acessado"))),
    )
    monkeypatch.setattr(tocar.config, "MUSIC_AGENT_ENABLED", True)
    sent = []

    async def command(action, **kwargs):
        sent.append((action, kwargs))
        return {"ok": True, "cancelled": True}

    monkeypatch.setattr(tocar, "music_agent_command", command)
    ctx = SimpleNamespace(
        guild=SimpleNamespace(id=1), channel=SimpleNamespace(id=60), message=None,
        author=SimpleNamespace(id=8, display_name="Pessoa"),
    )
    return flow, ctx, sent


@pytest.mark.asyncio
async def test_link_arquivado_evitar_provider_e_stream_temporario(catalogo, monkeypatch):
    ref = _publicar(catalogo)
    flow, ctx, sent = _flow(monkeypatch)
    await flow._run_play(ctx, "https://open.spotify.com/intl-pt/track/knowntrack?si=tracking")
    await asyncio.gather(*flow.router._music_fast_start_tasks)
    plays = [kwargs for action, kwargs in sent if action == "play"]
    assert len(plays) == 1
    track = plays[0]["track"]
    assert track["title"] == "Faixa conhecida"
    assert track["requester_id"] == 8 and track["requester_name"] == "Pessoa"
    assert track["archive_ref"] == ref and not track["stream_url"]
    assert plays[0]["trace_id"] and plays[0]["prepare_id"]
    assert plays[0]["controller_timing_ms"]["command_to_dispatch_ms"] >= 0
    flow.router.extractor.extract.assert_not_awaited()


@pytest.mark.asyncio
async def test_link_arquivado_respeita_guild_configurada(catalogo):
    _publicar(catalogo)
    from cogs.musica.comandos.tocar import FluxoTocar

    assert await FluxoTocar()._archived_url_batch(
        _track().original_url, guild_id=2, requester_id=8, requester_name="Outra",
    ) is None


@pytest.mark.asyncio
async def test_playlist_e_album_nunca_sao_reduzidos_a_faixa(catalogo, monkeypatch):
    from cogs.musica.comandos.tocar import FluxoTocar

    def forbidden(*args, **kwargs):
        pytest.fail("Coleções não devem acessar o atalho de uma faixa")

    monkeypatch.setattr(catalogo, "archived_track_for_url", forbidden)
    for query in (
        "https://open.spotify.com/playlist/knownplaylist",
        "https://open.spotify.com/album/knownalbum",
        "https://www.youtube.com/playlist?list=PLknown",
    ):
        assert await FluxoTocar()._archived_url_batch(
            query, guild_id=1, requester_id=8, requester_name="Pessoa",
        ) is None


@pytest.mark.asyncio
async def test_indice_indisponivel_continua_resolucao_normal(catalogo, monkeypatch):
    from cogs.musica.nucleo.modelos import ExtractedBatch

    def unavailable(*args, **kwargs):
        raise OSError("índice temporariamente indisponível")

    monkeypatch.setattr(catalogo, "archived_track_for_url", unavailable)
    flow, ctx, sent = _flow(monkeypatch)
    flow.router.extractor.extract = AsyncMock(return_value=ExtractedBatch(
        tracks=[_track()], query=_track().original_url,
    ))
    await flow._run_play(ctx, _track().original_url)
    await asyncio.gather(*flow.router._music_fast_start_tasks)
    flow.router.extractor.extract.assert_awaited_once()
    assert len([action for action, _ in sent if action == "play"]) == 1


def test_snapshot_preserva_referencia_manifesto_sem_url_assinada(monkeypatch):
    from cogs.musica.agente_telefone.conversao import faixa_do_payload

    ref = {"guild_id": 1, "forum_id": 20, "channel_id": 40, "message_id": 30,
           "attachment_id": 9, "manifest_attachment_id": 10}
    segments = [{"guild_id": 1, "channel_id": 40, "message_id": 30,
                 "attachment_id": 9, "offset": 0, "duration": 240}]
    restored = faixa_do_payload({"title": "Faixa", "archive_ref": ref, "archive_segments": segments})
    assert restored.archive_ref == ref and restored.archive_segments == segments
    assert not restored.stream_url
