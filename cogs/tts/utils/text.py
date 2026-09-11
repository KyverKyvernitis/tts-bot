"""Fachada legada para conversores de referências do TTS.

O código real foi movido para :mod:`cogs.tts.mensagens.referencias`, com nomes
em português. Estas funções permanecem para preservar imports e assinaturas já
usados pelo projeto e por integrações externas.
"""
from __future__ import annotations

from typing import Callable

from ..mensagens.referencias import (
    descricoes_anexos_tts,
    referencia_canal_tts,
    referencia_cargo_tts,
    referencia_link_tts,
    referencia_usuario_tts,
)


def tts_user_reference(member, *, resolver: Callable, guild_id: int | None = None) -> str:
    return referencia_usuario_tts(member, resolvedor=resolver, guild_id=guild_id)


def tts_role_reference(
    role,
    *,
    normalize_spaces: Callable[[str], str],
    looks_pronounceable_for_tts: Callable[[str], bool],
    speech_name: Callable[[str], str],
) -> str:
    return referencia_cargo_tts(
        role,
        normalizar_espacos=normalize_spaces,
        parece_pronunciavel_para_tts=looks_pronounceable_for_tts,
        nome_falado=speech_name,
    )


def tts_channel_reference(
    channel,
    *,
    normalize_spaces: Callable[[str], str],
    looks_pronounceable_for_tts: Callable[[str], bool],
    speech_name: Callable[[str], str],
) -> str:
    return referencia_canal_tts(
        channel,
        normalizar_espacos=normalize_spaces,
        parece_pronunciavel_para_tts=looks_pronounceable_for_tts,
        nome_falado=speech_name,
    )


def tts_link_reference(
    url: str,
    *,
    guild=None,
    discord_channel_url_pattern,
    channel_reference: Callable,
    extract_primary_domain: Callable[[str], str],
    looks_pronounceable_for_tts: Callable[[str], bool],
    speech_name: Callable[[str], str],
) -> str:
    return referencia_link_tts(
        url,
        guild=guild,
        padrao_url_canal_discord=discord_channel_url_pattern,
        referencia_canal=channel_reference,
        extrair_dominio_principal=extract_primary_domain,
        parece_pronunciavel_para_tts=looks_pronounceable_for_tts,
        nome_falado=speech_name,
    )


def tts_attachment_descriptions(
    attachments,
    *,
    image_extensions: tuple[str, ...],
    video_extensions: tuple[str, ...],
) -> list[str]:
    return descricoes_anexos_tts(
        attachments,
        extensoes_imagem=image_extensions,
        extensoes_video=video_extensions,
    )
