from __future__ import annotations

import re
from collections.abc import Callable

import discord

from ..common import _shorten
from .componentes import opcoes_com_valor_padrao
from .valores_atts import normalizar_localidade_atts


BuscadorCatalogoATTS = Callable[[str], list[dict[str, object]]]


def localidade_corresponde_idioma_atts(localidade_voz: str, idioma: str) -> bool:
    localidade_voz = normalizar_localidade_atts(localidade_voz or "", "").strip()
    idioma = normalizar_localidade_atts(idioma or "pt-BR", "pt-BR").strip()
    if not localidade_voz or not idioma:
        return False
    if localidade_voz.casefold() == idioma.casefold():
        return True
    return localidade_voz.split("-", 1)[0].casefold() == idioma.split("-", 1)[0].casefold()


def localidade_da_voz_atts(nome: str) -> str:
    bruto = str(nome or "").strip()
    match = re.match(r"^([a-z]{2})[-_]([A-Za-z]{2})", bruto)
    if match:
        return normalizar_localidade_atts(f"{match.group(1)}-{match.group(2)}", "")
    match = re.match(r"^([a-z]{2})[-_]", bruto)
    if match:
        return match.group(1).lower()
    return ""


def opcoes_idiomas_atts(
    atual: str = "pt-BR",
    vozes: list[dict[str, object]] | None = None,
) -> list[discord.SelectOption]:
    atual = normalizar_localidade_atts(atual, "pt-BR")
    rotulos = {
        "pt-BR": "Português Brasil",
        "pt-PT": "Português Portugal",
        "en-US": "Inglês EUA",
        "en-GB": "Inglês Reino Unido",
        "es-ES": "Espanhol Espanha",
        "es-US": "Espanhol EUA",
        "fr-FR": "Francês",
        "de-DE": "Alemão",
        "it-IT": "Italiano",
        "ja-JP": "Japonês",
        "ko-KR": "Coreano",
        "zh-CN": "Chinês",
    }
    ordenados: list[str] = [
        atual,
        "pt-BR",
        "pt-PT",
        "en-US",
        "en-GB",
        "es-ES",
        "es-US",
        "fr-FR",
        "de-DE",
        "it-IT",
        "ja-JP",
        "ko-KR",
        "zh-CN",
    ]
    for voz in vozes or []:
        localidade = normalizar_localidade_atts(
            str(
                (voz or {}).get("locale")
                or localidade_da_voz_atts(str((voz or {}).get("name") or ""))
                or ""
            ),
            "",
        )
        if localidade and localidade not in ordenados:
            ordenados.append(localidade)

    vistos: set[str] = set()
    opcoes: list[discord.SelectOption] = []
    for codigo in ordenados:
        codigo = normalizar_localidade_atts(codigo, "")
        if not codigo or codigo in vistos:
            continue
        vistos.add(codigo)
        rotulo = rotulos.get(codigo) or codigo
        opcoes.append(
            discord.SelectOption(
                label=_shorten(f"{rotulo} ({codigo})", 100),
                description="Idioma ATTS",
                value=codigo,
                default=(codigo == atual),
            )
        )
        if len(opcoes) >= 25:
            break
    return opcoes or [
        discord.SelectOption(
            label="Português Brasil (pt-BR)",
            description="Idioma ATTS",
            value="pt-BR",
            default=True,
        )
    ]


def pontuar_voz_atts(voz: dict[str, object], idioma: str) -> int:
    nome = str(voz.get("name") or "")
    localidade = str(voz.get("locale") or localidade_da_voz_atts(nome) or "")
    pontuacao = 0
    if normalizar_localidade_atts(localidade, "").casefold() == normalizar_localidade_atts(idioma, "pt-BR").casefold():
        pontuacao += 100
    elif localidade_corresponde_idioma_atts(localidade, idioma):
        pontuacao += 50
    nome_minusculo = nome.casefold()
    if "local" in nome_minusculo:
        pontuacao += 25
    if bool(voz.get("network_required")):
        pontuacao -= 40
    try:
        pontuacao += int(voz.get("quality") or 0) // 100
    except Exception:
        pass
    try:
        pontuacao -= int(voz.get("latency") or 0) // 100
    except Exception:
        pass
    return pontuacao


