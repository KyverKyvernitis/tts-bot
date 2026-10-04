"""Adapters estreitos para ações de voz preparadas pelo chatbot.

O chatbot decide a navegação automaticamente. Este módulo conserva o destino
definido e a posse da conexão; uma fala nunca conecta, move ou recupera voz.
"""
from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import os
import tempfile
import time
import weakref
from collections import OrderedDict
from uuid import uuid4
from typing import Any, Awaitable, Callable

import discord

log = logging.getLogger(__name__)


class ChatbotVoiceActionBlocked(RuntimeError):
    """O contexto de voz mudou antes da execução."""


def _result(ok: bool, message: str, *, uncertain: bool = False) -> dict[str, Any]:
    return {"ok": ok, "status": "executed" if ok else "uncertain" if uncertain else "failed", "message": message}


class ChatbotVoiceActionsMixin:
    def chatbot_voice_session_ref(self, guild_id: int, *, require_idle: bool = True) -> str | None:
        """Referência opaca da sessão local atual, sem autoridade embutida."""
        guild = self.bot.get_guild(int(guild_id))
        if guild is None:
            return None
        vc = self._get_voice_client_for_guild(guild)
        channel = self._voice_client_channel(vc)
        if (not self._voice_client_is_connected(vc) or not isinstance(channel, discord.VoiceChannel)
                or getattr(self._get_bot_voice_state_channel(guild), "id", None) != channel.id):
            return None
        if ((hasattr(vc, "listen") and hasattr(vc, "is_listening"))
                or self._voice_client_owned_by_music(vc) or self._music_player_is_active(guild.id)
                or self._music_should_own_voice(guild)):
            return None
        state = self._get_state(guild.id)
        if not state.accepting or not state.dashboard_enabled or getattr(self, "_tts_shutting_down", False):
            return None
        if require_idle and (self._voice_client_is_playing_or_paused(vc) or state.active_item is not None
                             or state.prefetch_item is not None or not state.queue.empty()
                             or bool(getattr(self, "_chatbot_voice_speaking", {}).get(guild.id))):
            return None
        signature = (id(getattr(vc, "_connection", None)), int(channel.id), str(getattr(vc, "session_id", None) or ""))
        previous = getattr(vc, "_chatbot_session_identity", None)
        if not isinstance(previous, tuple) or previous[0] != signature:
            previous = (signature, uuid4().hex)
            vc._chatbot_session_identity = previous
        return previous[1]

    def _register_chatbot_speech(self, item, vc, source) -> None:
        token = self.chatbot_voice_session_ref(int(item.guild_id), require_idle=False)
        if token is None:
            return
        active = getattr(self, "_chatbot_owned_speech", None)
        if active is None:
            active = self._chatbot_owned_speech = {}
        active[int(item.guild_id)] = {"vc": vc, "source": source, "item": item, "session_ref": token,
                                      "voice_channel_id": int(item.channel_id), "request_id": str(item.request_id),
                                      "user_id": int(item.author_id)}

    def _forget_chatbot_speech(self, guild_id, vc, source) -> None:
        active = getattr(self, "_chatbot_owned_speech", {})
        owned = active.get(int(guild_id))
        if owned and owned["vc"] is vc and owned["source"] is source:
            active.pop(int(guild_id), None)

    def chatbot_own_speech_ref(self, guild_id: int, user_id: int) -> dict | None:
        owned = getattr(self, "_chatbot_owned_speech", {}).get(int(guild_id))
        if not owned or owned["user_id"] != int(user_id):
            return None
        guild = self.bot.get_guild(int(guild_id))
        vc = self._get_voice_client_for_guild(guild) if guild else None
        if (vc is not owned["vc"] or not self._voice_client_is_playing_or_paused(vc)
                or getattr(vc, "source", owned["source"]) is not owned["source"]
                or self.chatbot_voice_session_ref(int(guild_id), require_idle=False) != owned["session_ref"]):
            return None
        return {key: owned[key] for key in ("session_ref", "voice_channel_id", "request_id")}

    async def chatbot_interrupt_speech(
        self, *, guild_id: int, user_id: int, channel_id: int, request_id: str,
        session_ref: str | None = None, speech_request_id: str | None = None,
        before_effect: Callable[[], Awaitable[None]] | None = None,
    ) -> dict[str, Any]:
        captured = self.chatbot_own_speech_ref(guild_id, user_id)
        if (captured is None or captured["voice_channel_id"] != int(channel_id)
                or (session_ref is not None and captured["session_ref"] != session_ref)
                or (speech_request_id is not None and captured["request_id"] != speech_request_id)):
            return _result(False, "Esta fala não está mais em reprodução ou pertence a outra conversa.")
        if before_effect is not None and await before_effect() is False:
            return _result(False, "O contexto da fala mudou.")
        if self.chatbot_own_speech_ref(guild_id, user_id) != captured:
            return _result(False, "A fala mudou antes da interrupção.")
        owned = self._chatbot_owned_speech[int(guild_id)]
        try:
            owned["item"].chatbot_interrupted = True
            owned["vc"].stop()
        except Exception:
            return _result(False, "Não consegui confirmar a interrupção; não vou repeti-la automaticamente.", uncertain=True)
        return _result(True, "Interrompi esta fala.")

    async def _chatbot_change_voice(
        self, *, action: str, guild_id: int, user_id: int, channel_id: int, request_id: str,
        session_ref: str | None, before_effect: Callable[[], Awaitable[None]] | None,
    ) -> dict[str, Any]:
        captured = self.chatbot_voice_session_ref(guild_id)
        if captured is None or (session_ref is not None and captured != session_ref):
            return _result(False, "A sessão definida mudou ou está ocupada por outra fala ou recurso.")
        guild = self.bot.get_guild(int(guild_id))
        vc = self._get_voice_client_for_guild(guild)
        source_channel_id = int(self._voice_client_channel(vc).id)

        def check_destination():
            if self.chatbot_voice_session_ref(guild_id) != captured or self._get_voice_client_for_guild(guild) is not vc:
                raise ChatbotVoiceActionBlocked("A sessão de voz mudou; faça um novo pedido.")
            if action == "leave":
                if source_channel_id != int(channel_id):
                    raise ChatbotVoiceActionBlocked("O bot não está mais na call escolhida.")
                return None
            channel = guild.get_channel(int(channel_id))
            member = guild.get_member(int(user_id))
            if not isinstance(channel, discord.VoiceChannel) or getattr(getattr(getattr(member, "voice", None), "channel", None), "id", None) != int(channel_id):
                raise ChatbotVoiceActionBlocked("O membro saiu ou mudou da call escolhida.")
            perms = channel.permissions_for(guild.me)
            if not all(getattr(perms, name, False) for name in ("view_channel", "connect", "speak")):
                raise ChatbotVoiceActionBlocked("O bot perdeu as permissões da call de destino.")
            return channel

        started = False
        try:
            async with self._get_voice_connect_lock(int(guild_id)):
                async with self._get_tts_playback_lock(int(guild_id)):
                    check_destination()
                    if before_effect is not None and await before_effect() is False:
                        return _result(False, "O contexto da ação mudou.")
                    destination = check_destination()
                    started = True
                    if action == "move":
                        await vc.move_to(destination)
                        if not self._voice_client_is_connected(vc) or getattr(self._voice_client_channel(vc), "id", None) != int(channel_id):
                            return _result(False, "Não consegui confirmar a mudança de call.", uncertain=True)
                        temporary = getattr(self, "_chatbot_temporary_voice_channels", None)
                        if temporary is None:
                            temporary = self._chatbot_temporary_voice_channels = {}
                        temporary[int(guild_id)] = int(channel_id)
                        remember = getattr(self, "_remember_expected_voice_channel", None)
                        if callable(remember):
                            remember(int(guild_id), int(channel_id))
                        return _result(True, "Mudei para a call escolhida.")
                    for name in ("_mark_manual_voice_disconnect", "_cancel_runtime_voice_restore"):
                        handler = getattr(self, name, None)
                        if callable(handler):
                            handler(int(guild_id))
                    remember = getattr(self, "_remember_expected_voice_channel", None)
                    if callable(remember):
                        remember(int(guild_id), None)
                    await vc.disconnect(force=False)
                    if self._voice_client_is_connected(vc):
                        return _result(False, "Não consegui confirmar a saída da call.", uncertain=True)
                    getattr(self, "_chatbot_temporary_voice_channels", {}).pop(int(guild_id), None)
                    clear = getattr(self, "_clear_remembered_voice_channel", None)
                    if callable(clear):
                        await clear(int(guild_id))
                    return _result(True, "Saí da call escolhida.")
        except ChatbotVoiceActionBlocked as exc:
            return _result(False, str(exc))
        except ValueError:
            if not started:
                raise
            return _result(False, "Não consegui confirmar o resultado da ação de voz.", uncertain=True)
        except asyncio.CancelledError:
            raise
        except discord.HTTPException as exc:
            return _result(False, "O Discord não confirmou a ação de voz.", uncertain=not 400 <= exc.status < 500)
        except Exception:
            log.warning("[tts_voice] mudança de call não confirmada | guild=%s request=%s", guild_id, request_id)
            return _result(False, "Não consegui confirmar o resultado da ação de voz.", uncertain=started)

    async def chatbot_move_voice(self, *, guild_id: int, user_id: int, channel_id: int, request_id: str,
                                 session_ref: str | None = None, before_effect=None) -> dict[str, Any]:
        return await self._chatbot_change_voice(action="move", guild_id=guild_id, user_id=user_id,
                                               channel_id=channel_id, request_id=request_id,
                                               session_ref=session_ref, before_effect=before_effect)

    async def chatbot_leave_voice(self, *, guild_id: int, user_id: int, channel_id: int, request_id: str,
                                  session_ref: str | None = None, before_effect=None) -> dict[str, Any]:
        return await self._chatbot_change_voice(action="leave", guild_id=guild_id, user_id=user_id,
                                               channel_id=channel_id, request_id=request_id,
                                               session_ref=session_ref, before_effect=before_effect)

    def _chatbot_mirror_precheck(
        self, *, guild_id: int, user_id: int, text_channel_id: int,
        session: Any = None, channel_id: int | None = None, require_idle: bool = False,
    ) -> tuple[Any, Any, Any, str | None]:
        """Conserva a sessão atual e a visibilidade do anexo, sem exigir o autor na call."""
        guild = self.bot.get_guild(int(guild_id))
        if guild is None or guild.get_member(int(user_id)) is None:
            return guild, None, None, "O servidor ou solicitante não está mais disponível."
        vc = self._get_voice_client_for_guild(guild)
        channel = self._voice_client_channel(vc)
        if not self._voice_client_is_connected(vc) or not isinstance(channel, discord.VoiceChannel):
            return guild, channel, vc, "O bot não está conectado a uma call."
        if session is not None and vc is not session:
            return guild, channel, vc, "A sessão de voz foi substituída."
        if channel_id is not None and getattr(channel, "id", None) != int(channel_id):
            return guild, channel, vc, "O bot mudou de call."
        actual_channel = self._get_bot_voice_state_channel(guild)
        if getattr(actual_channel, "id", None) != getattr(channel, "id", None):
            return guild, channel, vc, "O estado de voz mudou."
        if hasattr(vc, "listen") and hasattr(vc, "is_listening"):
            return guild, channel, vc, "A sessão está em uso por outro recurso."
        if self._music_player_is_active(guild.id) or self._music_should_own_voice(guild) or self._voice_client_owned_by_music(vc):
            return guild, channel, vc, "A música está usando a sessão de voz."
        me = getattr(guild, "me", None)
        text_channel = guild.get_channel(int(text_channel_id))
        if text_channel is None:
            getter = getattr(guild, "get_thread", None)
            text_channel = getter(int(text_channel_id)) if callable(getter) else None
        if me is None or text_channel is None:
            return guild, channel, vc, "Não consegui verificar o acesso ao áudio."
        if isinstance(text_channel, discord.Thread) and text_channel.is_private():
            # permissions_for herda o canal pai e não prova filiação a uma
            # thread privada; conserve o arquivo somente no chat nesse caso.
            return guild, channel, vc, "O áudio pertence a uma conversa privada."
        try:
            perms = channel.permissions_for(me)
            if not all(getattr(perms, name, False) for name in ("view_channel", "connect", "speak")):
                return guild, channel, vc, "O bot perdeu as permissões de voz."
            if not getattr(text_channel.permissions_for(me), "view_channel", False):
                return guild, channel, vc, "O bot perdeu o acesso ao canal do áudio."
            if not getattr(text_channel.permissions_for(guild.get_member(int(user_id))), "view_channel", False):
                return guild, channel, vc, "O solicitante perdeu o acesso ao canal do áudio."
            # Uma conversa privada não deve ser lida para uma audiência que
            # não pode abrir o canal original. A cópia é omitida nesse caso.
            for member in channel.members:
                if not getattr(member, "bot", False) and not getattr(text_channel.permissions_for(member), "view_channel", False):
                    return guild, channel, vc, "A audiência da call não tem acesso ao canal do áudio."
        except Exception:
            return guild, channel, vc, "Não consegui verificar as permissões do áudio."
        voice = getattr(me, "voice", None)
        if getattr(voice, "mute", False) or getattr(voice, "suppress", False):
            return guild, channel, vc, "O bot está silenciado nessa call."
        state = self._get_state(guild.id)
        if not state.accepting or not state.dashboard_enabled or getattr(self, "_tts_shutting_down", False):
            return guild, channel, vc, "A fila de voz não está disponível."
        if require_idle and self._voice_client_is_playing_or_paused(vc):
            return guild, channel, vc, "Outro áudio está usando a sessão."
        return guild, channel, vc, None

    async def chatbot_mirror_audio(
        self, *, guild_id: int, user_id: int, text_channel_id: int, audio: bytes,
        request_id: str, before_effect: Callable[[], Awaitable[None]] | None = None,
        expected_voice_channel_id: int | None = None, expected_session_ref: str | None = None,
    ) -> dict[str, Any]:
        """Enfileira os bytes exatos já enviados no chat para a call atual.

        Não cria conexão e retorna assim que a cópia entra na fila canônica.
        A sessão e o canal ficam fixados; uma saída/mudança descarta a cópia.
        """
        from .audio import QueueItem

        if not isinstance(audio, bytes) or not audio or len(audio) > 8 * 1024 * 1024:
            return {"ok": False, "status": "skipped"}
        if expected_voice_channel_id is not None and (
            not isinstance(expected_voice_channel_id, int) or isinstance(expected_voice_channel_id, bool)
            or expected_voice_channel_id <= 0
        ):
            return {"ok": False, "status": "skipped"}
        if expected_session_ref is not None and (
            not isinstance(expected_session_ref, str) or not expected_session_ref
            or self.chatbot_voice_session_ref(int(guild_id), require_idle=False) != expected_session_ref
        ):
            return {"ok": False, "status": "skipped"}
        mirror_id = f"{int(guild_id)}:{str(request_id)[:64]}"
        seen = getattr(self, "_chatbot_mirror_seen", None)
        if seen is None:
            seen = self._chatbot_mirror_seen = OrderedDict()
        now = time.monotonic()
        while seen and next(iter(seen.values())) <= now - 300:
            seen.popitem(last=False)
        if mirror_id in seen:
            return {"ok": False, "status": "skipped"}
        guild, channel, vc, error = self._chatbot_mirror_precheck(
            guild_id=guild_id, user_id=user_id, text_channel_id=text_channel_id,
            channel_id=expected_voice_channel_id,
        )
        if error:
            return {"ok": False, "status": "skipped"}
        captured_session_ref = self.chatbot_voice_session_ref(int(guild_id), require_idle=False)
        if expected_session_ref is not None and captured_session_ref != expected_session_ref:
            return {"ok": False, "status": "skipped"}
        if before_effect is not None and await before_effect() is False:
            return {"ok": False, "status": "skipped"}
        _guild, _channel, _vc, error = self._chatbot_mirror_precheck(
            guild_id=guild_id, user_id=user_id, text_channel_id=text_channel_id,
            session=vc, channel_id=channel.id,
        )
        if error:
            return {"ok": False, "status": "skipped"}
        if self.chatbot_voice_session_ref(int(guild_id), require_idle=False) != captured_session_ref:
            return {"ok": False, "status": "skipped"}
        state = self._get_state(guild.id)
        # Um espelho nunca remove falas comuns já aguardando na fila.
        if mirror_id in seen or state.queue.full():
            return {"ok": False, "status": "skipped"}
        pending = getattr(self, "_chatbot_mirror_pending", None)
        if pending is None:
            pending = self._chatbot_mirror_pending = weakref.WeakValueDictionary()
        if sum(len(getattr(queued, "chatbot_mirror_audio", None) or b"") for queued in list(pending.values())) + len(audio) > 16 * 1024 * 1024:
            return {"ok": False, "status": "skipped"}
        item = QueueItem(
            guild_id=int(guild_id), channel_id=int(channel.id), author_id=int(user_id),
            text="Áudio do chatbot", engine="chatbot_audio", voice="", language="pt-br",
            rate="+0%", pitch="+0Hz", request_id=str(request_id)[:64],
            text_channel_id=int(text_channel_id), chatbot_no_auto_connect=True,
            chatbot_before_effect=before_effect, chatbot_mirror_audio=audio,
            chatbot_mirror_session=vc, chatbot_is_mirror=True,
            chatbot_mirror_session_ref=captured_session_ref or "",
        )
        item._dedup_signature = f"chatbot-mirror:{int(guild_id)}:{item.request_id}"
        accepted, _dropped, _deduplicated = await self._enqueue_tts_item(int(guild_id), item)
        if accepted:
            pending[id(item)] = item
            seen[mirror_id] = now
            while len(seen) > 512:
                seen.popitem(last=False)
            self._ensure_worker(int(guild_id))
        return {"ok": bool(accepted), "status": "enqueued" if accepted else "skipped"}

    async def _play_chatbot_mirror_item(self, item: Any) -> None:
        """Consumidor da fila: sem retry, sem síntese e sem mudança de sessão."""
        path = None
        try:
            async with self._get_voice_connect_lock(int(item.guild_id)):
                vc = item.chatbot_mirror_session
                _guild, _channel, current, error = self._chatbot_mirror_precheck(
                    guild_id=item.guild_id, user_id=item.author_id, text_channel_id=item.text_channel_id,
                    session=vc, channel_id=item.channel_id,
                )
                if error or current is not vc:
                    raise ChatbotVoiceActionBlocked(error or "A sessão de voz foi substituída.")
                with tempfile.NamedTemporaryFile(prefix="chatbot-mirror-", suffix=".mp3", delete=False) as handle:
                    path = handle.name
                    handle.write(item.chatbot_mirror_audio)
                await self._play_file(vc, path, item=item)
        finally:
            item.chatbot_mirror_audio = None
            item.chatbot_mirror_session = None
            item.chatbot_before_effect = None
            if path:
                with contextlib.suppress(OSError):
                    os.unlink(path)

    def _chatbot_voice_precheck(
        self, *, guild_id: int, user_id: int, channel_id: int,
        require_connected: bool = False, request_id: str = "",
    ) -> tuple[Any, Any, str | None]:
        guild = self.bot.get_guild(int(guild_id))
        if guild is None:
            return None, None, "Não encontrei este servidor."
        channel = guild.get_channel(int(channel_id))
        if not isinstance(channel, discord.VoiceChannel):
            return guild, None, "A call escolhida não existe ou não é um canal de voz comum."
        member = guild.get_member(int(user_id))
        member_channel = getattr(getattr(member, "voice", None), "channel", None)
        if member is None or getattr(member_channel, "id", None) != int(channel_id):
            return guild, channel, "O membro saiu ou mudou da call escolhida. Faça uma nova solicitação."
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
            return guild, channel, "O bot já está em outra call; não vou movê-lo com este pedido."
        if vc is not None and self._voice_client_is_connected(vc) and getattr(current_channel, "id", None) != int(channel_id):
            return guild, channel, "O bot já está em outra call; não vou movê-lo com este pedido."
        if require_connected:
            if not self._voice_client_is_connected(vc) or getattr(current_channel, "id", None) != int(channel_id):
                return guild, channel, "O bot não está mais conectado à call escolhida."
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
        if getattr(item, "chatbot_is_mirror", False):
            _guild, _channel, current, error = self._chatbot_mirror_precheck(
                guild_id=item.guild_id, user_id=item.author_id, text_channel_id=item.text_channel_id,
                session=item.chatbot_mirror_session, channel_id=item.channel_id, require_idle=True,
            )
            if (error or current is not vc or (item.chatbot_mirror_session_ref
                    and self.chatbot_voice_session_ref(int(item.guild_id), require_idle=False) != item.chatbot_mirror_session_ref)):
                raise ChatbotVoiceActionBlocked(error or "A sessão de voz foi substituída.")
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
                failure_context=f"entrada automática do chatbot · solicitação {request_id}",
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
            log.exception("[tts_voice] entrada automática do chatbot falhou | guild=%s request=%s", guild_id, request_id)
            return _result(False, "Não consegui confirmar a entrada na call; confira antes de tentar de novo.", uncertain=True)
        if vc is None or not self._voice_client_is_connected(vc) or getattr(self._voice_client_channel(vc), "id", None) != int(channel_id):
            return _result(False, "Não consegui confirmar a entrada na call escolhida; confira antes de tentar de novo.", uncertain=True)
        return _result(True, f"Entrei na call {channel.name}.")

    async def chatbot_speak_voice(
        self, *, guild_id: int, user_id: int, channel_id: int, request_id: str, text: str,
        before_effect: Callable[[], Awaitable[None]] | None = None,
        voice_override: str = "", language_override: str = "",
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
            if voice_override:
                options["voice"] = str(voice_override)
            if language_override:
                options["language"] = str(language_override)
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
            if result.get("chatbot_interrupted"):
                return _result(False, "A fala foi interrompida.")
            return {**_result(True, "Falei o áudio na call escolhida."), "first_frame_observed": True}
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
