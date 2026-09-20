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


@dataclass(frozen=True, slots=True)
class SinaisCandidato:
    titulo: float = 0.0
    artista: float = 0.0
    cobertura: float = 0.0
    ordem: float = 0.0
    oficialidade: float = 0.0
    versao: float = 0.0
    apresentacao: float = 0.0
    qualidade: float = 0.0
    penalidade: float = 0.0


@dataclass(frozen=True, slots=True)
class ResultadoRanking:
    indice_original: int
    score: float
    confianca: float
    sinais: SinaisCandidato
