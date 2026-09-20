from __future__ import annotations

import re

from .atributos import detectar_atributos
from .modelos import ConsultaNormalizada
from .normalizacao import limpar_apresentacao, remover_prefixo_busca, tokens_texto

_SEPARADOR_ARTISTA_TITULO = re.compile(r"\s+(?:-|–|—|\||:)\s+")


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
    atributos, apresentacao = detectar_atributos(raw)
    return ConsultaNormalizada(
        raw=raw,
        texto=texto,
        tokens=tokens_texto(texto),
        artista=artista,
        titulo=titulo,
        atributos=atributos,
        apresentacao=apresentacao,
        prefixo=prefixo,
    )