def vozes_atts_por_idioma(
    catalogo: list[dict[str, object]] | None,
    idioma: str,
) -> list[dict[str, object]]:
    idioma = normalizar_localidade_atts(idioma, "pt-BR")
    correspondentes: list[dict[str, object]] = []
    for voz in catalogo or []:
        if not isinstance(voz, dict):
            continue
        nome = str(voz.get("name") or "").strip()
        if not nome:
            continue
        localidade = str(voz.get("locale") or localidade_da_voz_atts(nome) or "")
        # O Android às vezes coloca o idioma apenas no nome da voz.
        if localidade_corresponde_idioma_atts(localidade, idioma) or localidade_corresponde_idioma_atts(
            localidade_da_voz_atts(nome),
            idioma,
        ):
            correspondentes.append(voz)
    correspondentes.sort(key=lambda item: (-pontuar_voz_atts(item, idioma), str(item.get("name") or "")))
    return correspondentes


def opcoes_vozes_atts_por_idioma(
    cog: "TTSVoice",
    *,
    idioma: str,
    atual: str = "",
    catalogo: list[dict[str, object]] | None = None,
    buscar_catalogo: BuscadorCatalogoATTS | None = None,
) -> list[discord.SelectOption]:
    del cog  # Mantém o contrato do helper legado; o catálogo é a fonte real.
    idioma = normalizar_localidade_atts(idioma, "pt-BR")
    atual = str(atual or "").strip()
    if catalogo is not None:
        catalogo_resolvido = list(catalogo)
    elif buscar_catalogo is not None:
        catalogo_resolvido = list(buscar_catalogo(idioma) or [])
    else:
        catalogo_resolvido = []

    correspondentes = vozes_atts_por_idioma(catalogo_resolvido, idioma)
    if atual and atual.casefold() not in {"auto", "default", "padrao", "padrão"} and not any(
        str(item.get("name") or "") == atual for item in correspondentes
    ):
        correspondentes.insert(
            0,
            {
                "name": atual,
                "locale": localidade_da_voz_atts(atual),
                "network_required": False,
                "quality": 0,
                "latency": 0,
            },
        )

    opcoes: list[discord.SelectOption] = [
        discord.SelectOption(
            label="Automática rápida",
            description="Prefere voz local",
            value="auto",
            default=(not atual or atual.casefold() in {"auto", "automatica", "automática"}),
        ),
        discord.SelectOption(
            label="Padrão",
            description="Voz padrão do sistema",
            value="default",
            default=(atual.casefold() in {"default", "padrao", "padrão"}),
        ),
    ]
    vistos = {"auto", "default"}
    for voz in correspondentes:
        nome = str(voz.get("name") or "").strip()
        if not nome or nome in vistos:
            continue
        vistos.add(nome)
        localidade = normalizar_localidade_atts(
            str(voz.get("locale") or localidade_da_voz_atts(nome) or idioma),
            idioma,
        )
        requer_rede = bool(voz.get("network_required")) or "network" in nome.casefold()
        local = "local" in nome.casefold() or not requer_rede
        descricao = [localidade, "local" if local else "online"]
        if requer_rede:
            descricao.append("usa internet")
        opcoes.append(
            discord.SelectOption(
                label=_shorten(nome, 100),
                description=_shorten(" · ".join(descricao), 100),
                value=nome[:100],
                default=(nome == atual),
            )
        )
        if len(opcoes) >= 25:
            break
    return opcoes_com_valor_padrao(opcoes, atual if atual else "auto")


def voz_atts_corresponde_idioma(
    voz: str,
    idioma: str,
    *,
    buscar_catalogo: BuscadorCatalogoATTS | None = None,
) -> bool:
    voz = str(voz or "").strip()
    if not voz or voz.casefold() in {"auto", "default", "padrao", "padrão", "automatica", "automática"}:
        return True
    catalogo = list(buscar_catalogo(idioma) or []) if buscar_catalogo is not None else []
    for item in catalogo:
        if str(item.get("name") or "") == voz:
            return localidade_corresponde_idioma_atts(
                str(item.get("locale") or localidade_da_voz_atts(voz) or ""),
                idioma,
            )
    return localidade_corresponde_idioma_atts(localidade_da_voz_atts(voz), idioma)


def primeira_voz_atts_por_idioma(opcoes: list[discord.SelectOption]) -> str:
    for opcao in opcoes:
        valor = str(getattr(opcao, "value", "") or "").strip()
        if valor and valor not in {"auto", "default"}:
            return valor
    return ""


def catalogo_atts_pronto_para_idioma(
    catalogo: list[dict[str, object]] | None,
    idioma: str,
) -> bool:
    return bool(vozes_atts_por_idioma(catalogo or [], idioma))
