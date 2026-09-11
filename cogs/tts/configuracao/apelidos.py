from __future__ import annotations

from collections.abc import Callable
from typing import Any


MensagemErro = str | None
NormalizadorEspacos = Callable[[str], str]
PredicadoPronunciavel = Callable[[str], bool]
NormalizadorNomeFalado = Callable[[str], str]


def obter_apelido_falado_salvo(
    banco: Any,
    guild_id: int | None,
    user_id: int | None,
    *,
    normalizar_espacos: NormalizadorEspacos,
) -> str:
    """Lê o apelido falado salvo sem propagar falhas do armazenamento."""
    if not guild_id or not user_id:
        return ""
    if banco is None or not hasattr(banco, "get_user_tts"):
        return ""
    try:
        dados = banco.get_user_tts(int(guild_id), int(user_id)) or {}
    except Exception:
        return ""
    return normalizar_espacos(str((dados or {}).get("speaker_name", "") or ""))


def validar_entrada_apelido_falado(
    valor_bruto: str,
    *,
    normalizar_espacos: NormalizadorEspacos,
    parece_pronunciavel: PredicadoPronunciavel,
    normalizar_nome_falado: NormalizadorNomeFalado,
) -> tuple[str | None, MensagemErro]:
    """Valida e normaliza o apelido falado informado pelo usuário."""
    valor = normalizar_espacos(str(valor_bruto or ""))
    if not valor:
        return "", None
    if not parece_pronunciavel(valor):
        return None, (
            "Esse apelido tem caracteres que o TTS não consegue pronunciar bem. "
            "Use letras, números, espaço, ponto, traço ou underline."
        )
    falado = normalizar_nome_falado(valor)
    if not falado or not parece_pronunciavel(falado):
        return None, "Esse apelido não ficou pronunciável depois da normalização do TTS."
    return falado[:32], None


def resolver_apelido_falado(
    membro: Any,
    *,
    guild_id: int | None,
    obter_apelido_salvo: Callable[[int | None, int | None], str],
    normalizar_espacos: NormalizadorEspacos,
    parece_pronunciavel: PredicadoPronunciavel,
    normalizar_nome_falado: NormalizadorNomeFalado,
    preparar_contexto_servidor: Callable[[Any], None] | None = None,
) -> tuple[str, str]:
    """Resolve o nome que deve ser pronunciado e informa sua origem."""
    if membro is None:
        return "usuário", "padrão"

    # Preserva a preparação histórica do contexto do servidor. O resultado não
    # participa da escolha do apelido, mas algumas implementações de banco podem
    # usar essa leitura para aquecer/cachear os padrões do servidor.
    if preparar_contexto_servidor is not None:
        try:
            preparar_contexto_servidor(membro)
        except Exception:
            pass

    apelido_salvo = obter_apelido_salvo(guild_id, getattr(membro, "id", None))
    if apelido_salvo:
        falado = normalizar_nome_falado(apelido_salvo)
        if falado and parece_pronunciavel(falado):
            return falado, "personalizado"

    nome_exibicao = normalizar_espacos(getattr(membro, "display_name", None) or "")
    nome_usuario = normalizar_espacos(getattr(membro, "name", None) or "")

    if parece_pronunciavel(nome_exibicao):
        falado = normalizar_nome_falado(nome_exibicao)
        if falado:
            return falado, "apelido do servidor"

    if parece_pronunciavel(nome_usuario):
        falado = normalizar_nome_falado(nome_usuario)
        if falado:
            return falado, "nome de usuário"

    return "usuário", "padrão"
