from __future__ import annotations

import re

from .modelos import ConsultaNormalizada
from .normalizacao import limpar_apresentacao, remover_prefixo_busca, tokens_texto

_ATRIBUTOS: dict[str, tuple[str, ...]] = {
    "live": ("live", "ao vivo"),
    "remix": ("remix", "remixed"),
    "cover": ("cover", "versao cover"),
    "karaoke": ("karaoke",),
    "instrumental": ("instrumental",),
    "slowed": ("slowed", "slow reverb", "slowed reverb"),
    "sped_up": ("sped up", "speed up", "nightcore"),
    "reverb": ("reverb", "reverbed"),
    "acoustic": ("acoustic", "acustico", "acustica"),
    "extended": ("extended", "long version"),
    "edit": ("radio edit", "edit"),
    "remaster": ("remaster", "remastered", "remasterizado"),
}

_SEPARADOR_ARTISTA_TITULO = re.compile(r"\s+(?:-|–|—|\||:)\s+")


def _atributos(texto: str) -> frozenset[str]:
    normalizado = " ".join(tokens_texto(texto))
    encontrados: set[str] = set()
    for nome, frases in _ATRIBUTOS.items():
        if any(f" {frase} " in f" {normalizado} " for frase in frases):
            encontrados.add(nome)
    return frozenset(encontrados)


def _artista_titulo(raw: str) -> tuple[str, str]:
    partes = _SEPARADOR_ARTISTA_TITULO.split(raw, maxsplit=1)
    if len(partes) != 2:
        return "", ""
    esquerda, direita = (parte.strip() for parte in partes)
    if not esquerda or not direita:
        return "", ""
    # Evita interpretar duração/URLs ou uma frase curta demais como identidade.
    if esquerda.startswith(("http://", "https://", "www.")):
        return "", ""
    return limpar_apresentacao(esquerda), limpar_apresentacao(direita)


def analisar_consulta(query: str) -> ConsultaNormalizada:
    raw, prefixo = remover_prefixo_busca(query)
    texto = limpar_apresentacao(raw)
    artista, titulo = _artista_titulo(raw)
    return ConsultaNormalizada(
        raw=raw,
        texto=texto,
        tokens=tokens_texto(texto),
        artista=artista,
        titulo=titulo,
        atributos=_atributos(raw),
        prefixo=prefixo,
    )
