from __future__ import annotations

import re
import unicodedata

_PREFIXOS_BUSCA = (
    "ytsearch:",
    "ytmsearch:",
    "scsearch:",
    "amsearch:",
    "dzsearch:",
    "spsearch:",
)

# Palavras de apresentação que não identificam a música em si. Atributos de
# versão (live/remix/etc.) são tratados separadamente e não somem da intenção.
_RUIDO = {
    "official",
    "oficial",
    "music",
    "video",
    "audio",
    "lyrics",
    "lyric",
    "letra",
    "visualizer",
    "visualiser",
    "clip",
    "mv",
    "hd",
    "hq",
    "4k",
}


def sem_acentos(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value or "")
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def texto_basico(value: str) -> str:
    value = sem_acentos(value).lower()
    value = value.replace("&", " and ")
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def tokens_texto(value: str, *, remover_ruido: bool = False) -> tuple[str, ...]:
    tokens = tuple(part for part in texto_basico(value).split() if part)
    if not remover_ruido:
        return tokens
    return tuple(token for token in tokens if token not in _RUIDO)


def remover_prefixo_busca(value: str) -> tuple[str, str]:
    raw = re.sub(r"\s+", " ", (value or "").strip())
    lower = raw.lower()
    for prefixo in _PREFIXOS_BUSCA:
        if lower.startswith(prefixo):
            return raw[len(prefixo) :].strip(), prefixo[:-1]
    return raw, ""


def limpar_apresentacao(value: str) -> str:
    """Normaliza texto sem apagar atributos semanticamente úteis.

    Parênteses e colchetes são mantidos como conteúdo: ``(live)`` ou
    ``[remix]`` precisam continuar disponíveis para o ranking de versão.
    """
    tokens = tokens_texto(value, remover_ruido=True)
    return " ".join(tokens)


def token_set(value: str, *, remover_ruido: bool = True) -> frozenset[str]:
    return frozenset(tokens_texto(value, remover_ruido=remover_ruido))
