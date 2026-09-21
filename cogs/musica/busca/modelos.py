from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class ConsultaNormalizada:
    raw: str
    texto: str
    tokens: tuple[str, ...]
    artista: str = ""
    titulo: str = ""
    colaboradores: tuple[str, ...] = field(default_factory=tuple)
    estrutura: str = "livre"
    atributos: frozenset[str] = field(default_factory=frozenset)
    apresentacao: frozenset[str] = field(default_factory=frozenset)
    prefixo: str = ""
