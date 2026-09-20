from __future__ import annotations

from .atributos import detectar_atributos
from .estrutura import analisar_estrutura
from .modelos import ConsultaNormalizada
from .normalizacao import limpar_apresentacao, remover_prefixo_busca, tokens_texto


def analisar_consulta(query: str) -> ConsultaNormalizada:
    raw, prefixo = remover_prefixo_busca(query)
    estrutura = analisar_estrutura(raw)
    if estrutura.estruturada:
        texto = " ".join(
            parte
            for parte in (estrutura.artista, estrutura.titulo, *estrutura.colaboradores)
            if parte
        ).strip()
    else:
        texto = limpar_apresentacao(raw)

    # Quando artista/título foram separados com confiança, qualificadores de
    # versão/apresentação pertencem ao lado da música. Isso impede falsos
    # positivos como a banda "Live" ser interpretada como versão ao vivo.
    alvo_atributos = estrutura.alvo_atributos or raw
    atributos, apresentacao = detectar_atributos(alvo_atributos)
    return ConsultaNormalizada(
        raw=raw,
        texto=texto,
        tokens=tokens_texto(texto),
        artista=estrutura.artista,
        titulo=estrutura.titulo,
        colaboradores=estrutura.colaboradores,
        estrutura=estrutura.fonte,
        atributos=atributos,
        apresentacao=apresentacao,
        prefixo=prefixo,
    )
