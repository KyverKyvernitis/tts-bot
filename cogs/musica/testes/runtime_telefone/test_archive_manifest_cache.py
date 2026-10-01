from __future__ import annotations

import copy
import json
import shutil
import ssl
import subprocess
import time
from types import SimpleNamespace

import aiohttp
from aiohttp import web
import pytest

from cogs.musica.runtime_telefone.agente.archive_manifest import (
    ArchiveManifestCache, encode_manifest, hydrate_archive_manifest,
)
from cogs.musica.runtime_telefone.agente.resolucao import ResolucaoMixin

KEY = "d" * 32
REF = {"guild_id": 1, "forum_id": 20, "channel_id": 40, "message_id": 30,
       "attachment_id": 9, "manifest_attachment_id": 19}


def archive_fixture():
    technical = {"audio_codec": "opus", "audio_sample_rate": 48000, "audio_channels": 2,
                 "audio_stream_index": 0, "audio_abr": 160}
    manifest = {"version": 1, "archive_key": KEY, "duration": 20, "sha256": "a" * 64,
                **technical, "segments": [{"order": 0, "duration": 20, "offset_seconds": 0,
                    "filename": "part0.ogg", "size_bytes": 123, "sha256": "b" * 64, **technical,
                    "reference": {name: REF[name] for name in
                                  ("guild_id", "forum_id", "channel_id", "message_id", "attachment_id")}}]}
    expiry = format(int(time.time() + 600), "x")
    raw = {"id": "30", "author": {"id": "5"}, "guild_id": "1", "channel_id": "40",
           "embeds": [{"url": f"https://youtu.be/test#music-archive-v8-{KEY}",
                       "footer": {"text": "Arquivo de músicas"},
                       "description": "🎵 YouTube • 0:20\nOPUS · OGG · 48 kHz • ≈160 kbps"}],
           "attachments": [{"id": "9", "filename": "part0.ogg", "size": 123,
                            "url": f"https://cdn.discordapp.com/attachments/40/9/part0.ogg?ex={expiry}&hm=audio"},
                           {"id": "19", "filename": "archive-manifest.json", "size": len(encode_manifest(manifest)),
                            "url": f"https://cdn.discordapp.com/attachments/40/19/archive-manifest.json?ex={expiry}&hm=manifest"}]}
    return raw, manifest


class FakeSession:
    closed = False

    def __init__(self, manifest):
        self.payload = encode_manifest(manifest)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        owner = self

        class Response:
            status = 200

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            @property
            def content(self):
                return self

            async def iter_chunked(self, size):
                yield owner.payload

        return Response()

    async def close(self):
        self.closed = True


@pytest.mark.asyncio
async def test_signed_url_refresh_reuses_validated_manifest_without_cdn_download():
    original, manifest = archive_fixture()
    session, cache = FakeSession(manifest), ArchiveManifestCache()
    first = await hydrate_archive_manifest(copy.deepcopy(original), session=session, metadata_cache=cache,
        archive_key=KEY, reference=REF, bot_id=5)
    refreshed = copy.deepcopy(original)
    for attachment in refreshed["attachments"]:
        attachment["url"] = attachment["url"].replace("hm=", "hm=renewed-")
    second = await hydrate_archive_manifest(refreshed, session=session, metadata_cache=cache,
        archive_key=KEY, reference=REF, bot_id=5)
    assert first["_archive_manifest"] == second["_archive_manifest"] == manifest
    assert len(session.calls) == 1
    second["_archive_manifest"]["segments"][0]["filename"] = "mutated.ogg"
    assert cache.get(ArchiveManifestCache.revision(original, archive_key=KEY, reference=REF, bot_id=5))["segments"][0]["filename"] == "part0.ogg"
    assert "cdn.discordapp.com" not in repr(cache._entries)


@pytest.mark.asyncio
@pytest.mark.parametrize("mutate", [
    lambda raw: raw["author"].update(id="6"),
    lambda raw: raw.update(guild_id="2"),
    lambda raw: raw.update(channel_id="41"),
    lambda raw: raw.update(id="31"),
    lambda raw: raw["embeds"][0].update(url="https://youtu.be/test#music-archive-v8-" + "e" * 32),
    lambda raw: raw["attachments"][1].update(id="20"),
    lambda raw: raw["attachments"].pop(),
])
async def test_cached_manifest_still_rejects_deleted_or_foreign_discord_revision(mutate):
    raw, manifest = archive_fixture()
    session, cache = FakeSession(manifest), ArchiveManifestCache()
    await hydrate_archive_manifest(copy.deepcopy(raw), session=session, metadata_cache=cache,
        archive_key=KEY, reference=REF, bot_id=5)
    mutate(raw)
    with pytest.raises(ValueError):
        await hydrate_archive_manifest(raw, session=session, metadata_cache=cache,
            archive_key=KEY, reference=REF, bot_id=5)
    assert len(session.calls) == 1


