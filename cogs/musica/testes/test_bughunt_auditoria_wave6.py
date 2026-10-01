from __future__ import annotations

import builtins
from pathlib import Path

import pytest

from cogs.musica.runtime_telefone.ponte_worker import resolucao as worker_resolucao


@pytest.mark.parametrize(
    ("url", "provider"),
    [
        ("https://open.spotify.com/track/abc123?si=secret", "spotify"),
        ("https://www.deezer.com/track/123456", "deezer"),
        ("https://music.apple.com/br/album/x/123?i=456", "apple"),
    ],
)
def test_worker_bloqueia_provider_metadata_antes_de_importar_ytdlp(monkeypatch, capsys, url: str, provider: str) -> None:
    real_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name == "yt_dlp":
            raise AssertionError("yt-dlp não deve ser importado para URL metadata-only")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    result = worker_resolucao.resolve_ytdlp({"query": url}, job_timeout=20)
    assert result["ok"] is False
    assert result["error"] == "metadata_provider_url_blocked"
    assert result["provider"] == provider
    assert result["blocked_before_ytdlp"] is True
    audit = capsys.readouterr().out
    assert f"provider={provider}" in audit
    assert "?si=secret" not in audit


def test_worker_only_legacy_prefere_query_interna_a_url_spotify() -> None:
    root = Path(__file__).resolve().parents[1]
    source = (root / "legado" / "roteador_audio.py").read_text(encoding="utf-8")
    needle = 'source = str(getattr(track, "query", "") or getattr(track, "webpage_url", "") or getattr(track, "original_url", "")'
    assert needle in source


def test_seek_music_agent_prefere_query_estavel_a_url_publica() -> None:
    root = Path(__file__).resolve().parents[1]
    source = (root / "runtime_telefone" / "agente" / "reproducao.py").read_text(encoding="utf-8")
    assert "track.query or track.webpage_url or track.title" in source


def test_music_agent_audit_remove_tracking_sem_alterar_parametros_funcionais(capsys) -> None:
    from cogs.musica.runtime_telefone.agente.servidor import MusicAgent, _audit_value

    spotify = "https://open.spotify.com/track/abc?si=secret&utm_source=copy-link"
    assert _audit_value("query", spotify) == "https://open.spotify.com/track/abc"
    youtube = "https://www.youtube.com/watch?v=abc123&list=PL1&feature=share"
    assert _audit_value("query", youtube) == "https://www.youtube.com/watch?v=abc123&list=PL1"
    signed = "https://media.invalid/a?token=private&x=1"
    assert "private" not in _audit_value("url", signed)
    assert "x=1" in _audit_value("url", signed)

    # Exercita o destino real do log sem inicializar o cliente Discord. A
    # sanitização deve ocorrer na saída, preservando o payload do comando.
    fields = {"query": spotify, "webpage_url": youtube, "url": signed}
    MusicAgent.log(None, "resolve", guild_id=123, **fields)
    audit = capsys.readouterr().out
    assert "resolve guild=123" in audit
    assert "secret" not in audit and "private" not in audit
    assert "utm_source" not in audit and "feature=share" not in audit
    assert "v=abc123&list=PL1" in audit and "x=1" in audit
    assert fields == {"query": spotify, "webpage_url": youtube, "url": signed}


def test_metadata_provider_block_response_remove_query_tracking(monkeypatch) -> None:
    worker_resolucao._METADATA_PROVIDER_AUDIT.clear()
    result = worker_resolucao.resolve_ytdlp(
        {"query": "https://open.spotify.com/track/abc?si=secret&utm_source=share"},
        job_timeout=20,
    )
    assert result["query"] == "https://open.spotify.com/track/abc"
