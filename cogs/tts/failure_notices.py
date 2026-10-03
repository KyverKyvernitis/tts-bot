"""Avisos de TTS com destino fixado, sem reproduzir texto ou erros privados."""
from __future__ import annotations

import asyncio
import logging
import time

import discord

log = logging.getLogger(__name__)

_MESSAGES = {
    "opus_missing": "O codec Opus não está disponível para reproduzir a fala.",
    "ffmpeg_missing": "O FFmpeg não está disponível para reproduzir a fala.",
    "no_frames": "O áudio gerado não produziu nenhum quadro de som.",
    "route_failed": "O destino de reprodução não confirmou a fala.",
    "synthesis_failed": "O serviço de voz não conseguiu gerar a fala agora.",
    "connection_failed": "Não consegui usar a conexão da call para falar.",
    "audio_missing": "O arquivo de áudio não estava disponível para reprodução.",
    "audio_source_failed": "Não consegui preparar o som para reprodução. Confira o FFmpeg e os codecs da instalação.",
    "playback_failed": "Não consegui reproduzir a fala na call.",
    "tts_failed": "Não consegui concluir essa fala.",
    "queue_unavailable": "O TTS está pausado ou sua fila está indisponível.",
    "bot_muted": "Estou silenciado na call. A staff precisa retirar meu silenciamento para eu falar.",
    "tts_disabled": "O TTS está desativado na instalação do bot.",
    "tts_guild_disabled": "O TTS está desativado neste servidor. A staff pode ativá-lo no painel de TTS.",
    "ignored_role": "Seu cargo está configurado para não usar TTS neste servidor.",
    "author_not_in_voice": "Entre em uma call para usar esse prefixo de fala.",
}


def failure_code(reason_code: str, error: Exception | None = None) -> str:
    # Somente categorias conhecidas chegam ao chat; mensagens de exceção podem
    # conter texto de fala, caminhos, URLs de provedores ou credenciais.
    cause = error
    for _ in range(4):
        if cause is None:
            break
        if type(cause).__name__ == "OpusNotLoaded":
            return "opus_missing"
        if type(cause).__name__ == "NoAudioReceived":
            return "synthesis_failed"
        if isinstance(cause, FileNotFoundError):
            filename = str(getattr(cause, "filename", "") or "").replace("\\", "/").rsplit("/", 1)[-1]
            return "ffmpeg_missing" if filename.lower() in {"ffmpeg", "ffmpeg.exe"} else "audio_missing"
        cause = getattr(cause, "__cause__", None)
    code = getattr(error, "code", None)
    if isinstance(code, str) and code in _MESSAGES:
        return code
    return reason_code if reason_code in _MESSAGES else "tts_failed"


async def notify_tts_failure(cog, item, reason_code: str, error: Exception | None = None) -> None:
    """Responde apenas ao prefixo original e limita avisos repetidos por membro."""
    message_id = int(getattr(item, "message_id", 0) or 0)
    channel_id = int(getattr(item, "text_channel_id", 0) or 0)
    if not message_id or not channel_id:
        return
    try:
        guild_id, author_id = int(item.guild_id), int(item.author_id)
        guild = cog.bot.get_guild(guild_id)
        if guild is None:
            return
        channel = guild.get_channel_or_thread(channel_id)
        if channel is None or getattr(getattr(channel, "guild", None), "id", None) != guild_id:
            return
        member = guild.get_member(author_id)
        if member is None:
            member = await guild.fetch_member(author_id)
        if not channel.permissions_for(member).view_channel:
            return
        if isinstance(channel, discord.Thread) and channel.is_private():
            if not channel.permissions_for(member).manage_threads:
                participant = await channel.fetch_member(author_id)
                if participant.id != author_id:
                    return
        me = guild.me
        permissions = channel.permissions_for(me)
        can_send = permissions.send_messages_in_threads if isinstance(channel, discord.Thread) else permissions.send_messages
        if not permissions.view_channel or not can_send:
            return
        notices = getattr(cog, "_tts_failure_notice_times", None)
        if notices is None:
            notices = cog._tts_failure_notice_times = {}
        now = time.monotonic()
        key = (guild_id, author_id)
        if now - notices.get(key, float("-inf")) < 30.0:
            return
        for old_key, stamp in list(notices.items()):
            if now - stamp >= 60.0:
                notices.pop(old_key, None)
        if len(notices) >= 512:
            notices.pop(min(notices, key=notices.get), None)
        notices[key] = now
        code = failure_code(reason_code, error)
        engine = {"edge": "Edge", "gtts": "gTTS"}.get(str(getattr(item, "engine", "")), "TTS")
        reference = discord.MessageReference(
            message_id=message_id, channel_id=channel_id, guild_id=guild_id, fail_if_not_exists=False,
        )
        await channel.send(
            f"{engine}: {_MESSAGES[code]} Código: `{code}`.", reference=reference,
            allowed_mentions=discord.AllowedMentions.none(),
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log.warning("[tts_voice] aviso de falha indisponível | type=%s", type(exc).__name__)
