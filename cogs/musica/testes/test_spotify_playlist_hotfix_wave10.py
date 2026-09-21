from __future__ import annotations

from pathlib import Path

from cogs.musica.agente_telefone.conversao import faixa_do_payload
from cogs.musica.metadados.fontes.spotify_publico_parser import parse_embed_document
from cogs.musica.runtime_telefone.agente.resolucao import ResolucaoMixin


SPOTIFY_PLAYLIST = "https://open.spotify.com/playlist/4HQZyAM6swVZZZn308z4v8?si=wave10"


def _spotify_meta(title: str, artist: str, *, duration: float = 240.0) -> dict:
    return {
        "title": f"{artist} - {title}",
        "display_title": f"{artist} - {title}",
        "uploader": artist,
        "display_uploader": artist,
        "source": "Spotify público",
        "display_source": "Spotify público",
        "extractor": "metadata",
        "original_url": SPOTIFY_PLAYLIST,
        "webpage_url": SPOTIFY_PLAYLIST,
        "duration": duration,
    }


def test_cache_do_agent_distingue_faixas_da_mesma_playlist_spotify() -> None:
    resolver = object.__new__(ResolucaoMixin)
    juliet = _spotify_meta("Juliet", "Cavetown", duration=278)
    home = _spotify_meta("Home", "Cavetown", duration=269)

    key_juliet = resolver._resolve_cache_key("ytsearch1:Cavetown - Juliet official audio", juliet)
    key_home = resolver._resolve_cache_key("ytsearch1:Cavetown - Home official audio", home)

    assert key_juliet != key_home
    assert SPOTIFY_PLAYLIST.lower() not in key_juliet
    assert SPOTIFY_PLAYLIST.lower() not in key_home


def test_cache_do_agent_reaproveita_repeticao_legitima_da_mesma_faixa() -> None:
    resolver = object.__new__(ResolucaoMixin)
    meta = _spotify_meta("Juliet", "Cavetown", duration=278)
    query = "ytsearch1:Cavetown - Juliet official audio"

    assert resolver._resolve_cache_key(query, meta) == resolver._resolve_cache_key(query, dict(meta))


def test_parser_do_embed_remove_selo_explicit_do_nome_do_artista() -> None:
    html = """
    <html><body>
      <h1>Cavetown playlist &lt;3</h1>
      <h3>Juliet</h3>
      <h4><span>E</span><span>Cavetown</span></h4>
      <span>4:38</span>
    </body></html>
    """
    document = parse_embed_document(html, limit=5)

    assert len(document.rows) == 1
    assert document.rows[0].title == "Juliet"
    assert document.rows[0].artist == "Cavetown"


def test_faixa_spotify_resolvida_expoe_youtube_como_fonte_e_spotify_como_origem() -> None:
    resolver = object.__new__(ResolucaoMixin)
    meta = _spotify_meta("Juliet", "Cavetown", duration=278)
    meta["thumbnail"] = "https://spotify.example/playlist-cover.jpg"
    resolved = {
        "title": "Cavetown - Juliet",
        "uploader": "Cavetown",
        "duration": 278.0,
        "thumbnail": "https://youtube.example/video-thumb.jpg",
        "webpage_url": "https://www.youtube.com/watch?v=abc",
        "stream_url": "https://stream.example/audio",
        "audio_codec": "opus",
        "audio_abr": 140,
    }

    agent_track = resolver._agent_track_from_resolved(
        resolved,
        query="ytsearch1:Cavetown - Juliet official audio",
        track_meta=meta,
        body={"requester_id": 1, "requester_name": "Core"},
    )

    assert agent_track.source == "YouTube"
    assert agent_track.thumbnail == "https://youtube.example/video-thumb.jpg"

    public = agent_track.public()
    public["original_url"] = SPOTIFY_PLAYLIST
    public["webpage_url"] = SPOTIFY_PLAYLIST
    converted = faixa_do_payload(public)
    assert converted is not None
    assert converted.display_source == "YouTube"
    assert converted.fallback_reason == "Spotify"


def test_interface_prioriza_fonte_real_e_nao_expoe_detalhes_de_implementacao() -> None:
    root = Path(__file__).resolve().parents[1]
    components = (root / "interface" / "componentes.py").read_text(encoding="utf-8")

    assert 'for attr in ("display_source", "source", "extractor")' in components
    assert 'requester_line = f"-# Pedido por {requester}" + (f" · via' in components
    assert 'total_text = f"{total}+" if virtual else str(total)' in components
    assert 'lines.append(f"-# + {hidden} música' in components
    lower = components.lower()
    assert "restante da playlist carregado automaticamente conforme necessário" not in lower
    assert "duração carregada" not in lower
    assert "já carregadas" not in lower


def test_respostas_de_playlist_nao_expoem_janela_memoria_ou_preparando_stale() -> None:
    root = Path(__file__).resolve().parents[1]
    tocar = (root / "comandos" / "tocar.py").read_text(encoding="utf-8")
    components = (root / "interface" / "componentes.py").read_text(encoding="utf-8")

    for text in (tocar, components):
        assert "janela leve em memória" not in text.lower()
        assert "restante será carregado sob demanda" not in text.lower()
    virtual_block = tocar[tocar.index("if virtual_active:", tocar.index("if is_multi:")):]
    virtual_block = virtual_block[: virtual_block.index("else:", virtual_block.index("if virtual_active:")) + 5]
    assert "Preparando a primeira faixa" not in virtual_block
