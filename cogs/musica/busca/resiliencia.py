from __future__ import annotations

_BUSCAS_PROFUNDAS_ATIVAS = 0


def buscas_profundas_ativas() -> int:
    return _BUSCAS_PROFUNDAS_ATIVAS


def tentar_reservar_busca_profunda(*, limite: int) -> bool:
    global _BUSCAS_PROFUNDAS_ATIVAS
    maximo = max(0, int(limite or 0))
    if maximo <= 0 or _BUSCAS_PROFUNDAS_ATIVAS >= maximo:
        return False
    _BUSCAS_PROFUNDAS_ATIVAS += 1
    return True


def liberar_busca_profunda() -> None:
    global _BUSCAS_PROFUNDAS_ATIVAS
    _BUSCAS_PROFUNDAS_ATIVAS = max(0, _BUSCAS_PROFUNDAS_ATIVAS - 1)


def limpar_resiliencia_busca() -> None:
    global _BUSCAS_PROFUNDAS_ATIVAS
    _BUSCAS_PROFUNDAS_ATIVAS = 0