@pytest.mark.asyncio
async def test_metadata_edit_invalidates_revision_and_cannot_reuse_changed_audio_attachment():
    original, manifest = archive_fixture()
    session, cache = FakeSession(manifest), ArchiveManifestCache()
    for edit in (None, "2026-10-01T12:00:00.000Z"):
        raw = copy.deepcopy(original)
        raw["edited_timestamp"] = edit
        await hydrate_archive_manifest(raw, session=session, metadata_cache=cache,
            archive_key=KEY, reference=REF, bot_id=5)
    assert len(session.calls) == 2
    raw = copy.deepcopy(original)
    raw["attachments"][0]["size"] = 124
    with pytest.raises(ValueError, match="primeiro anexo"):
        await hydrate_archive_manifest(raw, session=session, metadata_cache=cache,
            archive_key=KEY, reference=REF, bot_id=5)
    assert len(session.calls) == 3


def test_manifest_cache_has_bounded_ram_ttl_and_never_keeps_arbitrary_urls():
    _, manifest = archive_fixture()
    manifest["stream_url"] = "https://cdn.discordapp.com/attachments/private"
    manifest["segments"][0]["url"] = manifest["stream_url"]
    cache = ArchiveManifestCache(max_entries=1)
    cache.put(("first",), manifest)
    assert "url" not in repr(cache.get(("first",)))
    cache.put(("second",), manifest)
    assert cache.get(("first",)) is None and cache.get(("second",)) is not None
    cache.ttl_seconds = 0
    assert cache.get(("second",)) is None and cache._bytes == 0
    cache = ArchiveManifestCache(max_bytes=10)
    cache.put(("large",), manifest)
    assert not cache._entries


@pytest.mark.asyncio
async def test_resolver_pool_is_owned_reused_and_closed_without_touching_inline_posts(monkeypatch):
    raw, manifest = archive_fixture()
    session, created, connectors = FakeSession(manifest), [], []
    original_factory = aiohttp.ClientSession

    def make_session(**kwargs):
        created.append(kwargs)
        # The lifecycle test replaces only the session. Dispose of the real
        # connector explicitly once this test finishes.
        connectors.append(kwargs["connector"])
        return session

    monkeypatch.setattr(aiohttp, "ClientSession", make_session)
    agent = ResolucaoMixin()
    agent.client = SimpleNamespace(user=SimpleNamespace(id=5))
    try:
        for _ in range(2):
            await agent._hydrate_archive_manifest(copy.deepcopy(raw), key=KEY, reference=REF)
        assert len(created) == 1 and len(session.calls) == 1
        inline = copy.deepcopy(raw)
        inline["_archive_manifest"] = manifest
        await agent._hydrate_archive_manifest(inline, key=KEY, reference=REF)
        assert len(created) == 1
    finally:
        await agent._close_archive_manifest_http()
        for connector in connectors:
            await connector.close()
    assert session.closed and agent._archive_manifest_http is None
    monkeypatch.setattr(aiohttp, "ClientSession", original_factory)


