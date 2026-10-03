"""Executores estreitos chamados somente depois de validação e claim único."""
from __future__ import annotations

import asyncio
import inspect
import io
from dataclasses import dataclass

import discord

from .action_policy import ActionDenied, _audio_channel, _fresh_member, validate_action
from .audio import DEFAULT_TTS_VOICE


class ActionExecutionUncertain(ActionDenied):
    """Um efeito pode ter ocorrido; o fluxo não deve repetir automaticamente."""


@dataclass(frozen=True)
class ExecutionResult:
    public_result: str
    message_id: int | None = None


def _request_id(doc: dict) -> str:
    return str(doc.get("request_id") or doc.get("_id") or "chatbot")[:64]


async def _await(value):
    return await value if inspect.isawaitable(value) else value


async def execute_action(bot, doc: dict, *, actor_id: int) -> ExecutionResult:
    # O claim não concede autoridade: confirme permissões também após a reserva.
    await validate_action(bot, doc, actor_id)
    guild = bot.get_guild(int(doc["guild_id"]))
    if guild is None:
        raise ActionDenied("O servidor dessa solicitação não está disponível.")
    payload = doc["payload"]
    action = doc["action"]
    if action in {"join_voice", "speak_voice"}:
        tts = bot.get_cog("TTSVoice")
        adapter = getattr(tts, "chatbot_join_voice" if action == "join_voice" else "chatbot_speak_voice", None)
        if not callable(adapter):
            raise ActionDenied("A ação de voz está indisponível agora.")
        arguments = {"guild_id": guild.id, "user_id": int(payload["target_id"]),
                     "channel_id": int(payload["voice_channel_id"]), "request_id": _request_id(doc)}
        async def before_effect():
            await validate_action(bot, doc, actor_id)
        arguments["before_effect"] = before_effect
        if action == "speak_voice":
            arguments["text"] = payload["text"]
        result = await adapter(**arguments)
        if not isinstance(result, dict):
            raise ActionExecutionUncertain("Não consegui confirmar o resultado da ação de voz. Verifique a call.")
        if result.get("status") == "uncertain":
            raise ActionExecutionUncertain("Não consegui confirmar o resultado da ação de voz. Verifique a call.")
        if not result.get("ok"):
            # Mensagens vêm do adaptador local, que nunca inclui a fala privada.
            raise ActionDenied(str(result.get("message") or "Não consegui executar essa ação de voz."))
        return ExecutionResult("Entrou na call autorizada." if action == "join_voice" else "Falou na call autorizada.")
    if action == "send_audio":
        tts = bot.get_cog("TTSVoice")
        adapter = getattr(tts, "synthesize_chatbot_attachment", None)
        settings_db = getattr(bot, "settings_db", None)
        if not callable(adapter) or not callable(getattr(settings_db, "resolve_tts", None)):
            raise ActionDenied("A geração de áudio está indisponível agora.")
        settings = dict(await _await(settings_db.resolve_tts(guild.id, int(doc["requester_id"]))) or {})
        audio = await adapter(
            guild_id=guild.id, user_id=int(doc["requester_id"]), text=payload["text"],
            voice=str(settings.get("edge_voice") or DEFAULT_TTS_VOICE),
            language=str(settings.get("gtts_language", settings.get("language", "pt-br")) or "pt-br"),
            rate=str(settings.get("edge_rate", settings.get("rate", "+0%")) or "+0%"),
            pitch=str(settings.get("edge_pitch", settings.get("pitch", "+0Hz")) or "+0Hz"),
            max_bytes=8 * 1024 * 1024,
        )
        if not isinstance(audio, bytes) or not audio or len(audio) > 8 * 1024 * 1024:
            raise ActionDenied("Não consegui gerar o áudio agora.")
        # A síntese pode demorar; autorização anterior não sobrevive a mudanças
        # nas configurações, no acesso do solicitante ou nas permissões do bot.
        me = await _fresh_member(guild, int(bot.user.id))
        channel = _audio_channel(guild, int(doc["channel_id"]), me)
        await validate_action(bot, doc, actor_id)
        channel = _audio_channel(guild, int(doc["channel_id"]), me)
        message_id = int(doc.get("card_message_id") or doc.get("message_id") or doc["origin_message_id"])
        reference = discord.MessageReference(message_id=message_id, channel_id=channel.id, guild_id=guild.id,
                                             fail_if_not_exists=False)
        attachment = discord.File(io.BytesIO(audio), filename="resposta.mp3")
        try:
            sent = await channel.send(file=attachment, reference=reference, allowed_mentions=discord.AllowedMentions.none())
        except (asyncio.TimeoutError, discord.HTTPException) as exc:
            if isinstance(exc, discord.HTTPException) and 400 <= exc.status < 500:
                raise ActionDenied("Não consegui enviar o áudio no canal original.") from None
            raise ActionExecutionUncertain("Não consegui confirmar o envio do áudio. Verifique o canal antes de tentar novamente.") from None
        finally:
            attachment.close()
        return ExecutionResult("Áudio enviado.", message_id=int(sent.id))
    if action == "ban_member":
        target = await _fresh_member(guild, int(payload["target_id"]))
        audit = f"Chatbot; aprovação {int(actor_id)}; pedido {_request_id(doc)}; {payload['reason']}"[:512]
        await validate_action(bot, doc, actor_id)
        try:
            await target.ban(reason=audit, delete_message_seconds=0)
        except (asyncio.TimeoutError, discord.HTTPException) as exc:
            if isinstance(exc, discord.HTTPException) and 400 <= exc.status < 500:
                raise ActionDenied("O Discord recusou esse banimento. Verifique permissões e hierarquia.") from None
            raise ActionExecutionUncertain("Não consegui confirmar o banimento. Verifique o membro antes de tentar novamente.") from None
        return ExecutionResult("Membro banido. O histórico de mensagens foi preservado.")
    raise ActionDenied("Essa ação não está disponível.")
