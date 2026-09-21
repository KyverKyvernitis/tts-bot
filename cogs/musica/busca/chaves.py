from __future__ import annotations

from .intencao import analisar_consulta
from .normalizacao import texto_basico


def chave_semantica_busca(query: str) -> str:
    """Gera chave estável sem misturar intenções musicais diferentes.

    A chave unifica apenas diferenças de apresentação textual que já são
    compreendidas pelo parser (caixa, espaços e formas estruturadas equivalentes).
    Qualificadores de versão/apresentação e prefixos explícitos continuam fazendo
    parte da identidade para não misturar, por exemplo, ``live`` com ``lyrics``.
    """
    consulta = analisar_consulta(query)
    prefixo = texto_basico(consulta.prefixo)
    atributos = ",".join(sorted(consulta.atributos))
    apresentacao = ",".join(sorted(consulta.apresentacao))

    if consulta.artista and consulta.titulo:
        artista = texto_basico(consulta.artista)
        titulo = texto_basico(consulta.titulo)
        colaboradores = ",".join(
            sorted(
                item
                for item in (texto_basico(valor) for valor in consulta.colaboradores)
                if item
            )
        )
        base = f"artista={artista}|titulo={titulo}|colab={colaboradores}"
    else:
        texto = texto_basico(consulta.texto or consulta.raw)
        base = f"texto={texto}"

    return f"pfx={prefixo}|{base}|attr={atributos}|ap={apresentacao}"
