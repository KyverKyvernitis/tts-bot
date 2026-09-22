from __future__ import annotations

import asyncio

from cogs.musica.runtime_telefone.agente.resolucao import ResolucaoMixin


PLAYLIST_A = "https://open.spotify.com/playlist/playlistA?si=abc"
PLAYLIST_B = "https://open.spotify.com/playlist/playlistB?si=def"


def _meta(title: str, *, playlist: str = PLAYLIST_A) -> dict:
    return {
        "title": f"Cavetown - {title}",
        "display_title": f"Cavetown - {title}",
        "uploader": "Cavetown",
        "display_uploader": "Cavetown",
        "source": "Spotify público",
        "display_source": "Spotify público",
        "extractor": "spotify",
        "webpage_url": "",
        "original_url": playlist,
        "duration": 240.0,
        "query": f"ytsearch1:Cavetown - {title} official audio",
    }


def _bare_resolver() -> ResolucaoMixin:
    resolver = object.__new__(ResolucaoMixin)
    resolver.metadata_cache_ttl = 21600.0
    resolver.stream_cache_ttl = 300.0
    resolver._metadata_cache = {}
    resolver._resolve_cache = {}
    return resolver


def test_cache_em_duas_camadas_separa_musica_logica_de_stream() -> None:
    resolver = _bare_resolver()
    home = _meta("Home")
    sweet = _meta("Sweet Tooth")
    home_logical = resolver._resolve_cache_key(home["query"], home)
    sweet_logical = resolver._resolve_cache_key(sweet["query"], sweet)
    assert home_logical != sweet_logical

    home_resolved = {
        "title": "Cavetown - Home",
        "webpage_url": "https://youtu.be/home123?si=tracking",
        "stream_url": "https://rr1.googlevideo.com/videoplayback?expire=1",
        "audio_format_id": "251",
    }
    sweet_resolved = {
        "title": "Cavetown - Sweet Tooth",
        "webpage_url": "https://www.youtube.com/watch?v=sweet456&feature=share",
        "stream_url": "https://rr2.googlevideo.com/videoplayback?expire=2",
        "audio_format_id": "251",
    }
    resolver._metadata_cache_put(home_logical, home_resolved)
    resolver._metadata_cache_put(sweet_logical, sweet_resolved)
    home_media = resolver._media_cache_key(home_resolved)
    sweet_media = resolver._media_cache_key(sweet_resolved)
    assert home_media != sweet_media
    assert home_media.startswith("media:v1:https://www.youtube.com/watch?v=home123")
    assert sweet_media.startswith("media:v1:https://www.youtube.com/watch?v=sweet456")

    resolver._resolve_cache_put(home_media, home_resolved)
    resolver._resolve_cache_put(sweet_media, sweet_resolved)
    assert resolver._cached_resolved_get(home_logical)["stream_url"] == home_resolved["stream_url"]
    assert resolver._cached_resolved_get(sweet_logical)["stream_url"] == sweet_resolved["stream_url"]


def test_mesma_musica_em_playlists_diferentes_reusa_identidade_logica() -> None:
    resolver = _bare_resolver()
    a = _meta("Home", playlist=PLAYLIST_A)
    b = _meta("Home", playlist=PLAYLIST_B)
    assert resolver._resolve_cache_key(a["query"], a) == resolver._resolve_cache_key(b["query"], b)


def test_stream_cache_rejeita_entrada_sem_fingerprint_wave13() -> None:
    resolver = _bare_resolver()
    key = "media:v1:https://www.youtube.com/watch?v=abc|fmt:251"
    resolver._resolve_cache[key] = (0.0, {"stream_url": "https://old.example/audio"})
    # timestamp 0 is also expired, but the important contract is that legacy
    # payloads never become valid hits merely because a key happens to match.
    assert resolver._resolve_cache_get(key) is None


def test_stream_expirado_reusa_video_resolvido_sem_nova_pesquisa_textual() -> None:
    # Source-level assertion keeps this regression test independent from Discord.
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    source = (root / "runtime_telefone" / "agente" / "resolucao.py").read_text(encoding="utf-8")
    assert "stable_target = self._metadata_media_target(cached_meta)" in source
    assert "resolve_target = stable_target" in source
    assert "return self._resolve_with_ytdlp(resolve_target)" in source
    assert 'reused_resolution=bool(resolve_target != query)' in source


def test_playlist_prefetch_imediato_nao_depende_de_desligar_cache() -> None:
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    playback = (root / "runtime_telefone" / "agente" / "reproducao.py").read_text(encoding="utf-8")
    assert "metadata_playlist_next = self._is_metadata_collection_item(meta)" in playback
    assert "if metadata_playlist_next:" in playback
    assert "delay = 0.0" in playback
    assert "self._cached_resolved_get(cache_key)" in playback


def test_stream_expirado_atualiza_por_video_id_sem_refazer_busca() -> None:
    import contextlib
    import threading

    class Dummy(ResolucaoMixin):
        def __init__(self) -> None:
            self.metadata_cache_ttl = 21600.0
            self.stream_cache_ttl = 300.0
            self._metadata_cache = {}
            self._resolve_cache = {}
            self._resolve_locks = {}
            self._resolve_lock_users = {}
            self.resolve_max_concurrency = 1
            self._resolve_active = 0
            self._resolve_waiters = []
            self._resolve_waiter_sequence = 0
            self._resolve_scheduler_lock = asyncio.Lock()
            self._resolve_thread_local = threading.local()
            self.calls: list[str] = []

        @contextlib.asynccontextmanager
        async def _registry_lock(self, *args, **kwargs):
            yield

        def log(self, *args, **kwargs):
            return None

        def _resolve_with_ytdlp(self, query: str) -> dict:
            self.calls.append(query)
            if query.startswith("https://www.youtube.com/watch?v=home123"):
                return {
                    "title": "Cavetown - Home",
                    "uploader": "Cavetown",
                    "webpage_url": "https://www.youtube.com/watch?v=home123",
                    "stream_url": "https://rr.example/home-refreshed",
                    "audio_format_id": "251",
                    "audio_codec": "opus",
                }
            return {
                "title": "Cavetown - Home",
                "uploader": "Cavetown",
                "webpage_url": "https://www.youtube.com/watch?v=home123",
                "stream_url": "https://rr.example/home-first",
                "audio_format_id": "251",
                "audio_codec": "opus",
            }

    async def scenario() -> None:
        agent = Dummy()
        meta = _meta("Home")
        query = meta["query"]
        first = await agent.resolve_track(query, track_meta=dict(meta), body={"guild_id": 1})
        assert first.stream_url.endswith("home-first")
        assert agent.calls == [query]

        # Hot hit: no yt-dlp call at all.
        second = await agent.resolve_track(query, track_meta=dict(meta), body={"guild_id": 1})
        assert second.stream_url.endswith("home-first")
        assert agent.calls == [query]

        # Simulate only the signed stream expiring. The stable logical->YouTube
        # mapping remains, so refresh goes directly to the video, not ytsearch1.
        agent._resolve_cache.clear()
        third = await agent.resolve_track(query, track_meta=dict(meta), body={"guild_id": 1})
        assert third.stream_url.endswith("home-refreshed")
        assert agent.calls == [query, "https://www.youtube.com/watch?v=home123"]

    asyncio.run(scenario())
