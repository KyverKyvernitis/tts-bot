"""Adapters estreitos para ações de voz já autorizadas pelo chatbot.

A autorização da staff pertence ao chatbot. Este módulo conserva o destino
aprovado e a posse da conexão; uma fala nunca conecta, move ou recupera voz.
"""
from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import os
import tempfile
from typing import Any, Awaitable, Callable

import discord

log = logging.getLogger(__name__)


class ChatbotVoiceActionBlocked(RuntimeError):
    """O contexto de voz mudou antes da execução."""


def _result(ok: bool, message: str, *, uncertain: bool = False) -> dict[str, Any]:
    return {"ok": ok, "status": "executed" if ok else "uncertain" if uncertain else "failed", "message": message}


class ChatbotVoiceActionsMixin:
    def _chatbot_voice_precheck(
        self, *, guild_id: int, user_id: int, channel_id: int,
        require_connected: bool = False, request_id: str = "",
    ) -> tuple[Any, Any, str | None]:
        guild = self.bot.get_guild(int(guild_id))
        if guild is None:
            return None, None, "Não encontrei este servidor."
        channel = guild.get_channel(int(channel_id))
        if not isinstance(channel, discord.VoiceChannel):
            return guild, None, "A call aprovada não existe ou não é um canal de voz comum."
        member = guild.get_member(int(user_id))
        member_channel = getattr(getattr(member, "voice", None), "channel", None)
        if member is None or getattr(member_channel, "id", None) != int(channel_id):
            return guild, channel, "O membro saiu ou mudou da call aprovada. Faça uma nova solicitação."
        me = getattr(guild, "me", None)
        if me is None:
            return guild, channel, "Não consegui verificar as permissões de voz do bot."
        try:
            perms = channel.permissions_for(me)
            if not all(getattr(perms, name, False) for name in ("view_channel", "connect", "speak")):
                return guild, channel, "O bot precisa de Ver Canal, Conectar e Falar nessa call."
        except Exception:
            return guild, channel, "Não consegui verificar as permissões dessa call."
        vc = self._get_voice_client_for_guild(guild)
        actual_channel = self._get_bot_voice_state_channel(guild)
        current_channel = self._voice_client_channel(vc)
        if vc is not None and hasattr(vc, "listen") and hasattr(vc, "is_listening"):
            return guild, channel, "A sessão de voz está em uso por outro recurso; tente quando ela estiver livre."
        if self._music_player_is_active(guild.id) or self._music_should_own_voice(guild) or self._voice_client_owned_by_music(vc):
            return guild, channel, "A música está usando a sessão de voz; tente quando ela estiver livre."
        if actual_channel is not None and getattr(actual_channel, "id", None) != int(channel_id):
            return guild, channel, "O bot já está em outra call; não vou movê-lo com esta autorização."
        if vc is not None and self._voice_client_is_connected(vc) and getattr(current_channel, "id", None) != int(channel_id):
            return guild, channel, "O bot já está em outra call; não vou movê-lo com esta autorização."
        if require_connected:
            if not self._voice_client_is_connected(vc) or getattr(current_channel, "id", None) != int(channel_id):
                return guild, channel, "O bot não está mais conectado à call aprovada."
            if self._voice_client_is_playing_or_paused(vc):
                return guild, channel, "Já há um áudio tocando nessa call; tente quando terminar."
            voice = getattr(me, "voice", None)
            if getattr(voice, "mute", False) or getattr(voice, "suppress", False):
                return guild, channel, "O bot está silenciado nessa call."
            state = getattr(self, "guild_states", {}).get(guild.id)
            if state is not None and (not state.accepting or not state.dashboard_enabled or state.active_item is not None or not state.queue.empty()):
                return guild, channel, "O TTS está ocupado; tente quando a fala e a fila terminarem."
            active = getattr(self, "_chatbot_voice_speaking", {}).get(guild.id)
            if active and active != request_id:
                return guild, channel, "O chatbot já está preparando ou falando outro áudio nessa call."
        return guild, channel, None

    def _validate_chatbot_voice_item(self, item: Any, vc: Any) -> None:
        if not getattr(item, "chatbot_no_auto_connect", False):
            return
        guild, _channel, error = self._chatbot_voice_precheck(
            guild_id=item.guild_id, user_id=item.author_id, channel_id=item.channel_id,
            require_connected=True, request_id=item.request_id,
        )
        if error:
            raise ChatbotVoiceActionBlocked(error)
        if self._get_voice_client_for_guild(guild) is not vc:
            raise ChatbotVoiceActionBlocked("A sessão de voz foi substituída. Faça uma nova solicitação.")

    def _forget_unconnected_chatbot_join(self, guild: Any, channel_id: int) -> None:
        temporary = getattr(self, "_chatbot_temporary_voice_channels", {})
        if temporary.get(guild.id) != int(channel_id):
            return
        if self._get_bot_voice_state_channel(guild) is None and not self._voice_client_is_connected(self._get_voice_client_for_guild(guild)):
            temporary.pop(guild.id, None)

    async def chatbot_join_voice(
        self, *, guild_id: int, user_id: int, channel_id: int, request_id: str,
        before_effect: Callable[[], Awaitable[None]] | None = None,
    ) -> dict[str, Any]:
        guild, channel, error = self._chatbot_voice_precheck(
            guild_id=guild_id, user_id=user_id, channel_id=channel_id,
        )
        if error:
            return _result(False, error)
        try:
            vc = await self._ensure_connected(
                guild, channel, report_failure=True,
                failure_context=f"entrada do chatbot autorizada pela staff · solicitação {request_id}",
                chatbot_target_user_id=int(user_id),
                chatbot_before_effect=before_effect,
            )
        except ChatbotVoiceActionBlocked as exc:
            self._forget_unconnected_chatbot_join(guild, channel_id)
            return _result(False, str(exc))
        except ValueError:
            self._forget_unconnected_chatbot_join(guild, channel_id)
            # O guard injetado pertence ao controlador de ações. Preserve sua
            # negação tipada sem registrar conteúdo privado no adapter de voz.
            raise
        except asyncio.TimeoutError:
            return _result(False, "A conexão demorou demais; confira se o bot entrou antes de tentar de novo.", uncertain=True)
        except Exception:
            log.exception("[tts_voice] entrada autorizada do chatbot falhou | guild=%s request=%s", guild_id, request_id)
            return _result(False, "Não consegui confirmar a entrada na call; confira antes de tentar de novo.", uncertain=True)
        if vc is None or not self._voice_client_is_connected(vc) or getattr(self._voice_client_channel(vc), "id", None) != int(channel_id):
            return _result(False, "Não consegui confirmar a entrada na call aprovada; confira antes de tentar de novo.", uncertain=True)
        return _result(True, f"Entrei na call {channel.name}.")

    async def chatbot_speak_voice(
        self, *, guild_id: int, user_id: int, channel_id: int, request_id: str, text: str,
        before_effect: Callable[[], Awaitable[None]] | None = None,
    ) -> dict[str, Any]:
        from .audio import QueueItem

        clean_text = str(text or "").strip()[:800]
        if not clean_text:
            return _result(False, "Não há texto para falar.")
        if int(guild_id) in getattr(self, "_chatbot_voice_speaking", {}):
            return _result(False, "O chatbot já está preparando ou falando outro áudio nessa call.")
        guild, _channel, error = self._chatbot_voice_precheck(
            guild_id=guild_id, user_id=user_id, channel_id=channel_id,
            require_connected=True, request_id=request_id,
        )
        if error:
            return _result(False, error)
        if self._get_tts_playback_lock(guild.id).locked():
            return _result(False, "Já há um áudio tocando nessa call; tente quando terminar.")
        active = getattr(self, "_chatbot_voice_speaking", None)
        if active is None:
            active = self._chatbot_voice_speaking = {}
        active[guild.id] = request_id
        path = None
        item = None
        try:
            settings = {}
            db = self._get_db()
            resolver = getattr(db, "resolve_tts", None)
            if callable(resolver):
                value = resolver(int(guild_id), int(user_id))
                settings = dict((await value if inspect.isawaitable(value) else value) or {})
            options = {
                "voice": str(settings.get("edge_voice") or "pt-BR-FranciscaNeural"),
                "language": str(settings.get("gtts_language", settings.get("language", "pt-br")) or "pt-br"),
                "rate": str(settings.get("edge_rate", settings.get("rate", "+0%")) or "+0%"),
                "pitch": str(settings.get("edge_pitch", settings.get("pitch", "+0Hz")) or "+0Hz"),
            }
            data = await self.synthesize_chatbot_attachment(
                guild_id=int(guild_id), user_id=int(user_id), text=clean_text, **options,
            )
            if not data:
                return _result(False, "Não consegui gerar o áudio para essa fala.")
            with tempfile.NamedTemporaryFile(prefix="chatbot-call-", suffix=".mp3", delete=False) as handle:
                path = handle.name
                handle.write(data)
            item = QueueItem(
                guild_id=int(guild_id), channel_id=int(channel_id), author_id=int(user_id),
                text=clean_text, engine="edge", request_id=str(request_id), **options,
                chatbot_before_effect=before_effect,
            )
            item.chatbot_no_auto_connect = True
            # Coordena mudanças de canal com o controlador existente. _play_file
            # também usa a trava canônica de playback e revalida antes de tocar.
            async with self._get_voice_connect_lock(guild.id):
                vc = self._get_voice_client_for_guild(guild)
                self._validate_chatbot_voice_item(item, vc)
                result = await self._play_file(vc, path, item=item)
            if not isinstance(result, dict) or result.get("first_frame_observed") is not True or result.get("tts_discarded") or result.get("ok") is False or result.get("music_route_failed"):
                return _result(False, "A sessão não confirmou a reprodução do áudio.", uncertain=True)
            return _result(True, "Falei o áudio na call aprovada.")
        except ChatbotVoiceActionBlocked as exc:
            return _result(False, str(exc))
        except ValueError:
            if not bool(getattr(item, "chatbot_playback_started", False)):
                raise
            return _result(False, "A reprodução começou, mas não consegui confirmar o áudio completo.", uncertain=True)
        except asyncio.TimeoutError:
            return _result(False, "O áudio demorou demais; não vou repetir automaticamente.", uncertain=bool(getattr(item, "chatbot_playback_started", False)))
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("[tts_voice] fala do chatbot falhou | guild=%s request=%s", guild_id, request_id)
            return _result(False, "Não consegui confirmar a fala na call; não vou repetir automaticamente.", uncertain=bool(getattr(item, "chatbot_playback_started", False)))
        finally:
            if active.get(guild.id) == request_id:
                active.pop(guild.id, None)
            if path:
                with contextlib.suppress(OSError):
                    os.unlink(path)
