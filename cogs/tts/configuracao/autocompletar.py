from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from typing import Any


ConstrutorEscolha = Callable[[str, str], Any]


def opcoes_autocomplete_vozes_edge(
    current: str,
    *,
    vozes_cache: Iterable[str] | None,
    nomes_vozes: Iterable[str],
    construir_escolha: ConstrutorEscolha,
) -> list[Any]:
    """Monta até 25 sugestões de voz Edge, preservando a prioridade do cache."""
    consulta = (current or "").strip().lower()
    cache = list(vozes_cache or [])
    vozes = cache or sorted(nomes_vozes)
    vozes = [voice for voice in vozes if str(voice).lower().startswith("pt-")]

    resultados: list[Any] = []
    for voice in vozes:
        voice = str(voice)
        if consulta and consulta not in voice.lower():
            continue
        resultados.append(construir_escolha(voice[:100], voice))
        if len(resultados) >= 25:
            break
    return resultados


def opcoes_autocomplete_idiomas_gtts(
    current: str,
    *,
    idiomas: Mapping[str, str],
    construir_escolha: ConstrutorEscolha,
) -> list[Any]:
    """Monta até 25 sugestões de idiomas gTTS por código ou nome."""
    consulta = (current or "").strip().lower()
    resultados: list[Any] = []
    for code, name in sorted(idiomas.items()):
        label = f"{code} — {name}"
        haystack = f"{code} {name}".lower()
        if consulta and consulta not in haystack:
            continue
        resultados.append(construir_escolha(label[:100], code))
        if len(resultados) >= 25:
            break
    return resultados
