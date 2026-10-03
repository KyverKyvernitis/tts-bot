"""Executores estreitos chamados somente depois de validação e claim único."""
from __future__ import annotations

import asyncio
import inspect
import io
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import discord

from .action_policy import ActionDenied, _audio_channel, _enabled, _fresh_member, _origin_channel, validate_action
from .audio import DEFAULT_TTS_VOICE
from . import constants as C
from .memory import MemoryEpoch

log = logging.getLogger(__name__)


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


async def _conversation_preferences(bot, doc):
    getter = getattr(bot.get_cog("Chatbot"), "get_conversation_preferences", None)
    epoch = doc.get("memory_epoch")
    if not callable(getter) or epoch is None:
        return None
    try:
        preferences = await getter(int(doc["guild_id"]), int(doc["channel_id"]),
                                   int(doc["requester_id"]), MemoryEpoch(**epoch))
    except asyncio.CancelledError:
        raise
    except Exception:
        raise ActionDenied("Não consegui confirmar as preferências atuais dessa conversa.") from None
    if getattr(preferences, "mode", "auto") == "text":
        raise ActionDenied("Essa conversa foi configurada para respostas em texto.")
    return preferences


async def _validate_mirror_context(bot, doc: dict, actor_id: int) -> None:
    await validate_action(bot, doc, actor_id)
    cog = bot.get_cog("Chatbot")
    store = getattr(cog, "_config", None)
    if C.SAFE_MODE or store is None:
        raise ActionDenied("A reprodução do chatbot foi desativada.")
    config = await store.get_config(int(doc["guild_id"]), fresh=True)
    voice_enabled = config.get("voice_actions_enabled", True) if isinstance(config, dict) else getattr(config, "voice_actions_enabled", True)
    if not voice_enabled or not _enabled(config, "send_audio"):
        raise ActionDenied("A reprodução do chatbot foi desativada.")
    guild = bot.get_guild(int(doc["guild_id"]))
    if guild is None:
        raise ActionDenied("O servidor da conversa não está disponível.")
    _origin_channel(guild, doc, config)
    epoch = doc.get("memory_epoch")
    if epoch is not None:
        memory = getattr(cog, "_memory", None)
        if memory is None or await memory.capture_epoch(int(doc["guild_id"]), int(doc["requester_id"])) != MemoryEpoch(**epoch):
            raise ActionDenied("A memória da conversa foi reiniciada.")
    if C.SAFE_MODE:
        raise ActionDenied("A reprodução do chatbot foi desativada.")


