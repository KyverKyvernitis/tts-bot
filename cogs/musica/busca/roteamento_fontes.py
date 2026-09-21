from __future__ import annotations

from dataclasses import dataclass

from .intencao import analisar_consulta
from .normalizacao import texto_basico


@dataclass(frozen=True, slots=True)
class PlanoFontes:
    prioridades: tuple[str, ...]
    max_fontes: int
    motivo: str

    @property
    def assinatura(self) -> str:
        fontes = ",".join(self.prioridades)
        return f"{self.motivo}:{self.max_fontes}:{fontes}"


_VARIANTES_YOUTUBE_FIRST = frozenset({
    "cover",
    "karaoke",
    "instrumental",
    "slowed",
    "sped_up",
    "reverb",
    "extended",
    "edit",
})
_VARIANTES_CATALOGO = frozenset({"live", "acoustic", "remaster", "clean", "explicit"})
_HINTS = ("youtube", "spotify", "deezer", "soundcloud")


def _hint_explicito(query: str) -> str:
    normalizado = texto_basico(query)
    tokens = tuple(normalizado.split())
    if not tokens:
        return ""
    for nome in _HINTS:
        if nome in tokens:
            return nome
    return ""


def planejar_fontes(
    query: str,
    *,
    profundo: bool = False,
    incluir_youtube: bool = True,
) -> PlanoFontes:
    """Escolhe apenas as fontes que podem melhorar a consulta atual.

    O plano expressa prioridade lógica; disponibilidade real de credenciais fica
    no provider. ``max_fontes`` é aplicado depois de remover fontes indisponíveis,
    para que a ausência de Spotify, por exemplo, permita usar Deezer como fallback.
    """
    consulta = analisar_consulta(query)
    hint = _hint_explicito(consulta.raw or query)

    if hint:
        prioridades = (hint, "youtube", "spotify", "deezer", "soundcloud")
        # Remove duplicatas preservando ordem.
        prioridades = tuple(dict.fromkeys(prioridades))
        max_fontes = 2 if profundo else 1
        motivo = f"hint_{hint}"
    elif consulta.apresentacao:
        # Video/lyrics/visualizer/official são sinais nativos do ecossistema de
        # vídeo. Catálogos entram só no deep pass para confirmar identidade.
        prioridades = ("youtube", "spotify", "deezer")
        max_fontes = 2 if profundo else 1
        motivo = "apresentacao"
    elif consulta.atributos & _VARIANTES_YOUTUBE_FIRST:
        prioridades = ("youtube", "soundcloud", "spotify", "deezer")
        max_fontes = 2 if profundo else 1
        motivo = "variante_web"
    elif "remix" in consulta.atributos:
        prioridades = ("youtube", "soundcloud", "spotify", "deezer")
        max_fontes = 3 if profundo else 2
        motivo = "remix"
    elif consulta.atributos & _VARIANTES_CATALOGO:
        prioridades = ("youtube", "spotify", "deezer")
        max_fontes = 3 if profundo else 2
        motivo = "variante_catalogo"
    elif consulta.artista and consulta.titulo:
        # Consulta estruturada já carrega identidade forte. Um único catálogo
        # auxiliar basta no fast pass quando a API-first do YouTube já foi tentada.
        prioridades = ("youtube", "spotify", "deezer")
        max_fontes = 3 if profundo else 2
        motivo = "estruturada"
    else:
        # Texto livre é mais ambíguo; mantemos dois sinais externos no fast pass.
        prioridades = ("youtube", "spotify", "deezer")
        max_fontes = 3 if profundo else 2
        motivo = "livre"

    if not incluir_youtube:
        prioridades = tuple(nome for nome in prioridades if nome != "youtube")
        # Se o YouTube já está coberto por API-first/worker, não substituímos essa
        # vaga automaticamente por catálogos que não conseguem representar uma
        # apresentação como lyric/video/visualizer. No deep pass eles voltam como
        # evidência de identidade.
        if motivo == "apresentacao":
            max_fontes = 2 if profundo else 0
        elif motivo in {"variante_web", "estruturada"}:
            max_fontes = min(max_fontes, 1 if not profundo else 2)

    return PlanoFontes(
        prioridades=prioridades,
        max_fontes=max(0, int(max_fontes)),
        motivo=motivo,
    )
