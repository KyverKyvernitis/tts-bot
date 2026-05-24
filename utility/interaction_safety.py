from __future__ import annotations

import logging
from typing import Any

import discord

UNKNOWN_INTERACTION_CODE = 10062


def is_unknown_interaction(exc: BaseException) -> bool:
    """True quando Discord já invalidou a interação inicial.

    Interações precisam receber a primeira resposta rapidamente. Quando a VPS
    fica ocupada ou o event loop atrasa, Discord devolve 10062/Unknown
    interaction. Esse helper evita que fallback tente responder novamente pela
    mesma interação morta e transforme atraso em traceback grande.
    """
    code = getattr(exc, "code", None)
    text = str(exc or "")
    return isinstance(exc, discord.NotFound) and (code == UNKNOWN_INTERACTION_CODE or "Unknown interaction" in text)


async def safe_defer_interaction(
    interaction: discord.Interaction,
    *,
    thinking: bool = False,
    ephemeral: bool = False,
    log: logging.Logger | None = None,
    label: str = "interaction",
) -> bool:
    """Tenta reconhecer uma interação sem deixar 10062 estourar no comando/view."""
    try:
        if not interaction.response.is_done():
            await interaction.response.defer(thinking=thinking, ephemeral=ephemeral)
        return True
    except discord.InteractionResponded:
        return True
    except Exception as exc:  # discord.NotFound/HTTPException variam entre versões
        if log is not None:
            if is_unknown_interaction(exc):
                log.warning("[%s] interação expirada antes do defer: %s", label, exc)
            else:
                log.exception("[%s] falha ao deferir interação", label)
        return False


async def safe_send_interaction_message(
    interaction: discord.Interaction,
    content: str | None = None,
    *,
    log: logging.Logger | None = None,
    label: str = "interaction",
    **kwargs: Any,
) -> bool:
    """Envia response/followup sem gerar traceback quando a interação expirou."""
    try:
        if not interaction.response.is_done():
            await interaction.response.send_message(content, **kwargs)
        else:
            await interaction.followup.send(content, **kwargs)
        return True
    except discord.InteractionResponded:
        try:
            await interaction.followup.send(content, **kwargs)
            return True
        except Exception as exc:
            if log is not None:
                if is_unknown_interaction(exc):
                    log.warning("[%s] interação expirada antes do followup: %s", label, exc)
                else:
                    log.exception("[%s] falha ao enviar followup", label)
            return False
    except Exception as exc:
        if log is not None:
            if is_unknown_interaction(exc):
                log.warning("[%s] interação expirada antes da resposta: %s", label, exc)
            else:
                log.exception("[%s] falha ao responder interação", label)
        return False


async def safe_edit_original_or_message(
    interaction: discord.Interaction,
    *,
    message: discord.Message | None = None,
    log: logging.Logger | None = None,
    label: str = "interaction",
    **kwargs: Any,
) -> bool:
    """Edita a resposta original; se o token morreu, tenta editar a mensagem salva."""
    try:
        await interaction.edit_original_response(**kwargs)
        return True
    except Exception as exc:
        if log is not None:
            if is_unknown_interaction(exc):
                log.warning("[%s] token da interação expirou antes do edit_original_response", label)
            else:
                log.debug("[%s] edit_original_response falhou; tentando message.edit", label, exc_info=True)
    if message is None:
        return False
    try:
        await message.edit(**kwargs)
        return True
    except Exception as exc:
        if log is not None:
            log.exception("[%s] falha ao editar mensagem salva", label)
        return False