async def execute_action(bot, doc: dict, *, actor_id: int) -> ExecutionResult:
    # O claim não concede autoridade: confirme permissões também após a reserva.
    await validate_action(bot, doc, actor_id)
    guild = bot.get_guild(int(doc["guild_id"]))
    if guild is None:
        raise ActionDenied("O servidor dessa solicitação não está disponível.")
    payload = doc["payload"]
    action = doc["action"]
    if action in {"join_voice", "speak_voice", "move_voice", "leave_voice"}:
        tts = bot.get_cog("TTSVoice")
        adapter = getattr(tts, "chatbot_" + action, None)
        if not callable(adapter):
            raise ActionDenied("A ação de voz está indisponível agora.")
        arguments = {"guild_id": guild.id, "user_id": int(payload["target_id"]),
                     "channel_id": int(payload["voice_channel_id"]), "request_id": _request_id(doc)}
        async def before_effect():
            await validate_action(bot, doc, actor_id)
        arguments["before_effect"] = before_effect
        if action == "speak_voice":
            arguments["text"] = payload["text"]
            preferences = await _conversation_preferences(bot, doc)
            if getattr(preferences, "voice", ""):
                arguments["voice_override"] = preferences.voice
            if getattr(preferences, "language", ""):
                arguments["language_override"] = preferences.language
        if action in {"move_voice", "leave_voice"}:
            arguments["session_ref"] = payload["voice_session_ref"]
        result = await adapter(**arguments)
        if not isinstance(result, dict):
            raise ActionExecutionUncertain("Não consegui confirmar o resultado da ação de voz. Verifique a call.")
        if result.get("status") == "uncertain":
            raise ActionExecutionUncertain("Não consegui confirmar o resultado da ação de voz. Verifique a call.")
        if not result.get("ok"):
            # Mensagens vêm do adaptador local, que nunca inclui a fala privada.
            raise ActionDenied(str(result.get("message") or "Não consegui executar essa ação de voz."))
        spoken_result = ("Fala reproduzida na call autorizada." if result.get("first_frame_observed") is True
                         else "Fala enfileirada para a call autorizada.")
        return ExecutionResult({"join_voice": "Entrou na call autorizada.", "speak_voice": spoken_result,
                                "move_voice": "Moveu a própria sessão para a call autorizada.", "leave_voice": "Saiu da call autorizada."}[action])
    if action == "send_audio":
        tts = bot.get_cog("TTSVoice")
        adapter = getattr(tts, "synthesize_chatbot_attachment", None)
        settings_db = getattr(bot, "settings_db", None)
        if not callable(adapter) or not callable(getattr(settings_db, "resolve_tts", None)):
            raise ActionDenied("A geração de áudio está indisponível agora.")
        try:
            settings = dict(await _await(settings_db.resolve_tts(guild.id, int(doc["requester_id"]))) or {})
            preferences = await _conversation_preferences(bot, doc)
            audio = await adapter(
                guild_id=guild.id, user_id=int(doc["requester_id"]), text=payload["text"],
                voice=str(getattr(preferences, "voice", "") or settings.get("edge_voice") or DEFAULT_TTS_VOICE),
                language=str(getattr(preferences, "language", "") or settings.get("gtts_language", settings.get("language", "pt-br")) or "pt-br"),
                rate=str(settings.get("edge_rate", settings.get("rate", "+0%")) or "+0%"),
                pitch=str(settings.get("edge_pitch", settings.get("pitch", "+0Hz")) or "+0Hz"),
                max_bytes=8 * 1024 * 1024,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            # Synthesis has not sent an attachment or started playback; a text
            # fallback is safe only at this confirmed pre-effect failure stage.
            raise ActionDenied("Não consegui gerar o áudio agora.") from None
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
        cog = bot.get_cog("Chatbot")
        replies = getattr(cog, "_reply_store", None)
        if callable(getattr(replies, "record_sent", None)):
            try:
                uploaded = (getattr(sent, "attachments", None) or [None])[0]
                metadata = ({"id": int(uploaded.id), "filename": str(uploaded.filename), "size": int(uploaded.size)}
                            if uploaded is not None else None)
                await replies.record_sent(
                    guild_id=guild.id, channel_id=channel.id, requester_id=int(doc["requester_id"]),
                    origin_message_id=int(doc["origin_message_id"]), message_id=int(sent.id),
                    original_user_text=str(doc.get("original_user_text") or ""), text="", spoken_text=payload["text"],
                    format="audio", provider=str(doc.get("provider") or ""), model=str(doc.get("model") or ""),
                    epoch=doc.get("memory_epoch"), attachment=metadata,
                )
            except Exception as exc:
                log.warning("[chatbot] registro de resposta indisponível | guild=%s erro_tipo=%s", guild.id, type(exc).__name__)
        record_audio = getattr(cog, "record_audio_reply_sent", None)
        if callable(record_audio):
            try:
                await asyncio.wait_for(record_audio(guild_id=guild.id, channel_id=channel.id), timeout=2.0)
            except Exception as exc:
                log.warning("[chatbot] intervalo entre áudios não atualizado | guild=%s erro_tipo=%s", guild.id, type(exc).__name__)
        mirror = getattr(tts, "chatbot_mirror_audio", None)
        if callable(mirror):
            async def before_mirror_effect():
                await _validate_mirror_context(bot, doc, actor_id)
            try:
                await asyncio.wait_for(mirror(
                    guild_id=guild.id, user_id=int(doc["requester_id"]),
                    text_channel_id=channel.id, audio=audio, request_id=_request_id(doc),
                    before_effect=before_mirror_effect,
                ), timeout=8.0)
            except Exception as exc:
                # O envio já foi confirmado pelo Discord. A cópia opcional
                # para a call não deve virar falha nem repetir o arquivo.
                log.warning("[chatbot] cópia de áudio para call omitida | guild=%s request=%s erro_tipo=%s",
                            guild.id, _request_id(doc), type(exc).__name__)
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
    return await _execute_extended(bot, guild, doc, actor_id)


async def _execute_extended(bot, guild, doc, actor_id):
    """Only pinned objects and exact fields reach Discord's mutation methods."""
    action, payload = doc["action"], doc["payload"]
    audit = f"Chatbot; aprovação {int(actor_id)}; pedido {_request_id(doc)}; {payload.get('reason', '')}"[:512]
    target, role, channel, messages = None, None, None, []
    if action in {"timeout_member", "untimeout_member", "kick_member", "assign_role", "remove_role", "change_nickname"}:
        target = await _fresh_member(guild, int(payload["target_id"]))
        if action in {"assign_role", "remove_role"}:
            role = guild.get_role(int(payload["role_id"]))
    elif action == "purge_messages":
        channel = guild.get_channel_or_thread(int(doc["channel_id"]))
        cutoff = datetime.now(timezone.utc) - timedelta(days=14)
        for message_id in payload["message_ids"]:
            try:
                item = await channel.fetch_message(message_id)
            except discord.NotFound:
                continue
            except discord.HTTPException:
                raise ActionDenied("Não consegui confirmar todas as mensagens antes de apagar.") from None
            if (getattr(getattr(item, "guild", None), "id", None) != guild.id
                    or item.channel.id != channel.id or item.id != message_id or item.created_at <= cutoff):
                raise ActionDenied("Uma mensagem mudou de contexto ou ficou antiga demais para esse pedido.")
            messages.append(item)
    elif action == "edit_channel":
        channel = guild.get_channel(int(payload["affected_channel_id"]))
    elif action != "unban_member":
        raise ActionDenied("Essa ação não está disponível.")
    # Resource fetches can suspend; flags, permissions, hierarchy and scope are
    # checked once more immediately before the single side-effect invocation.
    await validate_action(bot, doc, actor_id)
    try:
        if action == "timeout_member":
            await target.timeout(timedelta(seconds=payload["duration_seconds"]), reason=audit)
            result = "Timeout aplicado ao membro autorizado."
        elif action == "untimeout_member":
            await target.timeout(None, reason=audit)
            result = "Timeout removido do membro autorizado."
        elif action == "kick_member":
            await target.kick(reason=audit)
            result = "Membro expulso."
        elif action == "unban_member":
            await guild.unban(discord.Object(id=int(payload["target_id"])), reason=audit)
            result = "Banimento revogado."
        elif action == "assign_role":
            await target.add_roles(role, reason=audit, atomic=True)
            result = "Cargo autorizado atribuído."
        elif action == "remove_role":
            await target.remove_roles(role, reason=audit, atomic=True)
            result = "Cargo autorizado removido."
        elif action == "change_nickname":
            await target.edit(nick=payload["nickname"] or None, reason=audit)
            result = "Apelido autorizado atualizado."
        elif action == "edit_channel":
            await channel.edit(**payload["channel_changes"], reason=audit)
            result = "Configurações autorizadas do canal atualizadas."
        else:
            if messages:
                await channel.delete_messages(messages, reason=audit)
            result = f"{len(messages)} mensagem(ns) autorizada(s) apagada(s)."
    except (asyncio.TimeoutError, discord.HTTPException) as exc:
        if isinstance(exc, discord.HTTPException) and 400 <= exc.status < 500:
            raise ActionDenied("O Discord recusou a ação. Verifique as permissões e a hierarquia.") from None
        raise ActionExecutionUncertain("Não consegui confirmar o resultado. Verifique antes de tentar novamente.") from None
    return ExecutionResult(result)
