from __future__ import annotations

_CANCELAMENTOS_ESPERADOS = (
    "MusicPlaybackError: Música pulada antes de iniciar o áudio.",
    "MusicPlaybackError: Playback cancelado.",
)


def eh_cancelamento_esperado(texto_excecao: str) -> bool:
    """Retorna ``True`` para cancelamentos normais de reprodução.

    A filtragem mora no domínio de música; o logger global apenas consulta o
    predicado para não conhecer mensagens/exceções específicas do player.
    """
    return any(marcador in str(texto_excecao or "") for marcador in _CANCELAMENTOS_ESPERADOS)
