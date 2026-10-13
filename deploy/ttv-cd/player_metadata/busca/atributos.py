from __future__ import annotations

from .normalizacao import tokens_texto

# Atributos que mudam a gravação/arranjo (ou a política de conteúdo) e,
# portanto, influenciam fortemente ranking e deduplicação.
_FRASES_VARIANTE: dict[str, tuple[str, ...]] = {
    "live": ("live", "ao vivo"),
    "remix": ("remix", "remixed", "mix remix"),
    "cover": ("cover", "versao cover"),
    "karaoke": ("karaoke",),
    "instrumental": ("instrumental",),
    "slowed": ("slowed", "slow reverb", "slowed reverb", "slowed and reverb"),
    "sped_up": ("sped up", "speed up", "nightcore"),
    "reverb": ("reverb", "reverbed"),
    "acoustic": ("acoustic", "acustico", "acustica"),
    "extended": ("extended", "extended version", "long version"),
    "edit": ("radio edit", "single edit", "edit version"),
    "remaster": ("remaster", "remastered", "remasterizado"),
    "clean": ("clean version", "clean edit", "radio clean"),
    "explicit": ("explicit", "explicit version", "uncensored"),
}

# Atributos de apresentação normalmente mantêm a mesma gravação, mas são
# relevantes quando o usuário pede uma forma específica do resultado.
_FRASES_APRESENTACAO: dict[str, tuple[str, ...]] = {
    "official": ("official", "oficial"),
    "lyrics": ("lyrics", "lyric video", "lyrics video", "letra", "com letra"),
    "audio": ("official audio", "audio oficial", "audio only"),
    "video": ("official video", "official music video", "music video", "video oficial", "videoclipe", "mv"),
    "visualizer": ("visualizer", "visualiser"),
}

ATRIBUTOS_VARIANTE = frozenset(_FRASES_VARIANTE)
ATRIBUTOS_APRESENTACAO = frozenset(_FRASES_APRESENTACAO)

# Para consulta neutra, estas versões costumam ser substitutas ruins da faixa
# original e recebem penalidade maior.
VARIANTES_FORTES = frozenset({
    "cover",
    "karaoke",
    "instrumental",
    "slowed",
    "sped_up",
    "remix",
})
VARIANTES_LEVES = frozenset({"live", "reverb", "acoustic", "extended", "edit"})

# Diferenças nesta assinatura impedem que duas entradas sejam tratadas como a
# mesma gravação mesmo quando título/duração são quase idênticos. Remaster fica
# fora do bloqueio por ISRC porque alguns catálogos descrevem a mesma gravação
# com o sufixo de remaster mantendo o mesmo ISRC.
VARIANTES_INCOMPATIVEIS_ISRC = frozenset({
    "live",
    "remix",
    "cover",
    "karaoke",
    "instrumental",
    "slowed",
    "sped_up",
    "reverb",
    "acoustic",
    "extended",
    "edit",
    "clean",
    "explicit",
})

# Formatos de apresentação que devem permanecer opções distintas quando dois
# resultados concretos do worker os declaram explicitamente.
FORMATOS_APRESENTACAO = frozenset({"audio", "video", "lyrics", "visualizer"})


def _normalizado(texto: str) -> str:
    return " ".join(tokens_texto(texto))


def _encontrar(normalizado: str, mapa: dict[str, tuple[str, ...]]) -> set[str]:
    padded = f" {normalizado} "
    encontrados: set[str] = set()
    for nome, frases in mapa.items():
        if any(f" {frase} " in padded for frase in frases):
            encontrados.add(nome)
    return encontrados


def detectar_variantes(texto: str) -> frozenset[str]:
    normalizado = _normalizado(texto)
    encontrados = _encontrar(normalizado, _FRASES_VARIANTE)

    # "Clean" sozinho é ambíguo (ex.: Clean Bandit). Só consideramos o token
    # isolado quando aparece como qualificador no fim, como "Song (Clean)".
    if normalizado.endswith(" clean") or normalizado == "clean":
        encontrados.add("clean")
    return frozenset(encontrados)


def detectar_apresentacao(texto: str) -> frozenset[str]:
    normalizado = _normalizado(texto)
    encontrados = _encontrar(normalizado, _FRASES_APRESENTACAO)

    # Usuários frequentemente terminam a busca com "audio"/"video" sem
    # escrever "official". Evitamos tratar a palavra no meio de um título como
    # intenção de apresentação.
    if normalizado.endswith(" audio") or normalizado == "audio":
        encontrados.add("audio")
    if normalizado.endswith(" video") or normalizado == "video":
        encontrados.add("video")
    return frozenset(encontrados)


def detectar_atributos(texto: str) -> tuple[frozenset[str], frozenset[str]]:
    return detectar_variantes(texto), detectar_apresentacao(texto)
