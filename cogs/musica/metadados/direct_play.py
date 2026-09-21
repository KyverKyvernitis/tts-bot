from __future__ import annotations


def consulta_metadata_direct_play(*, titulo: str, artista: str = "", fallback: str = "") -> str:
    """Monta a busca interna de uma faixa cuja identidade já é conhecida.

    Links Spotify/Deezer/Apple são direct play por metadata: a plataforma informa
    título/artista e o Phone Worker encontra uma única fonte tocável equivalente.
    O prefixo explícito evita herdar um ``ytsearch3`` (ou maior) do ambiente e,
    portanto, evita resolver candidatos que nunca serão mostrados ao usuário.
    """
    raw_fallback = str(fallback or "").strip()
    if raw_fallback.lower().startswith(("ytsearch", "ytmsearch")):
        return raw_fallback

    base = str(titulo or raw_fallback or "").strip()
    autor = str(artista or "").strip()
    if autor and autor.lower() not in base.lower():
        base = f"{autor} - {base}".strip(" -")
    if base and "official" not in base.lower():
        base = f"{base} official audio"
    return f"ytsearch1:{base}" if base else raw_fallback