@pytest.mark.asyncio
@pytest.mark.skipif(shutil.which("openssl") is None, reason="OpenSSL indisponível")
async def test_real_https_pool_reuses_connection_for_distinct_manifest_revisions(tmp_path):
    cert, key = tmp_path / "cert.pem", tmp_path / "key.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
        "-subj", "/CN=cdn.discordapp.com", "-keyout", str(key), "-out", str(cert)],
        check=True, capture_output=True)
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(cert, key)
    raw, manifest = archive_fixture()
    requests, transports = [], set()

    async def serve(request):
        requests.append(request.path)
        transports.add(request.transport)
        return web.Response(body=encode_manifest(manifest), content_type="application/json")

    app = web.Application()
    app.router.add_get("/attachments/40/19/archive-manifest.json", serve)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0, ssl_context=tls)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]

    class LocalResolver(aiohttp.abc.AbstractResolver):
        async def resolve(self, host, port_argument=0, family=0):
            return [{"hostname": host, "host": "127.0.0.1", "port": port,
                     "family": family, "proto": 0, "flags": 0}]

        async def close(self):
            pass

    try:
        async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(resolver=LocalResolver(), ssl=False)) as session:
            cache = ArchiveManifestCache()
            for edited in (None, None, "2026-10-01T12:00:00.000Z"):
                message = copy.deepcopy(raw)
                message["edited_timestamp"] = edited
                await hydrate_archive_manifest(message, session=session, metadata_cache=cache,
                    archive_key=KEY, reference=REF, bot_id=5)
        assert len(requests) == 2  # One cache hit makes no request.
        assert len(transports) == 1  # Both actual HTTPS reads reuse one socket.
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["author_identity", "audio_path", "manifest_attachment"])
async def test_resolver_does_not_reuse_url_cache_after_identity_change_or_failed_refresh(change):
    from cogs.musica.runtime_telefone.agente.estado import AgentTrack
    raw, manifest = archive_fixture()
    raw["_archive_manifest"] = manifest
    calls = []

    async def get_message(channel, message):
        calls.append((channel, message))
        return copy.deepcopy(raw)

    class Agent(ResolucaoMixin):
        def _agent_track_from_metadata(self, metadata, *, body):
            return AgentTrack(title="Teste")

    agent = Agent()
    agent.client = SimpleNamespace(user=SimpleNamespace(id=5), http=SimpleNamespace(get_message=get_message))
    metadata = {"archive_key": KEY, "archive_ref": REF}
    first = await agent._resolve_archive_attachment(track_meta=metadata, body={"guild_id": 1})
    assert first.archive_segments and len(calls) == 1
    if change == "author_identity":
        agent.client.user.id = 6
    elif change == "audio_path":
        raw["attachments"][0]["url"] = raw["attachments"][0]["url"].replace("/40/9/", "/40/99/")
    else:
        raw["attachments"][1]["id"] = "20"
    with pytest.raises(ValueError):
        await agent._resolve_archive_attachment(track_meta=metadata, body={"guild_id": 1},
                                               force_refresh=change != "author_identity")
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_segment_cache_is_invalidated_when_manifest_filename_size_or_hash_changes():
    from cogs.musica.runtime_telefone.agente.estado import AgentTrack
    raw, manifest = archive_fixture()
    raw["_archive_manifest"] = manifest
    calls = []

    async def get_message(channel, message):
        calls.append((channel, message))
        return copy.deepcopy(raw)

    class Agent(ResolucaoMixin):
        def _agent_track_from_metadata(self, metadata, *, body):
            return AgentTrack(title="Teste")

    agent = Agent()
    agent.client = SimpleNamespace(user=SimpleNamespace(id=5), http=SimpleNamespace(get_message=get_message))
    track = await agent._resolve_archive_attachment(track_meta={"archive_key": KEY, "archive_ref": REF}, body={"guild_id": 1})
    await agent._resolve_archive_segment(track, 0)
    assert len(calls) == 1
    track.archive_segments[0]["size_bytes"] = 124
    with pytest.raises(ValueError, match="não corresponde"):
        await agent._resolve_archive_segment(track, 0)
    assert len(calls) == 2


def test_track_constructors_preserve_trace_without_importing_remote_monotonic_clock():
    agent = ResolucaoMixin()
    metadata = {"title": "Teste", "query": "Teste", "trace_id": "play-123",
                "controller_timing_ms": {"catalog_ms": 3.2, "invalid": float("inf")},
                "agent_timing_ms": {"remote_bad": -1, "resolve_ms": 2.4},
                "agent_received_monotonic": 9999999}
    body = {"trace_id": "body-fallback", "agent_received_monotonic": 8888888}
    lazy = agent._agent_track_from_metadata(metadata, body=body)
    resolved = agent._agent_track_from_resolved({"title": "Teste", "stream_url": "https://example.test/audio"},
        query="Teste", track_meta=metadata, body=body)
    for track in (lazy, resolved):
        assert track.trace_id == "play-123"
        assert track.controller_timing_ms == {"catalog_ms": 3.2}
        assert track.agent_timing_ms == {"resolve_ms": 2.4}
        assert track.agent_received_monotonic == 0
        assert "agent_received_monotonic" not in track.public()
