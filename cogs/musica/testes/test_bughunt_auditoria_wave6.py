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


def test_music_agent_audit_remove_tracking_sem_alterar_parametros_funcionais() -> None:
    import importlib.util
    import sys
    import types

    # servidor importa discord/aiohttp; os testes de unidade só precisam da função pura.
    source = (Path(__file__).resolve().parents[1] / "runtime_telefone" / "agente" / "servidor.py").read_text(encoding="utf-8")
    assert 'AGENT_VERSION = "0.3.56"' in source
    assert '_audit_value(key, value)' in source

    # Exercita a mesma política por AST/exec sem inicializar o Discord client.
    import ast
    tree = ast.parse(source)
    wanted = {"_audit_value"}
    nodes = [node for node in tree.body if isinstance(node, (ast.Assign, ast.AnnAssign, ast.FunctionDef))]
    selected = []
    for node in nodes:
        if isinstance(node, ast.FunctionDef) and node.name == "_audit_value":
            selected.append(node)
        elif isinstance(node, ast.Assign) and any(getattr(t, "id", "") in {"_AUDIT_URL_FIELDS", "_AUDIT_DROP_QUERY_KEYS"} for t in node.targets):
            selected.append(node)
        elif isinstance(node, ast.AnnAssign) and getattr(node.target, "id", "") in {"_AUDIT_URL_FIELDS", "_AUDIT_DROP_QUERY_KEYS"}:
            selected.append(node)
    module = ast.Module(body=selected, type_ignores=[])
    ns = {"Any": object, "urllib": __import__("urllib")}
    # __import__('urllib') does not eagerly expose parse in every interpreter.
    import urllib.parse
    ns["urllib"] = urllib
    exec(compile(module, "<audit>", "exec"), ns)
    audit_value = ns["_audit_value"]

    spotify = "https://open.spotify.com/track/abc?si=secret&utm_source=copy-link"
    assert audit_value("query", spotify) == "https://open.spotify.com/track/abc"
    youtube = "https://www.youtube.com/watch?v=abc123&list=PL1&feature=share"
    assert audit_value("query", youtube) == "https://www.youtube.com/watch?v=abc123&list=PL1"
    signed = "https://media.invalid/a?token=private&x=1"
    assert "private" not in audit_value("url", signed)
    assert "x=1" in audit_value("url", signed)


def test_metadata_provider_block_response_remove_query_tracking(monkeypatch) -> None:
    worker_resolucao._METADATA_PROVIDER_AUDIT.clear()
    result = worker_resolucao.resolve_ytdlp(
        {"query": "https://open.spotify.com/track/abc?si=secret&utm_source=share"},
        job_timeout=20,
    )
    assert result["query"] == "https://open.spotify.com/track/abc"
