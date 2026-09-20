from __future__ import annotations

from dataclasses import dataclass
import re

from .normalizacao import limpar_apresentacao

_SEPARADOR_ARTISTA_TITULO = re.compile(r"\s+(?:-|–|—|\||:)\s+")
_POR_ARTISTA = re.compile(r"^(.+)\s+(?:by|por)\s+(.+)$", re.IGNORECASE)
_FEAT = re.compile(r"\s+\b(?:feat(?:uring)?|ft)\.?\s+", re.IGNORECASE)
_ASPAS = re.compile(r'["“”]([^"“”]{1,180})["“”]')
_LIMPAR_BORDAS = re.compile(r"^(?:-|–|—|\||:|by\b|por\b)+|(?:-|–|—|\||:)+$", re.IGNORECASE)
_URL_PREFIXOS = ("http://", "https://", "www.")


@dataclass(frozen=True, slots=True)
class EstruturaConsulta:
    artista: str = ""
    titulo: str = ""
    colaboradores: tuple[str, ...] = ()
    alvo_atributos: str = ""
    fonte: str = "livre"
    confianca: float = 0.0

    @property
    def estruturada(self) -> bool:
        return bool(self.artista and self.titulo)


def _limpar_bordas(value: str) -> str:
    text = re.sub(r"\s+", " ", (value or "").strip())
    text = _LIMPAR_BORDAS.sub("", text).strip()
    return text


def _separar_feat(value: str) -> tuple[str, tuple[str, ...]]:
    text = _limpar_bordas(value)
    partes = _FEAT.split(text, maxsplit=1)
    if len(partes) != 2:
        return text, ()
    base, colaborador = (_limpar_bordas(parte) for parte in partes)
    if not base or not colaborador:
        return text, ()
    return base, (limpar_apresentacao(colaborador),)


def _montar(artista_raw: str, titulo_raw: str, *, fonte: str, confianca: float) -> EstruturaConsulta:
    artista_raw = _limpar_bordas(artista_raw)
    titulo_raw = _limpar_bordas(titulo_raw)
    if not artista_raw or not titulo_raw:
        return EstruturaConsulta()
    if artista_raw.lower().startswith(_URL_PREFIXOS) or titulo_raw.lower().startswith(_URL_PREFIXOS):
        return EstruturaConsulta()

    artista_base, feat_artista = _separar_feat(artista_raw)
    titulo_base, feat_titulo = _separar_feat(titulo_raw)
    colaboradores = tuple(item for item in (*feat_artista, *feat_titulo) if item)
    artista = limpar_apresentacao(artista_base)
    titulo = limpar_apresentacao(titulo_base)
    if not artista or not titulo:
        return EstruturaConsulta()
    return EstruturaConsulta(
        artista=artista,
        titulo=titulo,
        colaboradores=colaboradores,
        alvo_atributos=titulo_raw,
        fonte=fonte,
        confianca=confianca,
    )


def _por_artista(raw: str) -> EstruturaConsulta:
    match = _POR_ARTISTA.match(raw)
    if not match:
        return EstruturaConsulta()
    titulo_raw, artista_raw = match.groups()
    # Evita converter expressões muito curtas como "stand by" em identidade.
    if len(titulo_raw.strip()) < 2 or len(artista_raw.strip()) < 2:
        return EstruturaConsulta()
    return _montar(artista_raw, titulo_raw, fonte="by", confianca=0.92)


def _com_aspas(raw: str) -> EstruturaConsulta:
    match = _ASPAS.search(raw)
    if not match:
        return EstruturaConsulta()
    titulo_raw = match.group(1).strip()
    antes = _limpar_bordas(raw[: match.start()])
    depois = _limpar_bordas(raw[match.end() :])

    # O trecho fora das aspas é tratado como artista. Se houver conteúdo dos
    # dois lados, preferimos o lado anterior (forma "Artista \"Música\"").
    artista_raw = antes or depois
    if not artista_raw:
        return EstruturaConsulta()
    return _montar(artista_raw, titulo_raw, fonte="aspas", confianca=0.95)


def analisar_estrutura(raw: str) -> EstruturaConsulta:
    text = re.sub(r"\s+", " ", (raw or "").strip())
    if not text or text.lower().startswith(_URL_PREFIXOS):
        return EstruturaConsulta(alvo_atributos=text)

    partes = _SEPARADOR_ARTISTA_TITULO.split(text, maxsplit=1)
    if len(partes) == 2:
        estrutura = _montar(partes[0], partes[1], fonte="separador", confianca=1.0)
        if estrutura.estruturada:
            return estrutura

    estrutura = _por_artista(text)
    if estrutura.estruturada:
        return estrutura

    estrutura = _com_aspas(text)
    if estrutura.estruturada:
        return estrutura

    return EstruturaConsulta(alvo_atributos=text)
