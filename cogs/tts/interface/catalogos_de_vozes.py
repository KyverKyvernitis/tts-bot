from __future__ import annotations

import discord

from ..common import _shorten


def idioma_edge_da_voz(voz: str, padrao: str = "pt-BR") -> str:
    voz = str(voz or "").strip()
    partes = voz.split("-")
    if len(partes) >= 2 and partes[0] and partes[1]:
        return f"{partes[0]}-{partes[1]}"
    return str(padrao or "pt-BR")


def voz_edge_corresponde_idioma(voz: str, idioma: str) -> bool:
    voz = str(voz or "").strip()
    idioma = str(idioma or "").strip()
    return bool(voz and idioma and voz.startswith(idioma + "-"))


def opcoes_idiomas_edge(cog: "TTSVoice", atual: str = "") -> list[discord.SelectOption]:
    atual = str(atual or "pt-BR").strip() or "pt-BR"
    preferidos = [atual, "pt-BR", "pt-PT", "en-US", "en-GB", "es-ES", "es-MX", "fr-FR", "de-DE", "it-IT", "ja-JP", "ko-KR", "zh-CN"]
    descobertos = sorted({
        idioma_edge_da_voz(v)
        for v in list(getattr(cog, "edge_voice_cache", []) or []) + sorted(getattr(cog, "edge_voice_names", set()) or set())
        if str(v or "").strip()
    })
    conjunto_descobertos = set(descobertos)
    vistos: set[str] = set()
    opcoes: list[discord.SelectOption] = []
    for codigo in preferidos + descobertos:
        codigo = str(codigo or "").strip()
        if not codigo or codigo in vistos:
            continue
        # Quando o catálogo do Edge está carregado, não inventa idiomas que
        # atualmente não tenham nenhuma voz disponível nele.
        if conjunto_descobertos and codigo not in conjunto_descobertos:
            continue
        vistos.add(codigo)
        opcoes.append(discord.SelectOption(label=_shorten(codigo, 100), description="Idioma Edge", value=codigo, default=(codigo == atual)))
        if len(opcoes) >= 25:
            break
    # Catálogo indisponível: mantém somente o idioma atual para o fallback do
    # modal continuar utilizável sem fabricar uma lista de vozes.
    return opcoes or [discord.SelectOption(label=_shorten(atual, 100), description="Idioma Edge", value=atual, default=True)]


def opcoes_vozes_edge_por_idioma(cog: "TTSVoice", *, idioma: str, atual: str = "") -> list[discord.SelectOption]:
    idioma = str(idioma or idioma_edge_da_voz(atual)).strip() or "pt-BR"
    atual = str(atual or "").strip()
    fonte_vozes = {
        str(v or "").strip()
        for v in list(getattr(cog, "edge_voice_cache", []) or []) + sorted(getattr(cog, "edge_voice_names", set()) or set())
        if str(v or "").strip()
    }
    disponiveis = {v for v in fonte_vozes if voz_edge_corresponde_idioma(v, idioma)}
    # A ordem preferida é apenas cosmética. Uma voz só entra no modal se ela
    # também existir no catálogo retornado pelo edge_tts.list_voices().
    preferidas = [
        "pt-BR-FranciscaNeural",
        "pt-BR-AntonioNeural",
        "pt-BR-BrendaNeural",
        "pt-BR-DonatoNeural",
        "pt-BR-ElzaNeural",
        "pt-BR-FabioNeural",
        "pt-BR-GiovannaNeural",
        "pt-BR-HumbertoNeural",
        "pt-BR-JulioNeural",
        "pt-BR-LeilaNeural",
        "pt-BR-LeticiaNeural",
        "pt-BR-ManuelaNeural",
        "pt-BR-NicolauNeural",
        "pt-BR-ValerioNeural",
        "pt-BR-YaraNeural",
    ]
    candidatos: list[str] = []
    if atual in disponiveis:
        candidatos.append(atual)
    candidatos.extend([v for v in preferidas if v in disponiveis])
    candidatos.extend(sorted(disponiveis))
    vistos: set[str] = set()
    vozes: list[str] = []
    for voz in candidatos:
        voz = str(voz or "").strip()
        if not voz or voz in vistos:
            continue
        vistos.add(voz)
        vozes.append(voz)
        if len(vozes) >= 25:
            break
    if not vozes:
        return []
    tem_atual = bool(atual and any(v == atual for v in vozes))
    opcoes: list[discord.SelectOption] = []
    for indice, voz in enumerate(vozes[:25]):
        opcoes.append(
            discord.SelectOption(
                label=_shorten(voz, 100),
                description="Voz Edge",
                value=voz,
                default=(voz == atual if tem_atual else indice == 0),
            )
        )
    return opcoes


def primeira_voz_edge_por_idioma(cog: "TTSVoice", idioma: str, atual: str = "") -> str:
    opcoes = opcoes_vozes_edge_por_idioma(cog, idioma=idioma, atual=atual)
    if not opcoes:
        return ""
    return str(getattr(opcoes[0], "value", None) or getattr(opcoes[0], "label", None) or "")


def principais_opcoes_vozes_edge(cog: "TTSVoice", atual: str = "") -> list[discord.SelectOption]:
    idioma = idioma_edge_da_voz(atual)
    return opcoes_vozes_edge_por_idioma(cog, idioma=idioma, atual=atual)


def principais_opcoes_idiomas_gtts(cog: "TTSVoice", atual: str = "") -> list[discord.SelectOption]:
    preferidos = [
        "pt-br", "pt", "en", "es", "fr", "de", "it", "ja", "ko", "zh-cn",
        "ru", "ar", "hi", "id", "tr", "pl", "nl", "sv", "no", "da",
    ]
    itens: list[discord.SelectOption] = []
    vistos: set[str] = set()
    idiomas = dict(cog.gtts_languages or {})
    for codigo in ([atual] if atual else []) + preferidos + sorted(idiomas):
        codigo = str(codigo or "").strip().lower()
        if not codigo or codigo in vistos:
            continue
        vistos.add(codigo)
        nome = idiomas.get(codigo) or codigo
        rotulo = _shorten(f"{nome} ({codigo})", 100) if nome != codigo else _shorten(codigo, 100)
        itens.append(discord.SelectOption(label=rotulo, description="Idioma gTTS", value=codigo, default=(codigo == str(atual or "").strip().lower())))
        if len(itens) >= 25:
            break
    return itens or [discord.SelectOption(label="Português Brasil (pt-br)", description="Idioma gTTS", value="pt-br", default=True)]
