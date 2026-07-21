from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from datetime import datetime, timezone
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands
from pymongo import ReturnDocument

from .components import (
    build_active_status_view,
    build_additional_confirmation,
    build_additional_message_view,
    build_delivery_failure_view,
    build_feedback_created_dm,
    build_owner_update_dm,
    build_resolved_dm,
    category_info,
    notice_view,
    protocol_of,
)
from .constants import (
    CATEGORY_OPTIONS,
    DESCRIPTION_MAX_LENGTH,
    DESCRIPTION_MIN_LENGTH,
    DM_MESSAGE_PREFIX,
    DM_STATUS_COMMAND,
    DM_SWITCH_COMMAND,
    FEEDBACK_COUNTER_COLLECTION_SUFFIX,
    FEEDBACK_DOC_COLLECTION_SUFFIX,
    FEEDBACK_FORUM_CHANNEL_ID,
    INTERNAL_OWNER_PREFIX,
    MAX_FORWARDED_ATTACHMENTS,
    MAX_OPEN_FEEDBACKS_PER_USER,
    OPEN_STATUSES,
    PROTOCOL_PREFIX,
    STATUS_IN_REVIEW,
    STATUS_OPEN,
    STATUS_RESOLVED,
    STATUS_RESOLVING,
)
from .modals import FeedbackModal
from .views import FeedbackSwitchView, FeedbackThreadView, ResolveConfirmationView


log = logging.getLogger("bot.feedback")


class FeedbackCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._locks: dict[str, asyncio.Lock] = {}
        self._registered_views: set[tuple[str, int]] = set()
        self._restore_lock = asyncio.Lock()

    @property
    def settings_db(self):
        return getattr(self.bot, "settings_db", None)

    @property
    def feedbacks(self):
        settings = self.settings_db
        if settings is None:
            raise RuntimeError("Banco de dados ainda não está disponível.")
        base_name = str(getattr(settings.coll, "name", "settings") or "settings")
        return settings.db[f"{base_name}_{FEEDBACK_DOC_COLLECTION_SUFFIX}"]

    @property
    def counters(self):
        settings = self.settings_db
        if settings is None:
            raise RuntimeError("Banco de dados ainda não está disponível.")
        base_name = str(getattr(settings.coll, "name", "settings") or "settings")
        return settings.db[f"{base_name}_{FEEDBACK_COUNTER_COLLECTION_SUFFIX}"]

    async def cog_load(self) -> None:
        await self._ensure_indexes()
        await self._restore_persistent_views()

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        await self._restore_persistent_views()

    def _lock_for(self, key: str) -> asyncio.Lock:
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock

    async def _ensure_indexes(self) -> None:
        try:
            await self.feedbacks.create_index(
                "protocol", unique=True, name="feedback_protocol_unique"
            )
            await self.feedbacks.create_index(
                [("author_id", 1), ("guild_id", 1), ("status", 1), ("updated_at", -1)],
                name="feedback_author_guild_status",
            )
            await self.feedbacks.create_index(
                [("author_id", 1), ("dm_active", 1), ("status", 1), ("updated_at", -1)],
                name="feedback_dm_active",
            )
            await self.feedbacks.create_index(
                "thread_id",
                unique=True,
                name="feedback_thread_unique",
                partialFilterExpression={"thread_id": {"$gt": 0}},
            )
            await self.feedbacks.create_index(
                [("status", 1), ("starter_message_id", 1)],
                name="feedback_restore_views",
            )
        except Exception:
            log.exception("falha ao garantir índices da cog de feedback")
            raise

    async def _restore_persistent_views(self) -> None:
        if self.settings_db is None:
            return
        async with self._restore_lock:
            cursor = self.feedbacks.find(
                {
                    "status": {"$in": list(OPEN_STATUSES)},
                    "starter_message_id": {"$gt": 0},
                }
            )
            async for feedback in cursor:
                protocol = protocol_of(feedback)
                message_id = int(feedback.get("starter_message_id") or 0)
                key = (protocol, message_id)
                if not message_id or key in self._registered_views:
                    continue
                try:
                    self.bot.add_view(
                        FeedbackThreadView(self, feedback), message_id=message_id
                    )
                    self._registered_views.add(key)
                except Exception:
                    log.exception("falha ao restaurar view do feedback %s", protocol)

    def _is_source_staff(self, member: discord.Member) -> bool:
        perms = member.guild_permissions
        return bool(
            member.id == member.guild.owner_id
            or perms.administrator
            or perms.manage_guild
            or perms.manage_messages
            or perms.kick_members
        )

    async def _can_manage_feedback(self, user: discord.abc.User) -> bool:
        try:
            if await self.bot.is_owner(user):
                return True
        except Exception:
            log.debug("não foi possível validar dono do bot", exc_info=True)
        if not isinstance(user, discord.Member):
            return False
        perms = user.guild_permissions
        return bool(
            user.id == user.guild.owner_id
            or perms.administrator
            or perms.manage_guild
            or perms.manage_messages
            or perms.manage_threads
        )

    async def _send_interaction_view(
        self,
        interaction: discord.Interaction,
        view: discord.ui.LayoutView,
        *,
        ephemeral: bool = True,
    ) -> None:
        try:
            if interaction.response.is_done():
                await interaction.followup.send(view=view, ephemeral=ephemeral)
            else:
                await interaction.response.send_message(view=view, ephemeral=ephemeral)
        except discord.HTTPException:
            log.debug("falha ao responder interação da cog de feedback", exc_info=True)

    async def _next_protocol(self) -> tuple[int, str]:
        doc = await self.counters.find_one_and_update(
            {"_id": "protocol"},
            {"$inc": {"value": 1}},
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
        number = max(1, int((doc or {}).get("value") or 1))
        return number, f"{PROTOCOL_PREFIX}-{number:06d}"

    async def _open_feedbacks_for_user(self, user_id: int) -> list[dict[str, Any]]:
        cursor = self.feedbacks.find(
            {"author_id": int(user_id), "status": {"$in": list(OPEN_STATUSES)}}
        ).sort("updated_at", -1)
        return [doc async for doc in cursor]

    async def _find_open_feedback(
        self, *, user_id: int, guild_id: int
    ) -> dict[str, Any] | None:
        return await self.feedbacks.find_one(
            {
                "author_id": int(user_id),
                "guild_id": int(guild_id),
                "status": {"$in": list(OPEN_STATUSES)},
            },
            sort=[("updated_at", -1)],
        )

    async def _set_active_feedback(
        self, user_id: int, protocol: str
    ) -> dict[str, Any] | None:
        async with self._lock_for(f"active:{int(user_id)}"):
            feedback = await self.feedbacks.find_one(
                {
                    "author_id": int(user_id),
                    "protocol": str(protocol),
                    "status": {"$in": list(OPEN_STATUSES)},
                }
            )
            if feedback is None:
                return None
            now = datetime.now(timezone.utc)
            await self.feedbacks.update_many(
                {"author_id": int(user_id), "status": {"$in": list(OPEN_STATUSES)}},
                {"$set": {"dm_active": False}},
            )
            await self.feedbacks.update_one(
                {"_id": feedback["_id"]},
                {"$set": {"dm_active": True, "updated_at": now}},
            )
            feedback["dm_active"] = True
            feedback["updated_at"] = now
            return feedback

    async def _active_feedback_for_user(self, user_id: int) -> dict[str, Any] | None:
        feedback = await self.feedbacks.find_one(
            {
                "author_id": int(user_id),
                "dm_active": True,
                "status": {"$in": list(OPEN_STATUSES)},
            },
            sort=[("updated_at", -1)],
        )
        if feedback is not None:
            return feedback
        fallback = await self.feedbacks.find_one(
            {"author_id": int(user_id), "status": {"$in": list(OPEN_STATUSES)}},
            sort=[("updated_at", -1)],
        )
        if fallback is None:
            return None
        return await self._set_active_feedback(int(user_id), protocol_of(fallback))

    async def _activate_fallback_after_resolution(self, user_id: int) -> None:
        fallback = await self.feedbacks.find_one(
            {"author_id": int(user_id), "status": {"$in": list(OPEN_STATUSES)}},
            sort=[("updated_at", -1)],
        )
        if fallback is not None:
            await self._set_active_feedback(int(user_id), protocol_of(fallback))

    async def _forum_channel(self) -> discord.ForumChannel:
        channel = self.bot.get_channel(FEEDBACK_FORUM_CHANNEL_ID)
        if channel is None:
            channel = await self.bot.fetch_channel(FEEDBACK_FORUM_CHANNEL_ID)
        if not isinstance(channel, discord.ForumChannel):
            raise RuntimeError(
                f"O canal {FEEDBACK_FORUM_CHANNEL_ID} não é um canal de fórum."
            )
        return channel

    def _matching_forum_tags(
        self, forum: discord.ForumChannel, category: str
    ) -> list[discord.ForumTag]:
        info = CATEGORY_OPTIONS.get(category, CATEGORY_OPTIONS["help"])
        aliases = {str(value).casefold() for value in info.get("tag_aliases", ())}
        available = list(getattr(forum, "available_tags", None) or [])
        for tag in available:
            if str(tag.name or "").strip().casefold() in aliases:
                return [tag]
        if (
            bool(getattr(getattr(forum, "flags", None), "require_tag", False))
            and available
        ):
            log.warning(
                "fórum de feedback exige tag, mas nenhuma corresponde à categoria %s; usando %s",
                category,
                available[0].name,
            )
            return [available[0]]
        return []

    def _thread_name(self, feedback: dict[str, Any]) -> str:
        info = category_info(feedback)
        author_name = re.sub(
            r"[\r\n\t]+", " ", str(feedback.get("author_name") or "Usuário")
        ).strip()
        author_name = re.sub(r"\s+", " ", author_name)[:40] or "Usuário"
        return f"[{info['title']}] {protocol_of(feedback)} · {author_name}"[:100]

    async def _copy_attachments(self, message: discord.Message) -> list[discord.File]:
        files: list[discord.File] = []
        for attachment in list(message.attachments or [])[:MAX_FORWARDED_ATTACHMENTS]:
            try:
                files.append(await attachment.to_file(use_cached=True))
            except Exception:
                log.warning(
                    "não foi possível copiar anexo feedback message=%s filename=%s",
                    getattr(message, "id", 0),
                    getattr(attachment, "filename", "?"),
                    exc_info=True,
                )
        return files

    async def _fetch_thread(self, feedback: dict[str, Any]) -> discord.Thread | None:
        thread_id = int(feedback.get("thread_id") or 0)
        if not thread_id:
            return None
        channel = self.bot.get_channel(thread_id)
        if isinstance(channel, discord.Thread):
            return channel
        try:
            fetched = await self.bot.fetch_channel(thread_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return None
        return fetched if isinstance(fetched, discord.Thread) else None

    async def _ensure_thread_writable(self, thread: discord.Thread) -> bool:
        try:
            if thread.archived:
                await thread.edit(archived=False, reason="Novo complemento do feedback")
            return not thread.locked
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return False

    @app_commands.command(
        name="feedback",
        description="Envia ajuda, sugestão ou relato de bug para o dono do bot",
    )
    @app_commands.default_permissions(manage_messages=True)
    async def feedback_command(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or not isinstance(
            interaction.user, discord.Member
        ):
            await self._send_interaction_view(
                interaction,
                notice_view(
                    "Servidor necessário",
                    "Use `/feedback` dentro de um servidor.",
                    ok=False,
                ),
            )
            return
        if not self._is_source_staff(interaction.user):
            await self._send_interaction_view(
                interaction,
                notice_view(
                    "Sem permissão",
                    "Apenas membros da staff podem enviar feedback por este comando.",
                    ok=False,
                ),
            )
            return

        try:
            existing = await self._find_open_feedback(
                user_id=interaction.user.id, guild_id=interaction.guild.id
            )
            if existing is not None:
                active = (
                    await self._set_active_feedback(
                        interaction.user.id, protocol_of(existing)
                    )
                    or existing
                )
                await self._send_interaction_view(
                    interaction,
                    notice_view(
                        "Feedback já aberto",
                        [
                            f"Você já possui o atendimento `{protocol_of(active)}` neste servidor.",
                            "Ele foi definido como destino ativo. Use minha DM e comece a mensagem com `_` para adicionar informações.",
                        ],
                        ok=False,
                        accent_color=category_info(active)["accent"],
                    ),
                )
                return

            open_feedbacks = await self._open_feedbacks_for_user(interaction.user.id)
            if len(open_feedbacks) >= MAX_OPEN_FEEDBACKS_PER_USER:
                await self._send_interaction_view(
                    interaction,
                    notice_view(
                        "Limite de atendimentos",
                        [
                            f"Você já possui {len(open_feedbacks)} feedbacks abertos.",
                            "Resolva um atendimento existente antes de criar outro.",
                        ],
                        ok=False,
                    ),
                )
                return

            await interaction.response.send_modal(
                FeedbackModal(
                    self, guild_id=interaction.guild.id, opener_id=interaction.user.id
                )
            )
        except Exception:
            log.exception("falha ao abrir /feedback")
            await self._send_interaction_view(
                interaction,
                notice_view(
                    "Não foi possível abrir",
                    "O formulário de feedback não pôde ser iniciado.",
                    ok=False,
                ),
            )

    async def handle_feedback_submit(
        self,
        interaction: discord.Interaction,
        *,
        source_guild_id: int,
        opener_id: int,
        category: str,
        description: str,
    ) -> None:
        if interaction.guild is None or not isinstance(
            interaction.user, discord.Member
        ):
            await self._send_interaction_view(
                interaction,
                notice_view(
                    "Servidor necessário",
                    "Este formulário não é mais válido.",
                    ok=False,
                ),
            )
            return
        if int(interaction.guild.id) != int(source_guild_id) or int(
            interaction.user.id
        ) != int(opener_id):
            await self._send_interaction_view(
                interaction,
                notice_view(
                    "Formulário inválido",
                    "Abra novamente o comando `/feedback`.",
                    ok=False,
                ),
            )
            return
        if not self._is_source_staff(interaction.user):
            await self._send_interaction_view(
                interaction,
                notice_view(
                    "Sem permissão",
                    "Sua permissão de staff não está mais disponível.",
                    ok=False,
                ),
            )
            return

        category = str(category or "").strip().casefold()
        description = str(description or "").strip()
        if category not in CATEGORY_OPTIONS:
            await self._send_interaction_view(
                interaction,
                notice_view(
                    "Tipo inválido", "Selecione novamente o tipo do feedback.", ok=False
                ),
            )
            return
        if not DESCRIPTION_MIN_LENGTH <= len(description) <= DESCRIPTION_MAX_LENGTH:
            await self._send_interaction_view(
                interaction,
                notice_view(
                    "Descrição inválida",
                    f"Escreva entre {DESCRIPTION_MIN_LENGTH} e {DESCRIPTION_MAX_LENGTH} caracteres.",
                    ok=False,
                ),
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        lock_key = f"create:{interaction.user.id}"
        async with self._lock_for(lock_key):
            existing = await self._find_open_feedback(
                user_id=interaction.user.id, guild_id=interaction.guild.id
            )
            if existing is not None:
                await self._set_active_feedback(
                    interaction.user.id, protocol_of(existing)
                )
                await interaction.edit_original_response(
                    view=notice_view(
                        "Feedback já aberto",
                        [
                            f"O atendimento `{protocol_of(existing)}` continua aberto neste servidor.",
                            "Use minha DM e comece a mensagem com `_` para acrescentar informações.",
                        ],
                        ok=False,
                        accent_color=category_info(existing)["accent"],
                    )
                )
                return

            open_feedbacks = await self._open_feedbacks_for_user(interaction.user.id)
            if len(open_feedbacks) >= MAX_OPEN_FEEDBACKS_PER_USER:
                await interaction.edit_original_response(
                    view=notice_view(
                        "Limite de atendimentos",
                        "Resolva um atendimento existente antes de criar outro.",
                        ok=False,
                    )
                )
                return

            protocol_number, protocol = await self._next_protocol()
            now = datetime.now(timezone.utc)
            feedback: dict[str, Any] = {
                "protocol_number": protocol_number,
                "protocol": protocol,
                "guild_id": int(interaction.guild.id),
                "guild_name": str(interaction.guild.name),
                "author_id": int(interaction.user.id),
                "author_name": str(interaction.user.display_name),
                "category": category,
                "description": description,
                "status": STATUS_OPEN,
                "forum_channel_id": FEEDBACK_FORUM_CHANNEL_ID,
                "thread_id": 0,
                "starter_message_id": 0,
                "dm_active": True,
                "created_at": now,
                "updated_at": now,
            }

            insert_result = await self.feedbacks.insert_one(feedback)
            feedback["_id"] = insert_result.inserted_id
            created_thread: discord.Thread | None = None
            try:
                forum = await self._forum_channel()
                view = FeedbackThreadView(self, feedback)
                created = await forum.create_thread(
                    name=self._thread_name(feedback),
                    view=view,
                    applied_tags=self._matching_forum_tags(forum, category),
                    allowed_mentions=discord.AllowedMentions.none(),
                    reason=f"Feedback {protocol} enviado por {interaction.user} ({interaction.user.id})",
                )
                thread = created.thread
                created_thread = thread
                starter = created.message
                feedback["thread_id"] = int(thread.id)
                feedback["starter_message_id"] = int(starter.id)
                feedback["updated_at"] = datetime.now(timezone.utc)
                await self.feedbacks.update_one(
                    {"_id": feedback["_id"]},
                    {
                        "$set": {
                            "thread_id": feedback["thread_id"],
                            "starter_message_id": feedback["starter_message_id"],
                            "updated_at": feedback["updated_at"],
                        }
                    },
                )
                await self._set_active_feedback(interaction.user.id, protocol)
                key = (protocol, int(starter.id))
                if key not in self._registered_views:
                    self.bot.add_view(
                        FeedbackThreadView(self, feedback), message_id=int(starter.id)
                    )
                    self._registered_views.add(key)
            except Exception:
                log.exception("falha ao criar publicação do feedback %s", protocol)
                if created_thread is not None:
                    with contextlib.suppress(
                        discord.HTTPException, discord.Forbidden, discord.NotFound
                    ):
                        await created_thread.delete(
                            reason=f"Rollback do feedback {protocol}"
                        )
                await self.feedbacks.delete_one({"_id": feedback["_id"]})
                await interaction.edit_original_response(
                    view=notice_view(
                        "Falha ao enviar",
                        "Não foi possível criar a publicação no canal de feedback. Nenhum atendimento foi aberto.",
                        ok=False,
                    )
                )
                return

            dm_delivered = True
            try:
                await interaction.user.send(
                    view=build_feedback_created_dm(feedback),
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except (discord.Forbidden, discord.HTTPException):
                dm_delivered = False
                await self.feedbacks.update_one(
                    {"_id": feedback["_id"]},
                    {
                        "$set": {
                            "author_dm_available": False,
                            "updated_at": datetime.now(timezone.utc),
                        }
                    },
                )

            confirmation_lines = [
                f"O feedback `{protocol}` foi enviado como **{CATEGORY_OPTIONS[category]['label']}**.",
                "As respostas serão entregues pela DM do bot.",
            ]
            if not dm_delivered:
                confirmation_lines.append(
                    "Não consegui enviar a confirmação na sua DM. Ative mensagens diretas deste servidor para receber respostas."
                )
            await interaction.edit_original_response(
                view=notice_view(
                    "Feedback enviado",
                    confirmation_lines,
                    ok=dm_delivered,
                    accent_color=CATEGORY_OPTIONS[category]["accent"],
                )
            )

    async def mark_in_review(
        self, interaction: discord.Interaction, protocol: str
    ) -> None:
        if not await self._can_manage_feedback(interaction.user):
            await self._send_interaction_view(
                interaction,
                notice_view(
                    "Sem permissão", "Você não pode alterar este feedback.", ok=False
                ),
            )
            return
        async with self._lock_for(f"feedback:{protocol}"):
            feedback = await self.feedbacks.find_one({"protocol": str(protocol)})
            if feedback is None or str(feedback.get("status")) not in OPEN_STATUSES:
                await self._send_interaction_view(
                    interaction,
                    notice_view(
                        "Feedback indisponível",
                        "Este atendimento já foi encerrado.",
                        ok=False,
                    ),
                )
                return
            if str(feedback.get("status")) == STATUS_IN_REVIEW:
                await self._send_interaction_view(
                    interaction,
                    notice_view(
                        "Já está em análise",
                        f"`{protocol}` já possui este status.",
                        ok=True,
                    ),
                )
                return
            now = datetime.now(timezone.utc)
            feedback["status"] = STATUS_IN_REVIEW
            feedback["updated_at"] = now
            feedback["reviewed_by"] = int(interaction.user.id)
            feedback["reviewed_at"] = now
            await self.feedbacks.update_one(
                {"_id": feedback["_id"]},
                {
                    "$set": {
                        "status": STATUS_IN_REVIEW,
                        "updated_at": now,
                        "reviewed_by": int(interaction.user.id),
                        "reviewed_at": now,
                    }
                },
            )
            await interaction.response.edit_message(
                view=FeedbackThreadView(self, feedback)
            )

    async def ask_resolve_confirmation(
        self, interaction: discord.Interaction, protocol: str
    ) -> None:
        if not await self._can_manage_feedback(interaction.user):
            await self._send_interaction_view(
                interaction,
                notice_view(
                    "Sem permissão", "Você não pode resolver este feedback.", ok=False
                ),
            )
            return
        feedback = await self.feedbacks.find_one({"protocol": str(protocol)})
        if feedback is None or str(feedback.get("status")) not in OPEN_STATUSES:
            await self._send_interaction_view(
                interaction,
                notice_view(
                    "Feedback indisponível",
                    "Este atendimento já foi encerrado.",
                    ok=False,
                ),
            )
            return
        await interaction.response.send_message(
            view=ResolveConfirmationView(self, protocol, actor_id=interaction.user.id),
            ephemeral=True,
        )

    async def resolve_feedback(
        self, interaction: discord.Interaction, protocol: str
    ) -> None:
        if not await self._can_manage_feedback(interaction.user):
            await self._send_interaction_view(
                interaction,
                notice_view(
                    "Sem permissão", "Você não pode resolver este feedback.", ok=False
                ),
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        async with self._lock_for(f"feedback:{protocol}"):
            feedback = await self.feedbacks.find_one({"protocol": str(protocol)})
            if feedback is None or str(feedback.get("status")) not in OPEN_STATUSES:
                await interaction.edit_original_response(
                    view=notice_view(
                        "Feedback indisponível",
                        "Este atendimento já foi encerrado.",
                        ok=False,
                    )
                )
                return

            resolving_at = datetime.now(timezone.utc)
            changed = await self.feedbacks.update_one(
                {"_id": feedback["_id"], "status": {"$in": list(OPEN_STATUSES)}},
                {"$set": {"status": STATUS_RESOLVING, "updated_at": resolving_at}},
            )
            if int(getattr(changed, "modified_count", 0) or 0) != 1:
                await interaction.edit_original_response(
                    view=notice_view(
                        "Feedback em alteração",
                        "Outra ação já está encerrando este atendimento.",
                        ok=False,
                    )
                )
                return

            dm_delivered = True
            try:
                user = self.bot.get_user(
                    int(feedback["author_id"])
                ) or await self.bot.fetch_user(int(feedback["author_id"]))
                await user.send(
                    view=build_resolved_dm(feedback),
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                dm_delivered = False

            resolved_at = datetime.now(timezone.utc)
            await self.feedbacks.update_one(
                {"_id": feedback["_id"]},
                {
                    "$set": {
                        "status": STATUS_RESOLVED,
                        "dm_active": False,
                        "resolved_at": resolved_at,
                        "resolved_by": int(interaction.user.id),
                        "resolution_dm_delivered": dm_delivered,
                        "updated_at": resolved_at,
                    }
                },
            )
            await self._activate_fallback_after_resolution(int(feedback["author_id"]))

            deleted = False
            thread = await self._fetch_thread(feedback)
            if thread is not None:
                try:
                    await thread.delete(
                        reason=f"Feedback {protocol} marcado como resolvido por {interaction.user}"
                    )
                    deleted = True
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    log.exception("falha ao excluir tópico resolvido %s", protocol)
                    with contextlib.suppress(
                        discord.HTTPException, discord.Forbidden, discord.NotFound
                    ):
                        await thread.edit(
                            locked=True,
                            archived=True,
                            reason=f"Feedback {protocol} resolvido",
                        )

            if not deleted:
                await self.feedbacks.update_one(
                    {"_id": feedback["_id"]},
                    {"$set": {"thread_delete_failed": True}},
                )

            lines = [f"O atendimento `{protocol}` foi resolvido."]
            if deleted:
                lines.append("A publicação do fórum foi excluída.")
            else:
                lines.append(
                    "Não consegui excluir a publicação; ela foi encerrada quando possível."
                )
            if not dm_delivered:
                lines.append("A DM final não pôde ser entregue ao autor.")
            await interaction.edit_original_response(
                view=notice_view("Feedback resolvido", lines, ok=deleted)
            )

    async def switch_active_feedback(
        self,
        interaction: discord.Interaction,
        protocol: str,
        *,
        owner_id: int,
    ) -> None:
        if int(interaction.user.id) != int(owner_id):
            await self._send_interaction_view(
                interaction,
                notice_view(
                    "Seleção privada",
                    "Este seletor pertence a outro usuário.",
                    ok=False,
                ),
            )
            return
        feedback = await self._set_active_feedback(owner_id, protocol)
        if feedback is None:
            await interaction.response.edit_message(
                view=notice_view(
                    "Atendimento indisponível",
                    "Este feedback não está mais aberto.",
                    ok=False,
                )
            )
            return
        count = len(await self._open_feedbacks_for_user(owner_id))
        await interaction.response.edit_message(
            view=build_active_status_view(feedback, open_count=count)
        )

    async def _handle_dm_status(self, message: discord.Message) -> None:
        feedbacks = await self._open_feedbacks_for_user(message.author.id)
        active = await self._active_feedback_for_user(message.author.id)
        await message.channel.send(
            view=build_active_status_view(active, open_count=len(feedbacks)),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _handle_dm_switch(self, message: discord.Message) -> None:
        feedbacks = await self._open_feedbacks_for_user(message.author.id)
        if not feedbacks:
            await message.channel.send(
                view=build_active_status_view(None),
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        if len(feedbacks) == 1:
            active = await self._set_active_feedback(
                message.author.id, protocol_of(feedbacks[0])
            )
            await message.channel.send(
                view=build_active_status_view(active or feedbacks[0], open_count=1),
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        await message.channel.send(
            view=FeedbackSwitchView(
                self, owner_id=message.author.id, feedbacks=feedbacks
            ),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _handle_additional_dm(self, message: discord.Message) -> None:
        feedback = await self._active_feedback_for_user(message.author.id)
        if feedback is None:
            await message.channel.send(
                view=notice_view(
                    "Nenhum feedback ativo",
                    "Use `/feedback` em um servidor antes de enviar informações adicionais.",
                    ok=False,
                ),
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        content = str(message.content or "")
        content = (
            content[1:].lstrip()
            if content.startswith(DM_MESSAGE_PREFIX)
            else content.strip()
        )
        files = await self._copy_attachments(message)
        if not content and not files:
            await message.channel.send(
                view=notice_view(
                    "Mensagem vazia",
                    "Escreva algo depois de `_` ou envie um arquivo junto.",
                    ok=False,
                ),
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        protocol = protocol_of(feedback)
        async with self._lock_for(f"feedback:{protocol}"):
            fresh = await self.feedbacks.find_one({"_id": feedback["_id"]})
            if fresh is None or str(fresh.get("status")) not in OPEN_STATUSES:
                await message.channel.send(
                    view=notice_view(
                        "Feedback encerrado",
                        f"O atendimento `{protocol}` não aceita novas informações.",
                        ok=False,
                    ),
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
            thread = await self._fetch_thread(fresh)
            if thread is None or not await self._ensure_thread_writable(thread):
                await message.channel.send(
                    view=notice_view(
                        "Tópico indisponível",
                        f"Não foi possível acessar a publicação de `{protocol}`.",
                        ok=False,
                    ),
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
            try:
                await thread.send(
                    view=build_additional_message_view(
                        fresh, content, attachment_count=len(files)
                    ),
                    files=files,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                log.exception("falha ao encaminhar complemento para %s", protocol)
                await message.channel.send(
                    view=notice_view(
                        "Falha no envio",
                        f"Não consegui adicionar a mensagem ao atendimento `{protocol}`.",
                        ok=False,
                    ),
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
            await self.feedbacks.update_one(
                {"_id": fresh["_id"]},
                {
                    "$set": {
                        "updated_at": datetime.now(timezone.utc),
                        "last_user_message_at": datetime.now(timezone.utc),
                    },
                    "$inc": {"user_message_count": 1},
                },
            )
            await message.channel.send(
                view=build_additional_confirmation(fresh),
                allowed_mentions=discord.AllowedMentions.none(),
            )

    async def _forward_owner_message(
        self, message: discord.Message, feedback: dict[str, Any]
    ) -> None:
        content = str(message.content or "").strip()
        if content.startswith(INTERNAL_OWNER_PREFIX):
            return
        files = await self._copy_attachments(message)
        if not content and not files:
            return
        try:
            user = self.bot.get_user(
                int(feedback["author_id"])
            ) or await self.bot.fetch_user(int(feedback["author_id"]))
            await user.send(
                view=build_owner_update_dm(
                    feedback, content, attachment_count=len(files)
                ),
                files=files,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            await self.feedbacks.update_one(
                {"_id": feedback["_id"]},
                {
                    "$set": {
                        "author_dm_available": False,
                        "last_delivery_failed_at": datetime.now(timezone.utc),
                        "updated_at": datetime.now(timezone.utc),
                    },
                    "$inc": {"delivery_failure_count": 1},
                },
            )
            with contextlib.suppress(
                discord.HTTPException, discord.Forbidden, discord.NotFound
            ):
                await message.channel.send(
                    view=build_delivery_failure_view(feedback),
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            with contextlib.suppress(
                discord.HTTPException, discord.Forbidden, discord.NotFound
            ):
                await message.add_reaction("⚠️")
            return

        await self.feedbacks.update_one(
            {"_id": feedback["_id"]},
            {
                "$set": {
                    "author_dm_available": True,
                    "last_owner_message_at": datetime.now(timezone.utc),
                    "updated_at": datetime.now(timezone.utc),
                },
                "$inc": {"owner_message_count": 1},
            },
        )
        with contextlib.suppress(
            discord.HTTPException, discord.Forbidden, discord.NotFound
        ):
            await message.add_reaction("✅")

    @commands.Cog.listener("on_message")
    async def feedback_message_listener(self, message: discord.Message) -> None:
        if message.author.bot or message.webhook_id is not None:
            return

        if message.guild is None:
            content = str(message.content or "")
            normalized = content.strip().casefold()
            if normalized == DM_STATUS_COMMAND:
                await self._handle_dm_status(message)
                return
            if normalized == DM_SWITCH_COMMAND:
                await self._handle_dm_switch(message)
                return
            if content.startswith(DM_MESSAGE_PREFIX):
                await self._handle_additional_dm(message)
            return

        if not isinstance(message.channel, discord.Thread):
            return
        if (
            int(getattr(message.channel, "parent_id", 0) or 0)
            != FEEDBACK_FORUM_CHANNEL_ID
        ):
            return
        try:
            is_owner = await self.bot.is_owner(message.author)
        except Exception:
            log.exception("falha ao validar dono para mensagem do feedback")
            return
        if not is_owner:
            return
        feedback = await self.feedbacks.find_one(
            {
                "thread_id": int(message.channel.id),
                "status": {"$in": list(OPEN_STATUSES)},
            }
        )
        if feedback is None:
            return
        protocol = protocol_of(feedback)
        async with self._lock_for(f"feedback:{protocol}"):
            fresh = await self.feedbacks.find_one({"_id": feedback["_id"]})
            if fresh is None or str(fresh.get("status")) not in OPEN_STATUSES:
                return
            await self._forward_owner_message(message, fresh)
