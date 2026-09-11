"""Conversão de referências do Discord e anexos para texto falado.

Este módulo concentra regras puras de apresentação de referências usadas pelo
TTS. Ele não conhece estado do cog, fila de áudio, Worker, Termux ou APK.
"""
from __future__ import annotations

from typing import Callable
from urllib.parse import urlparse


def referencia_usuario_tts(
    membro,
    *,
    resolvedor: Callable,
    guild_id: int | None = None,
) -> str:
    falado, _ = resolvedor(membro, guild_id=guild_id)
    return falado


def referencia_cargo_tts(
    cargo,
    *,
    normalizar_espacos: Callable[[str], str],
    parece_pronunciavel_para_tts: Callable[[str], bool],
    nome_falado: Callable[[str], str],
) -> str:
    nome = normalizar_espacos(getattr(cargo, "name", None) or "")
    if parece_pronunciavel_para_tts(nome):
        falado = nome_falado(nome)
        if falado:
            return f"cargo {falado}"
    return "cargo do discord"


def referencia_canal_tts(
    canal,
    *,
    normalizar_espacos: Callable[[str], str],
    parece_pronunciavel_para_tts: Callable[[str], bool],
    nome_falado: Callable[[str], str],
) -> str:
    nome = normalizar_espacos(getattr(canal, "name", None) or "")
    if parece_pronunciavel_para_tts(nome):
        falado = nome_falado(nome)
        if falado:
            return f"canal {falado}"
    return "canal do discord"


def referencia_link_tts(
    url: str,
    *,
    guild=None,
    padrao_url_canal_discord,
    referencia_canal: Callable,
    extrair_dominio_principal: Callable[[str], str],
    parece_pronunciavel_para_tts: Callable[[str], bool],
    nome_falado: Callable[[str], str],
) -> str:
    url_limpa = str(url or "").strip().rstrip(".,!?)]}")
    correspondencia = padrao_url_canal_discord.fullmatch(url_limpa)
    if correspondencia and guild is not None:
        channel_id = int(correspondencia.group(2))
        canal = guild.get_channel(channel_id)
        return referencia_canal(canal)

    try:
        analisada = urlparse(url_limpa)
    except Exception:
        return "link"

    dominio = extrair_dominio_principal(analisada.hostname or "")
    if parece_pronunciavel_para_tts(dominio):
        falado = nome_falado(dominio)
        if falado:
            return f"link do {falado}"
    return "link"


def descricoes_anexos_tts(
    anexos,
    *,
    extensoes_imagem: tuple[str, ...],
    extensoes_video: tuple[str, ...],
) -> list[str]:
    descricoes: list[str] = []
    for anexo in anexos or []:
        tipo_conteudo = str(getattr(anexo, "content_type", "") or "").lower()
        nome_arquivo = str(getattr(anexo, "filename", "") or "").lower()
        if tipo_conteudo == "image/gif" or nome_arquivo.endswith(".gif"):
            descricoes.append("Anexo em GIF")
        elif tipo_conteudo.startswith("image/") or nome_arquivo.endswith(extensoes_imagem):
            descricoes.append("Anexo de imagem")
        elif tipo_conteudo.startswith("video/") or nome_arquivo.endswith(extensoes_video):
            descricoes.append("Anexo de vídeo")
    return descricoes
